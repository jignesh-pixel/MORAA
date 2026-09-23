"""Regression tests for the recurring Gemini/OpenAI balance drain (2026-09-23).

Root causes reproduced here:
1. DRY_RUN_IMAGE_MODE was read with os.getenv(); pydantic-settings never exports
   backend/.env into os.environ, so DRY_RUN_IMAGE_MODE=true in .env was ignored
   and every WhatsApp photo still triggered paid image generation.
2. The startup probe (IMAGE_PROVIDER_STARTUP_DIAGNOSTICS=true in .env) issued a
   real, billed image generation on every process start (every --reload).
Plus defense in depth: a kill switch + daily cap at the one generation chokepoint,
and the onboarding parser's quota cooldown.
"""

import asyncio
import importlib
import os
import sys
import types
from unittest.mock import patch

import httpx

from app.ai import image_generation_manager as igm
from app.ai.providers.image_base import ImageGenerationResult
from app.config import Settings, settings


class _FakeProvider:
    is_available = True

    def __init__(self):
        self.calls = 0

    def supports_reference_image(self):
        return True

    async def generate_image(self, *args, **kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        return ImageGenerationResult(success=True, image_url="data:image/png;base64,AA", provider_name="fake")


def _manager_with(provider):
    m = igm.ImageGenerationManager()
    m._get_provider = lambda name: provider
    m._get_provider_chain = lambda: ["gemini"]
    return m


def _reset_spend():
    igm._spend_day, igm._spend_count = None, 0


# ── Root cause 1: .env DRY_RUN must actually reach the WhatsApp pipeline ──

def test_env_file_value_is_not_in_os_environ(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DRY_RUN_IMAGE_MODE=true\n")
    os.environ.pop("DRY_RUN_IMAGE_MODE", None)
    s = Settings(_env_file=str(env))
    assert s.DRY_RUN_IMAGE_MODE is True           # Settings sees it now
    assert "DRY_RUN_IMAGE_MODE" not in os.environ  # os.getenv never could


def test_whatsapp_dry_run_follows_settings_not_os_environ():
    os.environ.pop("DRY_RUN_IMAGE_MODE", None)
    import app.services.meta_whatsapp_service as mws
    try:
        with patch.object(settings, "DRY_RUN_IMAGE_MODE", True):
            mws = importlib.reload(mws)
            assert mws.DRY_RUN_IMAGE_MODE is True

            async def boom(*a, **k):
                raise AssertionError("paid generation called in dry-run")

            with patch.object(igm.ImageGenerationManager, "generate_image", boom):
                out = asyncio.run(mws._generate_single_pack_style(
                    "ing-1", "Clean E-Commerce", "p", b"\x89PNG", "image/png", "req-1"))
            assert out and out.startswith("data:image/png")
    finally:
        with patch.object(settings, "DRY_RUN_IMAGE_MODE", False):
            importlib.reload(mws)


# ── Root cause 2: startup probe must never generate an image ──

def test_startup_probe_makes_no_billable_call():
    from app.services import gemini_diagnostics as gd
    seen = []
    model = settings.GEMINI_IMAGE_MODEL

    def handler(request):
        seen.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": f"models/{model}"}]})
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    fake = lambda **kw: real_client(transport=httpx.MockTransport(handler))
    with patch.object(gd.httpx, "AsyncClient", fake), \
         patch.object(settings, "IMAGE_PROVIDER_STARTUP_DIAGNOSTICS", True), \
         patch.object(settings, "GEMINI_API_KEY", "fake-key"), \
         patch.object(settings, "PRIMARY_IMAGE_PROVIDER", "gemini"):
        gd._STARTUP_PROBE_DONE = False
        diag = asyncio.run(gd.log_startup_image_provider_diagnosis())
    assert diag is not None and diag.ok
    assert seen == ["GET"]  # listing only, no POST generateContent


# ── Defense in depth at the generation chokepoint ──

def test_kill_switch_blocks_every_provider_call():
    _reset_spend()
    p = _FakeProvider()
    with patch.object(settings, "GENERATION_ENABLED", False):
        r = asyncio.run(_manager_with(p).generate_image("x", {"request_id": "k"}))
    assert not r.success and p.calls == 0


def test_daily_cap_stops_runaway_duplicates():
    _reset_spend()
    p = _FakeProvider()
    m = _manager_with(p)
    with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 3):
        results = [asyncio.run(m.generate_image("x", {"request_id": f"r{i}"})) for i in range(10)]
    assert p.calls == 3
    assert sum(r.success for r in results) == 3


def test_concurrent_burst_cannot_exceed_cap():
    _reset_spend()
    p = _FakeProvider()
    m = _manager_with(p)

    async def burst():
        return await asyncio.gather(*[m.generate_image("x", {"request_id": "same"}) for _ in range(25)])

    with patch.object(settings, "MAX_GENERATIONS_PER_DAY", 5):
        asyncio.run(burst())
    assert p.calls == 5


def test_default_settings_do_not_block_normal_generation():
    _reset_spend()
    p = _FakeProvider()
    r = asyncio.run(_manager_with(p).generate_image("x", {"request_id": "ok"}))
    assert r.success and p.calls == 1


# ── Onboarding parser: quota error -> cooldown, no repeated paid calls ──

def test_onboarding_quota_error_starts_cooldown():
    from app.services import onboarding_service as ob
    calls = {"n": 0}

    class _Models:
        def generate_content(self, **kw):
            calls["n"] += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")

    class _Client:
        def __init__(self, **kw):
            self.models = _Models()

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = _Client
    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = lambda **kw: kw
    fake_genai.types = fake_types
    google_pkg = sys.modules.get("google") or types.ModuleType("google")
    mods = {"google": google_pkg, "google.genai": fake_genai, "google.genai.types": fake_types}

    ob._onboarding_quota_cooldown_until = 0.0
    ob._onboarding_daily_call_count = 0
    with patch.dict(sys.modules, mods), patch.object(google_pkg, "genai", fake_genai, create=True), \
         patch.object(settings, "GEMINI_API_KEY", "fake-key"):
        for _ in range(5):  # same registration message re-delivered 5 times
            assert asyncio.run(ob._extract_via_gemini("Name: A\nBusiness name: B")) is None
    assert calls["n"] == 1
    ob._onboarding_quota_cooldown_until = 0.0
