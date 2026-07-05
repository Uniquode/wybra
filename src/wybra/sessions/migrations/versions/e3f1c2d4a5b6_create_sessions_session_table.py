"""create sessions session table

Revision ID: e3f1c2d4a5b6
Revises:
Create Date: 2026-07-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3f1c2d4a5b6"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions_session",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sessions_session_expires_at",
        "sessions_session",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_session_expires_at", table_name="sessions_session")
    op.drop_table("sessions_session")
