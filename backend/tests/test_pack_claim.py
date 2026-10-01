"""Phase 0 / U9 (MON-8): the Pack worker claims an order atomically and only from a runnable status."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.database import Base
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.services import meta_whatsapp_service as mws

SENDER = "919812345678"


class PackClaimTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.provider = AsyncMock()
        self.patches = [
            patch("app.database.SessionLocal", return_value=self.db),
            patch.object(self.db, "close"),
            patch.object(mws, "send_whatsapp_text", new=AsyncMock(return_value=True)),
            patch("app.ai.image_generation_manager.ImageGenerationManager.generate_image", self.provider),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()
        self.engine.dispose()

    def _order(self, status):
        row = WhatsAppIngestion(external_user_id=SENDER, external_message_id=f"wamid.{status}",
                                external_media_id="m", channel="whatsapp", status=status, amount_charged=500)
        self.db.add(row)
        self.db.commit()
        return row.id

    def _run(self, ingestion_id):
        return asyncio.run(mws.process_whatsapp_catalog_pack(ingestion_id))

    def test_only_queued_or_reset_orders_are_runnable(self):
        self.assertEqual(set(mws.PACK_RUNNABLE_STATUSES), {"pack_queued", "stored"})

    def test_non_runnable_orders_are_not_started_or_changed(self):
        for status in ("processing", "delivered", "delivered_partial", "failed", "awaiting_choice",
                       "choice_claimed", "white_queued"):
            with self.subTest(status):
                order_id = self._order(status)
                self.assertFalse(self._run(order_id))
                self.db.expire_all()
                row = self.db.get(WhatsAppIngestion, order_id)
                self.assertEqual(row.status, status)
                self.assertEqual(row.amount_charged, 500)
        self.provider.assert_not_called()

    def test_unknown_order_is_not_started(self):
        self.assertFalse(self._run("does-not-exist"))
        self.provider.assert_not_called()

    def test_second_start_of_a_claimed_order_is_refused(self):
        order_id = self._order("pack_queued")
        # First start claims it and then fails fast on the missing image record
        # (no provider call, refund path runs); a second start must not run again.
        self._run(order_id)
        self.provider.assert_not_called()
        self.db.expire_all()
        first_status = self.db.get(WhatsAppIngestion, order_id).status
        self.assertNotIn(first_status, mws.PACK_RUNNABLE_STATUSES)
        self.assertFalse(self._run(order_id))


if __name__ == "__main__":
    unittest.main()
