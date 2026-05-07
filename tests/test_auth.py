"""
Tests for the Users & Authentication system (Item 1).

Covers:
- Register a user successfully
- Reject duplicate email registration
- Reject login with wrong password
- Authenticated request succeeds with valid key
- Authenticated request fails with invalid key
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTER_URL = "/users/register"
TOKEN_URL = "/users/token"


async def register_user(client: AsyncClient, email: str, password: str):  # type: ignore[return]
    return await client.post(REGISTER_URL, json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_success(client: AsyncClient) -> None:
    resp = await register_user(client, "alice@example.com", "password123")
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "alice@example.com"
    assert "api_key" in body
    assert len(body["api_key"]) == 36  # UUID4 format


@pytest.mark.asyncio
async def test_register_returns_id_and_created_at(client: AsyncClient) -> None:
    resp = await register_user(client, "bob@example.com", "password123")
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    assert "created_at" in body


@pytest.mark.asyncio
async def test_register_duplicate_email(client: AsyncClient) -> None:
    await register_user(client, "dup@example.com", "password123")
    resp = await register_user(client, "dup@example.com", "other_password")
    assert resp.status_code == 409
    body = resp.json()
    assert "already exists" in body.get("detail", "")


@pytest.mark.asyncio
async def test_register_invalid_email(client: AsyncClient) -> None:
    resp = await register_user(client, "not-an-email", "password123")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_short_password(client: AsyncClient) -> None:
    resp = await register_user(client, "shortpw@example.com", "abc")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Token (login)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_success(client: AsyncClient) -> None:
    await register_user(client, "carol@example.com", "password123")
    resp = await client.post(TOKEN_URL, json={"email": "carol@example.com", "password": "password123"})
    assert resp.status_code == 200
    body = resp.json()
    assert "api_key" in body
    assert len(body["api_key"]) == 36


@pytest.mark.asyncio
async def test_token_wrong_password(client: AsyncClient) -> None:
    await register_user(client, "dave@example.com", "correct_password")
    resp = await client.post(TOKEN_URL, json={"email": "dave@example.com", "password": "wrong_password"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_unknown_email(client: AsyncClient) -> None:
    resp = await client.post(TOKEN_URL, json={"email": "nobody@example.com", "password": "password123"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Authentication on protected routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticated_request_succeeds(client: AsyncClient) -> None:
    reg = await register_user(client, "eve@example.com", "password123")
    api_key = reg.json()["api_key"]

    resp = await client.get("/links", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_authenticated_request_fails_invalid_key(client: AsyncClient) -> None:
    resp = await client.get("/links", headers={"Authorization": "Bearer invalid-key-xyz"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_authenticated_request_fails_missing_header(client: AsyncClient) -> None:
    """No Authorization header — FastAPI/Starlette rejects with 401 (or 403 on older versions)."""
    resp = await client.get("/links")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Token rotates on each /users/token call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_rotates_api_key(client: AsyncClient) -> None:
    """Calling /users/token should issue a new API key, invalidating the old one."""
    reg = await register_user(client, "frank@example.com", "password123")
    old_key = reg.json()["api_key"]

    new_token_resp = await client.post(
        TOKEN_URL, json={"email": "frank@example.com", "password": "password123"}
    )
    new_key = new_token_resp.json()["api_key"]

    assert old_key != new_key

    # Old key should now be invalid
    resp_old = await client.get("/links", headers={"Authorization": f"Bearer {old_key}"})
    assert resp_old.status_code == 401

    # New key should work
    resp_new = await client.get("/links", headers={"Authorization": f"Bearer {new_key}"})
    assert resp_new.status_code == 200

