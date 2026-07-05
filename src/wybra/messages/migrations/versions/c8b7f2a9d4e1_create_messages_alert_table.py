"""create messages alert table

Revision ID: c8b7f2a9d4e1
Revises: e3f1c2d4a5b6
Create Date: 2026-07-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8b7f2a9d4e1"
down_revision: str | Sequence[str] | None = "e3f1c2d4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "messages_alert",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("queue_key", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_messages_alert_queue_key_id",
        "messages_alert",
        ["queue_key", "id"],
    )
    op.create_index(
        "ix_messages_alert_expires_at",
        "messages_alert",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_alert_expires_at", table_name="messages_alert")
    op.drop_index("ix_messages_alert_queue_key_id", table_name="messages_alert")
    op.drop_table("messages_alert")
