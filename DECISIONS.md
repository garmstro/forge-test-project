# DECISIONS.md — LinkVault Architectural Decisions

This file documents every intentional ambiguity from `AGENTS.md` and how it was resolved.

---

## 1. Slug Collision on Auto-Generate

**Decision:** Up to **5 retries** with a fresh random draw on each attempt before returning `409 Conflict`.

**Rationale:** The Base58 alphabet has 58 characters. A 6-character slug gives 58⁶ ≈ 38.1 billion possible values. At 1 million stored links the probability of a single collision is ≈ 2.6 × 10⁻⁵, and the probability of exhausting all 5 retries is negligibly small (≈ (2.6 × 10⁻⁵)⁵). Exponential back-off is intentionally omitted because the operation is in-process and the retry loop completes in microseconds.

---

## 2. Click Count vs. Clicks Table — Source of Truth

**Decision:** `links.click_count` is the **authoritative counter** for all real-time and summary queries. The `clicks` table is used exclusively for detailed per-click analytics (day bucketing, unique IPs, referers, user agents).

**Consistency strategy:**
- The counter is incremented atomically via `UPDATE links SET click_count = click_count + 1 WHERE slug = :slug` — no read-then-write race condition.
- The `clicks` row insert happens in the same database transaction immediately after the counter update. If the insert fails (rare), the counter is still incremented but no detail row exists. This is an acceptable, self-documenting discrepancy (visible in logs at `WARNING` level).
- `click_count` and `COUNT(clicks.id)` may therefore differ by at most the number of failed inserts since last restart, which is expected to be zero under normal operation.

---

## 3. Soft-Delete and Analytics

**Decision:** Clicks are associated with `link_id` (UUID), **not** with the slug string. When a link is soft-deleted and a new link later claims the same slug, it receives a different UUID. Historical click data for the old UUID is therefore never mixed with data for the new link.

**Consequence:** After a soft-delete, `GET /analytics/{slug}` will return 404 (no active link with that slug). If the slug is reclaimed, analytics start fresh. This is the safest default and avoids any possibility of data leakage between owners.

---

## 4. Timezone Handling for `expires_at`

**Decision:** Naive datetimes (no `Z` or UTC offset) are **assumed to be UTC** and stored as-is. The API documents this behaviour and recommends always supplying an explicit offset (e.g., `2025-12-31T23:59:59Z`).

**Rationale:** Silently assuming local server time would make behaviour non-deterministic across deployments. UTC is the unambiguous safe default for a server-side API.

---

## 5. Rate Limiting

**Decision:** **Application-level rate limiting is implemented** using the `slowapi` library with per-endpoint limits.

**Rationale:** 
- The redirect endpoint (`GET /{slug}`) is the highest-volume path and is rate-limited at **1000 requests/minute** per IP address to protect against abuse while maintaining sub-10ms latency.
- Authentication endpoints (`POST /users/register`, `POST /users/token`) are rate-limited at **10 requests/minute** per IP to prevent brute-force attacks.
- General API endpoints (`POST /links`, `GET /links`, `PATCH /links`, `DELETE /links`, `/analytics/*`) are rate-limited at **100 requests/minute** per IP to prevent resource exhaustion.
- Rate limits are configurable via environment variables (`RATE_LIMIT_REDIRECT`, `RATE_LIMIT_API`, `RATE_LIMIT_AUTH`) and can be disabled entirely with `RATE_LIMIT_ENABLED=false` for local development or testing.
- Rate limit exceeded responses return HTTP 429 with a consistent error envelope: `{"error": "rate_limit_exceeded", "detail": "Too many requests. Please try again later."}`.
- The implementation uses IP address as the rate limit key for public endpoints and can be extended to use user ID for authenticated endpoints in future phases.
- This approach balances security, performance, and operational flexibility: it prevents common abuse patterns without adding significant latency to the hot path, and operators can adjust limits based on their deployment environment.

---

## 6. API Key Rotation on `/users/token`

**Decision:** Each call to `POST /users/token` **generates and persists a new API key**, invalidating the previous one.

**Rationale:** Because API keys are stored as one-way SHA-256 hashes, the server cannot return the original key. Re-issuing a new key on each token request is the simplest correct behaviour, doubles as a rotation mechanism, and is clearly documented. Clients that need a stable key should store it after registration and avoid calling `/users/token` unnecessarily.

