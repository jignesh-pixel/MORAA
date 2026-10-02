"""Durable background jobs (Q-5). See ``app/models/outbox_job.py``.

``enqueue`` records the work in the database first; ``drain_once`` / ``run_outbox_forever`` run it. Delivery is
at-least-once: a job that ran but could not be marked done (the process died in between) runs again, so handlers
must be safe to repeat (forwarding a message to the ops team twice, or re-sending an invoice, is harmless).

Jobs are claimed with one guarded UPDATE (``pending`` -> ``running``) so two processes never run the same job at the
same time. A ``running`` job nobody finished for ``STALE_RUNNING_SECONDS`` (its process died) is taken over.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from app.models.outbox_job import DEAD, DONE, PENDING, RUNNING, OutboxJob
from app.utils.executors import run_io
from app.utils.logger import logger

MAX_ATTEMPTS = 6
BACKOFF_SECONDS = (30, 120, 600, 1800, 3600)          # wait before attempt 2, 3, ... (the last repeats)
STALE_RUNNING_SECONDS = 600
SWEEP_INTERVAL_SECONDS = 20
DONE_KEEP_DAYS = 14

Handler = Callable[[Dict[str, Any]], Awaitable[Any]]
_handlers: Dict[str, Handler] = {}


def register_handler(kind: str, handler: Handler) -> None:
    _handlers[kind] = handler


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(db, kind: str, payload: Dict[str, Any], dedupe_key: str) -> bool:
    """Record a job. True if recorded, False if the same ``dedupe_key`` already exists. Commits."""
    try:
        db.add(OutboxJob(kind=kind, payload=payload, dedupe_key=dedupe_key[:120], status=PENDING,
                         attempts=0, next_attempt_at=_now()))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def enqueue_detached(kind: str, payload: Dict[str, Any], dedupe_key: str) -> bool:
    """``enqueue`` in its own short session. Returns False (and logs) if the database could not record it."""
    try:
        from app.database import SessionLocal

        with SessionLocal() as db:
            return enqueue(db, kind, payload, dedupe_key)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Outbox enqueue failed for {kind}: {type(e).__name__}: {e}")
        return False


def _claim_next() -> Optional[Dict[str, Any]]:
    """Claim one due job (sync). Returns {id, kind, payload, attempts} or None."""
    from app.database import SessionLocal

    now = _now()
    stale = now - timedelta(seconds=STALE_RUNNING_SECONDS)
    with SessionLocal() as db:
        for _ in range(5):                               # a few tries if another process wins the race
            row = (
                db.query(OutboxJob.id)
                .filter(or_(
                    (OutboxJob.status == PENDING) & (OutboxJob.next_attempt_at <= now),
                    (OutboxJob.status == RUNNING) & (OutboxJob.updated_at <= stale),
                ))
                .order_by(OutboxJob.next_attempt_at, OutboxJob.id)
                .first()
            )
            if row is None:
                return None
            claimed = db.execute(
                update(OutboxJob)
                .where(OutboxJob.id == row[0],
                       or_(OutboxJob.status == PENDING, (OutboxJob.status == RUNNING) & (OutboxJob.updated_at <= stale)))
                .values(status=RUNNING, attempts=OutboxJob.attempts + 1, updated_at=now)
            ).rowcount
            db.commit()
            if claimed == 1:
                job = db.get(OutboxJob, row[0])
                return {"id": job.id, "kind": job.kind, "payload": dict(job.payload or {}), "attempts": job.attempts}
    return None


def _finish(job_id: int, error: Optional[str], attempts: int) -> None:
    from app.database import SessionLocal

    with SessionLocal() as db:
        job = db.get(OutboxJob, job_id)
        if job is None:
            return
        if error is None:
            job.status, job.last_error = DONE, None
        elif attempts >= MAX_ATTEMPTS:
            job.status, job.last_error = DEAD, error[:500]
            logger.bind(category="system").error(f"OUTBOX job {job_id} ({job.kind}) gave up after {attempts} attempts")
        else:
            wait = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
            job.status, job.last_error = PENDING, error[:500]
            job.next_attempt_at = _now() + timedelta(seconds=wait)
        db.commit()


async def drain_once(limit: int = 20) -> int:
    """Run up to ``limit`` due jobs. Returns how many were run. Never raises (except on cancellation)."""
    ran = 0
    for _ in range(limit):
        try:
            job = await run_io(_claim_next)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 -- table missing / database down: try again next sweep
            logger.warning(f"Outbox claim failed: {type(e).__name__}: {e}")
            return ran
        if job is None:
            return ran
        handler = _handlers.get(job["kind"])
        error: Optional[str] = None
        if handler is None:
            error = f"no handler registered for kind {job['kind']!r}"
        else:
            try:
                result = await handler(job["payload"])
                if result is False or result == "failed":
                    error = "handler reported failure"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"
        try:
            await run_io(_finish, job["id"], error, job["attempts"])
        except Exception as e:  # noqa: BLE001 -- the job stays 'running' and is taken over when stale
            logger.warning(f"Outbox could not record the result of job {job['id']}: {type(e).__name__}")
        ran += 1
    return ran


def purge_finished() -> int:
    """Delete ``done`` jobs older than DONE_KEEP_DAYS (dead jobs are kept for a person to look at)."""
    from app.database import SessionLocal

    cutoff = _now() - timedelta(days=DONE_KEEP_DAYS)
    with SessionLocal() as db:
        n = db.query(OutboxJob).filter(OutboxJob.status == DONE, OutboxJob.updated_at < cutoff).delete(
            synchronize_session=False
        )
        db.commit()
        return int(n or 0)


def counts() -> Dict[str, int]:
    """Jobs per status (sync): for metrics and alerts."""
    from sqlalchemy import func

    from app.database import SessionLocal

    with SessionLocal() as db:
        return {s: int(c) for s, c in db.query(OutboxJob.status, func.count(OutboxJob.id)).group_by(OutboxJob.status)}


async def run_outbox_forever() -> None:
    """Background loop started from the application lifespan."""
    ensure_default_handlers()
    last_purge = 0.0
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await drain_once()
            if loop.time() - last_purge > 6 * 3600:
                last_purge = loop.time()
                await run_io(purge_finished)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Outbox sweep failed: {type(e).__name__}: {e}")


# ── the job kinds the application uses ────────────────────────────────────────────────────────────────────

async def _handle_ops_forward(payload: Dict[str, Any]) -> Any:
    from app.services.ops_forward import forward_to_ops_checked

    return await forward_to_ops_checked(payload["message"])


async def _handle_payment_invoice(payload: Dict[str, Any]) -> Any:
    from app.services.billing_service import dispatch_payment_invoice
    from app.services.invoice_service import generate_invoice_pdf
    from app.services.meta_whatsapp_service import send_document_to_whatsapp

    return await dispatch_payment_invoice(
        recipient_id=payload["recipient_id"],
        payment_id=payload["payment_id"],
        amount=int(payload["amount"]),
        customer_name=payload.get("customer_name") or "Valued Customer",
        customer_snapshot=payload.get("customer_snapshot"),
        local_pdf_fn=generate_invoice_pdf,
        send_document_fn=send_document_to_whatsapp,
    )


def ensure_default_handlers() -> None:
    _handlers.setdefault("ops_forward", _handle_ops_forward)
    _handlers.setdefault("payment_invoice", _handle_payment_invoice)


def kinds() -> List[str]:
    return sorted(_handlers)
