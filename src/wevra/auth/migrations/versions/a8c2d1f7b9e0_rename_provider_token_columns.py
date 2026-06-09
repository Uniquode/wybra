"""Rename identity provider token columns"""

from collections.abc import Sequence

from alembic import op

revision: str = "a8c2d1f7b9e0"
down_revision: str | Sequence[str] | None = "f4d2b8a1e9c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("identity_provider") as batch_op:
        batch_op.alter_column("access_token", new_column_name="crypt_access_token")
        batch_op.alter_column("refresh_token", new_column_name="crypt_refresh_token")


def downgrade() -> None:
    with op.batch_alter_table("identity_provider") as batch_op:
        batch_op.alter_column("crypt_access_token", new_column_name="access_token")
        batch_op.alter_column("crypt_refresh_token", new_column_name="refresh_token")
