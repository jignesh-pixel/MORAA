"""No-cost load / failure simulation for the Moraa GemVision backend.

Run with the backend venv:

    backend\\venv\\Scripts\\python.exe backend\\tests\\load_scenarios\\run_load_scenarios.py [--only a,b,...]

Everything external is mocked (see harness_env.py / fakes.py). Application code
under backend/app is imported and exercised unmodified.
"""

from __future__ import annotations

import harness_env  # noqa: F401  (must be first: isolates env, DB, network)

import argparse
import asyncio
import base64
import collections
import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import fakes
from fakes import (
    FakeImageProvider,
    LATENCY_SCALE,
    LoopLagMonitor,
    MetaRecorder,
    SqlCounter,
    install_fake_providers,
    install_meta_stubs,
    make_noise_jpeg,
)

from loguru import logger

logger.remove()
LOG_COUNTS: Dict[str, int] = collections.Counter()
LOG_SAMPLES: Dict[str, str] = {}


def _log_sink(message) -> None:  # type: ignore[no-untyped-def]
    text = str(message.record["message"])
    level = message.record["level"].name
    key = f"{level}:{text[:70]}"
    LOG_COUNTS[key] += 1
    LOG_SAMPLES.setdefault(key, text[:200])
    if "locked" in text.lower():
        LOG_COUNTS["SQLITE_LOCK_ERRORS"] += 1


logger.add(_log_sink, level="WARNING")

from fastapi import BackgroundTasks, FastAPI  # noqa: E402
from sqlalchemy import func  # noqa: E402

from app.api.routes import meta_webhook as mw  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.audit_log import AuditLog  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.whatsapp_ingestion import WhatsAppIngestion  # noqa: E402
from app.services import meta_whatsapp_service as mws  # noqa: E402
from app.services.upload_service import UploadService  # noqa: E402
from app.services.wallet_service import charge_customer_balance, get_balance, price_per_image  # noqa: E402

RESULTS: Dict[str, Any] = {}
REC = MetaRecorder()
SMALL_JPEG = make_noise_jpeg(0.06)
_phone_counter = [0]
_shared_images: Dict[str, str] = {}


# ─── helpers ────────────────────────────────────────────────────────────


def new_phone() -> str:
    _phone_counter[0] += 1
    return f"9188{_phone_counter[0]:08d}"


def make_customer(wa_id: str, balance: int) -> str:
    db = SessionLocal()
    try:
        c = Customer(
            whatsapp_id=wa_id, full_name="Load Sim", business_name="LoadSim Jewels",
            gst_number="N/A", address="Sim City", wallet_balance=balance, is_registered=True,
        )
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


async def stored_image(jpeg: bytes, key: str = "small") -> str:
    """One real Image row + file per (key); ingestions share it (worker only reads the file)."""
    if key in _shared_images:
        return _shared_images[key]
    db = SessionLocal()
    try:
        r = await UploadService(db).process_upload(
            file_data=jpeg, filename=f"{key}.jpg", file_size=len(jpeg), mime_type="image/jpeg"
        )
        _shared_images[key] = r.id
        return r.id
    finally:
        db.close()


def make_ingestions(wa_id: str, n: int, image_id: str, mime: str = "image/jpeg") -> List[str]:
    db = SessionLocal()
    try:
        rows = [
            WhatsAppIngestion(
                external_user_id=wa_id, external_message_id=f"wamid.{uuid.uuid4().hex}",
                external_media_id="m", channel="whatsapp", image_id=image_id, mime_type=mime,
                status="awaiting_choice", file_size=1,
            )
            for _ in range(n)
        ]
        db.add_all(rows)
        db.commit()
        return [r.id for r in rows]
    finally:
        db.close()


async def place_and_run(sender: str, ingestion_id: str, button: str = "gv_pack1") -> Optional[bool]:
    """Product-choice tap (real charge path) then the queued worker (real pack path)."""
    db = SessionLocal()
    try:
        job = await mw._handle_product_choice(db, sender, button, ingestion_id)
    finally:
        db.close()
    if not job:
        return None
    worker, wid = job
    return await worker(wid)


def refunds_for(db, ids: List[str]) -> Tuple[int, int]:
    rows = db.query(AuditLog).filter(AuditLog.action == mws.REFUND_AUDIT_ACTION, AuditLog.resource_id.in_(ids)).all()
    total = 0
    for r in rows:
        try:
            total += int(json.loads(r.details or "{}").get("amount", 0))
        except Exception:
            pass
    return len(rows), total


def reconcile(customers: Dict[str, int]) -> Dict[str, Any]:
    """balance == initial - sum(amount_charged) + refunds, and never negative."""
    db = SessionLocal()
    try:
        statuses: collections.Counter = collections.Counter()
        per: Dict[str, Any] = {}
        all_ok = True
        for wa, initial in customers.items():
            rows = db.query(WhatsAppIngestion).filter(WhatsAppIngestion.external_user_id == wa).all()
            for r in rows:
                statuses[r.status] += 1
            charged = sum(int(r.amount_charged or 0) for r in rows)
            _, refunded = refunds_for(db, [r.id for r in rows])
            bal = get_balance(db, wa)
            ok = bal == initial - charged + refunded and bal >= 0
            all_ok &= ok
            per[wa] = {"initial": initial, "charged": charged, "refunded": refunded, "balance": bal, "reconciled": ok}
        return {"statuses": dict(statuses), "per_customer": per, "all_reconciled": all_ok}
    finally:
        db.close()


