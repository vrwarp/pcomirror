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

#: Work nobody is waiting on. It shares the same budget as everything else, but
#: yields: a counselor's read must not queue behind a background sweep, and the
#: `priority` argument every caller already passes used to be accepted and then
#: ignored, so "the shadow reads run at low priority" was not true of anything.
DEFERRABLE = frozenset({"reconcile", "backfill", "webhook_hydrate", "divergence"})

#: How much of the bucket is held back from deferrable work. Foreground needs one
#: token; background needs one *plus* this much headroom, so background only ever
#: spends what the foreground is demonstrably not using. When PCO is quiet the
#: bucket sits full and background runs freely; when a caller is busy the tokens
#: stay low and background stalls, which is the point.
FOREGROUND_RESERVE = 0.5


class RateLimitBusy(RuntimeError):
    """Deferrable work waited its whole budget and the foreground kept the bucket.

    Raised rather than waited out, because the scheduler runs its units in one
    thread: a background task blocking indefinitely on the limiter would stop the
    webhook drain and every sweep behind it.
    """


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

    def acquire(self, priority: str = "reconcile", max_wait: float | None = None) -> None:
        """Take one token, waiting for it.

        `max_wait` bounds that wait for a caller that would rather skip this round
        than hold a thread — raising `RateLimitBusy`. Without it the wait is
        unbounded, which is what every sweep has always done.
        """
        floor = (self.capacity * FOREGROUND_RESERVE) if priority in DEFERRABLE else 0.0
        deadline = None if max_wait is None else time.monotonic() + max_wait
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= 1.0 + floor and time.monotonic() >= self._pause_until:
                    self.tokens -= 1.0
                    return
                wait = max(0.005, min(0.25, (1.0 + floor - self.tokens) / self.rate))
                pause = self._pause_until - time.monotonic()
                if pause > 0:
                    wait = min(pause, 0.25)
            if deadline is not None and time.monotonic() + wait > deadline:
                raise RateLimitBusy(
                    f"{priority} waited {max_wait:.1f}s and the budget stayed spoken for")
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
