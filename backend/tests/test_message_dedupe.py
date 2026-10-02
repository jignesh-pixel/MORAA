"""Phase 2 / MON-10: a text, button or form message that Meta delivers twice is handled once."""

import threading
import unittest
from unittest.mock import AsyncMock

from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.processed_message import ProcessedMessage
from app.services.message_dedupe import claim_message, release_messages
from tests.db_support import make_engine, postgres, requires_postgres
from tests.test_access_tiers import TierWebhookBase, _tap
from tests.test_wallet_funded_slot_gate import SENDER


def _text(body, message_id):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"type": "text", "id": message_id, "from": SENDER, "timestamp": "1700000001",
                      "text": {"body": body}}]}}]}]}


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine(concurrent=True)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)

    def test_first_claim_wins_and_a_repeat_is_refused(self):
        with self.Session() as db:
            self.assertTrue(claim_message(db, "wamid.A"))
            self.assertFalse(claim_message(db, "wamid.A"))
            self.assertTrue(claim_message(db, "wamid.B"))

    def test_empty_id_is_never_tracked(self):
        with self.Session() as db:
            self.assertTrue(claim_message(db, ""))
            self.assertTrue(claim_message(db, ""))
            self.assertEqual(db.query(ProcessedMessage).count(), 0)

    def test_released_claim_can_be_taken_again(self):
        with self.Session() as db:
            self.assertTrue(claim_message(db, "wamid.A"))
            release_messages(db, ["wamid.A"])
            self.assertTrue(claim_message(db, "wamid.A"))


@postgres
@requires_postgres
class ClaimUnderRealLocksTests(ClaimTests):
    def test_30_simultaneous_deliveries_of_one_message_are_handled_once(self):
        results, errors = [], []
        barrier = threading.Barrier(30, timeout=120)

        def deliver():
            try:
                barrier.wait()
                with self.Session() as db:
                    results.append(claim_message(db, "wamid.same"))
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=deliver) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        self.assertEqual(errors, [])
        self.assertEqual(results.count(True), 1)


class PurgeTests(unittest.TestCase):
    def test_only_old_ids_are_purged(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch

        engine = make_engine()
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        self.addCleanup(engine.dispose)
        with Session() as db:
            now = datetime.now(timezone.utc)
            db.add(ProcessedMessage(message_id="old", created_at=now - timedelta(days=30)))
            db.add(ProcessedMessage(message_id="new", created_at=now - timedelta(days=1)))
            db.commit()
        with patch("app.database.SessionLocal", Session):
            from app.services.message_dedupe import purge_old_processed_messages

            self.assertEqual(purge_old_processed_messages(), 1)
        with Session() as db:
            self.assertEqual([m.message_id for m in db.query(ProcessedMessage).all()], ["new"])


class WebhookDuplicateTests(TierWebhookBase):
    def setUp(self):
        super().setUp()
        self._customer(1000)

    def test_a_recharge_text_delivered_twice_sends_one_payment_link(self):
        self._post(_text("recharge 500", "wamid.recharge.1"))
        self._post(_text("recharge 500", "wamid.recharge.1"))
        self.assertEqual(self.cta.await_count, 1)

    def test_two_different_messages_are_both_handled(self):
        self._post(_text("recharge 500", "wamid.recharge.1"))
        self._post(_text("recharge 500", "wamid.recharge.2"))
        self.assertEqual(self.cta.await_count, 2)

    def test_a_button_tap_delivered_twice_is_handled_once(self):
        row = self._upload()
        self._post(_tap(f"gv_white:{row.id}", "wamid.tap.dup"))
        first_texts = len(self.sent_texts)
        self._post(_tap(f"gv_white:{row.id}", "wamid.tap.dup"))
        self.assertEqual(len(self.sent_texts), first_texts)       # nothing new, not even "already chosen"
        self.white_worker.assert_awaited_once()

    def test_a_failure_later_in_the_payload_does_not_repeat_earlier_messages(self):
        two = _text("recharge 500", "wamid.batch.1")
        two["entry"][0]["changes"][0]["value"]["messages"].append(
            {"type": "text", "id": "wamid.batch.2", "from": SENDER, "timestamp": "1700000002",
             "text": {"body": "recharge 600"}})
        self.cta.side_effect = [True, RuntimeError("meta down"), True]
        with self.assertRaises(RuntimeError):
            self.client.post("/api/meta/webhook", json=two)
        self.assertEqual(self.cta.await_count, 2)
        ids = {m.message_id for m in self.session.query(ProcessedMessage).all()}
        self.assertEqual(ids, {"wamid.batch.1"})            # the finished one stays claimed, the failed one is free
        self.client.post("/api/meta/webhook", json=two)       # Meta retries the whole payload
        self.assertEqual(self.cta.await_count, 3)            # only the failed message was handled again

    def test_a_failed_message_can_be_handled_when_meta_retries_it(self):
        self.cta.side_effect = [RuntimeError("meta down"), True]
        with self.assertRaises(RuntimeError):
            self.client.post("/api/meta/webhook", json=_text("recharge 500", "wamid.retry.1"))
        self.assertEqual(self.session.query(ProcessedMessage).count(), 0)    # claim given back
        self._post(_text("recharge 500", "wamid.retry.1"))                   # Meta's retry
        self.assertEqual(self.cta.await_count, 2)


if __name__ == "__main__":
    unittest.main()