def pct(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


SQL = SqlCounter()


async def run_pack_orders(orders: List[Tuple[str, str]]) -> Dict[str, Any]:
    st0, sq0 = SQL.snapshot()
    t0 = time.perf_counter()
    async with LoopLagMonitor() as mon:
        results = await asyncio.gather(*[place_and_run(s, i) for s, i in orders], return_exceptions=True)
    wall = time.perf_counter() - t0
    st1, sq1 = SQL.snapshot()
    n = max(len(orders), 1)
    return {
        "orders": len(orders),
        "wall_s": round(wall, 2),
        "wall_s_x100_naive": round(wall / LATENCY_SCALE, 1),
        "worker_true": sum(1 for r in results if r is True),
        "worker_false": sum(1 for r in results if r is False),
        "declined_no_worker": sum(1 for r in results if r is None),
        "exceptions": [repr(r) for r in results if isinstance(r, BaseException)][:3],
        "loop_lag": mon.summary(),
        "sync_sql_statements": st1 - st0,
        "sync_sql_per_order": round((st1 - st0) / n, 1),
        "sync_sql_total_s": round(sq1 - sq0, 3),
    }


def provider_view(p: FakeImageProvider) -> Dict[str, Any]:
    s = p.stats
    return {"calls": s.calls, "ok": s.successes, "fail": s.failures, "max_inflight": s.max_inflight, "fail_modes": dict(s.by_mode)}


def fresh_providers(fail_rate: float = 0.0, thread_mode: bool = False, output_bytes: int = 64 * 1024, modes=fakes.FAILURE_MODES, seed: int = 7):
    primary = FakeImageProvider("primary", fail_rate=fail_rate, modes=modes, thread_mode=thread_mode, output_bytes=output_bytes, seed=seed)
    fallback = FakeImageProvider("fallback", fail_rate=fail_rate, modes=modes, thread_mode=thread_mode, output_bytes=output_bytes, seed=seed + 1)
    install_fake_providers(primary, fallback)
    return primary, fallback


async def setup_pack(n_orders: int, balance: Optional[int] = None) -> Tuple[str, List[str], int]:
    wa = new_phone()
    price = price_per_image()
    bal = balance if balance is not None else price * n_orders
    make_customer(wa, bal)
    image_id = await stored_image(SMALL_JPEG, "small")
    return wa, make_ingestions(wa, n_orders, image_id), bal


def verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


# ─── (a)/(b)/(c) throughput scenarios ───────────────────────────────────


async def scenario_a() -> Dict[str, Any]:
    REC.reset()
    price = price_per_image()
    primary, fallback = fresh_providers()
    # a1: a single generate_image() call through the real manager
    from app.ai.image_generation_manager import ImageGenerationManager

    t0 = time.perf_counter()
    r = await ImageGenerationManager().generate_image("p", {"request_id": "a1"}, reference_image=SMALL_JPEG)
    single = {"success": r.success, "wall_s": round(time.perf_counter() - t0, 3)}
    # a2: one paid pack (tap -> charge -> 6 styles -> delivery)
    primary, fallback = fresh_providers()
    wa, ids, bal = await setup_pack(1)
    out = await run_pack_orders([(wa, ids[0])])
    rec = reconcile({wa: bal})
    ok = rec["statuses"].get("delivered") == 1 and rec["all_reconciled"] and primary.stats.calls == len(mws.CATALOG_PACK_STYLES)
    return {
        "verdict": verdict(ok), "single_generate_image": single, "pack": out, "provider_primary": provider_view(primary),
        "reconcile": rec, "meta_deliveries": list(REC.pack_deliveries),
        "note": f"pack = {len(mws.CATALOG_PACK_STYLES)} styles fanned out with asyncio.gather (CATALOG_PACK_STYLES); price Rs {price}",
    }


async def scenario_b() -> Dict[str, Any]:
    REC.reset()
    primary, fallback = fresh_providers()
    wa, ids, bal = await setup_pack(10)
    out = await run_pack_orders([(wa, i) for i in ids])
    rec = reconcile({wa: bal})
    ok = rec["statuses"].get("delivered") == 10 and rec["all_reconciled"]
    return {
        "verdict": verdict(ok), "pack": out, "provider_primary": provider_view(primary), "reconcile": rec,
        "note": "functional PASS; provider concurrency is uncapped (max_inflight == 10 orders x 6 styles)",
    }


async def scenario_c() -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    from app.ai.image_generation_manager import ImageGenerationManager

    async def manager_fanout(images: int, outputs: int, thread_mode: bool) -> Dict[str, Any]:
        primary, fallback = fresh_providers(thread_mode=thread_mode)
        async def one_image() -> List[bool]:
            r = await asyncio.gather(*[
                ImageGenerationManager().generate_image("p", {"request_id": uuid.uuid4().hex}, reference_image=SMALL_JPEG, spend_reserved=True)
                for _ in range(outputs)
            ])
            return [x.success for x in r]
        t0 = time.perf_counter()
        async with LoopLagMonitor() as mon:
            results = await asyncio.gather(*[one_image() for _ in range(images)])
        wall = time.perf_counter() - t0
        total = images * outputs
        lo, hi = fakes.SCALED_LATENCY_RANGE
        ideal = (lo + hi) / 2 * math.ceil(total / max(primary.stats.max_inflight, 1))
        import os
        return {
            "calls": total, "success": sum(sum(r) for r in results), "wall_s": round(wall, 2),
            "wall_s_x100_naive": round(wall / LATENCY_SCALE, 0),
            "provider_max_inflight": primary.stats.max_inflight,
            "ideal_unbounded_wall_s": round(hi, 2), "loop_lag": mon.summary(), "cpu_count": os.cpu_count(),
        }

    res["c1_async_provider_100x8"] = await manager_fanout(100, 8, thread_mode=False)
    # The real GeminiImageProvider is native async now (no asyncio.to_thread, PERF-3), proved by
    # tests/test_provider_hardening.py::GeminiNativeAsyncTests, so it behaves like the async provider. c2 still
    # runs the OLD thread-backed model, kept only as a reference for what the default executor would cost.
    res["c2_thread_provider_100x8_legacy_model_for_reference"] = await manager_fanout(100, 8, thread_mode=True)
    t = res["c2_thread_provider_100x8_legacy_model_for_reference"]
    t["default_executor_max_workers"] = min(32, (t["cpu_count"] or 1) + 4)
    t["slowdown_vs_unbounded"] = round(t["wall_s"] / max(res["c1_async_provider_100x8"]["wall_s"], 1e-6), 1)

    # c3: real paid-pack path, 100 orders at once (6 styles each; 8 outputs is not what the code builds)
    REC.reset()
    primary, fallback = fresh_providers()
    wa, ids, bal = await setup_pack(100)
    out = await run_pack_orders([(wa, i) for i in ids])
    rec = reconcile({wa: bal})
    res["c3_real_pack_path_100_orders"] = {"pack": out, "provider_primary": provider_view(primary), "reconcile": rec}

    # c4: daily spend cap under the same burst (all-or-nothing reservation)
    from app.ai import image_generation_manager as igm

    old_cap = settings.MAX_GENERATIONS_PER_DAY
    settings.MAX_GENERATIONS_PER_DAY = 100
    try:
        primary, fallback = fresh_providers()
        wa2, ids2, bal2 = await setup_pack(30)
        out2 = await run_pack_orders([(wa2, i) for i in ids2])
        rec2 = reconcile({wa2: bal2})
        res["c4_spend_cap_100_calls_30_packs"] = {
            "pack": out2, "provider_calls": primary.stats.calls, "reconcile": rec2,
            "expected": "16 packs fit (16x6=96 <= 100); 14 refused BEFORE any provider call and refunded",
        }
    finally:
        settings.MAX_GENERATIONS_PER_DAY = old_cap
        igm._spend_day, igm._spend_count = None, 0

    c1, c2, c3 = res["c1_async_provider_100x8"], res["c2_thread_provider_100x8_legacy_model_for_reference"], res["c3_real_pack_path_100_orders"]
    c4 = res["c4_spend_cap_100_calls_30_packs"]
    functional = (
        c1["success"] == 800 and c2["success"] == 800 and rec["all_reconciled"]
        and rec["statuses"].get("delivered") == 100 and c4["reconcile"]["all_reconciled"]
    )
    # The real providers are native async (no worker threads), so the async model (c1) is the one that must
    # stay close to the unbounded ideal: 800 calls all overlapping.
    throughput_ok = c1["wall_s"] < 3 * c1["ideal_unbounded_wall_s"]
    res["verdict"] = "FAIL" if not functional else ("WARN" if not throughput_ok else "PASS")
    res["verdict_reason"] = (
        f"functional={'ok' if functional else 'broken'}; 800 async provider calls took {c1['wall_s']}s "
        f"(unbounded ideal {c1['ideal_unbounded_wall_s']}s); the OLD thread-backed model, kept for reference, "
        f"took {c2['wall_s']}s ({c2['slowdown_vs_unbounded']}x) on the default executor ({c2['default_executor_max_workers']} threads)"
    )
    return res


# ─── (d) five customers concurrently (+ webhook rate limit) ─────────────


async def scenario_d() -> Dict[str, Any]:
    REC.reset()
    primary, fallback = fresh_providers()
    price = price_per_image()
    plan = [10, 8, 6, 4, 2]  # funded orders each customer can afford, of 10 photos each
    customers: Dict[str, int] = {}
    orders: List[Tuple[str, str]] = []
    image_id = await stored_image(SMALL_JPEG, "small")
    for funded in plan:
        wa = new_phone()
        bal = funded * price + 137  # leave a remainder to catch rounding/bleed
        make_customer(wa, bal)
        customers[wa] = bal
        for iid in make_ingestions(wa, 10, image_id):
            orders.append((wa, iid))
    # interleave customers to maximise cross-talk opportunity
    orders.sort(key=lambda o: o[1])
    out = await run_pack_orders(orders)
    rec = reconcile(customers)
    expected_funded = sum(plan)
    delivered = rec["statuses"].get("delivered", 0)
    awaiting = rec["statuses"].get("awaiting_choice", 0)
    deliveries_by_recipient = collections.Counter(r for r, _ in REC.pack_deliveries)
    isolation = all(deliveries_by_recipient.get(wa, 0) == f for wa, f in zip(customers, plan))
    ok = delivered == expected_funded and awaiting == 50 - expected_funded and rec["all_reconciled"] and isolation
    # d2: RateLimitMiddleware seen from Meta's (single) source IP
    rl = await rate_limit_probe()
    return {
        "verdict": verdict(ok and rl["ok_count"] == 150),
        "verdict_reason": (
            f"isolation/funding correct={ok}; but RateLimitMiddleware keys on client IP ({rl['ok_count']}/150 webhook POSTs from one Meta egress IP "
            f"accepted, {rl['rejected_429']} got 429): all customers share one bucket"
        ),
        "pack": out, "provider_primary": provider_view(primary),
        "expected_funded_total": expected_funded, "delivered": delivered, "held_awaiting_choice": awaiting,
        "per_recipient_deliveries_match_funding": isolation, "reconcile": rec,
        "rate_limit_probe_meta_ip": rl,
    }


async def rate_limit_probe() -> Dict[str, Any]:
    import httpx
    from app.middleware.rate_limit import RateLimitMiddleware

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.post("/api/meta/webhook")
    async def hook() -> Dict[str, str]:
        return {"status": "ok"}

    codes: collections.Counter = collections.Counter()
    transport = httpx.ASGITransport(app=app, client=("157.240.22.35", 443))  # a Meta egress IP, shared by all customers
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        for _ in range(150):
            r = await c.post("/api/meta/webhook", json={})
            codes[r.status_code] += 1
    return {
        "requests_from_one_ip_in_window": 150, "limit": settings.RATE_LIMIT_REQUESTS, "window_s": settings.RATE_LIMIT_WINDOW_SECONDS,
        "status_codes": dict(codes), "ok_count": codes.get(200, 0), "rejected_429": codes.get(429, 0),
    }


# ─── (e) provider failure injection ─────────────────────────────────────


async def scenario_e() -> Dict[str, Any]:
    # The harness runs at 1/LATENCY_SCALE of real time, so the real timeout and backoff settings are scaled the
    # same way: a 90 s real provider timeout is 0.9 s here, and the 2 s retry backoff is 0.02 s.
    names = ("IMAGE_PROVIDER_TIMEOUT_SECONDS", "IMAGE_RETRY_BACKOFF_BASE_SECONDS", "IMAGE_RETRY_BACKOFF_CAP_SECONDS")
    saved = {n: getattr(settings, n) for n in names}
    settings.IMAGE_PROVIDER_TIMEOUT_SECONDS = 90 * LATENCY_SCALE
    settings.IMAGE_RETRY_BACKOFF_BASE_SECONDS = 2 * LATENCY_SCALE
    settings.IMAGE_RETRY_BACKOFF_CAP_SECONDS = 15 * LATENCY_SCALE
    try:
        return await _scenario_e_body()
    finally:
        for n, v in saved.items():
            setattr(settings, n, v)


async def _scenario_e_body() -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    from app.ai.image_generation_manager import ImageGenerationManager

    # e1: per-mode behaviour of the real manager (both providers fail at the same rate, independently)
    table: List[Dict[str, Any]] = []
    for rate in (0.10, 0.20, 0.30):
        for mode in fakes.FAILURE_MODES:
            primary, fallback = fresh_providers(fail_rate=rate, modes=(mode,), seed=int(rate * 100) + hash(mode) % 50)
            primary.latency = fallback.latency = (0.005, 0.015)
            n = 600
            results = await asyncio.gather(*[
                ImageGenerationManager().generate_image("p", {"request_id": str(i)}, reference_image=SMALL_JPEG, spend_reserved=True)
                for i in range(n)
            ])
            ok = sum(1 for r in results if r.success and len(r.as_bytes() or b"") > 30)
            blank = sum(1 for r in results if r.success and not len(r.as_bytes() or b"") > 30)
            table.append({
                "mode": mode, "rate": rate, "calls": n, "usable": ok, "silent_blank_success": blank,
                "failed_after_manager": sum(1 for r in results if not r.success),
                "fallback_calls": fallback.stats.calls, "fallback_used_ok": sum(1 for r in results if r.success and r.fallback_used),
                "usable_pct": round(100 * ok / n, 1),
            })
    res["e1_per_mode"] = table

    # e2: end-to-end paid packs with a mixed-fault provider (wallet integrity + customer impact)
    e2: List[Dict[str, Any]] = []
    for rate in (0.10, 0.20, 0.30):
        REC.reset()
        primary, fallback = fresh_providers(fail_rate=rate, seed=int(rate * 1000))
        primary.latency = fallback.latency = (0.02, 0.06)
        wa, ids, bal = await setup_pack(60)
        out = await run_pack_orders([(wa, i) for i in ids])
        rec = reconcile({wa: bal})
        st = rec["statuses"]
        partial = st.get("delivered_partial", 0)
        stuck = sum(v for k, v in st.items() if k in ("processing", "generated", "pack_queued"))
        e2.append({
            "fault_rate": rate, "orders": 60, "statuses": st, "paid_for_full_pack_but_got_partial": partial,
            "stuck_orders": stuck, "provider_primary": provider_view(primary), "provider_fallback": provider_view(fallback),
            "wallet_reconciled": rec["all_reconciled"], "refund_rows": refunds_for(SessionLocal(), ids)[0],
            "wall_s": out["wall_s"], "worker_exceptions": out["exceptions"],
        })
    res["e2_end_to_end_packs"] = e2

    # e3: a hung provider (no client timeout is set anywhere in the generation path)
    REC.reset()
    hang_primary = FakeImageProvider("primary", hang=True)
    ok_fallback = FakeImageProvider("fallback")
    install_fake_providers(hang_primary, ok_fallback)
    wa, ids, bal = await setup_pack(1)
    task = asyncio.create_task(place_and_run(wa, ids[0]))
    # The order must finish on its own once the hung provider is cut off at the timeout and the fallback
    # serves it: allow 300 s real-equivalent (at 1/100 scale), i.e. 90 s timeout + fallback + delivery + slack.
    await asyncio.wait({task}, timeout=3.0)
    still_running = not task.done()
    db = SessionLocal()
    status = db.query(WhatsAppIngestion.status).filter(WhatsAppIngestion.id == ids[0]).scalar()
    balance_now = get_balance(db, wa)
    db.close()
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    res["e3_hung_provider"] = {
        "still_running_after_3s_scaled": still_running, "ingestion_status": status, "wallet_balance_now": balance_now, "initial_balance": bal,
        "fallback_calls": ok_fallback.stats.calls,
        "meaning": "a hung provider is cut off by the manager's deadline (IMAGE_PROVIDER_TIMEOUT_SECONDS) and the fallback serves the order",
    }

    quota_row = [r for r in table if r["mode"] == "resource_exhausted_429" and r["rate"] == 0.20][0]
    plain_row = [r for r in table if r["mode"] == "rate_limit_429" and r["rate"] == 0.20][0]
    res["verdict"] = "FAIL" if (quota_row["fallback_calls"] == 0 or any(not x["wallet_reconciled"] or x["stuck_orders"] for x in e2) or still_running) else "PASS"
    res["verdict_reason"] = (
        f"wallet reconciled in all mixed-fault runs={all(x['wallet_reconciled'] for x in e2)}; "
        f"429 RESOURCE_EXHAUSTED (Gemini's real 429 text) is retried then falls back: {quota_row['fallback_calls']} fallback "
        f"call(s), usable {quota_row['usable_pct']}% vs {plain_row['usable_pct']}% for a plain '429 rate limit' at 20%; "
        f"hung provider leaves order processing={still_running}"
    )
    return res


# ─── (f) duplicate webhook delivery ─────────────────────────────────────


class FakeRequest:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode()
        self.headers: Dict[str, str] = {}

    async def body(self) -> bytes:
        return self._body


def webhook_payload(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": "1"}, "messages": messages}}]}],
    }


def image_msg(sender: str, msg_id: str) -> Dict[str, Any]:
    return {"from": sender, "id": msg_id, "timestamp": "1", "type": "image", "image": {"id": "media-1", "mime_type": "image/jpeg"}}


def text_msg(sender: str, msg_id: str, body: str) -> Dict[str, Any]:
    return {"from": sender, "id": msg_id, "timestamp": "1", "type": "text", "text": {"body": body}}


def button_msg(sender: str, msg_id: str, button_id: str) -> Dict[str, Any]:
    return {"from": sender, "id": msg_id, "timestamp": "1", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "x"}}}


