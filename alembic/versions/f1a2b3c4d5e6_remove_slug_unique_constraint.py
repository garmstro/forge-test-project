"""remove_slug_unique_constraint

Revision ID: f1a2b3c4d5e6
Revises: e9c9aef67bb5
Create Date: 2026-11-18 18:05:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e9c9aef67bb5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the unique constraint on slug to allow reuse after soft-delete.
    # Uniqueness among *active* links (deleted_at IS NULL) is enforced
    # at the application layer.
    op.drop_index(op.f('ix_links_slug'), table_name='links')
    op.create_index(op.f('ix_links_slug'), 'links', ['slug'], unique=False)


def downgrade() -> None:
    # Restore the unique constraint (for rollback).
    op.drop_index(op.f('ix_links_slug'), table_name='links')
    op.create_index(op.f('ix_links_slug'), 'links', ['slug'], unique=True)
