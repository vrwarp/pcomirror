"""Background scheduler (DESIGN §7.2 cadence, §7.3 interleaving).

Two lanes, two daemon threads, one shared limiter and one shared database:

  * The **hot lane** ticks through the work whose latency somebody feels —
    the webhook inbox, the hydration queue, divergence checks, the incremental
    sweeps, the merger poll, the drift probes and repair scans. Each of these
    is small by construction.

  * The **cold lane** runs the work that is honest days-scale bulk — initial
    and late backfills, the full-id delete audits, the per-parent walks — as
    **bounded resumable units**, one unit per tick. The units persist their
    cursors (`mirror_sync_state`, `mirror_meta`, `audit_scratch`), so a unit
    that dies loses nothing and a restart resumes mid-round.

The split exists because this was measured, not feared: on a real 1,900-person
organization the first-day audit and a burst of hang-and-retry cycles ran
inline and held the single loop for eight minutes — hydration tasks with
`not_before` in the past, webhook events, and every divergence check queued
behind work none of them needed — while serving carried on and hid it. A hung
upstream read in a cold unit now costs the cold lane its tick, and nothing
else anything.
"""
from __future__ import annotations

import threading
import time

from . import registry
from .config import now_iso


class Scheduler:
    #: Upstream pages/requests one cold unit may spend before yielding.
    COLD_UNIT_BUDGET = 8
    #: How long a cold unit that *failed* waits before being retried, so a dead
    #: upstream costs one request per backoff window, not one per tick.
    COLD_RETRY_S = 300.0
    #: Hydration tasks drained per hot tick. The queue survives; a burst simply
    #: takes a few ticks instead of monopolising one.
    HOT_HYDRATIONS_PER_TICK = 25

    def __init__(self, mirror, tick_seconds: float = 5.0):
        self.m = mirror
        self.tick = tick_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cold_thread: threading.Thread | None = None
        self._last_merger = 0.0
        self._last_drift = 0.0
        self._cold_retry_at: dict[str, float] = {}

    def start(self):
        self._thread = threading.Thread(target=self._run, name="pcomirror-scheduler", daemon=True)
        self._thread.start()
        self._cold_thread = threading.Thread(
            target=self._run_cold, name="pcomirror-scheduler-cold", daemon=True)
        self._cold_thread.start()

    def stop(self):
        self._stop.set()
        for t in (self._thread, self._cold_thread):
            if t:
                t.join(timeout=10)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                print(f"[scheduler] error: {e}")
            self._stop.wait(self.tick)

    def _run_cold(self):
        while not self._stop.is_set():
            try:
                self.run_cold_once()
            except Exception as e:  # noqa: BLE001
                print(f"[scheduler] cold error: {e}")
            self._stop.wait(self.tick)

    # -- the hot lane ------------------------------------------------------
    def run_once(self):
        ing, wh = self.m.ingestor, self.m.webhooks
        # local work always runs (no PCO calls)
        self._guard("webhook-drain", wh.drain)
        self._guard("hydration-drain", ing.drain_hydration, self.HOT_HYDRATIONS_PER_TICK)

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
            if r.method == "nested_walk":
                continue    # a walk is per-parent bulk; the cold lane paces it
            st = ing.state(r.name)
            if st["backfill_completed_at"] is None:
                continue    # the cold lane is backfilling it
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

    # -- the cold lane -----------------------------------------------------
    def run_cold_once(self) -> bool:
        """One bounded unit of bulk work, or nothing. Returns whether it worked.

        Order is priority: a resource nobody has backfilled serves empty pages,
        so backfills come first; then the delete audits — the third delete
        mechanism (DESIGN §7.2), the only one that needs no signal from PCO,
        and the one that was written, tested, documented, exposed on the CLI
        and then never scheduled; then the per-parent walk rounds. One unit per
        tick, so a day-scale piece of work and a five-second tick coexist.
        """
        ing = self.m.ingestor
        now = now_iso()
        for r in registry.full_and_lite():
            if ing.state(r.name)["backfill_completed_at"] is None:
                if not self._cold_ready(f"backfill:{r.name}"):
                    continue
                ok = self._guard(f"late-backfill:{r.name}",
                                 ing.backfill, r.name, self.COLD_UNIT_BUDGET)
                self._cold_outcome(f"backfill:{r.name}", ok)
                if ok and ing.state(r.name)["backfill_completed_at"]:
                    print(f"[scheduler] backfilled newly declared resource {r.name}", flush=True)
                return True
        for r in registry.full_and_lite():
            if not r.audit_interval_s:
                continue
            # A round in progress is continued whatever the cadence says —
            # abandoning it would strand its scratch sets; a new round starts
            # only when the audit is due.
            if ing.audit_round(r.name) is None and not self._audit_due(r.name, now):
                continue
            if not self._cold_ready(f"audit:{r.name}"):
                continue
            ok = self._guard(f"audit:{r.name}", self._audit_step, r.name)
            self._cold_outcome(f"audit:{r.name}", ok)
            return True
        for r in registry.full_and_lite():
            if r.method != "nested_walk":
                continue
            st = ing.state(r.name)
            if st["backfill_completed_at"] is None:
                continue
            due = st["next_run_at"] and st["next_run_at"] <= now
            if ing.walk_round(r.name) is None and not due:
                continue
            if not self._cold_ready(f"walk:{r.name}"):
                continue
            ok = self._guard(f"walk:{r.name}", self._walk_step, r.name)
            self._cold_outcome(f"walk:{r.name}", ok)
            return True
        return False

    def drain_cold(self, max_units: int = 10_000) -> int:
        """Run cold units until none is due. For tests and for the CLI, where
        "do the bulk work now" is the entire point and pacing is not."""
        n = 0
        while n < max_units and self.run_cold_once():
            n += 1
        return n

    def _cold_ready(self, key: str) -> bool:
        return time.monotonic() >= self._cold_retry_at.get(key, 0.0)

    def _cold_outcome(self, key: str, ok: bool) -> None:
        if ok:
            self._cold_retry_at.pop(key, None)
        else:
            # One failed unit per backoff window. Without this, a dead upstream
            # turned the cold lane into a request per tick, for ever, logged.
            self._cold_retry_at[key] = time.monotonic() + self.COLD_RETRY_S

    def _audit_step(self, name: str) -> None:
        outcome = self.m.ingestor.delete_audit_step(name, budget=self.COLD_UNIT_BUDGET)
        if outcome["done"]:
            if outcome["tombstoned"]:
                print(f"[scheduler] audit tombstoned {outcome['tombstoned']} deleted "
                      f"{name} record(s)", flush=True)
            if outcome["restored"]:
                print(f"[scheduler] audit restored {outcome['restored']} {name} record(s) "
                      f"PCO holds that the mirror lacked", flush=True)

    def _walk_step(self, name: str) -> None:
        r = registry.by_name(name)
        outcome = self.m.ingestor.walk_round_step(name, budget=self.COLD_UNIT_BUDGET)
        if outcome["done"]:
            self.m.ingestor._set(name, next_run_at=self._plus(r.incr_interval_s))

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
