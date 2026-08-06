# CODING_STANDARDS.md — LinkVault Code Style Guide

This document describes the coding standards, conventions, and patterns used throughout the LinkVault codebase. All contributors should follow these guidelines to maintain consistency and code quality.

---

## Table of Contents

- [General Principles](#general-principles)
- [Python Version and Imports](#python-version-and-imports)
- [Type Annotations](#type-annotations)
- [Async/Await Patterns](#asyncawait-patterns)
- [Database and SQLAlchemy](#database-and-sqlalchemy)
- [FastAPI Conventions](#fastapi-conventions)
- [Error Handling](#error-handling)
- [Code Organization](#code-organization)
- [Documentation and Comments](#documentation-and-comments)
- [Testing Standards](#testing-standards)
- [Security Practices](#security-practices)

---

## General Principles

1. **Explicit is better than implicit** — No magic. Every dependency, every side effect, every assumption should be visible in the code.
2. **Fail loudly** — Invalid configuration, missing dependencies, or constraint violations should raise exceptions at startup or request time, not silently degrade.
3. **No stubs or TODOs in main branch** — Every function must be fully implemented. Use feature branches for work-in-progress.
4. **Performance matters** — The redirect endpoint must resolve in < 10ms. Use indexed queries, atomic operations, and avoid N+1 patterns.

---

## Python Version and Imports

### Required Python Version

- **Python 3.11+** is required for all code.
- Use modern type annotation syntax (e.g., `list[str]` instead of `List[str]`).

### Import Order and Style

All modules must start with:

```python
from __future__ import annotations
```

This enables forward references and allows using standard collection types (`list`, `dict`) in type hints without importing from `typing`.

**Import order:**
1. Standard library imports
2. Third-party imports (FastAPI, SQLAlchemy, etc.)
3. Local application imports

**Example:**

```python
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from linkvault.api.deps import get_current_user
from linkvault.database import get_db
from linkvault.models.link import Link
```

---

## Type Annotations

### Mandatory Type Hints

- **All function signatures** must include type annotations for parameters and return values.
- **Class attributes** must use `Mapped[]` annotations for SQLAlchemy models.
- **No `Any` types** unless absolutely necessary (e.g., JSON payloads from external sources).

**Good:**

```python
def generate_slug(length: int = SLUG_LENGTH) -> str:
    """Return a random Base58 slug of *length* characters."""
    return "".join(random.choices(BASE58_ALPHABET, k=length))
```

**Bad:**

```python
def generate_slug(length=SLUG_LENGTH):  # Missing type hints
    return "".join(random.choices(BASE58_ALPHABET, k=length))
```

### Union Types and Optionals

Use the modern `|` syntax for union types:

```python
expires_at: datetime | None = None
```

Not:

```python
from typing import Optional
expires_at: Optional[datetime] = None
```

### Type Checking

All code must pass `mypy` with zero errors:

```bash
mypy linkvault/
```

Configuration in `pyproject.toml`:

```toml
[tool.mypy]
python_version = "3.11"
ignore_missing_imports = false
warn_return_any = true
warn_unused_ignores = true
```

---

## Async/Await Patterns

### Async All the Way Down

- **All database operations** must use `async`/`await`.
- **All API route handlers** must be `async def`.
- **No blocking I/O** in the request path (no `time.sleep()`, no synchronous file I/O).

**Good:**

```python
@router.get("/{slug}", response_model=LinkResponse)
async def get_link(
    slug: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LinkResponse:
    link = await _resolve_active_link(slug, current_user, db)
    return LinkResponse.model_validate(link)
```

**Bad:**

```python
@router.get("/{slug}")
def get_link(slug: str, db: Session = Depends(get_db)):  # Synchronous
    link = db.query(Link).filter_by(slug=slug).first()
    return link
```

### No `asyncio.run()` in Request Handlers

Never call `asyncio.run()` inside a FastAPI route handler — it creates a new event loop and will fail. Use `await` directly.

---

## Database and SQLAlchemy

### ORM Models

Use SQLAlchemy 2.x declarative style with `Mapped[]` type annotations:

```python
from sqlalchemy.orm import Mapped, mapped_column

class Link(Base):
    __tablename__ = "links"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    destination_url: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

### Query Patterns

- Use `select()` for all queries (not the legacy `Query` API).
- Always use `await` for query execution.
- Use `.scalar_one_or_none()` when expecting zero or one result.
- Use `.scalars().all()` when expecting multiple results.

**Example:**

```python
result = await db.execute(
    select(Link).where(Link.slug == slug, Link.deleted_at.is_(None))
)
link = result.scalar_one_or_none()
```

### Atomic Operations

For operations that must be atomic (e.g., incrementing click counts), use SQL expressions:

```python
await db.execute(
    update(Link)
    .where(Link.slug == slug)
    .values(click_count=Link.click_count + 1)
)
```

**Never** do read-then-write for counters:

```python
# BAD: Race condition
link = await db.get(Link, link_id)
link.click_count += 1
await db.flush()
```

### Indexes

- Add indexes to columns used in `WHERE` clauses (e.g., `slug`, `user_id`, `clicked_at`).
- Document index decisions in migration files.

---

## FastAPI Conventions

### Router Organization

- One router per resource (e.g., `links.py`, `users.py`, `analytics.py`).
- Use `APIRouter` with a `prefix` and `tags`:

```python
router = APIRouter(prefix="/links", tags=["links"])
```

- Include routers in `main.py` in logical order (catch-all routes like `/{slug}` must be last).

### Dependency Injection

Use FastAPI's `Depends()` for:
- Database sessions (`db: AsyncSession = Depends(get_db)`)
- Authentication (`current_user: User = Depends(get_current_user)`)
- Shared validation logic

**Example:**

```python
@router.post("", response_model=LinkResponse)
async def create_link(
    payload: LinkCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LinkResponse:
    # ...
```

### Response Models

- Always specify `response_model` for routes that return data.
- Use Pydantic models for request/response schemas (in `schemas/` directory).
- Use `.model_validate()` to convert ORM models to Pydantic models:

```python
return LinkResponse.model_validate(link)
```

### Status Codes

- Use `status` constants from `fastapi.status`:
  - `status.HTTP_201_CREATED` for resource creation
  - `status.HTTP_204_NO_CONTENT` for successful deletes
  - `status.HTTP_404_NOT_FOUND` for missing resources
  - `status.HTTP_409_CONFLICT` for uniqueness violations

---

## Error Handling

### Consistent Error Envelope

All error responses must follow this format:

```json
{
  "error": "machine_readable_code",
  "detail": "Human readable message."
}
```

### Custom Exception Handlers

Register exception handlers in `main.py`:

```python
@application.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    first_error = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(loc) for loc in first_error.get("loc", []))
    msg = first_error.get("msg", "Validation error.")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "validation_error", "detail": f"{field}: {msg}"},
    )
```

### Raising HTTP Exceptions

Use `HTTPException` with appropriate status codes and detail messages:

```python
if link is None:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Link not found.",
    )
```

---

## Code Organization

### Module Structure

```
linkvault/
├── main.py              # FastAPI app factory
├── config.py            # Pydantic Settings
├── database.py          # Async SQLAlchemy engine + session
├── models/              # ORM models
├── schemas/             # Pydantic request/response schemas
├── api/                 # FastAPI routers
│   └── deps.py          # Shared dependencies
├── services/            # Business logic (pure functions when possible)
└── cli/                 # Typer CLI app
```

### Separation of Concerns

- **Models** (`models/`) — Database schema only. No business logic.
- **Schemas** (`schemas/`) — Request/response validation. No database access.
- **API** (`api/`) — Route handlers. Orchestrate services and database calls.
- **Services** (`services/`) — Reusable business logic. Pure functions when possible.

### Helper Functions

Extract complex logic into helper functions with a leading underscore:

```python
async def _resolve_active_link(slug: str, user: User, db: AsyncSession) -> Link:
    """Return the active (non-deleted) link owned by *user* with *slug*, or raise 404/403."""
    # ...
```

---

## Documentation and Comments

### Docstrings

- Use triple-quoted docstrings for all public functions and classes.
- Use imperative mood ("Return the link" not "Returns the link").
- Keep docstrings concise — one line is often enough.

**Example:**

```python
def generate_slug(length: int = SLUG_LENGTH) -> str:
    """Return a random Base58 slug of *length* characters."""
    return "".join(random.choices(BASE58_ALPHABET, k=length))
```

### Inline Comments

Use inline comments to explain **why**, not **what**:

```python
# No DB-level UNIQUE on slug — uniqueness among *active* links is enforced
# in the application layer (filtered on deleted_at IS NULL).  A DB-level
# unique constraint would prevent slug reuse after a soft-delete.
slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
```

### Section Dividers

Use comment dividers to separate logical sections in long files:

```python
# ---------------------------------------------------------------------------
# POST /links — create a new short link
# ---------------------------------------------------------------------------
```

---

## Testing Standards

### Test Organization

- One test file per module (e.g., `test_links.py`, `test_auth.py`).
- Use `pytest` with `pytest-asyncio` for async tests.
- All tests must use an in-memory SQLite database.

### Test Naming

Test function names should describe the behavior being tested:

```python
async def test_create_link_with_custom_slug():
    """Custom slug is accepted and stored as-is."""
    # ...

async def test_create_link_rejects_reserved_slug():
    """Reserved slugs like 'admin' are rejected with 422."""
    # ...
```

### Test Structure

Follow the Arrange-Act-Assert pattern:

```python
async def test_redirect_increments_click_count(client, auth_headers, db):
    # Arrange
    link = await create_test_link(db, slug="test", user_id="user-123")
    
    # Act
    response = await client.get("/test")
    
    # Assert
    assert response.status_code == 302
    await db.refresh(link)
    assert link.click_count == 1
```

### Coverage Requirements

- All API endpoints must have at least one happy-path test and one error-case test.
- Critical business logic (slug generation, click counting) must have dedicated unit tests.

---

## Security Practices

### No Hardcoded Secrets

- All secrets must be loaded from environment variables via `pydantic-settings`.
- Never commit `.env` files (only `.env.example`).

### Password Hashing

- Use `bcrypt` for password hashing (never store plaintext passwords).
- API keys are stored as SHA-256 hashes.

**Example:**

```python
import bcrypt

password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
```

### Input Validation

- Use Pydantic models for all request validation.
- Validate custom slugs with regex: `^[a-zA-Z0-9-]{3,64}$`
- Reject reserved slugs: `api`, `admin`, `health`, `docs`, `metrics`

### SQL Injection Prevention

- Always use SQLAlchemy's parameterized queries (never string concatenation).
- Use `.where()` with bound parameters:

```python
select(Link).where(Link.slug == slug)  # Safe
```

Not:

```python
f"SELECT * FROM links WHERE slug = '{slug}'"  # NEVER DO THIS
```

---

## Summary Checklist

Before submitting code, verify:

- [ ] All functions have type annotations
- [ ] `mypy linkvault/` passes with zero errors
- [ ] All async functions use `await` (no blocking I/O)
- [ ] Database queries use `select()` and `await`
- [ ] Error responses follow the `{"error": "...", "detail": "..."}` format
- [ ] Tests pass: `pytest -v`
- [ ] No hardcoded secrets or connection strings
- [ ] Docstrings are present for public functions
- [ ] Code follows the established module structure

---

**Questions or clarifications?** See `DECISIONS.md` for architectural rationale, or open an issue for discussion.