async def deliver(payload: Dict[str, Any]) -> Tuple[Any, BackgroundTasks]:
    db = SessionLocal()
    bg = BackgroundTasks()
    try:
        resp = await mw.receive_webhook(FakeRequest(payload), bg, db)
    finally:
        db.close()
    return resp, bg


def deliver_in_thread(payload: Dict[str, Any]) -> Tuple[Any, BackgroundTasks]:
    return asyncio.run(deliver(payload))


def parallel_threads(fn: Callable[[int], Any], n: int) -> List[Any]:
    barrier = threading.Barrier(n)

    def wrapped(i: int) -> Any:
        barrier.wait()
        try:
            return fn(i)
        except BaseException as exc:  # noqa: BLE001
            return exc

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(wrapped, range(n)))


def count_ingestions(msg_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(func.count(WhatsAppIngestion.id)).filter(WhatsAppIngestion.external_message_id == msg_id).scalar() or 0
    finally:
        db.close()


async def scenario_f() -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    install_meta_stubs(REC, SMALL_JPEG)
    price = price_per_image()

    # f1: 20 concurrent copies inside ONE event loop (single uvicorn worker)
    REC.reset()
    wa = new_phone(); make_customer(wa, price * 5)
    mid = f"wamid.DUP-LOOP-{uuid.uuid4().hex[:8]}"
    t0 = time.perf_counter()
    outs = await asyncio.gather(*[deliver(webhook_payload([image_msg(wa, mid)])) for _ in range(20)], return_exceptions=True)
    res["f1_same_loop_x20"] = {
        "ingestion_rows": count_ingestions(mid), "button_messages_sent": len(REC.button_msgs),
        "media_lookups(meta_api_calls)": REC.media_lookups, "media_downloads": REC.media_downloads,
        "exceptions": [repr(o) for o in outs if isinstance(o, BaseException)][:3], "wall_s": round(time.perf_counter() - t0, 2),
    }

    # f2: 20 concurrent copies across 20 threads/loops/sessions (= multi-worker deployment)
    REC.reset()
    wa = new_phone(); make_customer(wa, price * 5)
    mid = f"wamid.DUP-THREAD-{uuid.uuid4().hex[:8]}"
    outs = parallel_threads(lambda i: deliver_in_thread(webhook_payload([image_msg(wa, mid)])), 20)
    res["f2_multi_worker_threads_x20"] = {
        "ingestion_rows": count_ingestions(mid), "button_messages_sent": len(REC.button_msgs),
        "media_lookups(meta_api_calls)": REC.media_lookups, "media_downloads": REC.media_downloads,
        "exceptions": [repr(o) for o in outs if isinstance(o, BaseException)][:3],
    }

    # f3: sequential Meta retries of an already-stored photo
    REC.reset()
    wa = new_phone(); make_customer(wa, price * 5)
    mid = f"wamid.DUP-SEQ-{uuid.uuid4().hex[:8]}"
    for _ in range(5):
        await deliver(webhook_payload([image_msg(wa, mid)]))
    res["f3_sequential_x5"] = {"ingestion_rows": count_ingestions(mid), "button_messages_sent": len(REC.button_msgs), "media_lookups": REC.media_lookups}

    # f4: same button tap delivered 20x concurrently across workers
    REC.reset()
    wa = new_phone(); bal = price * 5; make_customer(wa, bal)
    image_id = await stored_image(SMALL_JPEG, "small")
    iid = make_ingestions(wa, 1, image_id)[0]
    tap = webhook_payload([button_msg(wa, "wamid.TAP-DUP", f"{mws.PRODUCT_BUTTON_PACK_1}:{iid}")])
    outs = parallel_threads(lambda i: deliver_in_thread(tap), 20)
    jobs = sum(len(o[1].tasks) for o in outs if not isinstance(o, BaseException))
    db = SessionLocal(); final_bal = get_balance(db, wa); db.close()
    res["f4_duplicate_button_tap_threads_x20"] = {
        "background_jobs_enqueued": jobs, "balance_before": bal, "balance_after": final_bal, "debits": (bal - final_bal) // price,
        "already_chosen_notices": sum(1 for _, t in REC.texts if t == mw.ALREADY_CHOSEN_MESSAGE),
        "exceptions": [repr(o) for o in outs if isinstance(o, BaseException)][:3],
    }

    # f5: duplicate delivery of a plain text message (no message-id dedup exists for text)
    REC.reset()
    wa = new_phone(); make_customer(wa, 0)
    for _ in range(3):
        await deliver(webhook_payload([text_msg(wa, "wamid.HI-DUP", "hi")]))
    welcomes = sum(1 for _, t in REC.texts if t == mw.WELCOME_MESSAGE)
    for _ in range(3):
        await deliver(webhook_payload([text_msg(wa, "wamid.PAY-DUP", "recharge 500")]))
    res["f5_duplicate_text_x3"] = {"welcome_messages_sent_for_3_identical_deliveries": welcomes, "recharge_links_sent_for_3_identical_deliveries": REC.cta_links}

    # f6: duplicate WORKER start for the same paid order across two workers (retry endpoint / restart)
    for label, n in (("f6_duplicate_worker_start_same_loop_x2", 0), ("f6_duplicate_worker_start_threads_x4", 4)):
        primary, fallback = fresh_providers()
        REC.reset()
        wa = new_phone(); make_customer(wa, price)
        image_id = await stored_image(SMALL_JPEG, "small")
        iid = make_ingestions(wa, 1, image_id)[0]
        db = SessionLocal()
        db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == iid).update({"status": "pack_queued", "amount_charged": price, "product_code": "PACK_1"})
        db.commit(); db.close()
        if n == 0:
            await asyncio.gather(mws.process_whatsapp_catalog_pack(iid), mws.process_whatsapp_catalog_pack(iid))
        else:
            parallel_threads(lambda i: asyncio.run(mws.process_whatsapp_catalog_pack(iid)), n)
        res[label] = {"provider_calls": primary.stats.calls, "expected_calls": len(mws.CATALOG_PACK_STYLES), "pack_deliveries": len(REC.pack_deliveries)}

    ok = (
        res["f1_same_loop_x20"]["ingestion_rows"] == 1 and res["f1_same_loop_x20"]["button_messages_sent"] == 1
        and res["f2_multi_worker_threads_x20"]["ingestion_rows"] == 1 and res["f2_multi_worker_threads_x20"]["button_messages_sent"] == 1
        and res["f3_sequential_x5"]["button_messages_sent"] == 1
        and res["f4_duplicate_button_tap_threads_x20"]["debits"] == 1
    )
    text_ok = welcomes == 1
    dup_worker_ok = res["f6_duplicate_worker_start_threads_x4"]["provider_calls"] == len(mws.CATALOG_PACK_STYLES)
    res["verdict"] = verdict(ok and text_ok and dup_worker_ok)
    res["verdict_reason"] = (
        f"image + button dedup {'holds' if ok else 'BROKEN'}; text messages "
        f"{'deduped' if text_ok else 'NOT deduped'} (welcomes={welcomes} for 3 identical deliveries); "
        f"duplicate worker start across threads/workers made {res['f6_duplicate_worker_start_threads_x4']['provider_calls']} "
        f"provider calls (expected {len(mws.CATALOG_PACK_STYLES)})"
    )
    return res


