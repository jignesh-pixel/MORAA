#!/usr/bin/env python3
"""Read-only production startup check against the database configured in backend/.env.

Runs the same code the server runs at startup (config validation, schema-revision check,
money-once index check) through the real connection path -- including a Supabase pooler --
but every transaction is opened READ ONLY, so PostgreSQL itself rejects any write.

Prints facts only: never a connection string, host, user or password.

Usage (from backend/):  python scripts/supabase_readonly_check.py
Exit code 0 = every check passed.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402

from app import database  # noqa: E402
from app.config import ENV_FILE, settings  # noqa: E402

failures: list[str] = []


def report(label: str, value: object, ok: bool = True) -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {label}: {value}")
    if not ok:
        failures.append(label)


def main() -> int:
    report("env file loaded", "yes" if ENV_FILE else "no", bool(ENV_FILE))
    report("ENVIRONMENT", settings.ENVIRONMENT)
    report("database dialect", database.engine.dialect.name, database.engine.dialect.name == "postgresql")
    url = database.engine.url
    report("database port", url.port)

    # Every transaction on this engine is READ ONLY: the database refuses writes.
    read_only_engine = database.engine.execution_options(postgresql_readonly=True)
    database.engine = read_only_engine          # the app's own helpers use this module attribute

    with read_only_engine.connect() as conn:
        report("transaction_read_only", conn.execute(text("SHOW transaction_read_only")).scalar(),
               conn.execute(text("SHOW transaction_read_only")).scalar() == "on")
        report("server_version", conn.execute(text("SHOW server_version")).scalar())
        report("current_schema", conn.execute(text("SELECT current_schema()")).scalar())
        report("search_path", conn.execute(text("SHOW search_path")).scalar())

    # Our own SQLAlchemy pool reuses one client connection, so this cannot reveal the pooler's mode;
    # it only proves that separate read-only transactions work through whatever sits in front.
    for _ in range(6):
        with read_only_engine.connect() as conn:
            conn.execute(text("SELECT 1")).scalar()
    report("6 separate read-only transactions", "ok")
    report("connection path", "Supabase transaction-mode pooler port (6543)" if url.port == 6543
           else "direct/session connection port")

    status = database.get_schema_status(read_only_engine)
    report("schema revision in database", status.current, status.current is not None)
    report("schema head expected by code", status.head)
    report("revision known to this build", status.current_known, status.current_known)
    report("database up to date", status.up_to_date, status.up_to_date)

    index_present = database.money_once_index_present()
    report(f"{database.MONEY_ONCE_INDEX} present in current schema", index_present, index_present is True)

    try:
        ready = database.ensure_schema_ready(read_only_engine)
        report("ensure_schema_ready() (the startup guard)", f"passed at {ready.current}", ready.up_to_date)
    except Exception as exc:  # production guard raising is a legitimate, reportable outcome
        report("ensure_schema_ready() (the startup guard)", f"{type(exc).__name__}: {exc}", False)

    print()
    print("RESULT:", "ALL CHECKS PASSED (read-only)" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
