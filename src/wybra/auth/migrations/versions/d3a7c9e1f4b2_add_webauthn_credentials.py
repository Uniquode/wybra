"""add webauthn credentials

Revision ID: d3a7c9e1f4b2
Revises: c1e9a7d6b5c4
Create Date: 2026-07-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from wybra.db import types as generics

revision: str = "d3a7c9e1f4b2"
down_revision: str | Sequence[str] | None = "c1e9a7d6b5c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_webauthn_credential",
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("user_id", generics.GUID(), nullable=False),
        sa.Column("credential_id", sa.String(length=1024), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_used_at", sa.Float(), nullable=True),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.Column("user_verified", sa.Boolean(), nullable=False),
        sa.Column("credential_device_type", sa.String(length=32), nullable=True),
        sa.Column("credential_backed_up", sa.Boolean(), nullable=False),
        sa.Column("transports", sa.JSON(), nullable=True),
        sa.Column("aaguid", sa.String(length=64), nullable=True),
        sa.Column("attestation_format", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "credential_id",
            name="uq_identity_webauthn_credential_credential_id",
        ),
    )
    op.create_index(
        op.f("ix_identity_webauthn_credential_user_id"),
        "identity_webauthn_credential",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_webauthn_credential_status"),
        "identity_webauthn_credential",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_webauthn_credential_user_status"),
        "identity_webauthn_credential",
        ["user_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_identity_webauthn_credential_created_at"),
        "identity_webauthn_credential",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_identity_webauthn_credential_created_at"),
        table_name="identity_webauthn_credential",
    )
    op.drop_index(
        op.f("ix_identity_webauthn_credential_user_status"),
        table_name="identity_webauthn_credential",
    )
    op.drop_index(
        op.f("ix_identity_webauthn_credential_status"),
        table_name="identity_webauthn_credential",
    )
    op.drop_index(
        op.f("ix_identity_webauthn_credential_user_id"),
        table_name="identity_webauthn_credential",
    )
    op.drop_table("identity_webauthn_credential")
