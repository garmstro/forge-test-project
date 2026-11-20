# LinkVault 🔗

> A production-grade URL shortening and analytics platform — built with FastAPI, SQLAlchemy (async), APScheduler, and a full-featured terminal CLI.

Updated by Forge — 18 November 2026, 17:02 UTC

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
- [Running the Server](#running-the-server)
- [Running the CLI](#running-the-cli)
- [Running the Tests](#running-the-tests)
- [Full Lifecycle Example (curl)](#full-lifecycle-example-curl)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [Project Structure](#project-structure)
- [Design Decisions](#design-decisions)

---

## Overview

LinkVault is a self-hosted URL shortener with first-class analytics. It exposes a REST API for managing short links, tracks every click with anonymised metadata, and surfaces aggregated statistics through both the API and a Rich-powered terminal dashboard. A background scheduler automatically expires stale links every 15 minutes.

Everything runs **locally with zero paid dependencies** — no external services, no cloud accounts required.

---

## Features

- **Short link management** — create, update, soft-delete, and list links with optional custom slugs, expiry dates, and per-link click caps.
- **Redirect engine** — single-query slug resolution with atomic click-count increments; `301` for permanent links, `302` for capped/expiring ones, `410` when a link is exhausted.
- **Click analytics** — per-link breakdown by day, unique IPs, top referers, and top user-agents; user-level summary across all links.
- **Authentication** — email + password registration; API key (UUID4) issued on registration and exchangeable via `/users/token`; all protected routes use `Authorization: Bearer <api_key>`.
- **Background cleanup** — APScheduler job (SQLite-backed, survives restarts) expires links every 15 minutes and emits structured JSON log lines.
- **Terminal CLI** — `linkvault` command for every API operation, with Rich tables, bold-green short URLs, and a bar-chart view of daily click data.
- **Health endpoint** — `/health` reports DB and scheduler status; returns `503` when the database is unreachable.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Web Framework | FastAPI |
| Database | SQLite via SQLAlchemy 2.x (async) |
| Migrations | Alembic |
| Background Jobs | APScheduler |
| CLI | Typer + Rich |
| Testing | pytest + pytest-asyncio + httpx |
| Config | Pydantic Settings (`.env`) |

---

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/garmstro/forge-test-project.git
cd forge-test-project

# 2. Copy the example environment file and edit as needed
cp .env.example .env

# 3. Install the package and all dev dependencies, then run migrations
pip install -e ".[dev]" && alembic upgrade head
```

That single command installs LinkVault in editable mode (so `linkvault` CLI is on your `PATH`) and creates all database tables.

---

## Running the Server

```bash
uvicorn linkvault.main:app --reload
```

The API is now available at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs`  
Health check: `http://localhost:8000/health`

---

## Running the CLI

The `linkvault` command is a client for a **running** LinkVault server — it talks to the REST API and never touches the database directly. Every command supports `--help`:

```bash
linkvault --help
linkvault shorten --help
```

### Server address

Every command accepts `--api-url`. If it is not given, the CLI uses the `LINKVAULT_API_URL` environment variable, and falls back to `http://localhost:8000`:

```bash
export LINKVAULT_API_URL=http://localhost:8000      # optional
linkvault list --api-url http://links.internal:8000  # flag wins over the env var
```

### First-time setup

The CLI has no `register` command. Create your first user with the API (see the [curl example](#full-lifecycle-example-curl)):

```bash
curl -s -X POST http://localhost:8000/users/register \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com", "password": "s3cr3t!"}'
```

Then log in. You are prompted for email and password (the password is hidden as you type), and the API key is saved to `~/.linkvault/config.json`, readable only by your user:

```bash
linkvault login
```

> **Note:** `linkvault login` issues a **new** API key on every call and the previous key stops working (see `DECISIONS.md` §6). Scripts or other machines that use the old key need to log in again.

### Commands

```bash
# Create a short link — prints the full short URL in bold green
linkvault shorten https://example.com/some/long/path
linkvault shorten https://example.com --slug demo
linkvault shorten https://example.com --expires 2026-12-31 --max-clicks 100
#   --expires YYYY-MM-DD means the end of that day (23:59:59 UTC)

# Table of your links: Slug, Destination (cut to 50 chars), Clicks, Expires, Status
linkvault list
linkvault list --page 2

# Full details for one link, including the live click count
linkvault info demo

# Totals, a clicks-by-day bar chart, top referers and top user agents
linkvault stats demo              # last 30 days
linkvault stats demo --days 7     # 1-365

# Delete a link (the slug becomes reusable)
linkvault delete demo

# Remove the stored API key
linkvault logout
```

Status in `list` / `info` is **Active**, **Expired** (past its expiry date) or **Exhausted** (click limit reached).

### Errors

Errors from the server (wrong password, unknown slug, someone else's link, reserved or duplicate slug, invalid URL…) are printed in red with the server's message, and the command exits with a non-zero code. Commands that need a login print "Run `linkvault login` first" when no key is stored, and suggest logging in again if the stored key is rejected. If the server is unreachable you'll see `Could not connect to <address>. Is the server running?`.

---

## Running the Tests

```bash
pytest -v
```

The test suite uses an in-memory SQLite database — no running server required. CLI tests (`tests/test_cli.py`) use an in-memory stand-in for the server. Each test gets a fresh database via a session-scoped fixture.

To also check types:

```bash
mypy linkvault/
```

---

## Full Lifecycle Example (curl)

The following sequence demonstrates every major feature against a running server.

```bash
BASE="http://localhost:8000"

# 1. Register a new user — receive an API key
curl -s -X POST "$BASE/users/register" \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com", "password": "s3cr3t!"}' | tee /tmp/register.json

API_KEY=$(jq -r '.api_key' /tmp/register.json)

# 2. (Alternative) Exchange credentials for the API key
curl -s -X POST "$BASE/users/token" \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com", "password": "s3cr3t!"}'

# 3. Create a short link with a custom slug and a click cap
curl -s -X POST "$BASE/links" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "url": "https://example.com/some/very/long/path?utm_source=demo",
        "slug": "demo",
        "expires_at": "2026-12-31T23:59:59Z",
        "max_clicks": 1000
      }'

# 4. Follow the redirect (use -L to follow, or inspect the Location header)
curl -v "$BASE/demo"

# 5. List all your links (paginated)
curl -s "$BASE/links?page=1&page_size=10" \
  -H "Authorization: Bearer $API_KEY"

# 6. Fetch full metadata for a single link
curl -s "$BASE/links/demo" \
  -H "Authorization: Bearer $API_KEY"

# 7. Update the expiry date
curl -s -X PATCH "$BASE/links/demo" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"expires_at": "2027-06-30T00:00:00Z"}'

# 8. Get per-link analytics (last 30 days, Chicago time)
curl -s "$BASE/analytics/demo?days=30&tz=America/Chicago" \
  -H "Authorization: Bearer $API_KEY"

# 9. Get a summary across all your links
curl -s "$BASE/analytics/summary" \
  -H "Authorization: Bearer $API_KEY"

# 10. Soft-delete the link (slug becomes immediately reusable)
curl -s -X DELETE "$BASE/links/demo" \
  -H "Authorization: Bearer $API_KEY"

# 11. Confirm the server and scheduler are healthy
curl -s "$BASE/health"
```

---

## API Reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/users/register` | ✗ | Register a new user; returns plaintext API key |
| `POST` | `/users/token` | ✗ | Exchange email + password for the API key |
| `GET` | `/health` | ✗ | Service health (DB + scheduler status) |
| `GET` | `/{slug}` | ✗ | Redirect to destination URL; records click event |
| `POST` | `/links` | ✓ | Create a new short link |
| `GET` | `/links` | ✓ | Paginated list of the authenticated user's links |
| `GET` | `/links/{slug}` | ✓ | Full metadata + live click count for one link |
| `PATCH` | `/links/{slug}` | ✓ | Update `url`, `expires_at`, or `max_clicks` |
| `DELETE` | `/links/{slug}` | ✓ | Soft-delete a link (slug immediately reusable) |
| `GET` | `/analytics/{slug}` | ✓ | Per-link analytics (clicks by day, IPs, referers) |
| `GET` | `/analytics/summary` | ✓ | Aggregate stats across all of the user's links |

All error responses follow a consistent envelope:

```json
{
  "error": "machine_readable_code",
  "detail": "Human readable message."
}
```

---

## Configuration

Copy `.env.example` to `.env` and adjust values:

```env
DATABASE_URL=sqlite+aiosqlite:///./linkvault.db
SECRET_KEY=changeme-use-a-real-secret
BASE_URL=http://localhost:8000
LOG_LEVEL=INFO
SCHEDULER_DB_URL=sqlite:///./scheduler.db
```

All settings are validated at startup via `pydantic-settings`. The application will **fail loudly** if required settings are missing or malformed.

---

## Project Structure

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
│   ├── main.py              # FastAPI app factory
│   ├── config.py            # Pydantic Settings
│   ├── database.py          # Async SQLAlchemy engine + session
│   ├── models/
│   │   ├── link.py          # Link ORM model
│   │   ├── click.py         # Click event ORM model
│   │   └── user.py          # User + API key ORM model
│   ├── schemas/
│   │   ├── link.py          # Pydantic request/response schemas
│   │   ├── click.py
│   │   └── user.py
│   ├── api/
│   │   ├── deps.py          # Shared dependencies (auth, DB session)
│   │   ├── links.py         # /links router
│   │   ├── redirects.py     # /{slug} redirect router
│   │   ├── analytics.py     # /analytics router
│   │   └── users.py         # /users router
│   ├── services/
│   │   ├── shortener.py     # Slug generation (Base58, collision-safe)
│   │   ├── analytics.py     # Click aggregation logic
│   │   └── cleanup.py       # Expired link purge job
│   └── cli/
│       └── app.py           # Typer CLI app
└── tests/
    ├── conftest.py
    ├── test_links.py
    ├── test_redirects.py
    ├── test_analytics.py
    ├── test_auth.py
    └── test_cli.py
```

---

## Design Decisions

See [`DECISIONS.md`](DECISIONS.md) for the full rationale behind each architectural choice, including:

- **Slug collision strategy** — up to 5 retries with exponential back-off before returning a `409 Conflict`; probability of exhaustion at normal scale is negligible.
- **Click-count consistency** — `links.click_count` is the authoritative counter (atomically incremented via `UPDATE … SET click_count = click_count + 1`); the `clicks` table is used exclusively for detailed analytics. A discrepancy caused by a failed insert is acceptable and self-documents in logs.
- **Soft-delete and analytics** — clicks are tied to `link_id` (UUID), not slug. Reusing a slug for a new link creates a new UUID, so historical click data is never mixed across ownership boundaries.
- **Naive datetime handling** — datetimes without a timezone offset are assumed to be **UTC** and stored as-is. The API documents this behaviour and recommends always sending an explicit offset.
- **Rate limiting** — no rate limiting is implemented in this phase. The redirect endpoint is designed for sub-10 ms resolution; infrastructure-level rate limiting (e.g., a reverse proxy) is the recommended approach for production deployments.