# ─── (g) wallet concurrency ─────────────────────────────────────────────


async def scenario_g() -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    install_meta_stubs(REC, SMALL_JPEG)
    price = price_per_image()
    funded, attempts = 37, 100

    # g1: 100 simultaneous debits (own session+thread each) on one customer funded for 37
    for label, workers in (("g1_100_debits_100_threads", 100), ("g1b_100_debits_16_threads", 16)):
        wa = new_phone(); bal = funded * price + price // 2
        cid = make_customer(wa, bal)
        LOG_COUNTS["SQLITE_LOCK_ERRORS"] = 0
        barrier = threading.Barrier(workers)

        def debit(i: int) -> bool:
            db = SessionLocal()
            try:
                cust = db.get(Customer, cid)
                barrier.wait() if i < workers else None
                ok, _ = charge_customer_balance(db, cust, price)
                return ok
            finally:
                db.close()

        # 100 attempts over `workers` threads; barrier only gates the first wave
        with ThreadPoolExecutor(max_workers=workers) as pool:
            oks = list(pool.map(debit, range(attempts)))
        db = SessionLocal(); final = get_balance(db, wa); db.close()
        res[label] = {
            "attempts": attempts, "balance_before": bal, "funded_for": funded, "charged": sum(oks), "declined": attempts - sum(oks),
            "balance_after": final, "negative": final < 0, "money_conserved": bal - sum(oks) * price == final,
            "wrongly_declined_when_funds_left": max(0, funded - sum(oks)), "sqlite_lock_errors": LOG_COUNTS["SQLITE_LOCK_ERRORS"],
        }

    # g2: the real tap handler, 100 photos of one customer tapped simultaneously, funded for 37 packs
    wa = new_phone(); bal = funded * price + price // 2
    make_customer(wa, bal)
    image_id = await stored_image(SMALL_JPEG, "small")
    ids = make_ingestions(wa, attempts, image_id)
    LOG_COUNTS["SQLITE_LOCK_ERRORS"] = 0
    REC.reset()

    async def tap(iid: str) -> bool:
        db = SessionLocal()
        try:
            return (await mw._handle_product_choice(db, wa, "gv_pack1", iid)) is not None
        finally:
            db.close()

    outs = parallel_threads(lambda i: asyncio.run(tap(ids[i])), attempts)
    queued = sum(1 for o in outs if o is True)
    rec = reconcile({wa: bal})
    res["g2_tap_handler_100_threads"] = {
        "queued": queued, "funded_for": funded, "statuses": rec["statuses"], "balance_after": rec["per_customer"][wa]["balance"],
        "reconciled": rec["all_reconciled"], "exceptions": [repr(o) for o in outs if isinstance(o, BaseException)][:3],
        "sqlite_lock_errors": LOG_COUNTS["SQLITE_LOCK_ERRORS"],
    }

    # g3: same 100 taps inside one event loop (single-worker deployment)
    wa = new_phone(); bal = funded * price + price // 2
    make_customer(wa, bal)
    ids = make_ingestions(wa, attempts, image_id)
    outs2 = await asyncio.gather(*[tap2(wa, i) for i in ids], return_exceptions=True)
    rec2 = reconcile({wa: bal})
    res["g3_tap_handler_one_loop"] = {
        "queued": sum(1 for o in outs2 if o is True), "funded_for": funded, "statuses": rec2["statuses"],
        "balance_after": rec2["per_customer"][wa]["balance"], "reconciled": rec2["all_reconciled"],
    }

    # g4: refund idempotency race (check-then-refund-then-audit, 3 statements, no lock)
    wa = new_phone(); make_customer(wa, 0)
    image_id = await stored_image(SMALL_JPEG, "small")
    iid = make_ingestions(wa, 1, image_id)[0]
    db = SessionLocal()
    db.query(WhatsAppIngestion).filter(WhatsAppIngestion.id == iid).update({"status": "failed", "amount_charged": price})
    db.commit(); db.close()

    def refund(i: int) -> None:
        db = SessionLocal()
        try:
            ing = db.get(WhatsAppIngestion, iid)
            mws._refund_failed_ingestion(db, ing)
        finally:
            db.close()

    parallel_threads(refund, 20)
    db = SessionLocal(); bal_after = get_balance(db, wa); n_audit, _ = refunds_for(db, [iid]); db.close()
    res["g4_refund_race_20_threads"] = {
        "order_charge": price, "wallet_after_20_concurrent_refunds": bal_after, "audit_rows": n_audit,
        "over_refund": bal_after - price, "expected_wallet": price,
    }

    g1, g1b, g2, g3, g4 = res["g1_100_debits_100_threads"], res["g1b_100_debits_16_threads"], res["g2_tap_handler_100_threads"], res["g3_tap_handler_one_loop"], res["g4_refund_race_20_threads"]
    debit_ok = all(x["charged"] == funded and not x["negative"] and x["money_conserved"] for x in (g1, g1b))
    tap_ok = g2["queued"] == funded and g2["reconciled"] and g3["queued"] == funded and g3["reconciled"]
    refund_ok = g4["over_refund"] == 0
    res["verdict"] = verdict(debit_ok and tap_ok and refund_ok)
    res["verdict_reason"] = (
        f"guarded debit {'correct' if debit_ok else 'INCORRECT'} (charged {g1['charged']}/{funded}); tap handler {'correct' if tap_ok else 'INCORRECT'}; "
        f"refund double-credit race: wallet {g4['wallet_after_20_concurrent_refunds']} vs expected {price} ({g4['audit_rows']} audit row)"
    )
    return res


