"""create media tables

Revision ID: 8f2a4b6c1d90
Revises: 0d1f9f4a8b2c
Create Date: 2026-06-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from fastapi_users_db_sqlalchemy import generics

revision: str = "8f2a4b6c1d90"
down_revision: str | Sequence[str] | None = "0d1f9f4a8b2c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_item",
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("modified_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_media_item_storage_key"),
    )
    op.create_index("ix_media_item_category", "media_item", ["category"])
    op.create_index("ix_media_item_created_at", "media_item", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_media_item_created_at", table_name="media_item")
    op.drop_index("ix_media_item_category", table_name="media_item")
    op.drop_table("media_item")
