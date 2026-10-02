"""Indexes for the queries that run inside webhook handling and the background sweeps (DATA-3).

* ``whatsapp_ingestions``: (status, updated_at), (created_at), (external_user_id, status)
* ``audit_logs``: (action, resource_id), (action, created_at)

Idempotent: an index that already exists is skipped. The downgrade keeps them (an index is harmless).

Revision ID: 0014_hot_path_indexes
Revises: 0013_generation_spend
Create Date: 2026-10-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_hot_path_indexes"
down_revision: Union[str, None] = "0013_generation_spend"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = (
    ("ix_whatsapp_ingestions_status_updated", "whatsapp_ingestions", ["status", "updated_at"]),
    ("ix_whatsapp_ingestions_created_at", "whatsapp_ingestions", ["created_at"]),
    ("ix_whatsapp_ingestions_user_status", "whatsapp_ingestions", ["external_user_id", "status"]),
    ("ix_audit_logs_action_resource", "audit_logs", ["action", "resource_id"]),
    ("ix_audit_logs_action_created", "audit_logs", ["action", "created_at"]),
)


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    for name, table, columns in _INDEXES:
        if table not in tables:
            continue
        if name in {ix["name"] for ix in insp.get_indexes(table)}:
            continue
        op.create_index(name, table, columns)


def downgrade() -> None:
    """Deliberate no-op: indexes are harmless to keep."""
