"""add optional profile fields

Revision ID: 9a3f2e1ad4bc
Revises: 62d49d8c2f10
Create Date: 2026-06-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a3f2e1ad4bc"
down_revision: str | Sequence[str] | None = "62d49d8c2f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "profile_user_profile",
        sa.Column("bio", sa.Text(), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("first_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("last_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("pronouns", sa.JSON(), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("phone_number", sa.String(length=48), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("website_links", sa.JSON(), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("country_region", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("city", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("postal_code", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("job_title", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("company", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("company_industry", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("department", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("date_time_format", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("theme", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("notification_preferences", sa.JSON(), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column(
            "profile_visibility",
            sa.String(length=16),
            nullable=False,
            server_default="public",
        ),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column(
            "marketing_consent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.sql.expression.false(),
        ),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column("terms_accepted_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "profile_user_profile",
        sa.Column(
            "data_deletion_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.sql.expression.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("profile_user_profile", "data_deletion_requested")
    op.drop_column("profile_user_profile", "terms_accepted_at")
    op.drop_column("profile_user_profile", "marketing_consent")
    op.drop_column("profile_user_profile", "profile_visibility")
    op.drop_column("profile_user_profile", "notification_preferences")
    op.drop_column("profile_user_profile", "theme")
    op.drop_column("profile_user_profile", "date_time_format")
    op.drop_column("profile_user_profile", "department")
    op.drop_column("profile_user_profile", "company_industry")
    op.drop_column("profile_user_profile", "company")
    op.drop_column("profile_user_profile", "job_title")
    op.drop_column("profile_user_profile", "last_name")
    op.drop_column("profile_user_profile", "first_name")
    op.drop_column("profile_user_profile", "postal_code")
    op.drop_column("profile_user_profile", "city")
    op.drop_column("profile_user_profile", "country_region")
    op.drop_column("profile_user_profile", "website_links")
    op.drop_column("profile_user_profile", "phone_number")
    op.drop_column("profile_user_profile", "pronouns")
    op.drop_column("profile_user_profile", "bio")
