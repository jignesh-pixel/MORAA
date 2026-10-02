"""Q-5: durable outbox for ops forwards and invoices."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.database import SessionLocal, engine
from app.models.outbox_job import DEAD, DONE, PENDING, RUNNING, OutboxJob
from app.services import outbox


class _Base(unittest.TestCase):
    def setUp(self):
        OutboxJob.__table__.create(bind=engine, checkfirst=True)     # only our own table: other tests rely on others being absent
        self.addCleanup(lambda: OutboxJob.__table__.drop(bind=engine, checkfirst=True))
        with SessionLocal() as db:
            db.query(OutboxJob).delete()
            db.commit()
        saved = dict(outbox._handlers)
        outbox._handlers.clear()
        self.addCleanup(lambda: (outbox._handlers.clear(), outbox._handlers.update(saved)))

    def jobs(self):
        with SessionLocal() as db:
            return db.query(OutboxJob).order_by(OutboxJob.id).all()


class EnqueueTests(_Base):
    def test_the_same_dedupe_key_is_recorded_once(self):
        self.assertTrue(outbox.enqueue_detached("k", {"a": 1}, "same"))
        self.assertFalse(outbox.enqueue_detached("k", {"a": 1}, "same"))
        self.assertEqual(len(self.jobs()), 1)


class DrainTests(_Base):
    def test_a_successful_job_is_marked_done_and_runs_once(self):
        handler = AsyncMock(return_value=True)
        outbox.register_handler("k", handler)
        outbox.enqueue_detached("k", {"x": 1}, "a")
        self.assertEqual(asyncio.run(outbox.drain_once()), 1)
        self.assertEqual(asyncio.run(outbox.drain_once()), 0)
        handler.assert_awaited_once_with({"x": 1})
        self.assertEqual(self.jobs()[0].status, DONE)

    def test_a_failure_is_retried_later_then_dead_after_the_last_attempt(self):
        outbox.register_handler("k", AsyncMock(side_effect=RuntimeError("boom")))
        outbox.enqueue_detached("k", {}, "a")
        asyncio.run(outbox.drain_once())
        job = self.jobs()[0]
        self.assertEqual((job.status, job.attempts), (PENDING, 1))
        self.assertGreater(job.next_attempt_at.replace(tzinfo=timezone.utc), datetime.now(timezone.utc))
        self.assertEqual(asyncio.run(outbox.drain_once()), 0)          # not due yet
        for _ in range(outbox.MAX_ATTEMPTS - 1):                        # make it due and fail again
            with SessionLocal() as db:
                db.query(OutboxJob).update({"next_attempt_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
                db.commit()
            asyncio.run(outbox.drain_once())
        job = self.jobs()[0]
        self.assertEqual((job.status, job.attempts), (DEAD, outbox.MAX_ATTEMPTS))
        self.assertIn("boom", job.last_error)

    def test_a_handler_returning_failed_counts_as_a_failure(self):
        outbox.register_handler("k", AsyncMock(return_value="failed"))
        outbox.enqueue_detached("k", {}, "a")
        asyncio.run(outbox.drain_once())
        self.assertEqual(self.jobs()[0].status, PENDING)

    def test_a_job_nobody_finished_is_taken_over_when_stale(self):
        outbox.register_handler("k", AsyncMock(return_value=True))
        outbox.enqueue_detached("k", {}, "a")
        stale = datetime.now(timezone.utc) - timedelta(seconds=outbox.STALE_RUNNING_SECONDS + 5)
        with SessionLocal() as db:
            db.query(OutboxJob).update({"status": RUNNING, "updated_at": stale})
            db.commit()
        self.assertEqual(asyncio.run(outbox.drain_once()), 1)
        self.assertEqual(self.jobs()[0].status, DONE)

    def test_a_job_another_process_is_running_is_left_alone(self):
        handler = AsyncMock(return_value=True)
        outbox.register_handler("k", handler)
        outbox.enqueue_detached("k", {}, "a")
        with SessionLocal() as db:
            db.query(OutboxJob).update({"status": RUNNING, "updated_at": datetime.now(timezone.utc)})
            db.commit()
        self.assertEqual(asyncio.run(outbox.drain_once()), 0)
        handler.assert_not_awaited()

    def test_an_unknown_kind_is_a_failure_not_a_crash(self):
        outbox.enqueue_detached("mystery", {}, "a")
        asyncio.run(outbox.drain_once())
        self.assertIn("no handler", self.jobs()[0].last_error)

    def test_purge_removes_old_done_jobs_only(self):
        outbox.enqueue_detached("k", {}, "old")
        outbox.enqueue_detached("k", {}, "dead")
        old = datetime.now(timezone.utc) - timedelta(days=outbox.DONE_KEEP_DAYS + 1)
        with SessionLocal() as db:
            rows = db.query(OutboxJob).order_by(OutboxJob.id).all()
            rows[0].status, rows[0].updated_at = DONE, old
            rows[1].status, rows[1].updated_at = DEAD, old
            db.commit()
        self.assertEqual(outbox.purge_finished(), 1)
        self.assertEqual([j.status for j in self.jobs()], [DEAD])


class CallSiteTests(_Base):
    def test_ops_forward_is_recorded_once_per_message_when_enabled(self):
        from fastapi import BackgroundTasks

        from app.services import ops_forward

        tasks = BackgroundTasks()
        with patch.object(settings, "OUTBOX_ENABLED", True):
            ops_forward._queue_forward({"id": "wamid.1", "type": "text"}, tasks)
            ops_forward._queue_forward({"id": "wamid.1", "type": "text"}, tasks)      # Meta retry: same message
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0].kind, jobs[0].payload["message"]["id"]), ("ops_forward", "wamid.1"))

    def test_ops_forward_uses_the_plain_task_when_the_outbox_is_off(self):
        from fastapi import BackgroundTasks

        from app.services import ops_forward

        tasks = BackgroundTasks()
        ops_forward._queue_forward({"id": "wamid.2"}, tasks)                  # OUTBOX_ENABLED is False in tests
        self.assertEqual(self.jobs(), [])
        self.assertEqual(tasks.tasks[0].func, ops_forward.forward_to_ops)

    def test_invoice_job_payload_is_plain_json_and_deduped_by_payment(self):
        from fastapi import BackgroundTasks

        from app.api.routes import payment_routes

        args = dict(recipient_id="919800000001", payment_id="pay_1", amount=500, customer_name="A",
                    customer_snapshot={"full_name": "A"}, local_pdf_fn=lambda: None, send_document_fn=lambda: None)
        with patch.object(settings, "OUTBOX_ENABLED", True):
            payment_routes._queue_invoice(BackgroundTasks(), args)
            payment_routes._queue_invoice(BackgroundTasks(), args)
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].payload["payment_id"], "pay_1")
        self.assertNotIn("local_pdf_fn", jobs[0].payload)


if __name__ == "__main__":
    unittest.main()
