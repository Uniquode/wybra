"""remove redundant auth name fields

Revision ID: 6c4e8a9d1b2f
Revises: 0d1f9f4a8b2c, 2f9b8d7c1a2e
Create Date: 2026-06-26 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6c4e8a9d1b2f"
down_revision: str | Sequence[str] | None = ("0d1f9f4a8b2c", "2f9b8d7c1a2e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("identity_user") as batch_op:
        batch_op.drop_column("preferred_name")
        batch_op.drop_column("display_name")


def downgrade() -> None:
    with op.batch_alter_table("identity_user") as batch_op:
        batch_op.add_column(sa.Column("display_name", sa.String(length=320)))
        batch_op.add_column(sa.Column("preferred_name", sa.String(length=120)))
