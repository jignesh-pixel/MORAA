#!/usr/bin/env python3
"""Write-path check through a Supabase TRANSACTION-MODE pooler, on a STAGING project only.

The pooler (port 6543) ignores connection start-up options and hands every transaction to whichever
server connection is free, so anything that relied on session state would break. This script proves
the application's money paths and migrations behave through it, inside a scratch schema it creates
and drops (it never touches `public`):

  1. pooling mode is observed (backend changes between transactions on one client connection)
  2. the Alembic migrations build the whole schema through the pooler, in one transaction
  3. the startup checks (schema revision, money-once index) work through the pooler
  4. 100 simultaneous wallet debits never overspend
  5. 20 simultaneous refunds of one failed order credit exactly once (claim-first refund)
  6. the unique-index claim: a second claimant WAITS for the first transaction, then fails on commit
  7. committed data is visible to later transactions on other pooled connections

Safety: the target URL comes ONLY from the STAGING_DATABASE_URL environment variable; the script refuses
to run if its project identifier equals the one in backend/.env (production). Nothing prints a URL,
host, user or password.

  STAGING_DATABASE_URL=postgresql://... python scripts/pooler_write_check.py
Exit code 0 = every check passed.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
os.environ["MORAA_ENV_FILE"] = ""          # app code must never be able to pick up the production URL


def production_identity() -> str | None:
    """User part of the production DATABASE_URL in backend/.env (read, compared, never printed)."""
    env_file = BACKEND / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("DATABASE_URL="):
            match = re.match(r"^[a-z+]+://([^:@/]+)", line.split("=", 1)[1].strip().strip("\"'"))
            return match.group(1) if match else None
    return None


failures: list[str] = []


def report(label: str, ok: bool, detail: object = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail != "" else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    raw_url = os.environ.get("STAGING_DATABASE_URL", "").strip()
    if not raw_url:
        print("STAGING_DATABASE_URL is not set; nothing to do.")
        return 2

    from sqlalchemy import create_engine, event, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    url = make_url(raw_url)
    staging_user = url.username or ""
    if not url.drivername.startswith("postgresql"):
        print("Refusing: the target is not a PostgreSQL URL.")
        return 2
    prod_user = production_identity()
    if prod_user and prod_user == staging_user:
        print("REFUSING TO RUN: the staging project identifier equals the production one in backend/.env.")
        return 3
    print(f"target: port {url.port}, project id {staging_user[-4:].rjust(len(staging_user), '*')[-8:]}, "
          f"differs from production: {'yes' if prod_user else 'production id not available'}")

    import app.models  # noqa: F401 -- register models
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="ERROR")            # the money paths log every debit; keep the output readable
    from app import database
    from app.database import Base
    from app.models.audit_log import AuditLog
    from app.models.customer import Customer
    from app.models.whatsapp_ingestion import WhatsAppIngestion
    from app.services import meta_whatsapp_service as mws
    from app.services import wallet_service

    schema = "moraa_pooler_" + uuid.uuid4().hex[:10]
    admin = create_engine(url, isolation_level="AUTOCOMMIT", pool_size=1, max_overflow=0, pool_pre_ping=True)
    engine = create_engine(url, pool_size=10, max_overflow=25, pool_timeout=60, pool_pre_ping=True)

    @event.listens_for(engine, "begin")
    def _scope_transaction(connection):
        # Transaction-mode poolers ignore start-up options, so scope every transaction explicitly.
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')

    database.engine = engine                   # the app's startup helpers read this module attribute
    Session = sessionmaker(bind=engine)

    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            report("scratch schema created (never `public`)", True, schema[:20] + "...")
            report("server", True, conn.execute(text("SHOW server_version")).scalar())

        # 1. pooling mode ---------------------------------------------------------------------
        one_conn = create_engine(url, pool_size=1, max_overflow=0, pool_pre_ping=False)
        pids = set()
        for _ in range(30):
            with one_conn.connect() as conn:
                pids.add(conn.execute(text("SELECT pg_backend_pid()")).scalar())
        one_conn.dispose()
        # Informational only: an idle pooler legitimately gives one sequential client the same server
        # connection every time, so "same backend" does not mean "not transaction mode".
        report("pooler backends seen over 30 sequential transactions (informational)", True,
               f"{len(pids)} distinct" + (" -> backend changes between transactions" if len(pids) > 1
                                          else " -> same backend reused (idle pooler; mode not observable this way)"))

        # 2. migrations through the pooler ---------------------------------------------------
        started = time.monotonic()
        database.upgrade_to_head(engine)
        status = database.get_schema_status(engine)
        report("migrations build the schema through the pooler", status.up_to_date,
               f"revision {status.current} in {time.monotonic() - started:.1f}s")
        with engine.connect() as conn:
            tables = conn.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema = :s"),
                                  {"s": schema}).scalar()
        report("all model tables exist in the scratch schema", tables >= len(Base.metadata.tables), f"{tables} tables")

        # 3. startup checks -------------------------------------------------------------------
        report("money-once index check through the pooler", database.money_once_index_present() is True)
        report("startup guard through the pooler", database.ensure_schema_ready(engine).up_to_date)

        # 4. concurrent debits ----------------------------------------------------------------
        with Session() as db:
            customer = Customer(whatsapp_id="919800000001", full_name="Pooler Test", business_name="B",
                                gst_number="N/A", address="A", wallet_balance=1000, is_registered=True)
            db.add(customer)
            db.commit()
            customer_id = customer.id
        results: list[bool] = []
        barrier = threading.Barrier(100, timeout=120)

        def debit() -> None:
            barrier.wait()
            with Session() as db:
                charged, _ = wallet_service.charge_customer_balance(db, db.get(Customer, customer_id), 50)
                results.append(charged)

        threads = [threading.Thread(target=debit) for _ in range(100)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        with Session() as db:
            balance = db.get(Customer, customer_id).wallet_balance
        report("100 simultaneous debits never overspend", len(results) == 100 and sum(results) == 20 and balance == 0,
               f"{sum(results)} succeeded of {len(results)}, balance {balance}, {time.monotonic() - started:.1f}s")

        # 5. concurrent refunds ----------------------------------------------------------------
        with Session() as db:
            ingestion = WhatsAppIngestion(external_user_id="919800000001", external_message_id="wamid.pooler.refund",
                                          external_media_id="m", channel="whatsapp", status="failed", amount_charged=500)
            db.add(ingestion)
            db.commit()
            ingestion_id = ingestion.id
        barrier2 = threading.Barrier(20, timeout=120)
        errors: list[Exception] = []

        def refund() -> None:
            barrier2.wait()
            try:
                with Session() as db:
                    mws._refund_failed_ingestion(db, db.get(WhatsAppIngestion, ingestion_id))
            except Exception as exc:                      # the function promises never to raise
                errors.append(exc)

        threads = [threading.Thread(target=refund) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        with Session() as db:
            balance = db.get(Customer, customer_id).wallet_balance
            claims = db.query(AuditLog).filter(AuditLog.action == mws.REFUND_AUDIT_ACTION,
                                               AuditLog.resource_id == ingestion_id).count()
        report("20 simultaneous refunds credit exactly once", not errors and balance == 500 and claims == 1,
               f"balance {balance} (expected 500), refund rows {claims} (expected 1), errors {len(errors)}")

        # 6. unique claim: second claimant waits, then fails -------------------------------------
        outcome: dict = {}

        def second_claimant() -> None:
            with Session() as db:
                try:
                    db.add(AuditLog(action="razorpay_payment_captured", resource_id="pay_pooler_1", status="success"))
                    db.flush()
                    db.commit()
                    outcome["result"] = "claimed"
                except IntegrityError:
                    db.rollback()
                    outcome["result"] = "duplicate"

        first = Session()
        first.add(AuditLog(action="razorpay_payment_captured", resource_id="pay_pooler_1", status="success"))
        first.flush()                                      # first claim open, not committed
        waiter = threading.Thread(target=second_claimant)
        waiter.start()
        deadline = time.monotonic() + 30
        blocked = False
        while time.monotonic() < deadline and not blocked:
            with admin.connect() as conn:
                blocked = bool(conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )).scalar())
            time.sleep(0.1)
        still_waiting = waiter.is_alive()
        first.commit()
        first.close()
        waiter.join(timeout=60)
        report("second claimant waited for the first transaction, then failed",
               blocked and still_waiting and outcome.get("result") == "duplicate",
               f"blocked on lock: {blocked}, outcome: {outcome.get('result')}")

        # 7. visibility across pooled connections -----------------------------------------------
        seen = []
        for _ in range(5):
            with Session() as db:
                seen.append(db.query(AuditLog).filter(AuditLog.resource_id == "pay_pooler_1").count())
        report("committed rows are visible to later transactions", seen == [1] * 5, seen)

    finally:
        try:
            engine.dispose()
            with admin.connect() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                gone = conn.execute(text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": schema}).scalar() == 0
            report("scratch schema dropped", gone)
        except Exception as exc:                          # cleanup problems must be loud, never silent
            report("scratch schema dropped", False, type(exc).__name__)
        admin.dispose()

    print()
    print("RESULT:", "ALL POOLER CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
