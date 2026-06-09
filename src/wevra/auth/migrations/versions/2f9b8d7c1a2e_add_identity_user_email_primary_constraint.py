"""add identity user email primary constraint

Revision ID: 2f9b8d7c1a2e
Revises: b9c4f2a8d0a1
Create Date: 2026-06-10 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f9b8d7c1a2e"
down_revision: str | Sequence[str] | None = "b9c4f2a8d0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_identity_user_email_primary_per_user",
        "identity_user_email",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
        sqlite_where=sa.text("is_primary"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_identity_user_email_primary_per_user", table_name="identity_user_email"
    )
