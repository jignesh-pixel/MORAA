"""Mock providers, Meta stubs, loop-lag monitor and helpers for the load harness.

Nothing here talks to a paid service. All latency is real ``sleep`` time scaled
by ``LATENCY_SCALE`` (real 15-40 s per image call -> 0.15-0.40 s at 1/100).
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import random
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.ai.providers.image_base import BaseImageGenerationProvider, ImageGenerationResult

LATENCY_SCALE = 0.01                 # 1/100
REAL_LATENCY_RANGE = (15.0, 40.0)    # seconds, per generate_image call
SCALED_LATENCY_RANGE = (REAL_LATENCY_RANGE[0] * LATENCY_SCALE, REAL_LATENCY_RANGE[1] * LATENCY_SCALE)

FAILURE_MODES = ("rate_limit_429", "resource_exhausted_429", "server_500", "timeout", "empty_no_image", "blank_success")

ERROR_TEXT = {
    "rate_limit_429": "429 Too Many Requests: rate limit reached, retry later",
    "resource_exhausted_429": "429 RESOURCE_EXHAUSTED. Quota exceeded for metric generate_content requests per minute",
    "server_500": "500 Internal Server Error from upstream",
    "timeout": "Request timeout: deadline exceeded",
    "empty_no_image": "Gemini returned a response but no image data was found.",
}


def make_noise_jpeg(target_mb: float = 3.0) -> bytes:
    """Return a valid JPEG close to ``target_mb`` MB (random noise is incompressible)."""
    from PIL import Image

    side = 1400
    for _ in range(8):
        img = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        size_mb = buf.tell() / (1024 * 1024)
        if abs(size_mb - target_mb) / target_mb < 0.12:
            return buf.getvalue()
        side = max(64, int(side * (target_mb / size_mb) ** 0.5))
    return buf.getvalue()


@dataclass
class ProviderStats:
    calls: int = 0
    successes: int = 0
    failures: int = 0
    inflight: int = 0
    max_inflight: int = 0
    by_mode: Dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def enter(self) -> None:
        with self.lock:
            self.calls += 1
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)

    def leave(self, ok: bool, mode: Optional[str]) -> None:
        with self.lock:
            self.inflight -= 1
            if ok:
                self.successes += 1
            else:
                self.failures += 1
            if mode:
                self.by_mode[mode] = self.by_mode.get(mode, 0) + 1


class FakeImageProvider(BaseImageGenerationProvider):
    """Deterministic-seeded fake image provider with injectable failures.

    ``thread_mode=True`` reproduces GeminiImageProvider, which runs a blocking
    SDK call through ``asyncio.to_thread`` (default executor, capped threads).
    ``output_bytes`` is the size of the "generated" image (real ~1.5-3 MB).
    """

    def __init__(
        self,
        name: str,
        fail_rate: float = 0.0,
        modes: Tuple[str, ...] = FAILURE_MODES,
        latency: Tuple[float, float] = SCALED_LATENCY_RANGE,
        thread_mode: bool = False,
        output_bytes: int = 64 * 1024,
        seed: int = 1234,
        hang: bool = False,
    ) -> None:
        self._name = name
        self.fail_rate = fail_rate
        self.modes = modes
        self.latency = latency
        self.thread_mode = thread_mode
        self.stats = ProviderStats()
        self._rng = random.Random(seed)
        self._rng_lock = threading.Lock()
        self._payload = os.urandom(output_bytes)
        self.hang = hang

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def provider_version(self) -> str:
        return "fake-1.0"

    def supports_reference_image(self) -> bool:
        return True

    def _draw(self) -> Tuple[float, Optional[str], int]:
        with self._rng_lock:
            lat = self._rng.uniform(*self.latency)
            mode = self._rng.choice(self.modes) if self._rng.random() < self.fail_rate else None
            salt = self._rng.randrange(256)
        return lat, mode, salt

    async def generate_image(self, prompt, context=None, reference_image=None, reference_mime_type="image/jpeg"):  # type: ignore[override]
        lat, mode, salt = self._draw()
        self.stats.enter()
        start = time.perf_counter()
        try:
            if self.hang:
                await asyncio.sleep(3600)
            if self.thread_mode:
                await asyncio.to_thread(time.sleep, lat)
            else:
                await asyncio.sleep(lat)
            elapsed = time.perf_counter() - start
            if mode == "timeout":
                self.stats.leave(False, mode)
                return ImageGenerationResult(success=False, error=ERROR_TEXT[mode], provider_name=self._name, processing_time=elapsed)
            if mode in ERROR_TEXT:
                self.stats.leave(False, mode)
                return ImageGenerationResult(success=False, error=ERROR_TEXT[mode], provider_name=self._name, processing_time=elapsed)
            if mode == "blank_success":
                self.stats.leave(True, mode)
                return ImageGenerationResult(
                    success=True, image_url="data:image/png;base64,", image_data=b"", provider_name=self._name, processing_time=elapsed
                )
            # Fresh allocation per result, like a real API response. The real providers return the raw bytes
            # only (no base64 data-URL copy, PERF-5), so the fake does the same.
            data = self._payload[:-1] + bytes([salt])
            self.stats.leave(True, None)
            return ImageGenerationResult(
                success=True, image_data=data, provider_name=self._name, processing_time=elapsed
            )
        except BaseException:
            self.stats.leave(False, "cancelled")
            raise


def install_fake_providers(primary: FakeImageProvider, fallback: FakeImageProvider) -> None:
    """Route every ImageGenerationManager instance to the fakes (names per settings chain)."""
    from app.ai import image_generation_manager as igm
    from app.config import settings

    p_name = settings.PRIMARY_IMAGE_PROVIDER.strip().lower()
    f_name = settings.FALLBACK_IMAGE_PROVIDER.strip().lower()
    primary._name, fallback._name = p_name, f_name

    def _init(self) -> None:  # type: ignore[no-untyped-def]
        self._providers = {p_name: primary, f_name: fallback}
        self._initialised = True

    igm.ImageGenerationManager._init_providers = _init  # type: ignore[assignment]
    igm._spend_day, igm._spend_count = None, 0


class LoopLagMonitor:
    """Measures asyncio loop scheduling delay with a 10 ms heartbeat."""

    def __init__(self, interval: float = 0.01) -> None:
        self.interval = interval
        self.lags: List[float] = []
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _run(self) -> None:
        while self._running:
            t0 = time.perf_counter()
            await asyncio.sleep(self.interval)
            self.lags.append(max(0.0, time.perf_counter() - t0 - self.interval))

    async def __aenter__(self) -> "LoopLagMonitor":
        self._running = True
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._running = False
        assert self._task is not None
        await self._task

    def summary(self) -> Dict[str, Any]:
        lags = sorted(self.lags) or [0.0]
        n = len(lags)
        return {
            "heartbeats": n,
            "lag_max_ms": round(lags[-1] * 1000, 1),
            "lag_p99_ms": round(lags[min(n - 1, int(n * 0.99))] * 1000, 1),
            "lag_mean_ms": round(statistics.mean(lags) * 1000, 2),
            "stalls_over_100ms": sum(1 for x in lags if x > 0.1),
            "stalled_total_s": round(sum(x for x in lags if x > 0.05), 2),
        }


class MetaRecorder:
    """Records every WhatsApp/Meta call the app would have made (cost/spam proxy)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.texts: List[Tuple[str, str]] = []
        self.button_msgs: List[str] = []
        self.pack_deliveries: List[Tuple[str, int]] = []
        self.media_uploads = 0
        self.media_lookups = 0
        self.media_downloads = 0
        self.cta_links = 0
        self.flows = 0

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]


