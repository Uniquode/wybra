"""remove redundant identity link constraints

Revision ID: f4d2b8a1e9c3
Revises: 4b9a6c2d1e3f
Create Date: 2026-06-09 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f4d2b8a1e9c3"
down_revision: str | Sequence[str] | None = "4b9a6c2d1e3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("identity_external_identity_link") as batch_op:
        batch_op.drop_constraint(
            "uq_identity_external_identity_link_user_provider",
            type_="unique",
        )
        batch_op.drop_index("ix_identity_external_identity_link_provider_id")


def downgrade() -> None:
    with op.batch_alter_table("identity_external_identity_link") as batch_op:
        batch_op.create_unique_constraint(
            "uq_identity_external_identity_link_user_provider",
            ["user_id", "provider_id"],
        )
        batch_op.create_index(
            "ix_identity_external_identity_link_provider_id",
            ["provider_id"],
            unique=False,
        )
