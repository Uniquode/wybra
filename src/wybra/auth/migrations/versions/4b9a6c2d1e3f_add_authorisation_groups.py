"""add authorisation groups

Revision ID: 4b9a6c2d1e3f
Revises: b7f8c3b4b2a1
Create Date: 2026-06-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from wybra.db import types as generics

revision: str = "4b9a6c2d1e3f"
down_revision: str | Sequence[str] | None = "b7f8c3b4b2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_group",
        sa.Column("id", generics.GUID(), nullable=False),
        sa.Column("abbrev", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_identity_group_abbrev",
        "identity_group",
        ["abbrev"],
        unique=True,
    )

    op.create_table(
        "identity_scope",
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("scope"),
    )

    op.create_table(
        "identity_group_scope",
        sa.Column("group_id", generics.GUID(), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"], ["identity_group.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["scope"], ["identity_scope.scope"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("group_id", "scope"),
        sa.UniqueConstraint("group_id", "scope", name="uq_identity_group_scope_pair"),
    )
    op.create_index(
        "ix_identity_group_scope_group_id",
        "identity_group_scope",
        ["group_id"],
    )
    op.create_index(
        "ix_identity_group_scope_scope",
        "identity_group_scope",
        ["scope"],
    )

    op.create_table(
        "identity_group_user",
        sa.Column("group_id", generics.GUID(), nullable=False),
        sa.Column("user_id", generics.GUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"], ["identity_group.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_identity_group_user_pair"),
    )
    op.create_index(
        "ix_identity_group_user_group_id",
        "identity_group_user",
        ["group_id"],
    )
    op.create_index(
        "ix_identity_group_user_user_id",
        "identity_group_user",
        ["user_id"],
    )

    op.create_table(
        "identity_group_group",
        sa.Column("parent_group_id", generics.GUID(), nullable=False),
        sa.Column("child_group_id", generics.GUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_group_id"], ["identity_group.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["child_group_id"], ["identity_group.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("parent_group_id", "child_group_id"),
        sa.CheckConstraint(
            "parent_group_id <> child_group_id",
            name="ck_identity_group_group_no_self_membership",
        ),
        sa.UniqueConstraint(
            "parent_group_id",
            "child_group_id",
            name="uq_identity_group_group_pair",
        ),
    )
    op.create_index(
        "ix_identity_group_group_parent_group_id",
        "identity_group_group",
        ["parent_group_id"],
    )
    op.create_index(
        "ix_identity_group_group_child_group_id",
        "identity_group_group",
        ["child_group_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_identity_group_group_child_group_id",
        table_name="identity_group_group",
    )
    op.drop_index(
        "ix_identity_group_group_parent_group_id",
        table_name="identity_group_group",
    )
    op.drop_table("identity_group_group")
    op.drop_index("ix_identity_group_user_user_id", table_name="identity_group_user")
    op.drop_index("ix_identity_group_user_group_id", table_name="identity_group_user")
    op.drop_table("identity_group_user")
    op.drop_index("ix_identity_group_scope_scope", table_name="identity_group_scope")
    op.drop_index(
        "ix_identity_group_scope_group_id",
        table_name="identity_group_scope",
    )
    op.drop_table("identity_group_scope")
    op.drop_table("identity_scope")
    op.drop_index("ix_identity_group_abbrev", table_name="identity_group")
    op.drop_table("identity_group")
