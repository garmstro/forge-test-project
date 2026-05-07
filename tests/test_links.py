"""
Tests for Link Management (Item 2).

Covers:
- Create a link with auto-generated slug
- Create a link with a custom slug
- Reject a custom slug that is a reserved word
- Reject a duplicate slug
- Reject an invalid URL
- List links returns only the authenticated user's links
- Patch updates allowed fields
- Patch cannot change slug
- Soft-delete makes the slug available again
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTER_URL = "/users/register"
LINKS_URL = "/links"


async def register_and_get_key(client: AsyncClient, email: str, password: str = "password123") -> str:
    resp = await client.post(REGISTER_URL, json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


def auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Create link — auto-generated slug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_link_auto_slug(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "alice@example.com")
    resp = await client.post(
        LINKS_URL,
        json={"url": "https://example.com/some/long/path"},
        headers=auth(key),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["destination_url"] == "https://example.com/some/long/path"
    assert len(body["slug"]) == 6
    assert body["user_id"] is not None
    assert body["click_count"] == 0
    assert body["deleted_at"] is None


@pytest.mark.asyncio
async def test_create_link_auto_slug_is_base58(client: AsyncClient) -> None:
    """Auto-generated slug must only contain Base58 characters."""
    from linkvault.services.shortener import BASE58_ALPHABET

    key = await register_and_get_key(client, "base58@example.com")
    resp = await client.post(
        LINKS_URL,
        json={"url": "https://example.com/"},
        headers=auth(key),
    )
    assert resp.status_code == 201
    slug = resp.json()["slug"]
    for ch in slug:
        assert ch in BASE58_ALPHABET, f"Character '{ch}' is not in Base58 alphabet"


# ---------------------------------------------------------------------------
# Create link — custom slug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_link_custom_slug(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "bob@example.com")
    resp = await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": "my-custom-slug"},
        headers=auth(key),
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "my-custom-slug"


@pytest.mark.asyncio
async def test_create_link_custom_slug_case_insensitive(client: AsyncClient) -> None:
    """Slugs are stored lowercase; mixed-case input is normalised."""
    key = await register_and_get_key(client, "carol@example.com")
    resp = await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": "MySlug"},
        headers=auth(key),
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "myslug"


# ---------------------------------------------------------------------------
# Reject reserved slugs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", ["api", "admin", "health", "docs", "metrics"])
async def test_create_link_reserved_slug_rejected(client: AsyncClient, reserved: str) -> None:
    key = await register_and_get_key(client, f"{reserved}@example.com")
    resp = await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": reserved},
        headers=auth(key),
    )
    assert resp.status_code == 422
    assert "reserved" in resp.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# Reject duplicate slug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_link_duplicate_slug_rejected(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "dave@example.com")
    payload = {"url": "https://example.com/", "slug": "unique-slug"}
    resp1 = await client.post(LINKS_URL, json=payload, headers=auth(key))
    assert resp1.status_code == 201

    resp2 = await client.post(LINKS_URL, json=payload, headers=auth(key))
    assert resp2.status_code == 409


# ---------------------------------------------------------------------------
# Reject invalid URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_link_invalid_url_rejected(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "eve@example.com")
    resp = await client.post(
        LINKS_URL,
        json={"url": "not-a-valid-url"},
        headers=auth(key),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_link_ftp_url_rejected(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "ftp@example.com")
    resp = await client.post(
        LINKS_URL,
        json={"url": "ftp://example.com/file"},
        headers=auth(key),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# List links — isolation between users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_links_returns_only_own_links(client: AsyncClient) -> None:
    key_a = await register_and_get_key(client, "user_a@example.com")
    key_b = await register_and_get_key(client, "user_b@example.com")

    # User A creates 2 links
    await client.post(LINKS_URL, json={"url": "https://a.com/1"}, headers=auth(key_a))
    await client.post(LINKS_URL, json={"url": "https://a.com/2"}, headers=auth(key_a))

    # User B creates 1 link
    await client.post(LINKS_URL, json={"url": "https://b.com/1"}, headers=auth(key_b))

    resp_a = await client.get(LINKS_URL, headers=auth(key_a))
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    assert body_a["total"] == 2
    assert len(body_a["items"]) == 2

    resp_b = await client.get(LINKS_URL, headers=auth(key_b))
    assert resp_b.status_code == 200
    body_b = resp_b.json()
    assert body_b["total"] == 1
    assert len(body_b["items"]) == 1


@pytest.mark.asyncio
async def test_list_links_pagination(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "paginate@example.com")
    for i in range(5):
        await client.post(LINKS_URL, json={"url": f"https://example.com/{i}"}, headers=auth(key))

    resp = await client.get(LINKS_URL + "?page=1&page_size=3", headers=auth(key))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 3
    assert body["page"] == 1
    assert body["page_size"] == 3


# ---------------------------------------------------------------------------
# GET /links/{slug}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_link_by_slug(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "getslug@example.com")
    create_resp = await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": "getme"},
        headers=auth(key),
    )
    assert create_resp.status_code == 201

    resp = await client.get(f"{LINKS_URL}/getme", headers=auth(key))
    assert resp.status_code == 200
    assert resp.json()["slug"] == "getme"
    assert resp.json()["destination_url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_get_link_not_found(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "notfound@example.com")
    resp = await client.get(f"{LINKS_URL}/nonexistent", headers=auth(key))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_link_other_user_forbidden(client: AsyncClient) -> None:
    key_a = await register_and_get_key(client, "owner@example.com")
    key_b = await register_and_get_key(client, "intruder@example.com")

    await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": "private-link"},
        headers=auth(key_a),
    )

    resp = await client.get(f"{LINKS_URL}/private-link", headers=auth(key_b))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /links/{slug} — update allowed fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_updates_url(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "patch_url@example.com")
    await client.post(
        LINKS_URL,
        json={"url": "https://old.example.com/", "slug": "patch-me"},
        headers=auth(key),
    )

    resp = await client.patch(
        f"{LINKS_URL}/patch-me",
        json={"url": "https://new.example.com/"},
        headers=auth(key),
    )
    assert resp.status_code == 200
    assert resp.json()["destination_url"] == "https://new.example.com/"
    assert resp.json()["slug"] == "patch-me"  # slug unchanged


@pytest.mark.asyncio
async def test_patch_updates_expires_at(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "patch_exp@example.com")
    await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": "exp-link"},
        headers=auth(key),
    )

    resp = await client.patch(
        f"{LINKS_URL}/exp-link",
        json={"expires_at": "2099-12-31T23:59:59Z"},
        headers=auth(key),
    )
    assert resp.status_code == 200
    assert resp.json()["expires_at"] is not None


@pytest.mark.asyncio
async def test_patch_updates_max_clicks(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "patch_mc@example.com")
    await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": "mc-link"},
        headers=auth(key),
    )

    resp = await client.patch(
        f"{LINKS_URL}/mc-link",
        json={"max_clicks": 500},
        headers=auth(key),
    )
    assert resp.status_code == 200
    assert resp.json()["max_clicks"] == 500


@pytest.mark.asyncio
async def test_patch_cannot_change_slug(client: AsyncClient) -> None:
    """PATCH body including 'slug' must not change the slug (field is ignored or rejected)."""
    key = await register_and_get_key(client, "patch_slug@example.com")
    await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": "original-slug"},
        headers=auth(key),
    )

    # Send a slug field in the patch body — the API should either ignore it
    # (200 with unchanged slug) or reject it (422). Either is acceptable.
    resp = await client.patch(
        f"{LINKS_URL}/original-slug",
        json={"url": "https://example.com/updated", "slug": "new-slug"},
        headers=auth(key),
    )
    # The slug must remain unchanged regardless of response code
    if resp.status_code == 200:
        assert resp.json()["slug"] == "original-slug"
    else:
        # 422 is also acceptable if the server rejects unknown fields
        assert resp.status_code in (200, 422)


# ---------------------------------------------------------------------------
# DELETE /links/{slug} — soft-delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_link(client: AsyncClient) -> None:
    key = await register_and_get_key(client, "delete@example.com")
    await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": "delete-me"},
        headers=auth(key),
    )

    del_resp = await client.delete(f"{LINKS_URL}/delete-me", headers=auth(key))
    assert del_resp.status_code == 204

    # Link should no longer be accessible
    get_resp = await client.get(f"{LINKS_URL}/delete-me", headers=auth(key))
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_soft_delete_makes_slug_reusable(client: AsyncClient) -> None:
    """After a soft-delete the same slug can be claimed by a new link."""
    key = await register_and_get_key(client, "reuse@example.com")

    await client.post(
        LINKS_URL,
        json={"url": "https://old.example.com/", "slug": "reuse-slug"},
        headers=auth(key),
    )
    await client.delete(f"{LINKS_URL}/reuse-slug", headers=auth(key))

    resp = await client.post(
        LINKS_URL,
        json={"url": "https://new.example.com/", "slug": "reuse-slug"},
        headers=auth(key),
    )
    assert resp.status_code == 201
    assert resp.json()["destination_url"] == "https://new.example.com/"


@pytest.mark.asyncio
async def test_delete_other_user_link_forbidden(client: AsyncClient) -> None:
    key_a = await register_and_get_key(client, "del_owner@example.com")
    key_b = await register_and_get_key(client, "del_intruder@example.com")

    await client.post(
        LINKS_URL,
        json={"url": "https://example.com/", "slug": "del-private"},
        headers=auth(key_a),
    )

    resp = await client.delete(f"{LINKS_URL}/del-private", headers=auth(key_b))
    assert resp.status_code == 403

