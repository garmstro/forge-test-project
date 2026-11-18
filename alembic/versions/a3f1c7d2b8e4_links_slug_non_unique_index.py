"""links_slug_non_unique_index

The ``links.slug`` index was created UNIQUE in the initial migration, but the
``Link`` model intentionally has no DB-level unique constraint: uniqueness is
enforced in the application layer among *active* links only (deleted_at IS
NULL), so a slug can be reused after a soft-delete.  A unique index makes that
impossible on a migrated database, so replace it with a plain index.

Revision ID: a3f1c7d2b8e4
Revises: e9c9aef67bb5
Create Date: 2026-05-07 14:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a3f1c7d2b8e4'
down_revision: Union[str, None] = 'e9c9aef67bb5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f('ix_links_slug'), table_name='links')
    op.create_index(op.f('ix_links_slug'), 'links', ['slug'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_links_slug'), table_name='links')
    op.create_index(op.f('ix_links_slug'), 'links', ['slug'], unique=True)
