# AGENTS.md — LinkVault: URL Intelligence Platform

## Project Overview

Build **LinkVault**, a production-grade URL shortening and analytics platform. This is not a toy — it requires a REST API with authentication, background job processing, a persistent data layer, a CLI client, and a terminal dashboard. Every component must be functional, tested, and connected.

The agent is expected to make architectural decisions, resolve ambiguities independently, and produce working code — not scaffolding or stubs.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Web Framework | FastAPI |
| Database | SQLite via SQLAlchemy (async) |
| Background Jobs | APScheduler |
| CLI | Typer + Rich |
| Testing | pytest + httpx (async) |
| Config | Pydantic Settings (`.env`) |

No external services. Everything runs locally with zero paid dependencies.

---

## Repository Structure

The agent must create and populate this exact structure:

```
linkvault/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── .env.example
├── alembic.ini
├── alembic/
│   └── versions/
├── linkvault/
│   ├── __init__.py
│   ├── main.py              # FastAPI app factory
│   ├── config.py            # Pydantic Settings
│   ├── database.py          # Async SQLAlchemy engine + session
│   ├── models/
│   │   ├── __init__.py
│   │   ├── link.py          # Link ORM model
│   │   ├── click.py         # Click event ORM model
│   │   └── user.py          # User + API key ORM model
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── link.py          # Pydantic request/response schemas
│   │   ├── click.py
│   │   └── user.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py          # Shared dependencies (auth, db session)
│   │   ├── links.py         # /links router
│   │   ├── redirects.py     # /{slug} redirect router
│   │   ├── analytics.py     # /analytics router
│   │   └── users.py         # /users router
│   ├── services/
│   │   ├── __init__.py
│   │   ├── shortener.py     # Slug generation logic
│   │   ├── analytics.py     # Click aggregation logic
│   │   └── cleanup.py       # Expired link purge job
│   └── cli/
│       ├── __init__.py
│       └── app.py           # Typer CLI app
└── tests/
    ├── conftest.py
    ├── test_links.py
    ├── test_redirects.py
    ├── test_analytics.py
    └── test_auth.py
```

---

## Feature Specifications

### 1. User & Authentication System

- `POST /users/register` — Create a user with `email` + `password`. Returns an **API key** (UUID4, stored hashed).
- `POST /users/token` — Exchange email/password for the API key in plaintext (for CLI use).
- All protected routes require `Authorization: Bearer <api_key>` header.
- Users may not access or modify resources owned by other users.
- Passwords stored as bcrypt hashes. API keys stored as SHA-256 hashes.

### 2. Link Management

`POST /links` *(authenticated)*

Request body:
```json
{
  "url": "https://example.com/some/long/path",
  "slug": "my-custom-slug",   // optional; auto-generated if omitted
  "expires_at": "2025-12-31T23:59:59Z",  // optional
  "max_clicks": 100   // optional; link deactivates after N clicks
}
```

Rules:
- Auto-generated slugs: 6 characters, Base58 alphabet (no `0`, `O`, `I`, `l`).
- Custom slugs: 3–64 chars, alphanumeric + hyphens only, case-insensitive, globally unique.
- `url` must be a valid HTTP or HTTPS URL.
- Reject slugs that collide with reserved words: `api`, `admin`, `health`, `docs`, `metrics`.

`GET /links` *(authenticated)* — Paginated list of the authenticated user's links. Supports `?page=` and `?page_size=` (max 100). Response includes total count.

`GET /links/{slug}` *(authenticated)* — Returns full link metadata including live click count.

`PATCH /links/{slug}` *(authenticated)* — Update `url`, `expires_at`, or `max_clicks` only. Slug and owner are immutable.

`DELETE /links/{slug}` *(authenticated)* — Soft-delete (sets `deleted_at`). The slug becomes immediately reusable.

### 3. Redirect Engine

`GET /{slug}` *(public)*

- Resolve slug → destination URL.
- Return `301 Moved Permanently` for permanent links, `302 Found` for links with an expiry or click cap.
- Record a **click event** with: `timestamp`, `ip_address` (anonymized: last octet zeroed for IPv4), `user_agent`, `referer`.
- If the link is expired (`expires_at` < now) or has hit `max_clicks`, return `410 Gone` with a JSON body `{"error": "link_expired"}`.
- If the slug doesn't exist, return `404` with `{"error": "not_found"}`.
- Redirect resolution must complete in **< 10ms** measured at the database query level (use a single indexed lookup).