async def tap2(wa: str, iid: str) -> bool:
    db = SessionLocal()
    try:
        return (await mw._handle_product_choice(db, wa, "gv_pack1", iid)) is not None
    finally:
        db.close()


# ─── (h) event-loop blocking ────────────────────────────────────────────


async def lag_run(label: str, coro_factory: Callable[[], Awaitable[Any]]) -> Dict[str, Any]:
    # Collect the previous probe's multi-MB garbage BEFORE measuring, so a garbage-collection pause caused by
    # an earlier probe is not blamed on this one.
    import gc

    gc.collect()
    gc.freeze()           # like the application after startup (see app/main.py): long-lived objects are not rescanned
    t0 = time.perf_counter()
    async with LoopLagMonitor() as mon:
        await coro_factory()
    out = mon.summary()
    out["work_wall_s"] = round(time.perf_counter() - t0, 2)
    return out


async def scenario_h() -> Dict[str, Any]:
    from app.services.image_quality_floor import check_quality_floor
    from app.services.preprocessing_service import PreprocessingService

    res: Dict[str, Any] = {}
    big = make_noise_jpeg(3.0)
    res["test_image_mb"] = round(len(big) / 1048576, 2)
    work = Path(harness_env.WORKDIR) / "h_files"
    work.mkdir(exist_ok=True)
    paths = []
    for i in range(8):
        p = work / f"img{i}.jpg"
        p.write_bytes(big)
        paths.append(str(p))

    async def idle() -> None:
        await asyncio.sleep(0.5)

    res["h0_baseline_idle"] = await lag_run("idle", idle)

    svc = PreprocessingService()
    res["h1_PreprocessingService.preprocess_x8_async_wrapper_of_sync_PIL"] = await lag_run(
        "pre", lambda: asyncio.gather(*[svc.preprocess(p, str(work / "out")) for p in paths])
    )

    async def validate_direct() -> None:
        async def one() -> None:
            mws.validate_image(big, "image/jpeg")
        await asyncio.gather(*[one() for _ in range(50)])

    async def validate_thread() -> None:
        await asyncio.gather(*[asyncio.to_thread(mws.validate_image, big, "image/jpeg") for _ in range(50)])

    # "legacy" entries run the blocking function directly, as the code used to; they are kept only as a reference
    # for what the stall would be and are NOT part of the verdict. The "real path" entries below run the actual
    # webhook / route code, which now moves this work onto the CPU pool.
    res["h2_legacy_direct_validate_image_x50_reference"] = await lag_run("v", validate_direct)
    res["h2b_same_work_via_to_thread"] = await lag_run("vt", validate_thread)

    async def webhook_images_real_path() -> None:
        install_meta_stubs(REC, big)
        REC.reset()
        phones = []
        for _ in range(20):
            wa = new_phone()
            make_customer(wa, price_per_image() * 3)
            phones.append(wa)
        await asyncio.gather(*[deliver(webhook_payload([image_msg(wa, f"wamid.h-{wa}")])) for wa in phones])

    # INFORMATIONAL (not in the verdict): the stalls here are the webhook's own synchronous database commits
    # and queries on the event loop (SQLite file locks and disk syncs in this harness; remote round trips on
    # PostgreSQL). Profiled with a stack sampler: do_commit / do_execute. Image CPU work is already off the loop.
    # The cure is Phase 3 (the webhook only records and queues); see PERF-2 / Q-2.
    res["h2r_INFO_webhook_x20_photo_intake_3MB_sync_database_on_loop"] = await lag_run("wh", webhook_images_real_path)

    async def floor_direct() -> None:
        async def one() -> None:
            check_quality_floor(big)
        await asyncio.gather(*[one() for _ in range(20)])

    async def floor_thread() -> None:
        await asyncio.gather(*[asyncio.to_thread(check_quality_floor, big) for _ in range(20)])

    res["h3_legacy_direct_quality_floor_x20_reference"] = await lag_run("f", floor_direct)
    res["h3b_same_work_via_to_thread"] = await lag_run("ft", floor_thread)

    async def generate_route_real_path() -> None:
        from app.ai.providers.image_base import ImageGenerationResult
        from app.api.routes import image_generation as igr
        from app.schemas.image_generation import ImageGenerationRequest

        data_url = "data:image/jpeg;base64," + base64.b64encode(big).decode("ascii")

        class StubManager:
            async def generate_image(self, **kwargs):  # type: ignore[no-untyped-def]
                await asyncio.sleep(0.005)
                return ImageGenerationResult(success=True, image_data=big, provider_name="stub",
                                             processing_time=0.01)

        original = igr.ImageGenerationManager
        igr.ImageGenerationManager = StubManager  # type: ignore[assignment]
        try:
            await asyncio.gather(*[
                igr.generate_image(ImageGenerationRequest(prompt="a gold ring on white", enforce_quality_floor=True))
                for _ in range(20)
            ])
        finally:
            igr.ImageGenerationManager = original  # type: ignore[assignment]

    res["h3r_REAL_PATH_generate_image_route_x20_quality_floor"] = await lag_run("gr", generate_route_real_path)

    async def b64_direct() -> None:
        for _ in range(100):
            url = "data:image/png;base64," + base64.b64encode(big).decode("utf-8")
            mws._data_url_to_bytes(url)
            await asyncio.sleep(0)

    res["h4_base64_encode+decode_3MB_x100"] = await lag_run("b64", b64_direct)

    async def upload_many() -> None:
        # One session per upload, like real requests (each request gets its own); a single Session must never
        # be used by several concurrent tasks.
        async def one(i: int) -> None:
            db = SessionLocal()
            try:
                await UploadService(db).process_upload(big, f"h{i}.jpg", len(big), "image/jpeg")
            finally:
                db.close()

        await asyncio.gather(*[one(i) for i in range(20)])

    res["h5_UploadService.process_upload_x20_(sha256+disk+PIL_dims+sync_DB)"] = await lag_run("up", upload_many)

    res["h6_batch_upload_route"] = await batch_route_probe(big)

    base = res["h0_baseline_idle"]["lag_max_ms"]
    counted = {
        k: v["lag_max_ms"] for k, v in res.items()
        if isinstance(v, dict) and "lag_max_ms" in v and not k.endswith("via_to_thread")
        and "legacy" not in k and "_INFO_" not in k
    }
    worst = max(counted.values())
    worst_name = max(counted, key=counted.get)
    legacy = max(v["lag_max_ms"] for k, v in res.items() if isinstance(v, dict) and "legacy" in k and "lag_max_ms" in v)
    res["verdict"] = verdict(worst < 100)
    res["verdict_reason"] = (
        f"worst single stall on the real paths {worst} ms ({worst_name}; idle baseline {base} ms; the old direct "
        f"calls stalled up to {legacy} ms); threshold 100 ms"
    )
    return res


