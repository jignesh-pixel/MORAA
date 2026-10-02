"""E-Com Pack 1: full 6-shot pack restored (development throttle lifted).

All network/AI calls are mocked; no credits are used.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


from tests.db_support import make_engine
from app.config import settings
from app.database import Base
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.image import Image
from app.models.whatsapp_ingestion import PRODUCT_PACK_1, WhatsAppIngestion
from app.services import meta_whatsapp_service as mws
from app.services import wallet_service
from app.services.earring_close_up_ears_prompt import build_close_up_ears_prompt
from app.services.earring_complementary_shot_prompt import build_complementary_shot_prompt
from app.services.earring_ecommerce_prompt import build_earring_ecommerce_prompt
from app.services.earring_professional_shot_prompt import build_professional_shot_prompt
from app.services.earring_scale_reference_prompt import build_scale_reference_prompt
from app.services.earring_ugc_style_prompt import build_ugc_style_prompt

SENDER = "919000000002"
EXPECTED = [
    ("Clean E-Commerce", "prompt_ecommerce"),
    ("Close-up on Ear", "prompt_close_up"),
    ("Scale Reference", "prompt_scale_reference"),
    ("Professional Studio", "prompt_professional"),
    ("Lifestyle Shot", "prompt_complementary"),
    ("UGC Style", "prompt_ugc"),
]
PROMPTS = [build_earring_ecommerce_prompt(), build_close_up_ears_prompt(), build_scale_reference_prompt(),
           build_professional_shot_prompt(), build_complementary_shot_prompt(), build_ugc_style_prompt()]


class PackDefinitionTests(unittest.TestCase):
    def test_exactly_six_shots_in_original_order(self):
        self.assertEqual(mws.CATALOG_PACK_STYLES, EXPECTED)

    def test_throttle_lifted_by_default(self):
        self.assertIsNone(settings.MAX_STYLES_PER_PACK)
        self.assertIsNone(mws.MAX_STYLES_PER_PACK)
        self.assertIn("all 6 styles", mws.CATALOG_PACK_ACK_TEMPLATE)

    def test_pack_price_unchanged(self):
        self.assertEqual(wallet_service.price_per_image(), 500)


class PackRunTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        db = self.Session()
        db.add(Customer(whatsapp_id=SENDER, full_name="T", business_name="B", gst_number="N/A",
                        address="N/A", wallet_balance=0, is_registered=True))
        self.tmp = tempfile.TemporaryDirectory()
        photo = Path(self.tmp.name) / "p.jpg"
        self.photo_bytes = b"\xff\xd8\xff" + b"\x01" * 40
        photo.write_bytes(self.photo_bytes)
        image = Image(request_id="req-p1", original_filename="p.jpg", stored_filename="p.jpg",
                      file_path=str(photo), file_size=len(self.photo_bytes), mime_type="image/jpeg",
                      image_url="/uploads/req-p1/p.jpg")
        db.add(image)
        db.commit()
        # A paid Pack 1 order exactly as _handle_product_choice leaves it
        # (wallet already debited ₹500 once at the tap; balance now 0).
        row = WhatsAppIngestion(external_user_id=SENDER, external_message_id="wamid.p1", external_media_id="m",
                                channel="whatsapp", status="pack_queued", product_code=PRODUCT_PACK_1,
                                amount_charged=500, image_id=image.id, mime_type="image/jpeg")
        db.add(row)
        db.commit()
        self.oid = row.id
        db.close()
        self.text = AsyncMock(return_value=True)
        self.upload = AsyncMock(side_effect=[f"media-{i}" for i in range(1, 7)])
        self.deliver = AsyncMock(side_effect=lambda **kw: len(kw["image_urls"]))
        from app.ai import image_generation_manager as igm
        igm._spend_day, igm._spend_count = None, 0  # fresh daily spend counter
        self.patches = [patch("app.database.SessionLocal", self.Session),
                        patch.object(settings, "MAX_GENERATIONS_PER_DAY", 100000),
                        patch.object(mws, "send_whatsapp_text", self.text),
                        patch.object(mws, "upload_media_to_meta", self.upload),
                        patch.object(mws, "send_catalog_pack_images_to_whatsapp", self.deliver),
                        patch.object(mws, "_check_failure_rate", lambda db: None),
                        patch.object(mws, "DRY_RUN_IMAGE_MODE", False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
        self.engine.dispose()

    def _state(self):
        s = self.Session()
        try:
            row = s.get(WhatsAppIngestion, self.oid)
            bal = s.query(Customer.wallet_balance).filter(Customer.whatsapp_id == SENDER).scalar()
            refunds = s.query(AuditLog).filter(AuditLog.action == mws.REFUND_AUDIT_ACTION).count()
            return row.status, bal, refunds
        finally:
            s.close()

    def test_all_six_shots_generated_once_each_and_delivered(self):
        from app.ai.providers.image_base import ImageGenerationResult

        gen = AsyncMock(return_value=ImageGenerationResult(
            success=True, image_url="data:image/png;base64,QUJD", provider_name="gemini"))
        with patch("app.ai.image_generation_manager.ImageGenerationManager.generate_image", gen):
            self.assertTrue(asyncio.run(mws.process_whatsapp_catalog_pack(self.oid)))
        # Exactly one manager call per shot (no extra call, no retry), same order, existing prompts,
        # the customer's own photo as the reference, existing 4:5 pack ratio.
        self.assertEqual(gen.await_count, 6)
        self.assertEqual(sorted(c.kwargs["prompt"] for c in gen.await_args_list), sorted(PROMPTS))
        for call in gen.await_args_list:
            self.assertEqual(call.kwargs["reference_image"], self.photo_bytes)
            self.assertEqual(call.kwargs["context"]["aspect_ratio"], "4:5")
        self.assertEqual(self.upload.await_count, 6)
        self.assertEqual(self.deliver.await_args.kwargs["image_urls"], [f"media-{i}" for i in range(1, 7)])
        # Pack-level charge untouched: no extra debit, no refund.
        self.assertEqual(self._state(), ("delivered", 0, 0))
        self.text.assert_not_awaited()

    def test_generation_order_matches_pack_order(self):
        seen = []

        async def fake_style(**kw):
            seen.append((kw["style_title"], kw["prompt"]))
            return "data:image/png;base64,QUJD"

        with patch.object(mws, "_generate_single_pack_style", side_effect=fake_style):
            asyncio.run(mws.process_whatsapp_catalog_pack(self.oid))
        self.assertEqual(seen, [(title, prompt) for (title, _), prompt in zip(EXPECTED, PROMPTS)])

    def _run_with(self, successes):
        results = ["data:image/png;base64,QUJD" if ok else None for ok in successes]
        with patch.object(mws, "_generate_single_pack_style", AsyncMock(side_effect=results)):
            return asyncio.run(mws.process_whatsapp_catalog_pack(self.oid))

    def test_six_of_six_is_delivered(self):
        self.assertTrue(self._run_with([True] * 6))
        self.assertEqual(self._state(), ("delivered", 0, 0))
        s = self.Session(); self.assertIsNone(s.get(WhatsAppIngestion, self.oid).error_message); s.close()
        self.text.assert_not_awaited()

    def test_five_of_six_is_partial_not_delivered(self):
        self.assertTrue(self._run_with([True, True, False, True, True, True]))
        self.assertEqual(self.upload.await_count, 5)
        # Existing partial status; no extra charge and no refund (existing rule).
        self.assertEqual(self._state(), ("delivered_partial", 0, 0))
        s = self.Session(); self.assertEqual(s.get(WhatsAppIngestion, self.oid).error_message, "Delivered 5/6 images"); s.close()
        self.assertIn("5 of 6 images", self.text.await_args.args[1])

    def test_one_of_six_is_partial_not_delivered(self):
        self.assertTrue(self._run_with([True] + [False] * 5))
        self.assertEqual(self._state(), ("delivered_partial", 0, 0))
        self.assertIn("1 of 6 images", self.text.await_args.args[1])

    # ── Daily spend cap (MAX_GENERATIONS_PER_DAY) ──
    def _real_generation(self, cap):
        from app.ai.image_generation_manager import ImageGenerationManager
        from app.ai.providers.image_base import ImageGenerationResult

        provider = AsyncMock(return_value=ImageGenerationResult(
            success=True, image_url="data:image/png;base64,QUJD", provider_name="gemini"))
        fake = type("P", (), {"is_available": True, "supports_reference_image": lambda self: True,
                               "generate_image": provider})()
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", cap), \
             patch.object(ImageGenerationManager, "_get_provider", lambda self, name: fake), \
             patch.object(ImageGenerationManager, "_get_provider_chain", lambda self: ["gemini"]):
            ok = asyncio.run(mws.process_whatsapp_catalog_pack(self.oid))
        return ok, provider

    def test_pack_runs_all_six_when_cap_allows(self):
        from app.ai import image_generation_manager as igm
        ok, provider = self._real_generation(cap=6)
        self.assertTrue(ok)
        self.assertEqual(provider.await_count, 6)
        from app.services import spend_counter
        shared = spend_counter.used(igm.current_spend_day())      # the shared DB counter when it answers (PostgreSQL)
        self.assertEqual(igm._spend_count if shared is None else shared, 6)
        self.assertEqual(self._state(), ("delivered", 0, 0))

    def test_cap_too_small_blocks_whole_pack_before_any_call_and_refunds_once(self):
        from app.ai import image_generation_manager as igm
        ok, provider = self._real_generation(cap=1)
        self.assertFalse(ok)
        provider.assert_not_awaited()          # never a 1/6 pack
        self.assertEqual(igm._spend_count, 0)  # nothing consumed
        self.assertEqual(self._state(), ("failed", 500, 1))
        self.assertIn("₹500 has been refunded", self.text.await_args.args[1])

    def test_all_shots_failed_refunds_pack_price_once(self):
        with patch.object(mws, "_generate_single_pack_style", AsyncMock(return_value=None)):
            self.assertFalse(asyncio.run(mws.process_whatsapp_catalog_pack(self.oid)))
        self.assertEqual(self._state(), ("failed", 500, 1))
        self.assertIn("₹500 has been refunded", self.text.await_args.args[1])

    def test_fifty_rupee_white_flow_still_counts_per_call(self):
        from app.ai.providers.image_base import ImageGenerationResult
        from app.models.whatsapp_ingestion import PRODUCT_WHITE_BG

        s = self.Session()
        pack = s.get(WhatsAppIngestion, self.oid)
        white = WhatsAppIngestion(external_user_id=SENDER, external_message_id="wamid.w1", external_media_id="m",
                                  channel="whatsapp", status="white_queued", product_code=PRODUCT_WHITE_BG,
                                  amount_charged=50, image_id=pack.image_id, mime_type="image/jpeg")
        s.add(white); s.commit(); wid = white.id; s.close()
        gen = AsyncMock(return_value=ImageGenerationResult(
            success=True, image_url="data:image/png;base64,QUJD", provider_name="gemini"))
        with patch("app.ai.image_generation_manager.ImageGenerationManager.generate_image", gen), \
             patch.object(mws, "send_image_to_whatsapp", AsyncMock(return_value=True)):
            self.assertTrue(asyncio.run(mws.process_whatsapp_white_bg(wid)))
        self.assertEqual(gen.await_count, 1)
        self.assertNotIn("spend_reserved", gen.await_args.kwargs)  # normal per-call cap
        s = self.Session(); row = s.get(WhatsAppIngestion, wid)
        bal = s.query(Customer.wallet_balance).filter(Customer.whatsapp_id == SENDER).scalar(); s.close()
        self.assertEqual((row.status, row.amount_charged, bal), ("delivered", 50, 0))


class SpendGuardTests(unittest.TestCase):
    def setUp(self):
        from app.ai import image_generation_manager as igm
        self.igm = igm
        igm._spend_day, igm._spend_count = None, 0

    def _call(self, **kw):
        from app.ai.providers.image_base import ImageGenerationResult
        m = self.igm.ImageGenerationManager()
        p = type("P", (), {"is_available": True, "supports_reference_image": lambda self: True,
                            "generate_image": AsyncMock(return_value=ImageGenerationResult(
                                success=True, image_url="data:image/png;base64,QQ==", provider_name="gemini"))})()
        m._get_provider = lambda name: p
        m._get_provider_chain = lambda: ["gemini"]
        return asyncio.run(m.generate_image("p", {"request_id": "t"}, **kw)).success

    def test_single_calls_unchanged(self):
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 1):
            self.assertEqual([self._call(), self._call()], [True, False])

    def test_reservation_is_all_or_nothing(self):
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 6):
            for _ in range(5):
                self.assertTrue(self._call())
            self.assertIsNotNone(self.igm.reserve_generation_slots(6))
            self.assertEqual(self.igm._spend_count, 5)
            self.assertTrue(self._call())
            self.assertFalse(self._call())

    def test_reserved_calls_still_obey_kill_switch(self):
        with patch.object(settings, "GENERATION_ENABLED", False):
            self.assertFalse(self._call(spend_reserved=True))


if __name__ == "__main__":
    unittest.main()
