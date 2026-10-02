"""Per-provider circuit breaker and token bucket (EXT-6).

Circuit breaker: after ``CIRCUIT_BREAKER_FAILURES`` outage-type failures in a row (timeouts, 5xx, overload, rate or
quota exhaustion) a provider is treated as down for ``CIRCUIT_BREAKER_COOLDOWN_SECONDS``. Calls to it fail at once
(no paid call, no waiting for another timeout) and the order moves to the other provider or is refunded. After the
cool-down ONE probe call is let through; if it works the circuit closes, if it fails the cool-down restarts.
A failure of the request itself (blocked prompt, bad request) says nothing about the provider and is not counted.

Token bucket: ``IMAGE_PROVIDER_RPM`` calls per minute per provider (0 = off), sized to the provider's quota so a burst
waits briefly instead of being rejected by the provider. A wait longer than ``IMAGE_RATE_WAIT_MAX_SECONDS`` fails the
call fast (the order is refunded) rather than holding a customer for minutes.

State is per process. With several API/worker processes each keeps its own view; sharing it through Redis is a
later step (see docs/QUESTIONS_FOR_MORNING.md).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Dict, Optional

from app.config import settings


class CircuitBreaker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: Dict[str, int] = {}
        self._opened_at: Dict[str, float] = {}
        self._probing: Dict[str, float] = {}      # provider -> when its probe started

    @staticmethod
    def _threshold() -> int:
        return max(int(getattr(settings, "CIRCUIT_BREAKER_FAILURES", 5) or 0), 0)

    @staticmethod
    def _cooldown() -> float:
        return max(float(getattr(settings, "CIRCUIT_BREAKER_COOLDOWN_SECONDS", 60) or 0), 0.0)

    def allow(self, name: str, now: Optional[float] = None) -> bool:
        """True when a call may be made. While open: False, except one probe once the cool-down has passed."""
        if self._threshold() == 0:
            return True
        now = time.monotonic() if now is None else now
        with self._lock:
            opened = self._opened_at.get(name)
            if opened is None:
                return True
            if now - opened < self._cooldown():
                return False
            started = self._probing.get(name)
            if started is not None and now - started < self._cooldown():
                return False                    # a probe is already in flight (one that never reported expires)
            self._probing[name] = now
            return True

    def record_success(self, name: str) -> None:
        with self._lock:
            self._failures.pop(name, None)
            self._opened_at.pop(name, None)
            self._probing.pop(name, None)

    def record_outage_failure(self, name: str, now: Optional[float] = None) -> None:
        threshold = self._threshold()
        if threshold == 0:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            if name in self._opened_at:             # a failed probe: restart the cool-down
                self._opened_at[name] = now
                self._probing.pop(name, None)
                return
            count = self._failures.get(name, 0) + 1
            self._failures[name] = count
            if count >= threshold:
                self._opened_at[name] = now

    def record_neutral(self, name: str) -> None:
        """A failure that says nothing about the provider's health (e.g. a blocked prompt): free a probe slot."""
        with self._lock:
            self._probing.pop(name, None)

    def is_open(self, name: str) -> bool:
        with self._lock:
            return name in self._opened_at

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._opened_at.clear()
            self._probing.clear()


class TokenBucket:
    """Refills continuously at ``rpm`` per minute; capacity one minute's worth, capped at 10 (no huge bursts)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, list] = {}       # name -> [tokens, last_refill]

    def _take(self, name: str, rpm: int, now: float) -> float:
        """Take a token; return 0 when taken, else the seconds until one would be available."""
        capacity = float(min(max(rpm, 1), 10))
        rate = rpm / 60.0
        with self._lock:
            tokens, last = self._state.get(name, [capacity, now])
            tokens = min(capacity, tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._state[name] = [tokens - 1.0, now]
                return 0.0
            self._state[name] = [tokens, now]
            return (1.0 - tokens) / rate

    async def acquire(self, name: str) -> bool:
        """Wait for a token. False if it would take longer than IMAGE_RATE_WAIT_MAX_SECONDS."""
        rpm = int(getattr(settings, "IMAGE_PROVIDER_RPM", 0) or 0)
        if rpm <= 0:
            return True
        max_wait = float(getattr(settings, "IMAGE_RATE_WAIT_MAX_SECONDS", 20) or 0)
        deadline = time.monotonic() + max_wait
        while True:
            wait = self._take(name, rpm, time.monotonic())
            if wait <= 0:
                return True
            if time.monotonic() + wait > deadline:
                return False
            await asyncio.sleep(wait)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


breaker = CircuitBreaker()
bucket = TokenBucket()