async def batch_route_probe(big: bytes) -> Dict[str, Any]:
    import httpx
    from app.api.routes import batch_upload

    app = FastAPI()
    app.include_router(batch_upload.router)
    transport = httpx.ASGITransport(app=app)
    out: Dict[str, Any] = {}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=120) as c:
        r = await c.post("/api/upload/batch", files=[("files", (f"z{i}.jpg", SMALL_JPEG, "image/jpeg")) for i in range(21)])
        out["21_files_status"] = r.status_code
        out["21_files_detail"] = r.json().get("detail")

        async def one(k: int) -> str:
            try:
                rr = await c.post("/api/upload/batch", files=[("files", (f"b{k}_{i}.jpg", big, "image/jpeg")) for i in range(20)])
                return str(rr.status_code)
            except Exception as exc:  # noqa: BLE001
                return f"{type(exc).__name__}: {str(exc)[:110]}"

        async def five_batches() -> None:
            out["five_concurrent_20x3MB_results"] = await asyncio.gather(*[one(k) for k in range(5)])

        out.update(await lag_run("batch", five_batches))
        out["note"] = "route has a hard cap of 20 files/request, so 100 images need 5 requests"
    return out


# ─── (i) memory ─────────────────────────────────────────────────────────


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_mem_mb() -> Tuple[float, float]:
    pmc = _PMC(); pmc.cb = ctypes.sizeof(pmc)
    ctypes.windll.kernel32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ctypes.windll.kernel32.K32GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(pmc), pmc.cb)
    return pmc.WorkingSetSize / 1048576, pmc.PeakWorkingSetSize / 1048576