def install_meta_stubs(rec: MetaRecorder, jpeg_bytes: bytes, io_latency: float = 0.02) -> None:
    """Replace every Meta/WhatsApp/Razorpay network helper in the app with a recorder."""
    from app.api.routes import meta_webhook as mw
    from app.services import meta_whatsapp_service as mws

    async def send_text(recipient_id, message_text=None, reply_to_message_id=None, *a, **k):  # type: ignore[no-untyped-def]
        await asyncio.sleep(io_latency)
        with rec.lock:
            rec.texts.append((str(recipient_id), str(message_text)))
        return True

    async def upload_media(image_bytes, mime_type="image/png"):  # type: ignore[no-untyped-def]
        await asyncio.sleep(io_latency)
        with rec.lock:
            rec.media_uploads += 1
            return f"media-{rec.media_uploads}"

    async def send_pack(recipient_id, image_urls, balance_text, reply_to_message_id=None):  # type: ignore[no-untyped-def]
        # Real code sleeps CATALOG_PACK_SEND_THROTTLE_SECONDS (0.8 s) between images; scaled here.
        await asyncio.sleep(len(image_urls) * 0.8 * LATENCY_SCALE)
        with rec.lock:
            rec.pack_deliveries.append((str(recipient_id), len(image_urls)))
        return len(image_urls)

    async def send_buttons(recipient_id, ingestion_id, white_price, pack_price, balance, reply_to_message_id=None):  # type: ignore[no-untyped-def]
        await asyncio.sleep(io_latency)
        with rec.lock:
            rec.button_msgs.append(str(ingestion_id))
        return True

    async def get_media_url(media_id):  # type: ignore[no-untyped-def]
        await asyncio.sleep(io_latency)
        with rec.lock:
            rec.media_lookups += 1
        return "http://localhost/fake-media"

    async def download_media(media_url):  # type: ignore[no-untyped-def]
        await asyncio.sleep(io_latency)
        with rec.lock:
            rec.media_downloads += 1
        return jpeg_bytes, "image/jpeg"

    async def no_native_recharge(*a, **k):  # type: ignore[no-untyped-def]
        return False

    async def send_flow(*a, **k):  # type: ignore[no-untyped-def]
        with rec.lock:
            rec.flows += 1
        return False

    async def cta(*a, **k):  # type: ignore[no-untyped-def]
        with rec.lock:
            rec.cta_links += 1
        return True

    async def pay_link(*a, **k):  # type: ignore[no-untyped-def]
        return "http://localhost/fake-pay"

    for mod in (mws, mw):
        for attr, fn in (
            ("send_whatsapp_text", send_text),
            ("upload_media_to_meta", upload_media),
            ("send_catalog_pack_images_to_whatsapp", send_pack),
            ("send_product_selection_buttons", send_buttons),
            ("get_media_url", get_media_url),
            ("download_media", download_media),
            ("try_send_native_recharge", no_native_recharge),
            ("send_registration_flow", send_flow),
            ("send_whatsapp_cta_url_button", cta),
            ("create_recharge_payment_link", pay_link),
        ):
            if hasattr(mod, attr):
                setattr(mod, attr, fn)


class SqlCounter:
    """Counts synchronous SQL statements + time spent inside them (blocks the loop)."""

    def __init__(self) -> None:
        from sqlalchemy import event

        from app.database import engine

        self.statements = 0
        self.seconds = 0.0
        self._local = threading.local()
        self._lock = threading.Lock()

        @event.listens_for(engine, "before_cursor_execute")
        def _before(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
            self._local.t0 = time.perf_counter()

        @event.listens_for(engine, "after_cursor_execute")
        def _after(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
            dt = time.perf_counter() - getattr(self._local, "t0", time.perf_counter())
            with self._lock:
                self.statements += 1
                self.seconds += dt

    def snapshot(self) -> Tuple[int, float]:
        return self.statements, self.seconds
