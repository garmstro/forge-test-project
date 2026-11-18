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

**Decision:** **No application-level rate limiting** is implemented in this phase.

**Rationale:** The redirect endpoint is designed for sub-10 ms resolution via a single indexed DB lookup. Infrastructure-level rate limiting (e.g., nginx `limit_req`, Caddy rate-limit middleware, or a cloud WAF) is the recommended approach for production deployments and avoids adding latency or complexity to the hot path. This decision is revisable in a future phase.

---

## 6. API Key Rotation on `/users/token`

**Decision:** Each call to `POST /users/token` **generates and persists a new API key**, invalidating the previous one.

**Rationale:** Because API keys are stored as one-way SHA-256 hashes, the server cannot return the original key. Re-issuing a new key on each token request is the simplest correct behaviour, doubles as a rotation mechanism, and is clearly documented. Clients that need a stable key should store it after registration and avoid calling `/users/token` unnecessarily.


---

## 7. Background Cleanup Job Behaviour

**Decision:** The cleanup job (`linkvault/services/cleanup.py`) runs on an APScheduler `AsyncIOScheduler` inside the API process. Its job store is a SQLite database (`SCHEDULER_DB_URL`), and the job is saved under a fixed ID and a textual function reference (`linkvault.services.cleanup:run_cleanup_job`).

- **Restart:** the job is added only if it isn't already stored, so a restart keeps the stored `next_run_time` instead of resetting the 15-minute clock.
- **Missed runs:** `coalesce=True` and `misfire_grace_time=None`. Runs missed while the app was down become one catch-up run at startup. `max_instances=1` stops two runs from overlapping.
- **Query:** one bulk `UPDATE ... WHERE expires_at < now AND deleted_at IS NULL`. `now` is naive UTC, matching §4.
- **After cleanup:** an expired link that has been cleaned up is soft-deleted, so `GET /{slug}` returns `404` instead of `410`, and its slug can be reused. Before cleanup runs, the redirect engine still returns `410`.
- **Opt-out:** `SCHEDULER_ENABLED=false` turns the scheduler off. This is for running several API workers, where only one process should own the job.