async def memory_child(n: int, trace: bool) -> Dict[str, Any]:
    import tracemalloc

    jpeg3 = make_noise_jpeg(3.0)
    out_bytes = 3 * 1024 * 1024
    primary, fallback = fresh_providers(output_bytes=out_bytes)
    primary.latency = fallback.latency = (0.15, 0.30)  # outputs complete together like a real burst
    install_meta_stubs(REC, jpeg3)
    price = price_per_image()
    wa = new_phone(); make_customer(wa, price * n)
    image_id = await stored_image(jpeg3, "big")
    ids = make_ingestions(wa, n, image_id)
    base_ws, _ = process_mem_mb()
    if trace:
        tracemalloc.start()
    out = await run_pack_orders([(wa, i) for i in ids])
    _, peak_ws = process_mem_mb()
    res: Dict[str, Any] = {
        "packs": n, "styles_per_pack": len(mws.CATALOG_PACK_STYLES), "ref_mb": round(len(jpeg3) / 1048576, 2), "output_mb": 3.0,
        "baseline_ws_mb": round(base_ws, 0), "peak_ws_mb": round(peak_ws, 0), "peak_delta_mb": round(peak_ws - base_ws, 0),
        "delivered": reconcile({wa: price * n})["statuses"].get("delivered", 0), "wall_s": out["wall_s"], "lag": out["loop_lag"],
    }
    if trace:
        cur, peak = tracemalloc.get_traced_memory()
        res["tracemalloc_peak_mb"] = round(peak / 1048576, 0)
    return res


def run_memory_children() -> Dict[str, Any]:
    script = str(Path(__file__).resolve())
    points = []
    for n in (4, 8, 12):
        proc = subprocess.run([sys.executable, script, "--memchild", str(n)], capture_output=True, text=True, timeout=600)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("MEMJSON:")]
        if not line:
            points.append({"packs": n, "error": (proc.stderr or proc.stdout)[-600:]})
            continue
        points.append(json.loads(line[-1][8:]))
    trace = subprocess.run([sys.executable, script, "--memchild", "6", "--trace"], capture_output=True, text=True, timeout=600)
    tline = [ln for ln in trace.stdout.splitlines() if ln.startswith("MEMJSON:")]
    traced = json.loads(tline[-1][8:]) if tline else {"error": trace.stderr[-400:]}
    good = [p for p in points if "peak_delta_mb" in p]
    res: Dict[str, Any] = {"points": points, "tracemalloc_run": traced}
    if len(good) >= 2:
        xs = [p["packs"] for p in good]; ys = [p["peak_delta_mb"] for p in good]
        n = len(xs); mx, my = sum(xs) / n, sum(ys) / n
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        intercept = my - slope * mx
        per_output = slope / len(mws.CATALOG_PACK_STYLES)
        res.update({
            "mb_per_concurrent_pack": round(slope, 1), "mb_per_generated_output": round(per_output, 1), "intercept_mb": round(intercept, 0),
            "extrapolated_peak_delta_mb_100_packs_x6": round(intercept + slope * 100, 0),
            "extrapolated_peak_delta_mb_100_images_x8_outputs": round(intercept + per_output * 8 * 100, 0),
            "threshold_mb": 2048,
        })
        res["verdict"] = verdict(res["extrapolated_peak_delta_mb_100_images_x8_outputs"] < 2048)
        res["verdict_reason"] = (
            f"~{res['mb_per_generated_output']} MB per live 3 MB output (raw bytes only, uploaded to Meta as soon as it finishes); "
            f"100 images x 8 outputs extrapolates to ~{res['extrapolated_peak_delta_mb_100_images_x8_outputs']} MB above baseline (threshold 2048 MB, typical small container)"
        )
    else:
        res["verdict"] = "FAIL"; res["verdict_reason"] = "memory child runs failed"
    # 100 reference uploads of 3 MB (sequential, as the batch route does): measured separately, cheap
    return res


