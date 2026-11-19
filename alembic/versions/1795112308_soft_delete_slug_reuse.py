"""soft_delete_slug_reuse

Revision ID: soft_delete_slug_reuse
Revises: e9c9aef67bb5
Create Date: 2026-11-19 18:20:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'soft_delete_slug_reuse'
down_revision: Union[str, None] = 'e9c9aef67bb5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old unconditional UNIQUE constraint on slug if it exists
    with op.get_context().autocommit_block():
        try:
            op.drop_index(op.f('ix_links_slug'), table_name='links')
        except Exception:
            # Index might not exist if the original migration wasn't fully applied
            pass
    
    # Create a partial index that only applies to non-deleted links
    # This allows soft-deleted links to be "garbage collected" without blocking slug reuse
    op.create_index(
        'ix_links_slug_active',
        'links',
        ['slug'],
        unique=True,
        sqlite_where=sa.text('deleted_at IS NULL')
    )


def downgrade() -> None:
    # Revert to the original unconditional UNIQUE constraint
    op.drop_index('ix_links_slug_active', table_name='links')
    op.create_index(op.f('ix_links_slug'), 'links', ['slug'], unique=True)
