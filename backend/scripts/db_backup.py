#!/usr/bin/env python3
"""Back up the PostgreSQL database with pg_dump (DEP-4).

Usage (from backend/):   python scripts/db_backup.py [--out-dir backups] [--keep 14]

Writes ``moraa-YYYYmmdd-HHMMSS.dump`` (pg_dump custom format, compressed) into the output folder, then deletes the
oldest dumps beyond ``--keep``. The database password is passed to pg_dump through the PGPASSWORD environment
variable, never on the command line, and nothing about the connection (host, user, password) is printed.

Needs the ``pg_dump`` program (PostgreSQL client tools, version 15 or newer for Supabase). The wallet balances
live only in this database, so a backup that has never been restored is not yet a backup: run
``scripts/db_restore_check.py`` on a recent dump at least once a quarter.

Exit code 0 = a dump was written and is readable; anything else = no usable backup was made.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.engine import make_url  # noqa: E402

PREFIX = "moraa-"
SUFFIX = ".dump"


def build_dump_command(database_url: str, out_file: Path) -> Tuple[List[str], Dict[str, str]]:
    """The pg_dump argument list and the extra environment (holding the password). Pure: runs nothing."""
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("db_backup.py only backs up PostgreSQL databases")
    argv = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", f"--file={out_file}",
            f"--host={url.host or 'localhost'}", f"--port={url.port or 5432}",
            f"--username={url.username or 'postgres'}", url.database or "postgres"]
    env = {"PGPASSWORD": url.password} if url.password else {}      # never override a ~/.pgpass with an empty one
    if url.query.get("sslmode"):
        env["PGSSLMODE"] = str(url.query["sslmode"])
    return argv, env


def backup_name(now: datetime | None = None) -> str:
    return f"{PREFIX}{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}{SUFFIX}"


def prune_old_backups(folder: Path, keep: int) -> List[Path]:
    """Delete the oldest ``moraa-*.dump`` files beyond ``keep``. Returns what was deleted."""
    dumps = sorted(folder.glob(f"{PREFIX}*{SUFFIX}"))      # the timestamp in the name sorts oldest first
    doomed = dumps[: max(len(dumps) - max(keep, 1), 0)]
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", default="backups")
    parser.add_argument("--keep", type=int, default=14, help="how many dumps to keep (default 14)")
    args = parser.parse_args(argv)

    if shutil.which("pg_dump") is None:
        print("FAIL: pg_dump was not found. Install the PostgreSQL client tools (version 15 or newer) and retry.")
        return 2

    from app.config import settings

    out_dir = Path(args.out_dir)
    out_dir = out_dir if out_dir.is_absolute() else BACKEND / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / backup_name()
    try:
        cmd, extra_env = build_dump_command(settings.DATABASE_URL, out_file)
    except ValueError as e:
        print(f"FAIL: {e}")
        return 2

    result = subprocess.run(cmd, env={**os.environ, **extra_env}, capture_output=True, text=True)
    if result.returncode != 0:
        out_file.unlink(missing_ok=True)
        # pg_dump's own message names the server and user, so only the exit code is shown.
        print(f"FAIL: pg_dump exited with code {result.returncode} (run it by hand to see why)")
        return 1
    if not out_file.exists() or out_file.stat().st_size < 1024:
        print("FAIL: the dump file is missing or suspiciously small")
        return 1
    listing = subprocess.run(["pg_restore", "--list", str(out_file)], capture_output=True, text=True) \
        if shutil.which("pg_restore") else None
    if listing is not None and listing.returncode != 0:
        print("FAIL: the dump was written but pg_restore cannot read it")
        return 1
    removed = prune_old_backups(out_dir, args.keep)
    print(f"OK: wrote {out_file.name} ({out_file.stat().st_size / 1048576:.1f} MB); removed {len(removed)} old dump(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
