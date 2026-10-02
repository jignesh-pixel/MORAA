#!/usr/bin/env python3
"""Restore a backup into a THROW-AWAY database and prove the money is intact (DEP-4 restore drill).

Usage (from backend/):
    set RESTORE_TEST_DATABASE_URL=postgresql://user:password@localhost:5432/restore_scratch      (never production)
    python scripts/db_restore_check.py backups/moraa-20261002-020000.dump

Refuses to run against a Supabase host, against the application's own database, or against a database whose name does
not contain "scratch", "test" or "restore" (pg_restore --clean drops tables). Restores, then checks:
  * the schema revision recorded in the dump is printed (compare it with the code's head by eye)
  * every customer's wallet balance equals the sum of their ledger rows (SUM(wallet_transactions.amount))
  * no wallet balance is negative
  * the number of customers and ledger rows (printed, so a restore that came back empty is obvious)

Prints counts only: never a connection string, name, phone number or amount per customer.
Exit code 0 = restored and every check passed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

FORBIDDEN_HOST_PARTS = ("supabase.co", "supabase.com", "pooler.supabase")


SCRATCH_NAME_PARTS = ("scratch", "test", "restore")


def check_target(url_text: str, app_database_url: str = "") -> None:
    """Refuse anything that could be a real database. ``pg_restore --clean`` DROPS tables, so three guards:

    1. not a Supabase host;
    2. not the same host + database as the application's own DATABASE_URL, whatever it is hosted on;
    3. the database NAME must say it is a scratch copy (contains "scratch", "test" or "restore").
    """
    url = make_url(url_text)
    host = (url.host or "").lower()
    if any(part in host for part in FORBIDDEN_HOST_PARTS):
        raise ValueError("RESTORE_TEST_DATABASE_URL points at Supabase; use a scratch database, never production")
    if not url.drivername.startswith("postgresql"):
        raise ValueError("RESTORE_TEST_DATABASE_URL must be a PostgreSQL URL")
    if app_database_url:
        try:
            live = make_url(app_database_url)
        except Exception:  # noqa: BLE001
            live = None
        if live is not None and (live.host or "").lower() == host and (live.database or "") == (url.database or ""):
            raise ValueError("RESTORE_TEST_DATABASE_URL is the application's own database; refusing to overwrite it")
    if not any(part in (url.database or "").lower() for part in SCRATCH_NAME_PARTS):
        raise ValueError(
            "the restore database name must contain 'scratch', 'test' or 'restore' so a real database can never be "
            "overwritten by mistake"
        )


def build_restore_command(url_text: str, dump: Path) -> Tuple[List[str], Dict[str, str]]:
    url = make_url(url_text)
    argv = ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
            f"--host={url.host or 'localhost'}", f"--port={url.port or 5432}",
            f"--username={url.username or 'postgres'}", f"--dbname={url.database or 'postgres'}", str(dump)]
    return argv, {"PGPASSWORD": url.password or ""}


def verify_restored_money(engine) -> List[str]:
    """Return a list of problems (empty = all good) and print the counts."""
    problems: List[str] = []
    with engine.connect() as conn:
        customers = conn.execute(text("SELECT count(*) FROM customers")).scalar()
        ledger_rows = conn.execute(text("SELECT count(*) FROM wallet_transactions")).scalar()
        negative = conn.execute(text("SELECT count(*) FROM customers WHERE wallet_balance < 0")).scalar()
        drift = conn.execute(text(
            "SELECT count(*) FROM customers c WHERE c.wallet_balance <> "
            "COALESCE((SELECT SUM(w.amount) FROM wallet_transactions w WHERE w.customer_id = c.id), 0)"
        )).scalar()
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    print(f"restored: {customers} customers, {ledger_rows} ledger rows, schema revision {revision}")
    if negative:
        problems.append(f"{negative} customer(s) have a negative balance")
    if drift:
        problems.append(f"{drift} customer(s) whose balance differs from the sum of their ledger")
    if not customers:
        problems.append("the restored database has no customers (an empty restore?)")
    return problems


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dump", type=Path)
    args = parser.parse_args(argv)

    target = os.environ.get("RESTORE_TEST_DATABASE_URL", "")
    if not target:
        print("FAIL: set RESTORE_TEST_DATABASE_URL to a scratch PostgreSQL database (never production)")
        return 2
    try:
        from app.config import settings

        check_target(target, settings.DATABASE_URL)
    except ValueError as e:
        print(f"FAIL: {e}")
        return 2
    if not args.dump.exists():
        print("FAIL: the dump file does not exist")
        return 2
    if shutil.which("pg_restore") is None:
        print("FAIL: pg_restore was not found. Install the PostgreSQL client tools and retry.")
        return 2

    cmd, extra_env = build_restore_command(target, args.dump)
    result = subprocess.run(cmd, env={**os.environ, **extra_env}, capture_output=True, text=True)
    if result.returncode not in (0, 1):          # pg_restore returns 1 for warnings (e.g. roles that do not exist here)
        print(f"FAIL: pg_restore exited with code {result.returncode}")
        return 1
    if result.returncode == 1:
        print("note: pg_restore reported warnings (usually missing roles); the checks below decide")
    engine = create_engine(target)
    try:
        problems = verify_restored_money(engine)
    except Exception as e:  # noqa: BLE001 -- an SQL error text can name the host: show the kind only
        print(f"FAIL: the restored database could not be checked ({type(e).__name__})")
        return 1
    finally:
        engine.dispose()
    for problem in problems:
        print(f"FAIL: {problem}")
    print("RESULT:", "restore verified" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
