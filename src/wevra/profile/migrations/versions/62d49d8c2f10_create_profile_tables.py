"""create profile tables

Revision ID: 62d49d8c2f10
Revises: 4f2b9d8d0f91
Create Date: 2026-06-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from fastapi_users_db_sqlalchemy import generics

revision: str = "62d49d8c2f10"
down_revision: str | Sequence[str] | None = "4f2b9d8d0f91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profile_user_profile",
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("user_id", generics.GUID(), nullable=False),
        sa.Column("profile_picture_media_id", generics.GUID(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["profile_picture_media_id"],
            ["media_item.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_profile_user_profile_user_id"),
    )
    op.create_index(
        "ix_profile_user_profile_user_id",
        "profile_user_profile",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_profile_user_profile_user_id",
        table_name="profile_user_profile",
    )
    op.drop_table("profile_user_profile")
