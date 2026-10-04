#!/usr/bin/env python3
"""The release step (DEP-5): bring the database to the newest schema, then prove it, BEFORE the new app starts.

Usage (from backend/):   python scripts/release.py [--check-only]

1. Refuses to continue if the code has more than one migration head.
2. Runs ``alembic upgrade head`` (unless --check-only).
3. Checks that the database is at the head and that the money-once protection index exists.

A deploy runs this once, before starting any app process, so the app never boots against an old schema (the app
also refuses to boot in production on a database that is behind). Prints facts only, never the connection string.

Exit code 0 = database is at the head and protected; non-zero = do not start the new version.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check-only", action="store_true", help="only verify; do not run the migrations")
    args = parser.parse_args(argv)

    from app import database
    from app.config import settings

    print(f"environment: {settings.ENVIRONMENT}; database dialect: {database.engine.dialect.name}")
    try:
        status = database.get_schema_status()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: could not read the schema state: {type(e).__name__}: {e}")
        return 1
    print(f"database revision: {status.current or 'none'}; code expects: {status.head}")

    if not status.up_to_date and not args.check_only:
        print("running: alembic upgrade head")
        try:
            database.upgrade_to_head(database.engine)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: the upgrade failed: {type(e).__name__}: {e}")
            print("The database was NOT changed by the failing step (Alembic runs each migration in a transaction on "
                  "PostgreSQL). Fix the cause and run this again; do NOT start the new version.")
            return 1
        status = database.get_schema_status()

    ok = True
    if not status.up_to_date:
        print(f"FAIL: database is at {status.current}, code expects {status.head}")
        ok = False
    else:
        print(f"OK: database is at {status.head}")
    index_present = database.money_once_index_present()
    if index_present is False:
        print(f"FAIL: {database.MONEY_ONCE_INDEX} is missing: payments and refunds are not protected against double processing")
        ok = False
    else:
        print(f"OK: {database.MONEY_ONCE_INDEX} {'present' if index_present else 'check skipped on this database'}")
    print("RESULT:", "ready to start the new version" if ok else "DO NOT START the new version")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