### 4. Analytics API

`GET /analytics/{slug}` *(authenticated, owner only)*

Returns:
```json
{
  "slug": "abc123",
  "total_clicks": 412,
  "unique_ips": 308,
  "clicks_by_day": [
    {"date": "2025-01-15", "clicks": 42},
    ...
  ],
  "top_referers": [
    {"referer": "https://twitter.com", "clicks": 180},
    ...
  ],
  "top_user_agents": [
    {"user_agent": "Mozilla/5.0...", "clicks": 95},
    ...
  ]
}
```

Query params:
- `?days=30` (default) — lookback window, max 365.
- `?tz=America/Chicago` — IANA timezone for day bucketing (default UTC).

`GET /analytics/summary` *(authenticated)*

Aggregate across all of the user's links:
```json
{
  "total_links": 14,
  "active_links": 11,
  "total_clicks_all_time": 9821,
  "clicks_last_30_days": 1443,
  "top_link": {"slug": "xyz", "clicks": 2100}
}
```

### 5. Background Job: Expired Link Cleanup

- Runs every **15 minutes** via APScheduler.
- Finds all links where `expires_at < now` AND `deleted_at IS NULL`.
- Sets `deleted_at = now` on those links.
- Logs a structured message: `{"event": "cleanup", "links_expired": N, "timestamp": "..."}`.
- Job must survive application restart (APScheduler persistent job store backed by SQLite).

### 6. Health & Metrics Endpoint

`GET /health` *(public)*

```json
{
  "status": "ok",
  "db": "ok",
  "scheduler": "running",
  "version": "0.1.0"
}
```

If the DB is unreachable, return `503` with `"db": "error"`.

### 7. CLI Client

The CLI connects to a running LinkVault server (base URL configurable via `LINKVAULT_API_URL` env var or `--api-url` flag).

Commands:

```
linkvault login                          # prompt for email/password, store API key in ~/.linkvault/config.json
linkvault logout                         # remove stored credentials
linkvault shorten <url> [--slug SLUG] [--expires YYYY-MM-DD] [--max-clicks N]
linkvault list [--page N]
linkvault info <slug>
linkvault stats <slug> [--days N]
linkvault delete <slug>
```

Output requirements:
- `shorten`: Print the full short URL (e.g., `http://localhost:8000/abc123`) in bold green.
- `list`: Render a Rich table with columns: Slug, Destination (truncated to 50 chars), Clicks, Expires, Status.
- `stats`: Render a bar chart in the terminal using Rich's progress bars to show clicks-by-day.
- All error responses from the API must be printed in red with the error message from the JSON body.
- `--help` must be functional on every command.

---

## Database Schema

Define all models with SQLAlchemy 2.x declarative style. Use `Mapped[]` type annotations.

### `users`
| Column | Type | Notes |
|---|---|---|
| id | UUID | PK, default uuid4 |
| email | VARCHAR(255) | unique, not null |
| password_hash | VARCHAR(255) | bcrypt |
| api_key_hash | VARCHAR(255) | SHA-256 of the raw key |
| created_at | DATETIME | default now |

### `links`
| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| user_id | UUID | FK → users.id |
| slug | VARCHAR(64) | unique, indexed |
| destination_url | TEXT | not null |
| expires_at | DATETIME | nullable |
| max_clicks | INTEGER | nullable |
| click_count | INTEGER | default 0, not null |
| created_at | DATETIME | default now |
| deleted_at | DATETIME | nullable |

### `clicks`
| Column | Type | Notes |
|---|---|---|
| id | UUID | PK |
| link_id | UUID | FK → links.id |
| clicked_at | DATETIME | default now, indexed |
| ip_address | VARCHAR(45) | anonymized |
| user_agent | TEXT | nullable |
| referer | TEXT | nullable |

---

## Configuration (`.env.example`)

```env
DATABASE_URL=sqlite+aiosqlite:///./linkvault.db
SECRET_KEY=changeme-use-a-real-secret
BASE_URL=http://localhost:8000
LOG_LEVEL=INFO
SCHEDULER_DB_URL=sqlite:///./scheduler.db
```

All settings must be loaded via `pydantic-settings` with validation. The app must fail loudly on startup if required settings are missing.

