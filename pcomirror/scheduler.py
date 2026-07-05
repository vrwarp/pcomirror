"""Background scheduler (DESIGN §7.2 cadence).

A single loop that, each tick: drains the webhook inbox, drains hydration tasks,
runs due incremental sweeps per resource, and periodically polls mergers and the
drift probe. Runs as a daemon thread inside the one service process.
"""
from __future__ import annotations

import threading
import time

from . import registry
from .config import now_iso


class Scheduler:
    def __init__(self, mirror, tick_seconds: float = 5.0):
        self.m = mirror
        self.tick = tick_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_merger = 0.0
        self._last_drift = 0.0
        self._last_audit = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="pcomirror-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                print(f"[scheduler] error: {e}")
            self._stop.wait(self.tick)

    def run_once(self):
        ing, wh = self.m.ingestor, self.m.webhooks
        wh.drain()
        ing.drain_hydration()
        now = now_iso()
        for r in registry.full_and_lite():
            st = ing.state(r.name)
            if st["phase"] in ("idle",) or (st["backfill_completed_at"] is None and r.method != "reference_periodic"):
                continue  # not backfilled yet
            if st["next_run_at"] and st["next_run_at"] <= now:
                ing.incremental_sweep(r.name)
                ing._set(r.name, next_run_at=self._plus(r.incr_interval_s), last_sweep_started_at=now)
        t = time.monotonic()
        if t - self._last_merger > 120:
            ing.merger_poll()
            self._last_merger = t
        if t - self._last_drift > 900:
            for r in registry.full_and_lite():
                if ing.state(r.name)["backfill_completed_at"]:
                    ing.drift_probe(r.name)
            self._last_drift = t

    @staticmethod
    def _plus(seconds: int) -> str:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
