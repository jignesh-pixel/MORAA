"""generation_spend: the shared daily count of paid AI image calls (COST-1).

Idempotent (skipped if the table exists). The downgrade keeps the table (it is a small counter; the app falls
back to its in-process counter when the table is missing).

Revision ID: 0013_generation_spend
Revises: 0012_payment_reconcile
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_generation_spend"
down_revision: Union[str, None] = "0012_payment_reconcile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "generation_spend"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("day", sa.String(10), primary_key=True, comment="UTC date, YYYY-MM-DD"),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("used >= 0", name="ck_generation_spend_used_nonneg"),
    )


def downgrade() -> None:
    """Deliberate no-op: a tiny counter, harmless to keep."""
