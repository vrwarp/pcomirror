"""In-process, header-adaptive rate limiter (DESIGN §5.2).

One token bucket shared by every PCO caller in the process. It seeds from config
and continuously re-targets from PCO's `X-PCO-API-Request-Rate-*` headers, so it
follows the 100->75 high-offset drop or any per-endpoint limit without hard-coding.
At ~300 people it is barely load-bearing, but it keeps webhook-hydration bursts and
mass imports safely under 100 req/20s.
"""
from __future__ import annotations

import threading
import time


def _parse_period(v: str | None) -> float:
    if not v:
        return 20.0
    # "20 seconds" -> 20.0
    num = "".join(ch for ch in v if ch.isdigit() or ch == ".")
    try:
        return float(num) or 20.0
    except ValueError:
        return 20.0


class RateLimiter:
    def __init__(self, target_rps: float = 4.0, util: float = 0.80):
        self.util = util
        self.rate = max(0.1, target_rps)
        self.capacity = max(1.0, target_rps)
        self.tokens = self.capacity
        self._last = time.monotonic()
        self._pause_until = 0.0
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self, priority: str = "reconcile") -> None:
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= 1.0 and time.monotonic() >= self._pause_until:
                    self.tokens -= 1.0
                    return
                wait = max(0.005, min(0.25, (1.0 - self.tokens) / self.rate))
                pause = self._pause_until - time.monotonic()
                if pause > 0:
                    wait = min(pause, 0.25)
            time.sleep(wait)

    def on_response(self, headers: dict) -> None:
        h = {k.lower(): v for k, v in headers.items()}
        try:
            limit = float(h.get("x-pco-api-request-rate-limit", 0))
            period = _parse_period(h.get("x-pco-api-request-rate-period"))
            count = float(h.get("x-pco-api-request-rate-count", 0))
        except (TypeError, ValueError):
            return
        if limit > 0 and period > 0:
            with self._lock:
                self.rate = (limit / period) * self.util
                self.capacity = max(1.0, limit / period)
                if count >= 0.9 * limit:  # near the wall — coast one window
                    self._pause_until = time.monotonic() + period

    def on_429(self, retry_after: float | None) -> None:
        with self._lock:
            self.tokens = 0.0
            self._pause_until = time.monotonic() + (retry_after if retry_after else 20.0)
