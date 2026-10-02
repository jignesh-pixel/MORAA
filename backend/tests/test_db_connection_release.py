"""Phase 3 / PERF-1: a paid-order worker must not hold a database connection while it waits on a provider or Meta.

A Session keeps its connection from its first query until commit/rollback. Held across a 15-40 s provider
call, about 15 concurrent orders used up the pool and froze the whole app. The check below runs inside every
long wait and asserts the worker's session has no open transaction (so its connection is back in the pool).
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.image import Image
from app.models.whatsapp_ingestion import PRODUCT_WHITE_BG, WhatsAppIngestion
from app.services import meta_whatsapp_service as mws
from tests.test_white_bg_product import IMAGE_BYTES, SENDER, WHITE, _make_customer, _make_engine_and_session


class ConnectionReleasedDuringWaitsTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.db = _make_engine_and_session()
        _make_customer(self.db, balance=650)
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "x.jpg")
        Path(path).write_bytes(IMAGE_BYTES)
        img = Image(original_filename="x.jpg", stored_filename="x.jpg", file_path=path,
                    file_size=len(IMAGE_BYTES), mime_type="image/jpeg", image_url="/x.jpg")
        self.db.add(img)
        self.db.commit()
        self.ingestion = WhatsAppIngestion(
            external_user_id=SENDER, external_message_id="wamid.release", external_media_id="m1", channel="whatsapp",
            image_id=img.id, status="white_queued", mime_type="image/jpeg", product_code=PRODUCT_WHITE_BG,
            amount_charged=WHITE,
        )
        self.db.add(self.ingestion)
        self.db.commit()
        self.in_transaction_during = {}

        def watch(name, result):
            async def call(*args, **kwargs):
                self.in_transaction_during[name] = self.db.in_transaction()
                return result
            return call

        manager = MagicMock()
        manager.generate_image = watch("generate", MagicMock(
            success=True, image_url="data:image/png;base64,AAAA", error=None,
            provider_name="gemini", model_used="m", fallback_used=False))
        patches = [
            patch("app.database.SessionLocal", return_value=self.db),
            patch.object(self.db, "close"),
            patch.object(mws, "DRY_RUN_IMAGE_MODE", False),
            patch("app.ai.image_generation_manager.ImageGenerationManager", return_value=manager),
            patch.object(mws, "upload_media_to_meta", new=watch("upload", "media-1")),
            patch.object(mws, "send_image_to_whatsapp", new=watch("send", True)),
            patch.object(mws, "send_whatsapp_text", new=watch("text", True)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def test_white_worker_holds_no_transaction_during_generation_upload_or_send(self):
        self.assertTrue(asyncio.run(mws.process_whatsapp_white_bg(self.ingestion.id)))
        self.assertEqual(self.in_transaction_during, {"generate": False, "upload": False, "send": False})

    def test_white_worker_holds_no_transaction_while_telling_the_customer_it_failed(self):
        failed = MagicMock(success=False, image_url=None, error="boom")
        patch("app.ai.image_generation_manager.ImageGenerationManager",
              return_value=MagicMock(generate_image=AsyncMock(return_value=failed))).start()
        self.addCleanup(patch.stopall)
        self.assertFalse(asyncio.run(mws.process_whatsapp_white_bg(self.ingestion.id)))
        self.assertEqual(self.in_transaction_during.get("text"), False)


if __name__ == "__main__":
    unittest.main()
