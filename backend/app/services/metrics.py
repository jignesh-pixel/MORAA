"""A small, dependency-free metrics registry that renders the Prometheus text format (OBS-3).

Counters and gauges with labels, plus fixed-bucket histograms, all thread-safe. Nothing here talks to the
network: ``/metrics`` renders the current numbers on request. Gauges that come from the database (orders by
status, today's spend, parked payments) are filled in by ``collect_database_gauges`` at scrape time and cached
for a few seconds, so scraping never loads the database.

Label values are always fixed names chosen in code (status classes, provider names, order states): never a
phone number, id or any customer text.
"""

import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

LabelKey = Tuple[Tuple[str, str], ...]


def _key(labels: Optional[Dict[str, str]]) -> LabelKey:
    return tuple(sorted((labels or {}).items()))


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(name: str, key: LabelKey, value: float) -> str:
    if key:
        inner = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
        return f"{name}{{{inner}}} {value:g}"
    return f"{name} {value:g}"


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[LabelKey, float]] = {}
        self._gauges: Dict[str, Dict[LabelKey, float]] = {}
        self._hist: Dict[str, Dict[LabelKey, List[float]]] = {}
        self._hist_buckets: Dict[str, Tuple[float, ...]] = {}
        self._help: Dict[str, str] = {}

    def describe(self, name: str, help_text: str) -> None:
        self._help[name] = help_text

    def inc(self, name: str, labels: Optional[Dict[str, str]] = None, amount: float = 1.0) -> None:
        with self._lock:
            series = self._counters.setdefault(name, {})
            k = _key(labels)
            series[k] = series.get(k, 0.0) + amount

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self._gauges.setdefault(name, {})[_key(labels)] = float(value)

    def clear_gauge(self, name: str) -> None:
        with self._lock:
            self._gauges.pop(name, None)

    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None,
                buckets: Tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)) -> None:
        with self._lock:
            self._hist_buckets.setdefault(name, buckets)
            bounds = self._hist_buckets[name]
            series = self._hist.setdefault(name, {})
            k = _key(labels)
            row = series.setdefault(k, [0.0] * (len(bounds) + 2))      # bucket counts..., +Inf count, sum
            for i, bound in enumerate(bounds):
                if value <= bound:
                    row[i] += 1
            row[len(bounds)] += 1
            row[len(bounds) + 1] += value

    def counter_value(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        with self._lock:
            return self._counters.get(name, {}).get(_key(labels), 0.0)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hist.clear()

    def render(self) -> str:
        lines: List[str] = []
        with self._lock:
            for name in sorted(self._counters):
                lines.append(f"# HELP {name} {self._help.get(name, name)}")
                lines.append(f"# TYPE {name} counter")
                lines.extend(_fmt(name, k, v) for k, v in sorted(self._counters[name].items()))
            for name in sorted(self._gauges):
                lines.append(f"# HELP {name} {self._help.get(name, name)}")
                lines.append(f"# TYPE {name} gauge")
                lines.extend(_fmt(name, k, v) for k, v in sorted(self._gauges[name].items()))
            for name in sorted(self._hist):
                bounds = self._hist_buckets[name]
                lines.append(f"# HELP {name} {self._help.get(name, name)}")
                lines.append(f"# TYPE {name} histogram")
                for k, row in sorted(self._hist[name].items()):
                    for i, bound in enumerate(bounds):
                        lines.append(_fmt(f"{name}_bucket", k + (("le", f"{bound:g}"),), row[i]))
                    lines.append(_fmt(f"{name}_bucket", k + (("le", "+Inf"),), row[len(bounds)]))
                    lines.append(_fmt(f"{name}_sum", k, row[len(bounds) + 1]))
                    lines.append(_fmt(f"{name}_count", k, row[len(bounds)]))
        return "\n".join(lines) + "\n"


registry = Registry()
registry.describe("moraa_http_requests_total", "HTTP requests by method and status class")
registry.describe("moraa_http_request_seconds", "HTTP request duration in seconds")
registry.describe("moraa_provider_calls_total", "AI image provider calls by provider and outcome")
registry.describe("moraa_provider_fallbacks_total", "Image calls served by the fallback provider")
registry.describe("moraa_generation_slots_in_use", "Paid AI image calls counted so far today (UTC)")
registry.describe("moraa_generation_slots_cap", "Configured daily ceiling of paid AI image calls")
registry.describe("moraa_orders", "Customer photo orders by status (last 24 hours)")
registry.describe("moraa_pending_payments", "Paid Razorpay payments waiting for a person to credit them")
registry.describe("moraa_meta_send_total", "Messages sent to Meta by outcome")
registry.describe("moraa_recovery_sweep_total", "Orders touched by the recovery sweep by action")
registry.describe("moraa_db_up", "1 when the last database check succeeded")
registry.describe("moraa_alerts_sent_total", "Operations alerts sent, by alert key")


_KNOWN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def record_http(method: str, status: int, seconds: float) -> None:
    method = method if method in _KNOWN_METHODS else "OTHER"       # a client must not be able to mint label values
    registry.inc("moraa_http_requests_total", {"method": method, "status": f"{status // 100}xx"})
    registry.observe("moraa_http_request_seconds", seconds)


def record_provider(provider: str, outcome: str) -> None:
    registry.inc("moraa_provider_calls_total", {"provider": provider or "none", "outcome": outcome})


# ─── database-backed gauges, refreshed at scrape time ───────────────────────

_cache: Dict[str, float] = {"at": 0.0}
_CACHE_SECONDS = 15.0
_cache_lock = threading.Lock()


def collect_database_gauges(force: bool = False) -> None:
    """Refresh the gauges that come from the database (at most once per ``_CACHE_SECONDS``). Never raises."""
    now = time.monotonic()
    with _cache_lock:
        if not force and now - _cache["at"] < _CACHE_SECONDS:
            return
        _cache["at"] = now
    try:
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import func

        from app.config import settings
        from app.database import SessionLocal
        from app.models.pending_payment import STATUS_PENDING, PendingPayment
        from app.models.whatsapp_ingestion import WhatsAppIngestion
        from app.services import spend_counter

        with SessionLocal() as db:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            rows = (
                db.query(WhatsAppIngestion.status, func.count(WhatsAppIngestion.id))
                .filter(WhatsAppIngestion.created_at >= since)
                .group_by(WhatsAppIngestion.status)
                .all()
            )
            pending = db.query(func.count(PendingPayment.id)).filter(PendingPayment.status == STATUS_PENDING).scalar()
        registry.clear_gauge("moraa_orders")
        for status, count in rows:
            registry.set_gauge("moraa_orders", count, {"status": status or "unknown"})
        registry.set_gauge("moraa_pending_payments", int(pending or 0))
        registry.set_gauge("moraa_db_up", 1)

        from app.ai import image_generation_manager as igm

        used = spend_counter.used(igm.current_spend_day())
        if used is not None:
            registry.set_gauge("moraa_generation_slots_in_use", used)
        cap = getattr(settings, "MAX_GENERATIONS_PER_DAY", None)
        if isinstance(cap, int) and not isinstance(cap, bool):
            registry.set_gauge("moraa_generation_slots_cap", cap)
    except Exception:  # noqa: BLE001 -- a metrics problem must never break anything else
        registry.set_gauge("moraa_db_up", 0)


def render_all() -> str:
    collect_database_gauges()
    return registry.render()
