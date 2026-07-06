"""add profile edit fields and phone contacts

Revision ID: b0a2c1d3e4f5
Revises: 9a3f2e1ad4bc
Create Date: 2026-06-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from wybra.db import types as generics

revision: str = "b0a2c1d3e4f5"
down_revision: str | Sequence[str] | None = "9a3f2e1ad4bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "profile_user_profile",
        sa.Column("preferred_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("display_name", sa.String(length=200), nullable=True),
    )
    op.create_table(
        "profile_phone_contact",
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("user_id", generics.GUID(), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("subdivision_code", sa.String(length=16), nullable=True),
        sa.Column("normalised_number", sa.String(length=32), nullable=False),
        sa.Column("number_type", sa.String(length=32), nullable=False),
        sa.Column(
            "sms_capable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.sql.expression.false(),
        ),
        sa.Column("verified_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_profile_phone_contact_user_id",
        "profile_phone_contact",
        ["user_id"],
    )
    op.create_index(
        "ix_profile_phone_contact_normalised_number",
        "profile_phone_contact",
        ["normalised_number"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_profile_phone_contact_normalised_number",
        table_name="profile_phone_contact",
    )
    op.drop_index(
        "ix_profile_phone_contact_user_id",
        table_name="profile_phone_contact",
    )
    op.drop_table("profile_phone_contact")
    op.drop_column("profile_user_profile", "display_name")
    op.drop_column("profile_user_profile", "preferred_name")
