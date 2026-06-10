"""add totp and recovery tables

Revision ID: 0d1f9f4a8b2c
Revises: f4d2b8a1e9c3
Create Date: 2026-06-10 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from fastapi_users_db_sqlalchemy import generics

revision: str = "0d1f9f4a8b2c"
down_revision: str | Sequence[str] | None = "f4d2b8a1e9c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_totp_credential",
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("user_id", generics.GUID(), nullable=False),
        sa.Column("crypt_secret", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("activated_at", sa.Float(), nullable=True),
        sa.Column("disabled_at", sa.Float(), nullable=True),
        sa.Column("last_used_counter", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_identity_totp_credential_user_id"),
        "identity_totp_credential",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_totp_credential_status"),
        "identity_totp_credential",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_totp_credential_created_at"),
        "identity_totp_credential",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "identity_authentication_challenge",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", generics.GUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_identity_authentication_challenge_user_id"),
        "identity_authentication_challenge",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_authentication_challenge_expires_at"),
        "identity_authentication_challenge",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "identity_totp_recovery_code",
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("credential_id", generics.GUID(), nullable=False),
        sa.Column("code_verifier", sa.String(length=256), nullable=False),
        sa.Column("consumed_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["identity_totp_credential.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "credential_id",
            "code_verifier",
            name="uq_identity_totp_recovery_code_verifier",
        ),
    )
    op.create_index(
        op.f("ix_identity_totp_recovery_code_credential_id"),
        "identity_totp_recovery_code",
        ["credential_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_totp_recovery_code_consumed_at"),
        "identity_totp_recovery_code",
        ["consumed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_identity_totp_recovery_code_consumed_at"),
        table_name="identity_totp_recovery_code",
    )
    op.drop_index(
        op.f("ix_identity_totp_recovery_code_credential_id"),
        table_name="identity_totp_recovery_code",
    )
    op.drop_table("identity_totp_recovery_code")

    op.drop_index(
        op.f("ix_identity_authentication_challenge_expires_at"),
        table_name="identity_authentication_challenge",
    )
    op.drop_index(
        op.f("ix_identity_authentication_challenge_user_id"),
        table_name="identity_authentication_challenge",
    )
    op.drop_table("identity_authentication_challenge")

    op.drop_index(
        op.f("ix_identity_totp_credential_created_at"),
        table_name="identity_totp_credential",
    )
    op.drop_index(
        op.f("ix_identity_totp_credential_status"),
        table_name="identity_totp_credential",
    )
    op.drop_index(
        op.f("ix_identity_totp_credential_user_id"),
        table_name="identity_totp_credential",
    )
    op.drop_table("identity_totp_credential")
