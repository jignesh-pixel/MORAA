"""Auth hardening (SEC-4): admin flag, token invalidation time, single-use refresh tokens.

* ``users.is_admin`` (default false) and ``users.tokens_valid_after`` (null = no cut-off)
* ``revoked_tokens``: refresh tokens that were used or revoked

Idempotent. The downgrade keeps everything (columns with defaults are harmless on an older schema).

Revision ID: 0015_auth_hardening
Revises: 0014_hot_path_indexes
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_auth_hardening"
down_revision: Union[str, None] = "0014_hot_path_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "users" in tables:
        cols = {c["name"] for c in insp.get_columns("users")}
        if "is_admin" not in cols:
            op.add_column("users", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "tokens_valid_after" not in cols:
            op.add_column("users", sa.Column("tokens_valid_after", sa.DateTime(timezone=True), nullable=True))

    if "revoked_tokens" not in tables:
        op.create_table(
            "revoked_tokens",
            sa.Column("jti", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_revoked_tokens_user_id", "revoked_tokens", ["user_id"])
        op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"])


def downgrade() -> None:
    """Deliberate no-op: the columns have safe defaults and the table is small bookkeeping."""
