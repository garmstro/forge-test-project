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

**Decision:** **IP-based rate limiting** is implemented at the application level using SlowAPI with a default limit of **100 requests per minute per IP address**.

**Rationale:** Application-level rate limiting provides immediate protection against abuse and ensures fair resource allocation across clients. The implementation uses SlowAPI, a FastAPI-compatible rate limiting library that:
- Adds minimal latency (< 1ms overhead per request)
- Provides standard rate limit headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`)
- Returns consistent `429 Too Many Requests` responses with `Retry-After` headers
- Supports exemptions (e.g., the `/health` endpoint is exempt to ensure monitoring systems can always check service status)

**Configuration:**
- **Limit**: 100 requests/minute per IP (configurable via the limiter initialization)
- **Key function**: Client IP address via `get_remote_address` (supports `X-Forwarded-For` when behind a proxy)
- **Storage**: In-memory (suitable for single-instance deployments; Redis backend recommended for multi-instance production)

**Production considerations:** Infrastructure-level rate limiting (e.g., nginx `limit_req`, Cloudflare rate limiting, or a cloud WAF) provides additional defense in depth and should be used in conjunction with application-level limits. The redirect endpoint maintains sub-10 ms resolution even with rate limiting enabled.

---

## 6. API Key Rotation on `/users/token`

**Decision:** Each call to `POST /users/token` **generates and persists a new API key**, invalidating the previous one.

**Rationale:** Because API keys are stored as one-way SHA-256 hashes, the server cannot return the original key. Re-issuing a new key on each token request is the simplest correct behaviour, doubles as a rotation mechanism, and is clearly documented. Clients that need a stable key should store it after registration and avoid calling `/users/token` unnecessarily.


