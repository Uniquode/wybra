"""create identity tables

Revision ID: 34736375e2f8
Revises:
Create Date: 2026-05-24 20:27:44.194731
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from fastapi_users_db_sqlalchemy import generics

revision: str = "34736375e2f8"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_initial_admin_bootstrap",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "identity_user",
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_identity_user_email"), "identity_user", ["email"], unique=True
    )
    op.create_table(
        "identity_access_token",
        sa.Column("user_id", generics.GUID(), nullable=False),
        sa.Column("token", sa.String(length=43), nullable=False),
        sa.Column("created_at", generics.TIMESTAMPAware(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"], ondelete="cascade"),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index(
        op.f("ix_identity_access_token_created_at"),
        "identity_access_token",
        ["created_at"],
        unique=False,
    )
    op.create_table(
        "identity_oauth_account",
        sa.Column("user_id", generics.GUID(), nullable=False),
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("oauth_name", sa.String(length=100), nullable=False),
        sa.Column("access_token", sa.String(length=1024), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.Column("refresh_token", sa.String(length=1024), nullable=True),
        sa.Column("account_id", sa.String(length=320), nullable=False),
        sa.Column("account_email", sa.String(length=320), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"], ondelete="cascade"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_identity_oauth_account_account_id"),
        "identity_oauth_account",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_oauth_account_oauth_name"),
        "identity_oauth_account",
        ["oauth_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("identity_initial_admin_bootstrap")
    op.drop_index(
        op.f("ix_identity_oauth_account_oauth_name"),
        table_name="identity_oauth_account",
    )
    op.drop_index(
        op.f("ix_identity_oauth_account_account_id"),
        table_name="identity_oauth_account",
    )
    op.drop_table("identity_oauth_account")
    op.drop_index(
        op.f("ix_identity_access_token_created_at"),
        table_name="identity_access_token",
    )
    op.drop_table("identity_access_token")
    op.drop_index(op.f("ix_identity_user_email"), table_name="identity_user")
    op.drop_table("identity_user")
