"""add identity user email table

Revision ID: b9c4f2a8d0a1
Revises: a8c2d1f7b9e0
Create Date: 2026-06-10 00:00:00.000000
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from fastapi_users_db_sqlalchemy import generics

revision: str = "b9c4f2a8d0a1"
down_revision: str | Sequence[str] | None = "a8c2d1f7b9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_user_email",
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("user_id", generics.GUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["identity_user.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("email", name="uq_identity_user_email_email"),
    )
    op.create_index(
        "ix_identity_user_email_user_id",
        "identity_user_email",
        ["user_id"],
        unique=False,
    )

    identity_user_table = sa.table(
        "identity_user",
        sa.column("id"),
        sa.column("email"),
        sa.column("is_verified"),
    )
    identity_user_email_table = sa.table(
        "identity_user_email",
        sa.column("id"),
        sa.column("user_id"),
        sa.column("email"),
        sa.column("is_primary"),
        sa.column("is_verified"),
    )

    connection = op.get_bind()
    existing_rows = connection.execute(
        sa.select(
            identity_user_table.c.id,
            identity_user_table.c.email,
            identity_user_table.c.is_verified,
        )
    ).all()
    if existing_rows:
        connection.execute(
            sa.insert(identity_user_email_table),
            [
                {
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "email": email,
                    "is_primary": True,
                    "is_verified": is_verified,
                }
                for user_id, email, is_verified in existing_rows
            ],
        )


def downgrade() -> None:
    op.drop_index("ix_identity_user_email_user_id", table_name="identity_user_email")
    op.drop_table("identity_user_email")
