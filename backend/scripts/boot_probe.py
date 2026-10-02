#!/usr/bin/env python3
"""Boot the REAL application (FastAPI lifespan) against throw-away databases and check the startup rules.

  * development, fresh SQLite  -> the schema migrates itself to head; a second start is clean
  * production, EMPTY PostgreSQL -> refuses to start with the 'alembic upgrade head' message
  * production, migrated PostgreSQL -> starts

The PostgreSQL cases use a throw-away local server (pgserver dev dependency) and are SKIPPED, loudly,
if it is not installed. No live secrets, no provider keys, no network.

  python scripts/boot_probe.py
Exit code 0 = every startup rule held.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

CHILD = r'''
import json, sys
sys.path.insert(0, r"%s")
from fastapi.testclient import TestClient
from app.main import app
from app.database import get_schema_status
try:
    with TestClient(app):
        st = get_schema_status()
        print(json.dumps({"started": True, "revision": st.current, "head": st.head}))
except Exception as exc:
    print(json.dumps({"started": False, "error": type(exc).__name__ + ": " + str(exc)[:300]}))
''' % BACKEND

BASE_ENV = dict(
    os.environ, MORAA_ENV_FILE="", GEMINI_API_KEY="", OPENAI_API_KEY="", META_WHATSAPP_TOKEN="",
    RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="", IMAGE_PROVIDER_STARTUP_DIAGNOSTICS="false",
    WHATSAPP_PAY_ENABLED="false", ERPNEXT_INVOICE_ENABLED="false", GST_VERIFICATION_ENABLED="false",
)

results: list[bool] = []


def boot(env: dict) -> dict:
    out = subprocess.run([sys.executable, "-c", CHILD], env=env, capture_output=True, text=True, timeout=300, cwd=BACKEND)
    lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else {"started": None, "error": "no output: " + out.stderr[-300:]}


def check(label: str, ok: bool, detail: object) -> None:
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + f"{label:55} -> {detail}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bootprobe_")
    try:
        env = dict(BASE_ENV, DATABASE_URL=f"sqlite:///{tmp}/dev.db", UPLOAD_DIR=f"{tmp}/up", REPORT_DIR=f"{tmp}/rep",
                   LOG_DIR=f"{tmp}/logs", ENVIRONMENT="development")
        first, second = boot(env), boot(env)
        check("development + fresh SQLite migrates itself", first.get("started") is True and first.get("revision") == first.get("head"), first)
        check("development + same SQLite, second start", second.get("started") is True and second.get("revision") == second.get("head"), second)

        try:
            import pgserver
            import psycopg2
        except ImportError:
            print("SKIP production + PostgreSQL cases (pgserver is not installed)")
        else:
            server = pgserver.get_server(os.path.join(tmp, "pg"), cleanup_mode="stop")
            try:
                base = server.get_uri()
                conn = psycopg2.connect(base)
                conn.autocommit = True
                conn.cursor().execute("CREATE DATABASE bootprobe")
                conn.close()
                url = base.rsplit("/", 1)[0] + "/bootprobe"
                prod = dict(
                    BASE_ENV, DATABASE_URL=url, UPLOAD_DIR=f"{tmp}/up2", REPORT_DIR=f"{tmp}/rep2", LOG_DIR=f"{tmp}/logs2",
                    ENVIRONMENT="production", SECRET_KEY="aB3dE5fG7hJ9kL1mN2pQ4rS6tU8vW0xY2zA4bC6d",
                    META_APP_SECRET="meta-secret-x", RAZORPAY_WEBHOOK_SECRET="rzp-secret-x",
                )
                empty = boot(prod)
                check("production + EMPTY PostgreSQL is refused",
                      empty.get("started") is False and "alembic upgrade head" in empty.get("error", ""), empty)
                upgrade = subprocess.run(
                    [sys.executable, "-c", "from alembic.config import main; main(argv=['upgrade','head'])"],
                    env=prod, capture_output=True, text=True, cwd=BACKEND, timeout=300)
                check("alembic upgrade head (command line) succeeds", upgrade.returncode == 0, f"rc={upgrade.returncode}")
                migrated = boot(prod)
                check("production + migrated PostgreSQL starts",
                      migrated.get("started") is True and migrated.get("revision") == migrated.get("head"), migrated)
            finally:
                server.cleanup()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{sum(results)}/{len(results)} startup rules held")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
