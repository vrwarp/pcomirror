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
            # The household edge is stored twice — the household's `people` and
            # each member's `households` — and the halves can disagree with
            # neither record's `updated_at` ever moving again. Not per-resource:
            # the check is the *pair*.
            self._guard("repair:split-edges", self._split_edges)
            self._last_drift = t
        # The third delete mechanism (DESIGN §7.2), and the only one that needs no
        # signal from PCO: webhooks are lossy and merges only cover the merge path,
        # so a person hard-deleted in the UI is invisible to everything else here —
        # `where[updated_at]` cannot return an id that no longer exists. It was
        # written, tested, documented and exposed on the CLI, and then never
        # scheduled, which is why a live mirror was serving `total_count` 448
        # against PCO's 447 with nothing on course to notice.
        #
        # Every resource that declares an audit gets one, rather than `person`
        # alone. A household is deleted by exactly the same click and was just as
        # invisible: three of them, created and abandoned while somebody added a
        # family, were still live in a mirror a day later and still listed on the
        # parent's record. Enumerating a few hundred households costs four
        # requests a day.
        for r in registry.full_and_lite():
            if r.audit_interval_s and self._audit_due(r.name, now):
                self._guard(f"audit:{r.name}", self._audit, r.name)

    #: How soon a drift-requested audit may follow the previous audit. The probe
    #: runs every 15 minutes and re-requests for as long as the counts disagree,
    #: so a delta the audit cannot close — a population-semantics difference,
    #: not drift — costs one audit per hour, not one per probe.
    DRIFT_AUDIT_MIN_INTERVAL_S = 3600

    def _audit_due(self, name: str, now: str) -> bool:
        """Due off the *persisted* completion time, not this process's clock.

        The cadence is one setting for every audited resource —
        `PCOMIRROR_AUDIT_INTERVAL_HOURS`, zero to switch them all off. The
        registry decides *which* resources are audited, not how often.

        Every other cadence here is monotonic, which is right for something that
        runs every couple of minutes. It is wrong at a day: a service that
        restarts more often than its own interval restarts the countdown too, and
        a check written for once a night then never happens at all. The audit
        already records when it finished, so that is the clock to read.

        A request (`ingest.request_audit`, §7.4) pulls the audit forward, and
        who asked decides how far. `drift` is the probe measuring the id sets
        disagreeing — a ghost that waits out the nightly cadence is served, and
        offered back by every duplicate-check search, the whole time — but it
        still waits out the cooldown above, which is what stands between a
        persistent count difference and an audit every fifteen minutes, and
        `hours == 0` switches it off entirely. An `operator` request is a
        person on the console clicking the button: it runs at the next tick,
        cadence, cooldown and `hours == 0` notwithstanding — the same standing
        the CLI's `reconcile --audit` has always had, since both are a human
        explicitly spending the budget once.
        """
        st = self.m.ingestor.state(name)
        if not st["backfill_completed_at"]:
            return False
        request = self.m.ingestor.audit_requested(name)
        if request == "operator":
            return True
        hours = max(0, getattr(self.m.settings, "audit_interval_hours", 24))
        if not hours:
            return False
        # The later of started/completed, so an audit that *fails* waits a full
        # interval rather than re-enumerating the organization every five seconds.
        last = max(st["last_audit_started_at"] or "", st["last_audit_completed_at"] or "")
        if not last or last <= self._minus(hours * 3600, now):
            return True
        return bool(request) and last <= self._minus(self.DRIFT_AUDIT_MIN_INTERVAL_S, now)

    def _audit(self, name: str) -> None:
        tombstoned, restored = self.m.ingestor.delete_audit(name)
        if tombstoned:
            print(f"[scheduler] audit tombstoned {tombstoned} deleted {name} record(s)", flush=True)
        if restored:
            print(f"[scheduler] audit restored {restored} {name} record(s) PCO holds "
                  f"that the mirror lacked", flush=True)

    def _split_edges(self) -> None:
        n = self.m.ingestor.repair_split_edges()
        if n:
            print(f"[scheduler] queued {n} re-read(s) for household edges whose two "
                  f"copies disagree", flush=True)

    def _repair(self, name: str) -> None:
        n = self.m.ingestor.repair_incomplete(name)
        if n:
            print(f"[scheduler] queued {n} incomplete {name} record(s) for re-fetch", flush=True)
        # Two different kinds of wrong: a record missing a relationship, and a
        # relationship pointing at a record the mirror cannot serve. Neither
        # moves `updated_at`, so this is the only pass that sees either.
        n = self.m.ingestor.repair_dangling(name)
        if n:
            print(f"[scheduler] queued {n} re-fetch(es) for {name} edges that do not resolve",
                  flush=True)

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
