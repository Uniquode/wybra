"""add media resource keys table

Revision ID: 4f2b9d8d0f91
Revises: 8f2a4b6c1d90
Create Date: 2026-06-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from wybra.db import types as generics

revision: str = "4f2b9d8d0f91"
down_revision: str | Sequence[str] | None = "8f2a4b6c1d90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_resource_key",
        sa.Column(
            "media_id",
            generics.GUID(),
            sa.ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_key", sa.String(length=255), primary_key=True),
    )
    op.create_index(
        "ix_media_resource_key_media_id",
        "media_resource_key",
        ["media_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_resource_key_media_id",
        table_name="media_resource_key",
    )
    op.drop_table("media_resource_key")
