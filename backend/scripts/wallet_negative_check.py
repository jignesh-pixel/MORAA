#!/usr/bin/env python3
"""Read-only pre-migration check: how many customers have a negative wallet balance?

Migration 0009 adds CHECK (wallet_balance >= 0) and refuses to run if any negative
balance exists. Run this against production BEFORE ever applying 0009 there.

Every transaction is opened READ ONLY, so PostgreSQL itself rejects any write.
Prints counts only: never a connection string, host, user, phone number or name.

Usage (from backend/):  python scripts/wallet_negative_check.py
Exit code 0 = no negative balance; 1 = at least one negative balance (or a failure).
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402

from app import database  # noqa: E402


def main() -> int:
    engine = database.engine.execution_options(postgresql_readonly=True)
    with engine.connect() as conn:
        read_only = conn.execute(text("SHOW transaction_read_only")).scalar()
        print(f"transaction_read_only: {read_only}")
        if read_only != "on":
            print("RESULT: FAIL - connection is not read only, refusing to continue")
            return 1
        total, negative, zero, positive, worst = conn.execute(text(
            "SELECT count(*),"
            " count(*) FILTER (WHERE wallet_balance < 0),"
            " count(*) FILTER (WHERE wallet_balance = 0),"
            " count(*) FILTER (WHERE wallet_balance > 0),"
            " min(wallet_balance) FROM customers"
        )).one()
    print(f"customers total: {total}")
    print(f"  negative balance: {negative}")
    print(f"  zero balance: {zero}")
    print(f"  positive balance: {positive}")
    print(f"  lowest balance: {worst}")
    if negative:
        print("RESULT: FAIL - migration 0009 would refuse to run; fix these balances first")
        return 1
    print("RESULT: OK - no negative balances, migration 0009 can pass its own safety check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
