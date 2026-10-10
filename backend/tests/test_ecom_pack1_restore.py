"""E-Com Pack 1: the full 8-shot pack (six original shots + Stand Display + Luxury Drape). One Pack order generates every style,
with exactly ONE image-generation call per style: no retry and no fallback provider for any style.

All network/AI calls are mocked; no credits are used.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
from app.services.earring_on_stand_shot import build_stand_shot_prompt
from app.services.earring_luxury_drape_shot import build_luxury_drape_prompt

SENDER = "919000000002"
EXPECTED = [
    ("Clean E-Commerce", "prompt_ecommerce"),
    ("Close-up on Ear", "prompt_close_up"),
    ("Scale Reference", "prompt_scale_reference"),
    ("Professional Studio", "prompt_professional"),
    ("Lifestyle Shot", "prompt_complementary"),
    ("UGC Style", "prompt_ugc"),
    ("Stand Display", "prompt_stand"),
    ("Luxury Drape", "prompt_luxury_drape"),
]
PROMPTS = [build_earring_ecommerce_prompt(), build_close_up_ears_prompt(), build_scale_reference_prompt(),
           build_professional_shot_prompt(), build_complementary_shot_prompt(), build_ugc_style_prompt(),
           build_stand_shot_prompt(), build_luxury_drape_prompt()]


class PackDefinitionTests(unittest.TestCase):
    def test_exactly_eight_shots_original_six_first_then_stand_then_drape(self):
        self.assertEqual(mws.CATALOG_PACK_STYLES, EXPECTED)

    def test_pack_generates_every_style(self):
        from app import config
        from app.config import Settings

        self.assertEqual(mws.pack_generation_count(), len(mws.CATALOG_PACK_STYLES))
        self.assertEqual(mws.pack_generation_count(), 8)
        self.assertEqual(config._PACK_IMAGE_COUNT, mws.pack_generation_count())
        self.assertIn("generating all 8 styles", mws.CATALOG_PACK_ACK_TEMPLATE)
        # No cap left anywhere that could quietly shrink the multi-angle pack the customer paid for.
        self.assertFalse(hasattr(mws, "MAX_IMAGE_CALLS_PER_ORDER"))
        self.assertFalse(hasattr(mws, "MAX_STYLES_PER_PACK"))
        self.assertNotIn("MAX_STYLES_PER_PACK", Settings.model_fields)

    def test_pack_price_unchanged(self):
        self.assertEqual(wallet_service.price_per_image(), 500)


class PackRunTests(unittest.TestCase):
    def setUp(self):
        # Several threads use the database during one pack: the worker on the event loop, plus the I/O pool that
        # writes the provider cost log in the background (image_generation_manager._log_call_in_background) and
        # the spend counter. One shared in-memory connection let a background commit/rollback land inside the
        # worker's "processing -> generated" transaction and undo it, so the pack intermittently ended in
        # 'processing'. A file database gives every session its own connection, as PostgreSQL does in production.
        self.engine = make_engine(concurrent=True)
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
        self.upload = AsyncMock(side_effect=[f"media-{i}" for i in range(1, len(EXPECTED) + 1)])
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

    def test_all_eight_shots_generated_once_each_and_delivered(self):
        from app.ai.providers.image_base import ImageGenerationResult

        gen = AsyncMock(return_value=ImageGenerationResult(
            success=True, image_url="data:image/png;base64,QUJD", provider_name="gemini"))
        with patch("app.ai.image_generation_manager.ImageGenerationManager.generate_image", gen):
            self.assertTrue(asyncio.run(mws.process_whatsapp_catalog_pack(self.oid)))
        # Exactly one manager call per style (no extra call), each in single-attempt mode (no retry, no fallback),
        # existing prompts, the customer's own photo as the reference, existing 4:5 pack ratio.
        self.assertEqual(gen.await_count, len(mws.CATALOG_PACK_STYLES))
        self.assertEqual(sorted(c.kwargs["prompt"] for c in gen.await_args_list), sorted(PROMPTS))
        for call in gen.await_args_list:
            self.assertIs(call.kwargs["single_attempt"], True)
            self.assertEqual(call.kwargs["reference_image"], self.photo_bytes)
            self.assertEqual(call.kwargs["context"]["aspect_ratio"], "4:5")
        self.assertEqual(self.upload.await_count, len(EXPECTED))
        self.assertEqual(self.deliver.await_args.kwargs["image_urls"], [f"media-{i}" for i in range(1, len(EXPECTED) + 1)])
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

    def _run_on_drive(self, drive_ok):
        """Eight styles for a customer with a shared Drive folder; ``drive_ok[i]`` False = Drive refuses style i."""
        names = iter(range(1, len(EXPECTED) + 1))

        async def to_drive(image_bytes, customer_id, ingestion_id, style_title):
            index = next(names)
            return f"{index:03d} - {style_title}.png" if drive_ok[index - 1] else None

        with patch.object(mws, "_generate_single_pack_style", AsyncMock(return_value=b"png-bytes")), \
                patch.object(mws, "_upload_style_to_drive", side_effect=to_drive), \
                patch("app.services.drive_delivery.enabled", return_value=True), \
                patch("app.services.drive_delivery.customer_id_if_ready", return_value="cust-1"), \
                patch("app.services.batch_notify.schedule") as schedule:
            ok = asyncio.run(mws.process_whatsapp_catalog_pack(self.oid))
        return ok, schedule

    def test_a_pack_for_a_customer_with_a_drive_folder_stays_out_of_the_chat(self):
        ok, schedule = self._run_on_drive([True] * len(EXPECTED))
        self.assertTrue(ok)
        self.deliver.assert_not_awaited()
        self.upload.assert_not_awaited()
        self.assertEqual(self._state(), ("delivered", 0, 0))
        s = self.Session()
        try:
            self.assertEqual(s.get(WhatsAppIngestion, self.oid).delivery_channel, "drive")
        finally:
            s.close()
        schedule.assert_called_once_with("cust-1")

    def test_styles_drive_could_not_take_are_sent_on_whatsapp_and_the_pack_is_still_whole(self):
        ok, schedule = self._run_on_drive([True, False, True, True, False, True, True, True])
        self.assertTrue(ok)
        self.assertEqual(self.upload.await_count, 2)                 # only the two Drive could not take go to Meta
        self.assertEqual(len(self.deliver.await_args.kwargs["image_urls"]), 2)
        self.assertEqual(self._state(), ("delivered", 0, 0))
        schedule.assert_called_once_with("cust-1")

    def test_all_of_all_is_delivered(self):
        self.assertTrue(self._run_with([True] * len(EXPECTED)))
        self.assertEqual(self._state(), ("delivered", 0, 0))
        s = self.Session(); self.assertIsNone(s.get(WhatsAppIngestion, self.oid).error_message); s.close()
        self.text.assert_not_awaited()

    def test_seven_of_eight_is_partial_not_delivered(self):
        self.assertTrue(self._run_with([True, True, False, True, True, True, True, True]))
        self.assertEqual(self.upload.await_count, 7)
        # Existing partial status; no extra charge and no refund (existing rule).
        self.assertEqual(self._state(), ("delivered_partial", 0, 0))
        s = self.Session(); self.assertEqual(s.get(WhatsAppIngestion, self.oid).error_message, "Delivered 7/8 images"); s.close()
        self.assertIn("7 of 8 images", self.text.await_args.args[1])

    def test_only_the_last_shot_failing_is_partial_not_delivered(self):
        self.assertTrue(self._run_with([True] * 7 + [False]))
        self.assertEqual(self.upload.await_count, 7)
        self.assertEqual(self._state(), ("delivered_partial", 0, 0))
        self.assertIn("7 of 8 images", self.text.await_args.args[1])

    def test_one_of_eight_is_partial_not_delivered(self):
        self.assertTrue(self._run_with([True] + [False] * 7))
        self.assertEqual(self._state(), ("delivered_partial", 0, 0))
        self.assertIn("1 of 8 images", self.text.await_args.args[1])

    # ── Daily spend cap (MAX_GENERATIONS_PER_DAY) ──
    def _real_generation(self, cap, already_used=0):
        from app.ai import image_generation_manager as igm
        from app.ai.image_generation_manager import ImageGenerationManager
        from app.ai.providers.image_base import ImageGenerationResult

        provider = AsyncMock(return_value=ImageGenerationResult(
            success=True, image_url="data:image/png;base64,QUJD", provider_name="gemini"))
        fake = type("P", (), {"is_available": True, "supports_reference_image": lambda self: True,
                               "generate_image": provider})()
        with patch.object(settings, "MAX_GENERATIONS_PER_DAY", cap), \
             patch.object(ImageGenerationManager, "_get_provider", lambda self, name: fake), \
             patch.object(ImageGenerationManager, "_get_provider_chain", lambda self: ["gemini"]):
            if already_used:
                self.assertIsNone(igm.reserve_generation_slots(already_used))
            ok = asyncio.run(mws.process_whatsapp_catalog_pack(self.oid))
        return ok, provider

    def _spend_used(self):
        from app.ai import image_generation_manager as igm
        from app.services import spend_counter
        shared = spend_counter.used(igm.current_spend_day())      # the shared DB counter when it answers (PostgreSQL)
        return igm._spend_count if shared is None else shared

    def test_pack_runs_all_eight_when_cap_allows(self):
        ok, provider = self._real_generation(cap=8)
        self.assertTrue(ok)
        self.assertEqual(provider.await_count, 8)
        self.assertEqual(self._spend_used(), 8)
        self.assertEqual(self._state(), ("delivered", 0, 0))

    def test_cap_of_the_old_seven_shot_pack_now_blocks_the_whole_pack_and_refunds_once(self):
        ok, provider = self._real_generation(cap=7)
        self.assertFalse(ok)
        provider.assert_not_awaited()          # never a 7/8 pack: the reservation is all-or-nothing
        self.assertEqual(self._spend_used(), 0)
        self.assertEqual(self._state(), ("failed", 500, 1))

    def test_cap_too_small_blocks_whole_pack_before_any_call_and_refunds_once(self):
        ok, provider = self._real_generation(cap=1)
        self.assertFalse(ok)
        provider.assert_not_awaited()          # never a 1/8 pack
        self.assertEqual(self._spend_used(), 0)  # nothing consumed
        self.assertEqual(self._state(), ("failed", 500, 1))
        self.assertIn("₹500 has been refunded", self.text.await_args.args[1])

    def test_partly_used_cap_blocks_the_pack_before_any_call_and_refunds_once(self):
        ok, provider = self._real_generation(cap=8, already_used=1)
        self.assertFalse(ok)
        provider.assert_not_awaited()          # 7 slots left cannot hold an 8-style pack
        self.assertEqual(self._spend_used(), 1)  # only the pre-used slot
        self.assertEqual(self._state(), ("failed", 500, 1))
        self.assertIn("₹500 has been refunded", self.text.await_args.args[1])

    # ── End to end through the REAL Gemini provider (only the SDK client is faked) ──
    def _run_real_providers(self, gemini_outcome):
        """Run the pack with the real manager and GeminiImageProvider behind a chain of gemini -> openai.

        ``gemini_outcome`` is the fake SDK's return value, or an exception it raises. Returns
        (ok, gemini_sdk_calls, openai_mock, audit_lines)."""
        from app.ai import image_generation_manager as igm
        from app.ai.image_generation_manager import ImageGenerationManager
        from app.ai.provider_protection import breaker, bucket
        from app.ai.providers import gemini_image_provider as gip
        from app.ai.providers.openai_image_provider import OpenAIImageProvider
        from app.utils.logger import logger

        sdk_calls = []

        async def generate_content(**kwargs):
            sdk_calls.append(kwargs)
            if isinstance(gemini_outcome, BaseException):
                raise gemini_outcome
            return gemini_outcome

        fake_client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
        openai_call = AsyncMock()
        audit_lines = []
        sink = logger.add(lambda m: audit_lines.append(str(m)), level="INFO", format="{message}",
                          filter=lambda r: "[API-AUDIT]" in r["message"])
        breaker.reset()
        bucket.reset()
        gip._api_audit_counts.clear()
        try:
            with patch.object(settings, "GEMINI_API_KEY", "test-key"), \
                 patch.object(settings, "OPENAI_API_KEY", "test-key"), \
                 patch.object(settings, "IMAGE_RATE_LIMIT_RETRIES", 2), \
                 patch.object(igm, "_retry_delay_seconds", lambda error, attempt: 0.0), \
                 patch.object(ImageGenerationManager, "_get_provider_chain", lambda self: ["gemini", "openai"]), \
                 patch.object(gip, "_get_client", lambda genai, types: fake_client), \
                 patch.object(OpenAIImageProvider, "generate_image", openai_call):
                ok = asyncio.run(mws.process_whatsapp_catalog_pack(self.oid))
        finally:
            logger.remove(sink)
            breaker.reset()
        return ok, sdk_calls, openai_call, audit_lines

    def test_real_provider_makes_one_gemini_call_per_style_and_logs_each_audit_line(self):
        image_part = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x89PNG-generated", mime_type="image/png"))
        response = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[image_part]))])
        ok, sdk_calls, openai_call, audit_lines = self._run_real_providers(response)
        self.assertTrue(ok)
        styles = len(mws.CATALOG_PACK_STYLES)
        self.assertEqual(len(sdk_calls), styles)          # one Gemini call per style, never more
        openai_call.assert_not_awaited()
        gemini_lines = [line for line in audit_lines if "Gemini Image Call Triggered" in line]
        self.assertEqual(len(gemini_lines), styles)
        # Every style shares the order's request_id, so the audit counts the order's calls 1..7.
        for n, line in enumerate(gemini_lines, start=1):
            self.assertIn(f"[API-AUDIT] Gemini Image Call Triggered: Count {n} request_id=", line)
        self.assertEqual(self._state(), ("delivered", 0, 0))

    def test_rate_limited_gemini_is_not_retried_and_never_falls_back(self):
        rate_limit = RuntimeError(
            "429 RESOURCE_EXHAUSTED. Quota exceeded for metric generate_content requests per minute. retry in 1s"
        )
        # Circuit breaker off, so every style reaches Gemini and the count measures retries only (with the breaker
        # on, five outage failures in a row would stop the last styles before they call at all).
        with patch.object(settings, "CIRCUIT_BREAKER_FAILURES", 0):
            ok, sdk_calls, openai_call, audit_lines = self._run_real_providers(rate_limit)
        self.assertFalse(ok)
        # IMAGE_RATE_LIMIT_RETRIES=2 is ignored: exactly one call per style (with retries it would be 3 per style).
        self.assertEqual(len(sdk_calls), len(mws.CATALOG_PACK_STYLES))
        openai_call.assert_not_awaited()                  # recoverable error, still no fallback provider
        self.assertEqual(sum("Gemini Image Call Triggered" in line for line in audit_lines), len(sdk_calls))
        self.assertEqual(self._state(), ("failed", 500, 1))

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
        self.assertIs(gen.await_args.kwargs["single_attempt"], True)   # no retry, no fallback provider
        self.assertNotIn("spend_reserved", gen.await_args.kwargs)  # normal per-call cap
        s = self.Session(); row = s.get(WhatsAppIngestion, wid)
        bal = s.query(Customer.wallet_balance).filter(Customer.whatsapp_id == SENDER).scalar(); s.close()
        self.assertEqual((row.status, row.amount_charged, bal), ("delivered", 50, 0))


class ApiAuditCounterTests(unittest.TestCase):
    def test_every_call_for_an_order_is_counted_and_logged_at_info(self):
        from app.ai.providers import gemini_image_provider as gip
        from app.utils.logger import logger

        records = []
        sink = logger.add(lambda m: records.append((m.record["level"].name, m.record["message"])), level="INFO",
                          filter=lambda r: "[API-AUDIT]" in r["message"])
        gip._api_audit_counts.clear()
        try:
            self.assertEqual(gip._audit_gemini_image_call("order-a", "gemini-3.1-flash-image"), 1)
            self.assertEqual(gip._audit_gemini_image_call("order-b", "gemini-3.1-flash-image"), 1)
            self.assertEqual(gip._audit_gemini_image_call("order-a", "gemini-3.1-flash-image"), 2)
        finally:
            logger.remove(sink)
            gip._api_audit_counts.clear()
        # A Catalog Pack makes one call per style under one request_id: a second call is expected, not an error.
        self.assertEqual([level for level, _ in records], ["INFO", "INFO", "INFO"])
        self.assertIn("[API-AUDIT] Gemini Image Call Triggered: Count 1 request_id=order-a", records[0][1])
        self.assertIn("[API-AUDIT] Gemini Image Call Triggered: Count 1 request_id=order-b", records[1][1])
        self.assertIn("[API-AUDIT] Gemini Image Call Triggered: Count 2 request_id=order-a", records[2][1])


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