---

## Error Handling Contract

All error responses must follow this envelope:

```json
{
  "error": "machine_readable_code",
  "detail": "Human readable message."
}
```

Use a custom FastAPI exception handler — do not expose raw Pydantic validation error blobs to the client. Map `RequestValidationError` to 422 with this format.

---

## Testing Requirements

Write tests using `pytest` + `pytest-asyncio` + `httpx.AsyncClient`.

The test suite must include:

**`test_auth.py`**
- Register a user successfully
- Reject duplicate email registration
- Reject login with wrong password
- Authenticated request succeeds with valid key
- Authenticated request fails with invalid key

**`test_links.py`**
- Create a link with auto-generated slug
- Create a link with a custom slug
- Reject a custom slug that is a reserved word
- Reject a duplicate slug
- Reject an invalid URL
- List links returns only the authenticated user's links
- Patch updates allowed fields
- Patch cannot change slug
- Soft-delete makes the slug available again

**`test_redirects.py`**
- Valid slug redirects to destination URL
- Unknown slug returns 404
- Expired link (past `expires_at`) returns 410
- Link at `max_clicks` returns 410
- Click event is recorded on valid redirect
- Redirect response time is under 50ms (integration test)

**`test_analytics.py`**
- `clicks_by_day` groups correctly by UTC day
- `unique_ips` counts deduplicated
- `top_referers` sorted descending by clicks
- Summary counts only the authenticated user's links

Use a separate in-memory SQLite database for tests (`sqlite+aiosqlite:///:memory:`). Each test gets a fresh DB via a session-scoped fixture.

---

## Code Quality Requirements

- All modules must have type annotations. Run `mypy linkvault/` with zero errors (strict mode is not required, but `--ignore-missing-imports` is not allowed).
- Async all the way down — no `asyncio.run()` inside request handlers.
- No hardcoded secrets or connection strings anywhere in source files.
- Slug generation must be extracted into its own pure function with a unit test that verifies Base58 alphabet compliance across 10,000 generated slugs.
- The click-count increment on redirect must be atomic (use a SQL `UPDATE links SET click_count = click_count + 1` — no read-then-write race condition).

---

## README Requirements

The `README.md` must include:

1. One-command setup: `pip install -e ".[dev]" && alembic upgrade head`
2. How to run the server: `uvicorn linkvault.main:app --reload`
3. How to run the CLI: `linkvault --help`
4. How to run tests: `pytest -v`
5. A complete `curl` example for the full lifecycle: register → shorten → redirect → get stats
6. A table of all API endpoints with method, path, auth required, and brief description

---

## Acceptance Criteria

The agent's output is considered complete when:

- [ ] `pip install -e ".[dev]"` succeeds with no dependency conflicts
- [ ] `alembic upgrade head` creates all tables without error
- [ ] `uvicorn linkvault.main:app` starts without error and `/health` returns `200`
- [ ] `pytest -v` passes all tests with no failures or skips
- [ ] `mypy linkvault/` exits with code 0
- [ ] The full curl lifecycle in the README executes successfully against the running server
- [ ] The CLI `linkvault shorten`, `linkvault list`, and `linkvault stats` commands produce correctly formatted output

---

## Intentional Ambiguities (Agent Must Resolve)

These are left open deliberately. The agent must pick a reasonable approach and document the decision in a `DECISIONS.md` file:

1. **Slug collision on auto-generate**: What happens if the generated slug is already taken? How many retries before returning an error?
2. **Click count vs. clicks table**: The `click_count` column and the `clicks` table can drift (e.g., if a click insert fails). Which is the source of truth for analytics? How is consistency maintained?
3. **Soft-delete and analytics**: Should clicks recorded before a soft-delete appear in analytics after the link is restored (slug reused by a new link)? 
4. **Timezone handling for `expires_at`**: If a user sends a naive datetime (no `Z` or offset), what timezone is assumed?
5. **Rate limiting**: The spec doesn't mention rate limiting. Should the redirect endpoint have one? At what threshold?

---

## Non-Goals

Do not build:
- A frontend or web UI (terminal dashboard only)
- OAuth or social login
- Email verification
- Link preview / metadata scraping
- Geographic analytics (country/city from IP)
- Docker or containerization

These are explicitly out of scope for this phase.
