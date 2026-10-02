"""Honest delivery estimates for the "Processing your order" message (UX-2).

The fixed "20-30 seconds" promise was the same whatever the load. This keeps the time the last orders really took (per
product, in memory, last ``KEEP`` of them) and the number of orders ahead in line, and turns them into a short phrase.
Until enough orders have been measured (a fresh start) the original wording is kept, so nothing changes on day one and
the message only gets more honest as data arrives.
"""

from __future__ import annotations

import math
import statistics
import threading
from collections import deque
from typing import Deque, Dict, Optional

KEEP = 30
MIN_SAMPLES = 3

_lock = threading.Lock()
_samples: Dict[str, Deque[float]] = {}


def record_duration(kind: str, seconds: float) -> None:
    """Remember how long a delivered order of this kind took, from start of generation to delivery."""
    if seconds <= 0 or seconds > 3600:
        return
    with _lock:
        _samples.setdefault(kind, deque(maxlen=KEEP)).append(float(seconds))


def reset() -> None:
    with _lock:
        _samples.clear()


def typical_seconds(kind: str) -> Optional[float]:
    """Median recent duration, or None before ``MIN_SAMPLES`` orders have been measured."""
    with _lock:
        values = list(_samples.get(kind, ()))
    return statistics.median(values) if len(values) >= MIN_SAMPLES else None


def estimate_seconds(kind: str, orders_ahead: int, parallel_orders: Optional[int] = None) -> Optional[float]:
    """Typical time plus the wait for the orders in front. None when there is nothing measured yet.

    ``parallel_orders`` is how many orders the server runs at the same time (None = no limit: nobody waits)."""
    base = typical_seconds(kind)
    if base is None:
        return None
    ahead = max(int(orders_ahead), 0)
    if parallel_orders and parallel_orders > 0 and ahead >= parallel_orders:
        return base * (1 + ahead / parallel_orders)
    return base


def phrase(seconds: float) -> str:
    """A friendly, slightly generous phrase: 'under a minute', 'about 2 minutes'."""
    if seconds <= 45:
        return "under a minute"
    minutes = max(1, math.ceil(seconds / 60.0))
    return "about 1 minute" if minutes == 1 else f"about {minutes} minutes"


def ack_suffix(kind: str, orders_ahead: int, parallel_orders: Optional[int], default: str) -> str:
    """The "please allow ..." wording: measured when we have data, else ``default`` unchanged."""
    seconds = estimate_seconds(kind, orders_ahead, parallel_orders)
    if seconds is None:
        return default
    waiting = f" There are {orders_ahead} orders ahead of yours." if orders_ahead >= 3 else ""
    return f"Usually ready in {phrase(seconds)}.{waiting}"
