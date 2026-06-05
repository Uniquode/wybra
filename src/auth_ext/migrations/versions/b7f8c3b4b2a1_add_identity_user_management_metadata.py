"""add identity user management metadata

Revision ID: b7f8c3b4b2a1
Revises: 34736375e2f8
Create Date: 2026-05-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f8c3b4b2a1"
down_revision: str | Sequence[str] | None = "34736375e2f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
LEGACY_USER_TIMESTAMP = 1779654464.0  # 2026-05-24T20:27:44Z


def upgrade() -> None:
    op.add_column(
        "identity_user",
        sa.Column("is_admin", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "identity_user",
        sa.Column("created_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "identity_user",
        sa.Column("modified_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "identity_user",
        sa.Column("last_login_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "identity_user",
        sa.Column("expires_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "identity_user",
        sa.Column("email_verification_sent_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "identity_user",
        sa.Column("display_name", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "identity_user",
        sa.Column("preferred_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "identity_user",
        sa.Column("preferred_timezone", sa.String(length=64), nullable=True),
    )

    # Existing identity rows have no historical audit source. Use the identity
    # table migration timestamp as one deterministic legacy cutoff, so new
    # non-null timestamp columns are valid without pretending to recover
    # per-user chronology or making repeated migration runs produce different
    # seed values.
    op.execute(
        sa.text(
            "UPDATE identity_user "
            "SET is_admin = COALESCE(is_admin, :is_admin), "
            "created_at = COALESCE(created_at, :created_at), "
            "modified_at = COALESCE(modified_at, :modified_at) "
            "WHERE is_admin IS NULL "
            "OR created_at IS NULL "
            "OR modified_at IS NULL"
        ).bindparams(
            is_admin=False,
            created_at=LEGACY_USER_TIMESTAMP,
            modified_at=LEGACY_USER_TIMESTAMP,
        )
    )

    with op.batch_alter_table("identity_user") as batch_op:
        batch_op.alter_column("is_admin", existing_type=sa.Boolean(), nullable=False)
        batch_op.alter_column("created_at", existing_type=sa.Float(), nullable=False)
        batch_op.alter_column("modified_at", existing_type=sa.Float(), nullable=False)

    op.create_index(
        "ix_identity_user_is_active_expires_at",
        "identity_user",
        ["is_active", "expires_at"],
    )
    op.create_index(
        "ix_identity_user_last_login_at",
        "identity_user",
        ["last_login_at"],
    )
    op.create_index("ix_identity_user_created_at", "identity_user", ["created_at"])
    op.create_index("ix_identity_user_modified_at", "identity_user", ["modified_at"])
    op.create_index("ix_identity_user_is_admin", "identity_user", ["is_admin"])
    op.create_index(
        "ix_identity_user_is_superuser",
        "identity_user",
        ["is_superuser"],
    )


def downgrade() -> None:
    op.drop_index("ix_identity_user_is_superuser", table_name="identity_user")
    op.drop_index("ix_identity_user_is_admin", table_name="identity_user")
    op.drop_index("ix_identity_user_modified_at", table_name="identity_user")
    op.drop_index("ix_identity_user_created_at", table_name="identity_user")
    op.drop_index("ix_identity_user_last_login_at", table_name="identity_user")
    op.drop_index("ix_identity_user_is_active_expires_at", table_name="identity_user")
    op.drop_column("identity_user", "preferred_timezone")
    op.drop_column("identity_user", "preferred_name")
    op.drop_column("identity_user", "display_name")
    op.drop_column("identity_user", "email_verification_sent_at")
    op.drop_column("identity_user", "expires_at")
    op.drop_column("identity_user", "last_login_at")
    op.drop_column("identity_user", "modified_at")
    op.drop_column("identity_user", "created_at")
    op.drop_column("identity_user", "is_admin")
