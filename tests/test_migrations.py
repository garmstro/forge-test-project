"""
Migration tests: ``alembic upgrade head`` must build the full schema and the
migrations must stay in sync with the ORM models.

Alembic runs in a subprocess so its logging configuration (``fileConfig`` in
``alembic/env.py``) cannot leak into the rest of the test session.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_RUNNER = textwrap.dedent(
    """
    import sys
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", sys.argv[2])
    getattr(command, sys.argv[1])(cfg, *sys.argv[3:])
    """
)


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, args[0], f"sqlite:///{db_path}", *args[1:]],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_upgrade_head_creates_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "migrated.db"
    result = _alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    with sqlite3.connect(db) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"users", "links", "clicks", "alembic_version"} <= tables


def test_migrations_match_models(tmp_path: Path) -> None:
    db = tmp_path / "check.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    result = _alembic(db, "check")
    assert result.returncode == 0, result.stderr


def test_slug_can_be_reused_after_soft_delete_in_migrated_db(tmp_path: Path) -> None:
    """Slug index must not be UNIQUE (uniqueness is app-level, among active links)."""
    db = tmp_path / "slug.db"
    assert _alembic(db, "upgrade", "head").returncode == 0

    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO users (id, email, password_hash, api_key_hash) VALUES ('u','e','p','k')")
        for link_id in ("l1", "l2"):
            conn.execute(
                "INSERT INTO links (id, user_id, slug, destination_url, click_count) "
                "VALUES (?, 'u', 'same-slug', 'https://example.com', 0)",
                (link_id,),
            )
        count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    assert count == 2


def test_downgrade_to_base_removes_tables(tmp_path: Path) -> None:
    db = tmp_path / "down.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    result = _alembic(db, "downgrade", "base")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert not ({"users", "links", "clicks"} & tables)
