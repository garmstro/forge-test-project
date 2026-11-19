"""Thin synchronous HTTP client used by the LinkVault CLI."""
from __future__ import annotations

from typing import Any

import httpx

DEFAULT_TIMEOUT = 10.0


class ApiError(Exception):
    """Raised for non-2xx responses or connection failures."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _error_message(response: httpx.Response) -> str:
    """Extract a human-readable message from an API error response."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail")
        error = body.get("error")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(error, str) and error:
            return error
        if detail is not None:
            return str(detail)
    text = response.text.strip()
    return text or f"HTTP {response.status_code}"


class LinkVaultClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        if http is not None:
            # Pre-built client (e.g. Starlette's TestClient in tests); the
            # caller owns its lifecycle, so close() leaves it open.
            http.headers.update(headers)
            self._client = http
            self._owns_client = False
            return
        self._owns_client = True
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LinkVaultClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(f"Could not reach LinkVault at {self.base_url}: {exc}") from exc
        if response.is_error:
            raise ApiError(_error_message(response), response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ---- users -----------------------------------------------------------
    def token(self, email: str, password: str) -> dict[str, Any]:
        result: dict[str, Any] = self._request(
            "POST", "/users/token", json={"email": email, "password": password}
        )
        return result

    # ---- links -----------------------------------------------------------
    def create_link(
        self,
        url: str,
        slug: str | None = None,
        expires_at: str | None = None,
        max_clicks: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"url": url}
        if slug is not None:
            payload["slug"] = slug
        if expires_at is not None:
            payload["expires_at"] = expires_at
        if max_clicks is not None:
            payload["max_clicks"] = max_clicks
        result: dict[str, Any] = self._request("POST", "/links", json=payload)
        return result

    def list_links(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        result: dict[str, Any] = self._request(
            "GET", "/links", params={"page": page, "page_size": page_size}
        )
        return result

    def get_link(self, slug: str) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", f"/links/{slug}")
        return result

    def delete_link(self, slug: str) -> None:
        self._request("DELETE", f"/links/{slug}")

    # ---- analytics -------------------------------------------------------
    def link_stats(self, slug: str, days: int = 30) -> dict[str, Any]:
        result: dict[str, Any] = self._request(
            "GET", f"/analytics/{slug}", params={"days": days}
        )
        return result
