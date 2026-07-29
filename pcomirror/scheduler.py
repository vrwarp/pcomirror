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
        # local work always runs (no PCO calls)
        self._guard("webhook-drain", wh.drain)
        self._guard("hydration-drain", ing.drain_hydration)

        # anything that calls PCO only runs once there's a backfill to build on
        any_backfilled = any(
            ing.state(r.name)["backfill_completed_at"] for r in registry.full_and_lite())
        if not any_backfilled:
            return

        # Costs PCO budget, so it sits with the other upstream work and behind
        # the same backfill gate: comparing against an empty mirror would report
        # the whole organization as missing.
        checker = getattr(self.m, "divergence", None)
        if checker is not None and checker.enabled:
            self._guard("divergence", checker.run_once)

        now = now_iso()
        for r in registry.full_and_lite():
            st = ing.state(r.name)
            if st["backfill_completed_at"] is None:
                # A resource added to the registry after this mirror was first
                # backfilled has none of its own, and every sweep below is gated
                # on having one — so without this it would stay empty forever
                # while the sweeps around it ran, and reads would answer from an
                # empty table. Backfill it now, in the background, once.
                if self._guard(f"late-backfill:{r.name}", ing.backfill, r.name):
                    print(f"[scheduler] backfilled newly declared resource {r.name}", flush=True)
                continue
            if st["next_run_at"] and st["next_run_at"] <= now:
                if self._guard(f"sweep:{r.name}", ing.incremental_sweep, r.name):
                    ing._set(r.name, next_run_at=self._plus(r.incr_interval_s), last_sweep_started_at=now)
        t = time.monotonic()
        if t - self._last_merger > 120:
            self._guard("merger-poll", ing.merger_poll)
            self._last_merger = t
        if t - self._last_drift > 900:
            for r in registry.full_and_lite():
                if ing.state(r.name)["backfill_completed_at"]:
                    self._guard(f"drift:{r.name}", ing.drift_probe, r.name)
                    # Drift counts rows; this checks whether the rows are whole.
                    # A record can only be degraded, never repaired, if nothing
                    # looks — its `updated_at` will not move again.
                    self._guard(f"repair:{r.name}", self._repair, r.name)
            self._last_drift = t
        # The third delete mechanism (DESIGN §7.2), and the only one that needs no
        # signal from PCO: webhooks are lossy and merges only cover the merge path,
        # so a person hard-deleted in the UI is invisible to everything else here —
        # `where[updated_at]` cannot return an id that no longer exists. It was
        # written, tested, documented and exposed on the CLI, and then never
        # scheduled, which is why a live mirror was serving `total_count` 448
        # against PCO's 447 with nothing on course to notice.
        if self._audit_due("person", now):
            self._guard("audit:person", self._audit, "person")

    def _audit_due(self, name: str, now: str) -> bool:
        """Due off the *persisted* completion time, not this process's clock.

        Every other cadence here is monotonic, which is right for something that
        runs every couple of minutes. It is wrong at a day: a service that
        restarts more often than its own interval restarts the countdown too, and
        a check written for once a night then never happens at all. The audit
        already records when it finished, so that is the clock to read.
        """
        hours = max(0, getattr(self.m.settings, "audit_interval_hours", 24))
        if not hours:
            return False
        st = self.m.ingestor.state(name)
        if not st["backfill_completed_at"]:
            return False
        # The later of started/completed, so an audit that *fails* waits a full
        # interval rather than re-enumerating the organization every five seconds.
        last = max(st["last_audit_started_at"] or "", st["last_audit_completed_at"] or "")
        return not last or last <= self._minus(hours * 3600, now)

    def _audit(self, name: str) -> None:
        n = self.m.ingestor.delete_audit(name)
        if n:
            print(f"[scheduler] audit tombstoned {n} deleted {name} record(s)", flush=True)

    def _repair(self, name: str) -> None:
        n = self.m.ingestor.repair_incomplete(name)
        if n:
            print(f"[scheduler] queued {n} incomplete {name} record(s) for re-fetch", flush=True)

    def _guard(self, label, fn, *args) -> bool:
        """Run one unit; a failure (e.g. transient PCO/network) is logged, not fatal."""
        try:
            fn(*args)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[scheduler] {label}: {e}", flush=True)
            return False

    @staticmethod
    def _plus(seconds: int) -> str:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _minus(seconds: int, now: str) -> str:
        from datetime import datetime, timedelta, timezone
        at = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (at - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
