"""add password login enabled flag

Revision ID: c1e9a7d6b5c4
Revises: 6c4e8a9d1b2f
Create Date: 2026-07-01 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1e9a7d6b5c4"
down_revision: str | Sequence[str] | None = "6c4e8a9d1b2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "identity_user",
        sa.Column("password_login_enabled", sa.Boolean(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE identity_user "
            "SET password_login_enabled = COALESCE(password_login_enabled, :enabled)"
        ).bindparams(enabled=True)
    )
    with op.batch_alter_table("identity_user") as batch_op:
        batch_op.alter_column(
            "password_login_enabled",
            existing_type=sa.Boolean(),
            nullable=False,
        )
        batch_op.alter_column(
            "hashed_password",
            existing_type=sa.String(length=1024),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("identity_user") as batch_op:
        batch_op.alter_column(
            "hashed_password",
            existing_type=sa.String(length=1024),
            nullable=False,
        )
        batch_op.drop_column("password_login_enabled")
