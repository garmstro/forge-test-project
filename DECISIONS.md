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

## 7. Expired Link Cleanup Job

**Decision:** Every 15 minutes, an APScheduler job (`linkvault/services/cleanup.py`) soft-deletes every link where `expires_at < now` and `deleted_at IS NULL`. It does this with a single `UPDATE … WHERE … RETURNING` statement and sets `deleted_at = now`. "Now" is naive UTC, to match how datetimes are stored (§4). Each run logs `{"event": "cleanup", "links_expired": N, "timestamp": "..."}`, including when N is 0.

**Details:**
- **Same soft-delete as `DELETE /links/{slug}`.** The slug becomes reusable right away, and click history stays attached to the old link's UUID (§3). After cleanup, the short URL returns `404 not_found` instead of `410 link_expired`.
- **Click-capped links are not removed.** The spec only defines cleanup in terms of `expires_at`. A link that has hit `max_clicks` keeps returning `410 Gone`, so its owner can still raise the cap with `PATCH`. Removing those links automatically would silently free up slugs that their owners may still want to keep.
- **Persistence and no duplicates.** Jobs are stored in a SQLAlchemy job store at `SCHEDULER_DB_URL`, which is a separate database from the app data. On startup the job is registered under the fixed id `expired_link_cleanup`. If a stored job with the same function and interval already exists, it is kept along with its `next_run_time`, so frequent restarts don't keep pushing the next run back. An outdated definition is replaced under the same id rather than added a second time.
- **Missed runs.** `coalesce=True` with no misfire grace limit means that downtime leads to a single catch-up run, not one run per missed slot. `max_instances=1` stops runs from overlapping.
- **Failure handling.** The job wrapper catches any exception, rolls back the transaction, and logs a `cleanup_failed` event. The scheduler and the app keep running, and the next run happens on schedule.
- **Lifecycle.** The scheduler is started in the FastAPI lifespan handler and is stopped there on shutdown. Shutdown waits until the scheduler has actually stopped. `/health` reports `"scheduler": "running"` only while it is active.
