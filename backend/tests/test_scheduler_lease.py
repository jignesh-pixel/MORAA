"""ARC-2: one process owns each periodic job."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.database import SessionLocal, engine
from app.models.scheduler_lease import SchedulerLease
from app.services import scheduler_lease


class LeaseTests(unittest.TestCase):
    def setUp(self):
        SchedulerLease.__table__.create(bind=engine, checkfirst=True)
        self.addCleanup(lambda: SchedulerLease.__table__.drop(bind=engine, checkfirst=True))
        with SessionLocal() as db:
            db.query(SchedulerLease).delete()
            db.commit()

    def test_first_taker_wins_and_a_second_process_is_refused(self):
        self.assertTrue(asyncio.run(scheduler_lease.holds_lease("job", 60)))
        with patch.object(scheduler_lease, "HOLDER", "someone-else"):
            self.assertFalse(asyncio.run(scheduler_lease.holds_lease("job", 60)))

    def test_the_owner_renews_its_own_lease(self):
        self.assertTrue(asyncio.run(scheduler_lease.holds_lease("job", 60)))
        self.assertTrue(asyncio.run(scheduler_lease.holds_lease("job", 60)))

    def test_an_expired_lease_is_taken_over(self):
        with patch.object(scheduler_lease, "HOLDER", "dead-process"):
            self.assertTrue(asyncio.run(scheduler_lease.holds_lease("job", 60)))
        with SessionLocal() as db:
            db.query(SchedulerLease).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
            db.commit()
        self.assertTrue(asyncio.run(scheduler_lease.holds_lease("job", 60)))
        with SessionLocal() as db:
            self.assertEqual(db.query(SchedulerLease).one().holder, scheduler_lease.HOLDER)

    def test_different_jobs_have_different_leases(self):
        self.assertTrue(asyncio.run(scheduler_lease.holds_lease("a", 60)))
        with patch.object(scheduler_lease, "HOLDER", "other"):
            self.assertTrue(asyncio.run(scheduler_lease.holds_lease("b", 60)))

    def test_a_database_problem_lets_the_job_run_rather_than_stopping_it(self):
        with patch.object(scheduler_lease, "_try_acquire", side_effect=RuntimeError("db down")):
            self.assertTrue(asyncio.run(scheduler_lease.holds_lease("job", 60)))


if __name__ == "__main__":
    unittest.main()