async def pool_child(n: int) -> Dict[str, Any]:
    """Runs n simultaneous paid packs with the app's DEFAULT SQLAlchemy pool (5+10)."""
    install_meta_stubs(REC, SMALL_JPEG)
    price = price_per_image()
    primary, fallback = fresh_providers()
    wa, ids, bal = await setup_pack(n)
    out = await run_pack_orders([(wa, i) for i in ids])
    rec = reconcile({wa: bal})
    db = SessionLocal()
    balance = get_balance(db, wa)
    db.close()
    debited = (bal - balance) // price
    st = rec["statuses"]
    delivered = st.get("delivered", 0) + st.get("delivered_partial", 0)
    return {
        "orders": n, "wall_s": out["wall_s"], "loop_lag_max_s": round(out["loop_lag"]["lag_max_ms"] / 1000, 1),
        "loop_stalled_total_s": out["loop_lag"]["stalled_total_s"], "statuses": st,
        "tasks_raised_pool_timeout": len(out["exceptions"]) if out["exceptions"] else 0,
        "exception_sample": out["exceptions"][:1], "packs_debited": debited, "packs_delivered": delivered,
        "paid_but_not_delivered_and_not_refunded": max(0, debited - delivered - refunds_n(wa)),
        "wallet_reconciled": rec["all_reconciled"], "provider_calls": primary.stats.calls,
    }


def refunds_n(wa: str) -> int:
    db = SessionLocal()
    try:
        ids = [r[0] for r in db.query(WhatsAppIngestion.id).filter(WhatsAppIngestion.external_user_id == wa).all()]
        return refunds_for(db, ids)[0]
    finally:
        db.close()


def run_pool_children() -> Dict[str, Any]:
    script = str(Path(__file__).resolve())
    env = dict(os.environ, LOADSIM_POOL="default", LOADSIM_POOL_TIMEOUT="3")
    rows = []
    for n in (10, 15, 16, 20, 30):
        try:
            proc = subprocess.run([sys.executable, script, "--poolchild", str(n)], capture_output=True, text=True, timeout=400, env=env)
            line = [ln for ln in proc.stdout.splitlines() if ln.startswith("POOLJSON:")]
            rows.append(json.loads(line[-1][9:]) if line else {"orders": n, "error": (proc.stderr or proc.stdout)[-500:]})
        except subprocess.TimeoutExpired:
            rows.append({"orders": n, "error": "child exceeded 400 s (event loop starved by pool waits)"})
    bad = [r for r in rows if r.get("orders", 0) >= 16 and (r.get("tasks_raised_pool_timeout") or r.get("error") or r.get("paid_but_not_delivered_and_not_refunded"))]
    ok_small = all(not (r.get("tasks_raised_pool_timeout") or r.get("error")) for r in rows if r.get("orders", 99) <= 15)
    return {
        "pool_config": "SQLAlchemy defaults used by app/database.py: pool_size=5, max_overflow=10 (15 connections); pool_timeout shortened 30 s -> 3 s so the probe finishes (behaviour is identical, stalls are 10x shorter than production)",
        "rows": rows, "verdict": verdict(not bad and ok_small),
        "verdict_reason": (
            "workers end their transaction before every provider / Meta wait, so connections go back to the pool; "
            + "; ".join(
                f"{r.get('orders')} orders: "
                + ("ERROR" if r.get("error") else f"{r.get('tasks_raised_pool_timeout', 0)} pool timeouts, "
                   f"loop stalled {r.get('loop_stalled_total_s')}s, {r.get('packs_delivered')} delivered")
                for r in rows
            )
        ),
    }


# ─── driver ─────────────────────────────────────────────────────────────

SCENARIOS: Dict[str, Callable[[], Awaitable[Dict[str, Any]]]] = {
    "a": scenario_a, "b": scenario_b, "c": scenario_c, "d": scenario_d,
    "e": scenario_e, "f": scenario_f, "g": scenario_g, "h": scenario_h,
}


async def main_async(selected: List[str]) -> None:
    install_meta_stubs(REC, SMALL_JPEG)
    for key in selected:
        if key in ("i", "k"):
            continue
        t0 = time.perf_counter()
        try:
            RESULTS[key] = await SCENARIOS[key]()
        except Exception as exc:  # noqa: BLE001
            RESULTS[key] = {"verdict": "ERROR", "error": repr(exc), "trace": traceback.format_exc()[-1500:]}
        RESULTS[key]["harness_runtime_s"] = round(time.perf_counter() - t0, 1)
        print(f"[{key}] {RESULTS[key].get('verdict')} in {RESULTS[key]['harness_runtime_s']}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="a,b,c,d,e,f,g,h,i,k")
    ap.add_argument("--memchild", type=int, default=0)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--poolchild", type=int, default=0)
    args = ap.parse_args()
    if args.memchild:
        print("MEMJSON:" + json.dumps(asyncio.run(memory_child(args.memchild, args.trace))))
        return
    if args.poolchild:
        print("POOLJSON:" + json.dumps(asyncio.run(pool_child(args.poolchild)), default=str))
        return
    selected = [s.strip() for s in args.only.split(",") if s.strip()]
    asyncio.run(main_async(selected))
    if "k" in selected:
        t0 = time.perf_counter()
        RESULTS["k"] = run_pool_children()
        RESULTS["k"]["harness_runtime_s"] = round(time.perf_counter() - t0, 1)
        print(f"[k] {RESULTS['k'].get('verdict')}", flush=True)
    if "i" in selected:
        t0 = time.perf_counter()
        RESULTS["i"] = run_memory_children()
        RESULTS["i"]["harness_runtime_s"] = round(time.perf_counter() - t0, 1)
        print(f"[i] {RESULTS['i'].get('verdict')}", flush=True)
    RESULTS["_meta"] = {
        "latency_scale": LATENCY_SCALE, "real_latency_range_s": fakes.REAL_LATENCY_RANGE, "scaled_latency_range_s": fakes.SCALED_LATENCY_RANGE,
        "network_calls_blocked": list(harness_env.NETWORK_BLOCKED), "db": "sqlite (temp file, WAL)", "price_per_pack_rs": price_per_image(),
        "primary_image_provider": settings.PRIMARY_IMAGE_PROVIDER, "fallback_image_provider": settings.FALLBACK_IMAGE_PROVIDER,
        "log_warning_error_top": dict(collections.Counter(LOG_COUNTS).most_common(12)),
    }
    out = Path(__file__).resolve().parent / "last_results.json"
    out.write_text(json.dumps(RESULTS, indent=2, default=str), encoding="utf-8")
    print(f"results -> {out}")


if __name__ == "__main__":
    main()
