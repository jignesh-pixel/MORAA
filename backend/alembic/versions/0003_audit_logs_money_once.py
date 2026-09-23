"""Unique guard so a payment credit / generation refund happens at most once.

Revision ID: 0003_audit_money_once
Revises: ebdee18538d1

Adds a partial unique index on audit_logs(action, resource_id) for the two
money-moving actions. Concurrent duplicate Razorpay webhooks (or refunds)
then fail at the database instead of crediting twice.

If this upgrade fails with a unique violation, duplicate rows already exist
for one of these actions -- review them manually; this migration never
deletes data.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_audit_money_once"
down_revision: Union[str, None] = "ebdee18538d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WHERE = "action IN ('razorpay_payment_captured', 'whatsapp_generation_refund')"


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_logs_money_once "
        f"ON audit_logs (action, resource_id) WHERE {_WHERE}"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_audit_logs_money_once")
