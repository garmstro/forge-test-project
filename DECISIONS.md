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

**Decision:** **Application-level rate limiting** is implemented using `slowapi` with per-IP and per-user limits.

**Rationale:** While infrastructure-level rate limiting (e.g., nginx `limit_req`, Caddy rate-limit middleware, or a cloud WAF) is recommended for production deployments, application-level rate limiting provides:
1. **Fine-grained control** — different limits for different endpoints and user types
2. **User-based limiting** — authenticated endpoints are limited per user ID, preventing a single user from exhausting shared resources
3. **Defense in depth** — complements infrastructure-level protections
4. **Development/testing convenience** — works out-of-the-box without additional infrastructure

**Implementation details:**
- Public endpoints (registration, login, redirects) are limited by IP address
- Authenticated endpoints (links CRUD, analytics) are limited by user ID
- The redirect endpoint maintains sub-10 ms performance via a single indexed DB lookup; rate limiting adds negligible overhead
- Limits are configurable per endpoint:
  - Registration: 5 requests/minute per IP
  - Login: 10 requests/minute per IP
  - Link creation: 100 requests/hour per user
  - Link listing/retrieval: 200 requests/hour per user
  - Link updates/deletes: 100 requests/hour per user
  - Redirects: 1000 requests/hour per IP

**Trade-offs:**
- Adds minimal latency (< 1 ms) to each request
- Requires in-memory storage for rate limit counters (handled by slowapi)
- Infrastructure-level rate limiting should still be deployed for DDoS protection

---

## 6. API Key Rotation on `/users/token`

**Decision:** Each call to `POST /users/token` **generates and persists a new API key**, invalidating the previous one.

**Rationale:** Because API keys are stored as one-way SHA-256 hashes, the server cannot return the original key. Re-issuing a new key on each token request is the simplest correct behaviour, doubles as a rotation mechanism, and is clearly documented. Clients that need a stable key should store it after registration and avoid calling `/users/token` unnecessarily.
