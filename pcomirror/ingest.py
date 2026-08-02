"""Ingestion & reconciliation (DESIGN §5, §7).

Backfill (keyset on updated_at + include sideloading), the incremental catch-up
sweep, the merger poll, the delete audit, the drift probe, and hydration — all
through the shared limiter and the canonical writer.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import registry
from .config import now_iso
from .pcoclient import PcoClient
from .writer import Writer


def _plus_one_second(iso: str) -> str:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_in(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _related_ids(resource: dict) -> set[tuple[str, str]]:
    """Every `(type, id)` a resource's own relationships name."""
    out: set[tuple[str, str]] = set()
    for node in (resource.get("relationships") or {}).values():
        data = node.get("data") if isinstance(node, dict) else None
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("id"):
                out.add((item.get("type"), str(item["id"])))
    return out


class IngestError(RuntimeError):
    """A sync step could not complete — raised rather than recorded as success."""


# -- sync on demand ---------------------------------------------------------
#
# Module functions taking a `db`, not `Ingestor` methods, because the callers
# that *ask* are not the thread that *runs*: the drift probe and the admin page
# record a request, and the scheduler — the one thread that does ingest work on
# a cadence — answers it on its next tick. The admin page holds a database and
# nothing else, which is the point: a page that could reach the ingestor would
# be a page that can spend minutes of PCO budget inside one HTTP request.

def _audit_request_key(name: str) -> str:
    return f"audit_requested:{name}"


def request_audit(db, name: str, source: str) -> None:
    """Ask for an out-of-cadence id-set audit of `name`.

    `source` is the urgency contract. `drift` is the probe speaking — a
    measured count mismatch — and waits out the scheduler's cooldown so a
    delta the audit cannot close costs one audit an hour, not one per probe.
    `operator` is a person on the console clicking the button, which is the
    same standing the CLI's `reconcile --audit` has always had: it runs at the
    next tick, cadence, cooldown and even `PCOMIRROR_AUDIT_INTERVAL_HOURS=0`
    notwithstanding.

    A request only ever escalates. The probe re-measures every 15 minutes, so
    without this a person clicking the button on a drifted mirror had their
    request quietly demoted to `drift` by the very next probe — back behind
    the cooldown, or refused outright where scheduled audits are off.
    """
    if source == "drift" and audit_request(db, name) == "operator":
        return
    db.set_meta(_audit_request_key(name), f"{source} {now_iso()}")


def audit_request(db, name: str) -> str | None:
    """The pending request's source — `drift`, `operator` — or None."""
    held = db.get_meta(_audit_request_key(name))
    return held.split()[0] if held else None


def clear_audit_request(db, name: str) -> None:
    db.execute("DELETE FROM mirror_meta WHERE key=?", (_audit_request_key(name),))


def request_sweep(db, name: str) -> None:
    """Make `name`'s incremental sweep due on the scheduler's next tick.

    Nothing here runs the sweep — `next_run_at` is simply moved to now, and
    the scheduler's ordinary loop does what it does for any due resource. A
    resource that has never backfilled has no sweep to bring forward; the
    scheduler is already backfilling it on every tick.
    """
    db.execute("INSERT OR IGNORE INTO mirror_sync_state(resource_type) VALUES(?)", (name,))
    db.execute("UPDATE mirror_sync_state SET next_run_at=? WHERE resource_type=?",
               (now_iso(), name))


class Ingestor:
    ORG_META_KEY = "organization_id"

    def note_parent(self, body: dict) -> None:
        """Learn the organization id from `meta.parent`, which PCO puts on every
        collection response. The mirror has to echo it back on its own responses,
        and this is the only place it is ever told what it is."""
        parent = (body.get("meta") or {}).get("parent") or {}
        if parent.get("type") == "Organization" and parent.get("id"):
            if self.db.get_meta(self.ORG_META_KEY) != str(parent["id"]):
                self.db.set_meta(self.ORG_META_KEY, str(parent["id"]))

    def __init__(self, db, client: PcoClient, writer: Writer):
        self.db = db
        self.client = client
        self.writer = writer

    # -- sync_state helpers -----------------------------------------------
    def state(self, name: str) -> dict:
        row = self.db.query_one("SELECT * FROM mirror_sync_state WHERE resource_type=?", (name,))
        if row is None:
            self.db.execute("INSERT INTO mirror_sync_state(resource_type) VALUES(?)", (name,))
            row = self.db.query_one("SELECT * FROM mirror_sync_state WHERE resource_type=?", (name,))
        return dict(row)

    def _set(self, name: str, **cols) -> None:
        self.state(name)  # ensure row
        sets = ", ".join(f"{k}=:{k}" for k in cols)
        cols["_n"] = name
        self.db.execute(
            f"UPDATE mirror_sync_state SET {sets}, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            f"WHERE resource_type=:_n", cols)

    # -- nested walk (a child collection PCO only exposes per parent) -------
    def nested_walk(self, name: str, source: str = "reconcile") -> int:
        """Refresh a child collection PCO will only serve one parent at a time.

        `GET /household_memberships` is a 404; the rows exist only under
        `/households/{id}/household_memberships`. There is no `updated_at` on the
        child and no reliable one on the parent, so this is a **full walk** on a
        periodic schedule rather than a watermark sweep — the treatment a
        slowly-changing dimension wants. One request per parent.

        Each parent's answer is authoritative for that parent, so anything the
        mirror still holds for it and PCO no longer returns is tombstoned. That
        is what makes a removal visible: without it a walk could only ever add.
        """
        r = registry.by_name(name)
        parent = registry.by_name(r.parent)
        parents = [row["pco_id"] for row in self.db.query(
            f"SELECT pco_id FROM {parent.table} WHERE deleted_at IS NULL "
            f"ORDER BY CAST(pco_id AS INTEGER), pco_id")]
        applied, failed = 0, []
        for parent_id in parents:
            try:
                applied += self.walk_parent(name, parent_id, source)
            except Exception as e:  # noqa: BLE001
                # One unreachable parent must not cost the whole walk. Its rows stay
                # as they were — stale, not wrong — and the walk is not recorded as
                # complete, so it runs again rather than leaving a gap nothing knows
                # about.
                failed.append(f"{parent_id}: {e}")
        if failed:
            self._set(name, consecutive_errors=len(failed),
                      last_error=f"{len(failed)}/{len(parents)} parents failed: {failed[0][:120]}")
            raise IngestError(
                f"walk of {name} incomplete: {len(failed)}/{len(parents)} parents failed")
        self._set(name, last_sweep_completed_at=now_iso(), consecutive_errors=0, last_error=None,
                  mirror_count_last=self._live_count(r.table))
        return applied

    def walked_parents(self, name: str) -> int:
        return self.db.query_one(
            "SELECT count(*) c FROM nested_walk_state WHERE resource_type=?", (name,))["c"]

    def parent_walked(self, name: str, parent_id: str) -> bool:
        """Has this parent's child collection ever been fetched?

        The distinction the read path needs: a parent with no walk record has an
        *unknown* child collection, and unknown must not be served as empty.
        """
        return self.db.query_one(
            "SELECT 1 FROM nested_walk_state WHERE resource_type=? AND parent_pco_id=?",
            (name, parent_id)) is not None

    def ensure_parent_walked(self, name: str, parent_id: str, source: str = "passthrough") -> bool:
        """Fill one parent's child collection if it has never been fetched.

        Called from the read path, so it costs one upstream request the first
        time a parent is asked about and nothing thereafter. The periodic walk
        keeps it current from then on; this only closes the window between a
        parent appearing and the next sweep reaching it — including the window
        that opens the moment this resource is added to an existing mirror.

        Returns True if the parent's rows can now be trusted. Raises if PCO could
        not be reached, because the caller must answer with an error rather than
        an empty collection.
        """
        if self.parent_walked(name, parent_id):
            return True
        self.walk_parent(name, parent_id, source)
        return True

    #: Parents one walk step visits. A parent is one request, so this is the
    #: step's request ceiling too.
    WALK_STEP_PARENTS = 5

    def _walk_round_key(self, name: str) -> str:
        return f"walk_round:{name}"

    def walk_round(self, name: str) -> dict | None:
        held = self.db.get_meta(self._walk_round_key(name))
        return json.loads(held) if held else None

    def walk_round_step(self, name: str, budget: int = WALK_STEP_PARENTS) -> dict:
        """One bounded unit of a nested-walk refresh; call until `done`.

        The full walk is one request per live parent — five hundred households
        is five hundred requests, minutes of them — and it used to run inline,
        holding the scheduler for all of it. A round now walks at most `budget`
        parents per call, oldest-walked first, and is complete when every live
        parent has been walked since the round began. Per-parent authority is
        untouched: each parent's answer still tombstones whatever that parent
        no longer has, in `_walk_one`, exactly as the inline walk did.

        A parent that fails stays un-walked and is retried on a later step; the
        round does not complete past it, so a walk never records a sweep it did
        not finish — the same property the inline walk kept by raising.
        """
        r = registry.by_name(name)
        parent = registry.by_name(r.parent)
        state = self.walk_round(name)
        if state is None:
            state = {"started": now_iso(), "walked": 0}
            self.db.set_meta(self._walk_round_key(name), json.dumps(state))
            self._set(name, last_sweep_started_at=state["started"])
        due = self.db.query(
            f"SELECT p.pco_id FROM {parent.table} p "
            f"LEFT JOIN nested_walk_state w ON w.resource_type=? AND w.parent_pco_id=p.pco_id "
            f"WHERE p.deleted_at IS NULL AND (w.walked_at IS NULL OR w.walked_at < ?) "
            f"ORDER BY w.walked_at IS NOT NULL, w.walked_at, CAST(p.pco_id AS INTEGER) LIMIT ?",
            (name, state["started"], budget))
        if not due:
            self._set(name, last_sweep_completed_at=now_iso(), consecutive_errors=0,
                      last_error=None, mirror_count_last=self._live_count(r.table))
            self.db.execute("DELETE FROM mirror_meta WHERE key=?", (self._walk_round_key(name),))
            return {"done": True, "walked": state["walked"]}
        failed = []
        for row in due:
            try:
                self.walk_parent(name, row["pco_id"], "reconcile")
                state["walked"] += 1
            except Exception as e:  # noqa: BLE001 — the parent stays un-walked; retried later
                failed.append(f"{row['pco_id']}: {e}")
        self.db.set_meta(self._walk_round_key(name), json.dumps(state))
        if failed and len(failed) == len(due):
            # Nothing in this step landed — surface it so the guard logs it,
            # with the round intact for the next attempt.
            raise IngestError(f"walk step of {name}: all {len(failed)} parents failed: "
                              f"{failed[0][:120]}")
        return {"done": False, "walked": state["walked"]}

    def walk_parent(self, name: str, parent_id: str, source: str = "reconcile") -> int:
        """Walk exactly one parent and record that it was walked."""
        r = registry.by_name(name)
        parent = registry.by_name(r.parent)
        n = self._walk_one(r, parent, parent_id, source)
        self.db.execute(
            "INSERT INTO nested_walk_state(resource_type,parent_pco_id,walked_at,row_count) "
            "VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),?) "
            "ON CONFLICT(resource_type,parent_pco_id) DO UPDATE SET "
            "walked_at=excluded.walked_at, row_count=excluded.row_count",
            (name, parent_id, n))
        return n

    def _walk_one(self, r, parent, parent_id: str, source: str) -> int:
        seen: set[str] = set()
        offset = 0
        while True:
            resp = self.client.get(f"{parent.endpoint}/{parent_id}{r.parent_path}",
                                   {"per_page": 100, "offset": offset}, priority="reconcile")
            if resp.status == 404:
                # The parent went away between listing it and walking it — and
                # nothing else was ever going to notice. The comment here used to
                # say the parent's own sweep would tombstone it, which is not
                # something a sweep can do: it filters on `where[updated_at]`, and
                # a record that no longer exists cannot come back in a page. So a
                # household deleted at Planning Center stayed live in the mirror
                # indefinitely, was still served under `include=households`, and
                # cost one request per walk to be told again that it was gone.
                #
                # This 404 is the evidence. It is one request short of proof, and
                # that request is only ever spent on a parent whose collection has
                # just vanished, so it is worth asking.
                self._tombstone_if_absent(parent, parent_id)
                return 0
            if not resp.ok:
                raise IngestError(
                    f"walk {parent.endpoint}/{parent_id}{r.parent_path} failed: HTTP {resp.status}")
            body = resp.json() or {}
            data = body.get("data", [])
            self.writer.route_page(body, source)
            seen.update(d["id"] for d in data)
            if len(data) < 100:
                break
            offset += 100
        # Whatever this parent still has in the mirror and PCO did not return is
        # gone. `links.self` is how the row knows which parent it belongs to.
        stale = self.db.query(
            f"SELECT pco_id FROM {r.table} WHERE {r.parent_fk}=? AND deleted_at IS NULL",
            (parent_id,))
        for row in stale:
            if row["pco_id"] not in seen:
                self.writer.tombstone(r.table, row["pco_id"], None, "destroyed")
        return len(seen)

    def _tombstone_if_absent(self, r, pco_id: str) -> bool:
        """Bury a record only once PCO has answered `404` about the record itself.

        Anything short of that answer — a 500, a timeout, an unreachable host —
        leaves the row exactly as it was. Stale beats wrong: the walk runs again,
        and burying a live household would take every member's family away with
        it through the cascade.
        """
        try:
            resp = self.client.get(f"{r.endpoint}/{pco_id}", priority="reconcile")
        except Exception:  # noqa: BLE001
            return False
        if resp.status != 404:
            return False
        self.writer.tombstone(r.table, pco_id, None, "audit_absent")
        return True

    def _live_count(self, table: str) -> int:
        return self.db.query_one(f"SELECT count(*) c FROM {table} WHERE deleted_at IS NULL")["c"]

    # -- backfill ----------------------------------------------------------
    def backfill(self, name: str, max_pages: int | None = None) -> int:
        """Backfill one resource; `max_pages` makes it one bounded unit of it.

        Every cursor was already persisted page by page so a crashed backfill
        could resume; `max_pages` reuses exactly that, returning after N
        upstream pages with the phase still `backfilling`. The scheduler's cold
        lane calls it that way in a loop of ticks, so a first-day backfill no
        longer holds everything else the scheduler owes (§7.3). Completion is
        observable as `backfill_completed_at`, same as ever.
        """
        r = registry.by_name(name)
        if r.method == "nested_walk":
            if max_pages is not None:
                step = self.walk_round_step(name, budget=max_pages)
                if not step["done"]:
                    self._set(name, phase="backfilling")
                    return step["walked"]
                # The walk that just completed *is* the first sweep; without
                # pushing `next_run_at` out, the born-due default started a
                # second full round the moment the first one finished.
                self._set(name, phase="streaming", backfill_completed_at=now_iso(),
                          next_run_at=_iso_in(r.incr_interval_s))
                return step["walked"]
            n = self.nested_walk(name, source="backfill")
            self._set(name, phase="streaming", backfill_completed_at=now_iso(),
                      next_run_at=_iso_in(r.incr_interval_s))
            return n
        if r.method == "reference_periodic":
            n = self.reference_refresh(name)
            self._set(name, phase="streaming", backfill_completed_at=now_iso())
            return n
        if not r.supports_uat_filter:
            done, n = self._offset_page_all(r, source="backfill", max_pages=max_pages)
            if not done:
                self._set(name, phase="backfilling")
                return n
            hi = self._max_uat(r.table)
            self._set(name, phase="streaming", backfill_completed_at=now_iso(),
                      reconcile_watermark=hi or "", reconcile_cursor=hi or "")
            return n

        st = self.state(name)
        cursor = st["backfill_cursor_ts"] or ""
        seen = set(json.loads(st["backfill_seen_ids"] or "[]"))
        include = ",".join(r.includes) or None
        applied: set[str] = set()
        pages = 0
        while True:
            if max_pages is not None and pages >= max_pages:
                self._set(name, phase="backfilling", backfill_cursor_ts=cursor,
                          backfill_seen_ids=json.dumps(sorted(seen)))
                return len(applied)
            params = {"order": "updated_at", "per_page": 100, "include": include}
            if cursor:
                params["where[updated_at][gte]"] = cursor
            resp = self.client.get(r.endpoint, params, priority="backfill")
            pages += 1
            if not resp.ok:
                # A 404 here used to read as "the collection is empty", so the
                # backfill recorded success and the table stayed empty forever.
                raise IngestError(f"backfill {r.endpoint} failed: HTTP {resp.status}")
            body = resp.json() or {}
            self.note_parent(body)
            data = body.get("data", [])
            if not data:
                break
            self.writer.route_page(body, "backfill")
            applied.update(d["id"] for d in data)
            uats = [d["attributes"]["updated_at"] for d in data]
            max_ts = max(uats)
            if max_ts == cursor and len(data) == 100:            # saturated single second
                # The drain is re-run from its start if this unit's budget ends
                # inside it — its writes are idempotent, and the cursor only
                # advances past the second once the whole of it has been read.
                self._drain_second(r, cursor, seen, include)
                cursor = _plus_one_second(cursor)
                seen = set()
            else:
                fresh = [d for d in data if d["id"] not in seen]
                if not fresh and len(data) < 100:
                    cursor = max_ts
                    break
                seen = {d["id"] for d in data if d["attributes"]["updated_at"] == max_ts}
                cursor = max_ts
            self._set(name, phase="backfilling", backfill_cursor_ts=cursor,
                      backfill_seen_ids=json.dumps(sorted(seen)))
        self._set(name, phase="streaming", backfill_completed_at=now_iso(),
                  backfill_cursor_ts=cursor, reconcile_watermark=cursor, reconcile_cursor=cursor)
        return len(applied)

    def _drain_second(self, r, second, seen, include):
        off = 0
        while True:
            params = {"order": "updated_at", "per_page": 100, "offset": off, "include": include,
                      "where[updated_at][gte]": second, "where[updated_at][lt]": _plus_one_second(second)}
            body = self.client.get(r.endpoint, params, priority="backfill").json() or {}
            data = body.get("data", [])
            self.writer.route_page(body, "backfill")
            seen |= {d["id"] for d in data}
            if len(data) < 100:
                break
            off += 100

    def _offset_page_all(self, r, source: str, max_pages: int | None = None,
                         start_offset: int | None = None) -> tuple[bool, int]:
        """Page a collection with no `updated_at` filter; `(completed, applied)`.

        Bounded runs resume from the persisted `reconcile_cursor`, which for
        this method holds the next offset rather than a timestamp — the only
        cursor an offset walk has.
        """
        st = self.state(r.name)
        off = start_offset if start_offset is not None else (
            int(st["reconcile_cursor"]) if (st["reconcile_cursor"] or "").isdigit() else 0)
        total, pages = 0, 0
        while True:
            if max_pages is not None and pages >= max_pages:
                self._set(r.name, reconcile_cursor=str(off))
                return False, total
            body = self.client.get(r.endpoint, {"per_page": 100, "offset": off,
                                                 "include": ",".join(r.includes) or None},
                                    priority="backfill").json() or {}
            pages += 1
            data = body.get("data", [])
            total += self.writer.route_page(body, source)
            if len(data) < 100:
                self._set(r.name, reconcile_cursor="")
                return True, total
            off += 100

    # -- incremental sweep -------------------------------------------------
    def incremental_sweep(self, name: str) -> int:
        r = registry.by_name(name)
        if r.method == "nested_walk":
            return self.nested_walk(name)
        if r.method == "reference_periodic":
            return self.reference_refresh(name)
        if not r.supports_uat_filter:
            return self._descending_sweep(r)

        st = self.state(name)
        cursor = st["reconcile_watermark"] or ""
        include = ",".join(r.includes) or None
        applied = 0
        while True:
            params = {"order": "updated_at", "per_page": 100, "include": include}
            if cursor:
                params["where[updated_at][gte]"] = cursor
            body = self.client.get(r.endpoint, params, priority="reconcile").json() or {}
            data = body.get("data", [])
            if not data:
                break
            with self.db.transaction():
                applied += self.writer.route_page(body, "reconcile")
            if r.name == "person":
                for d in data:
                    self._include_diff(d, body.get("included", []), list(r.includes))
            max_ts = max(d["attributes"]["updated_at"] for d in data)
            all_boundary = all(d["attributes"]["updated_at"] == cursor for d in data)
            if max_ts == cursor and len(data) == 100:
                self._drain_second(r, cursor, set(), include)
                cursor = _plus_one_second(cursor)
            elif all_boundary and len(data) < 100:
                break
            else:
                cursor = max_ts
                if len(data) < 100:
                    break
            self._set(name, reconcile_watermark=cursor, reconcile_cursor=cursor,
                      last_sweep_completed_at=now_iso())
        self._set(name, reconcile_watermark=cursor, reconcile_cursor=cursor,
                  last_sweep_completed_at=now_iso())
        return applied

    def _descending_sweep(self, r) -> int:
        st = self.state(r.name)
        wm0 = st["reconcile_watermark"] or ""
        off, applied, hi = 0, 0, wm0
        stop = False
        while not stop:
            body = self.client.get(r.endpoint, {"order": "-updated_at", "per_page": 100,
                                                 "offset": off}, priority="reconcile").json() or {}
            data = body.get("data", [])
            for d in data:
                uat = d["attributes"]["updated_at"]
                if wm0 and uat <= wm0:
                    stop = True
                    break
                self.writer.route(d, "reconcile")
                applied += 1
                hi = max(hi, uat)
            if len(data) < 100:
                break
            off += 100
        self._set(r.name, reconcile_watermark=hi, reconcile_cursor=hi, last_sweep_completed_at=now_iso())
        return applied

    # -- reference list-and-replace ---------------------------------------
    def reference_refresh(self, name: str) -> int:
        r = registry.by_name(name)
        fetched, off = set(), 0
        while True:
            body = self.client.get(r.endpoint, {"per_page": 100, "offset": off},
                                    priority="reconcile").json() or {}
            data = body.get("data", [])
            for d in data:
                self.writer.confirm_live(r.table, d["id"], d, "reconcile")
                fetched.add(d["id"])
                if r.name == "field_definition":
                    self.writer.reproject_field_data(d["id"])
            if len(data) < 100:
                break
            off += 100
        # replace half: tombstone rows absent from the full list
        for row in self.db.query(
                f"SELECT pco_id FROM {r.table} WHERE deleted_at IS NULL", ()):
            if row["pco_id"] not in fetched:
                self.writer.tombstone(r.table, row["pco_id"], None, "absent")
        return len(fetched)

    # -- merger poll -------------------------------------------------------
    def merger_poll(self) -> int:
        """Tail `/person_mergers` and apply anything not applied already.

        The watermark filter is `gte`, deliberately (DESIGN §7.2): two merges in
        one second with a crash between them would permanently skip the second
        under `gt`. The cost is that every poll re-reads the merges sharing the
        newest `created_at`, for ever — so whether a merge is *new* has to be
        decided by the append-only log, not by the filter.

        It used to be decided by neither. The side effects ran on every row the
        filter returned, so the newest merge re-tombstoned its removed id and
        re-queued its survivor every 120 seconds, permanently: in a real mirror
        that showed up as `GET /people/{survivor}?include=…` on a perfect
        two-minute cadence, answered `404` because the survivor had since been
        deleted, in the diagnostics log for ever. `INSERT OR IGNORE` on the log
        below shows the row was always meant to be de-duplicated; only the work
        hanging off it was not.
        """
        st = self.state("person_merger")
        wm = st["merger_watermark"] or ""
        applied = 0
        while True:
            params = {"order": "created_at", "per_page": 100}
            if wm:
                params["where[created_at][gte]"] = wm
            body = self.client.get("/person_mergers", params, priority="reconcile").json() or {}
            data = body.get("data", [])
            if not data:
                break
            before, fresh = wm, 0
            for m in data:
                a = m["attributes"]
                wm = max(wm, a["created_at"])
                if not self._merger_is_new(m["id"]):
                    continue
                keep, gone = a["person_to_keep_id"], a["person_to_remove_id"]
                self.writer.tombstone("person", gone, None, "merged", merged_into=keep)
                self.enqueue_hydration("person", keep, reason="merge_survivor")
                # Recorded last: a crash before this leaves the merge looking new,
                # and re-applying one is idempotent. Recording first would make a
                # failed tombstone permanent.
                self._record_merger(m["id"], m, "reconcile")
                fresh += 1
            applied += fresh
            self._set("person_merger", merger_watermark=wm)
            # A full page that taught us nothing *and* could not move the cursor is
            # a spin, not progress: it means at least `per_page` merges share one
            # second, so `gte` keeps returning the same page. Paging on would ask
            # for it again for ever.
            if len(data) < 100 or (fresh == 0 and wm == before):
                break
        return applied

    def _merger_is_new(self, merger_id: str) -> bool:
        return self.db.query_one(
            "SELECT 1 FROM person_merger WHERE pco_id=?", (merger_id,)) is None

    def _record_merger(self, merger_id: str, raw: dict, source: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO person_merger(pco_id,raw,source,api_version) "
            "VALUES(?,?,?,?)",
            (merger_id, json.dumps(raw), source, self.writer.api_version))

    # -- delete audit ------------------------------------------------------

    #: Records the audit will re-fetch per run when it finds ids PCO has and the
    #: mirror does not. Bounds the audit's cost against its ordinary shape — a
    #: handful of gaps — while a mirror missing thousands (a half-finished
    #: backfill) converges over a few runs instead of turning the nightly audit
    #: into a full backfill.
    AUDIT_RESTORE_LIMIT = 500

    def delete_audit(self, name: str = "person") -> tuple[int, int]:
        """Reconcile the mirror's id set against PCO's, in both directions.

        The enumeration answers two questions. Ids the mirror holds live and
        PCO no longer lists are hard deletes `updated_at` filtering can never
        see — confirmed one by one, then tombstoned. Ids PCO lists and the
        mirror lacks are the reverse gap: a record a backfill, webhook and
        every sweep missed. Nothing else ever repairs those — a divergence
        report showed one two *years* behind the sweep watermark, which no
        sweep would ever collect again — so the audit, already holding the only
        full id list anybody pays for, re-fetches them.

        Run-to-completion form, for the CLI and for tests: the scheduler never
        calls this — it takes the same round one bounded step at a time through
        `delete_audit_step` (§7.3), which is also what this loops over, so the
        two cannot come to audit differently. Returns `(tombstoned, restored)`.
        """
        while True:
            outcome = self.delete_audit_step(name, budget=10_000)
            if outcome["done"]:
                return outcome["tombstoned"], outcome["restored"]

    #: How many upstream requests one audit step may spend. At the 100-per-page
    #: enumeration this is a few thousand people per tick; the point is the
    #: ceiling, not the throughput — everything else the scheduler owes runs
    #: between steps instead of behind the whole audit.
    AUDIT_STEP_REQUESTS = 8

    def _audit_round_key(self, name: str) -> str:
        return f"audit_round:{name}"

    def audit_round(self, name: str) -> dict | None:
        """The in-progress audit round's state, or None. A round in progress is
        the cold lane's to continue whatever the cadence says — abandoning it
        would leave the scratch sets to a next round that never comes."""
        held = self.db.get_meta(self._audit_round_key(name))
        return json.loads(held) if held else None

    def _save_audit_round(self, name: str, state: dict) -> None:
        self.db.set_meta(self._audit_round_key(name), json.dumps(state))

    def _scratch_count(self, name: str, kind: str) -> int:
        return self.db.query_one(
            "SELECT count(*) c FROM audit_scratch WHERE resource_type=? AND kind=?",
            (name, kind))["c"]

    def delete_audit_step(self, name: str = "person",
                          budget: int = AUDIT_STEP_REQUESTS) -> dict:
        """One bounded unit of the delete audit; call until `done`.

        The audit used to run to completion inline, and against a real
        organization that held the scheduler for minutes — hydration drains,
        webhook processing and every sweep queued behind an enumeration nobody
        could interrupt. Now the round's working sets live in `audit_scratch`,
        its phase and counters in `mirror_meta`, and each call spends at most
        `budget` upstream requests before yielding. A step that dies mid-phase
        loses nothing: the next call resumes exactly where the state says.

        The candidate snapshot is still taken *before* enumeration begins —
        first step, `start` phase — so records created while the round is in
        flight are never candidates, exactly as the one-shot form guaranteed.
        """
        r = registry.by_name(name)
        state = self.audit_round(name)
        spent = 0

        if state is None:
            self._set(name, last_audit_started_at=now_iso())
            self.db.execute("DELETE FROM audit_scratch WHERE resource_type=?", (name,))
            self.db.execute(
                f"INSERT INTO audit_scratch(resource_type, kind, pco_id) "
                f"SELECT ?, 'candidate', pco_id FROM {r.table} WHERE deleted_at IS NULL", (name,))
            self.db.execute(
                f"INSERT INTO audit_scratch(resource_type, kind, pco_id) "
                f"SELECT ?, 'known', pco_id FROM {r.table}", (name,))
            state = {"phase": "enumerate", "cursor": "", "tombstoned": 0, "restored": 0}
            self._save_audit_round(name, state)

        if state["phase"] == "enumerate":
            if "mode" not in state:
                state["mode"] = "keyset" if r.supports_cat_filter else "offset"
            while spent < budget:
                params = {"order": "created_at", "per_page": 100,
                          f"fields[{r.type}]": "created_at"}
                if state["mode"] == "keyset":
                    if state["cursor"]:
                        params["where[created_at][gte]"] = state["cursor"]
                    if state.get("offset"):
                        params["offset"] = state["offset"]
                else:
                    params["offset"] = state.get("offset", 0)
                body = self.client.get(r.endpoint, params, priority="backfill").json() or {}
                spent += 1
                data = body.get("data", [])
                for d in data:
                    self.db.execute(
                        "INSERT OR IGNORE INTO audit_scratch(resource_type,kind,pco_id) "
                        "VALUES(?,?,?)", (name, "live", d["id"]))
                if data:
                    page_max = data[-1]["attributes"]["created_at"]
                    if state["mode"] == "keyset" and state["cursor"] \
                            and page_max < state["cursor"]:
                        # A page *behind* the cursor is impossible under an
                        # honoured `gte` — it means this endpoint ignores the
                        # filter and every page came from the top. `/addresses`
                        # was measured doing exactly that, silently, and the
                        # round oscillated between two cursors for ever. Fall
                        # back to plain offset enumeration; the ids already
                        # collected dedup, so nothing is lost by starting the
                        # walk over.
                        print(f"[audit] {r.endpoint} ignores where[created_at]; "
                              f"enumerating {name} by offset", flush=True)
                        state.update(mode="offset", cursor="", offset=0)
                        self._save_audit_round(name, state)
                        continue
                    if state["mode"] == "offset":
                        state["offset"] = state.get("offset", 0) + len(data)
                    elif len(data) == 100 and page_max == state["cursor"]:
                        # A whole page inside one second — a bulk import's
                        # signature — and a keyset alone re-reads it for ever.
                        # Page *through* the saturated second by offset, exactly
                        # as the backfill's `_drain_second` does, and drop back
                        # to the keyset the moment the timestamps move again.
                        state["offset"] = state.get("offset", 0) + len(data)
                    else:
                        state["cursor"], state["offset"] = page_max, 0
                if len(data) < 100:
                    # Enumeration complete. Settle the cheap set arithmetic now,
                    # in SQL, so the per-id phases only ever see real work:
                    # candidates PCO still lists need no confirming GET, and
                    # live ids the mirror already knows need no restoring.
                    self.db.execute(
                        "DELETE FROM audit_scratch WHERE resource_type=? AND kind='candidate' "
                        "AND pco_id IN (SELECT pco_id FROM audit_scratch "
                        "               WHERE resource_type=? AND kind='live')", (name, name))
                    self.db.execute(
                        "DELETE FROM audit_scratch WHERE resource_type=? AND kind='live' "
                        "AND pco_id IN (SELECT pco_id FROM audit_scratch "
                        "               WHERE resource_type=? AND kind='known')", (name, name))
                    state["phase"] = "confirm"
                    break
                self._save_audit_round(name, state)
            self._save_audit_round(name, state)

        if state["phase"] == "confirm":
            while spent < budget:
                row = self.db.query_one(
                    "SELECT pco_id FROM audit_scratch WHERE resource_type=? AND kind='candidate' "
                    "ORDER BY CAST(pco_id AS INTEGER), pco_id LIMIT 1", (name,))
                if row is None:
                    state["phase"] = "restore"
                    break
                pid = row["pco_id"]
                resp = self.client.get(f"{r.endpoint}/{pid}", priority="backfill")
                spent += 1
                if resp.status == 404:
                    self.writer.tombstone(r.table, pid, None, "audit_absent")
                    state["tombstoned"] += 1
                elif resp.ok:
                    obj = (resp.json() or {}).get("data")
                    if obj:
                        self.writer.confirm_live(r.table, pid, obj, "reconcile")
                self.db.execute(
                    "DELETE FROM audit_scratch WHERE resource_type=? AND kind='candidate' "
                    "AND pco_id=?", (name, pid))
                self._save_audit_round(name, state)
            self._save_audit_round(name, state)

        if state["phase"] == "restore":
            while spent < budget:
                row = self.db.query_one(
                    "SELECT pco_id FROM audit_scratch WHERE resource_type=? AND kind='live' "
                    "ORDER BY CAST(pco_id AS INTEGER), pco_id LIMIT 1", (name,))
                if row is None or state["restored"] >= self.AUDIT_RESTORE_LIMIT:
                    self.db.execute("DELETE FROM audit_scratch WHERE resource_type=?", (name,))
                    self._set(name, last_audit_completed_at=now_iso())
                    # This round answers whatever drift asked for. Cleared on
                    # completion, not on start: a round that dies partway has
                    # not answered anything, and the request standing is what
                    # makes the scheduler come back.
                    clear_audit_request(self.db, name)
                    self.db.execute("DELETE FROM mirror_meta WHERE key=?",
                                    (self._audit_round_key(name),))
                    return {"done": True,
                            "tombstoned": state["tombstoned"], "restored": state["restored"]}
                pid = row["pco_id"]
                if self._restore_one(r, pid):
                    state["restored"] += 1
                spent += 1
                self.db.execute(
                    "DELETE FROM audit_scratch WHERE resource_type=? AND kind='live' "
                    "AND pco_id=?", (name, pid))
                self._save_audit_round(name, state)

        return {"done": False,
                "tombstoned": state["tombstoned"], "restored": state["restored"]}

    def _restore_one(self, r, pid: str) -> bool:
        """Fetch and store one record PCO lists that the mirror has no row for.

        A full fetch with the resource's declared includes, exactly the shape a
        hydration uses, so the restored record arrives no thinner than a synced
        one. The children ride through the ordinary router; the record itself is
        `confirm_live` — the id enumeration just said it exists, which is the
        authoritative liveness that writer exists for. A fetch that fails is
        left for the next audit rather than retried: the id list is re-derived
        every round, so a gap can only survive by outrunning every future pass.
        """
        include = ",".join(r.includes) or None
        resp = self.client.get(f"{r.endpoint}/{pid}",
                               {"include": include} if include else None,
                               priority="backfill")
        if not resp.ok:
            return False    # deleted in the race window, or transient — next round
        body = resp.json() or {}
        obj = body.get("data")
        if not obj:
            return False
        with self.db.transaction():
            self.writer.route_page({"data": [], "included": body.get("included") or []},
                                   "reconcile")
            self.writer.confirm_live(r.table, pid, obj, "reconcile")
        return True

    # -- drift probe -------------------------------------------------------

    def audit_requested(self, name: str) -> str | None:
        """The pending audit request's source, or None. See `audit_request`."""
        return audit_request(self.db, name)

    def drift_probe(self, name: str) -> dict:
        """Compare PCO's `total_count` with how many live rows the mirror holds.

        A `nested_walk` resource has no collection to count. `GET
        /household_memberships` is a 404 by design — the rows exist only under
        `/households/{id}/household_memberships` — so probing it spent a request
        every 15 minutes to write a permanent `404` into the diagnostics log, next
        to the real failures somebody is trying to read. Counting the mirror side
        is still worth doing: it is what `/admin` shows, and it costs nothing.

        A count that disagrees **asks for the audit** (DESIGN §7.4): more live
        rows than PCO ⇒ ghosts a missed delete left behind, fewer ⇒ rows nothing
        collected — and the id-set audit is the one mechanism that settles both.
        The probe only ever *requests*; when the audit runs is the scheduler's
        decision, under a cooldown, so a count PCO and the mirror genuinely
        disagree about — a population-semantics difference, not drift — costs a
        bounded re-audit rather than one per probe. A ghost used to wait for the
        nightly cadence: a person hard-deleted upstream with no webhook received
        was served live — and offered back by a duplicate-check search — for up
        to a day, while the probe wrote the discrepancy down every 15 minutes.
        """
        r = registry.by_name(name)
        mirror = self.db.query_one(
            f"SELECT count(*) c FROM {r.table} WHERE deleted_at IS NULL")["c"]
        countable = r.method != "nested_walk"
        total = None
        if countable:
            body = self.client.get(r.endpoint, {"per_page": 1}, priority="reconcile").json() or {}
            total = (body.get("meta") or {}).get("total_count")
        cols = {"mirror_count_last": mirror, "last_drift_at": now_iso()}
        if countable:
            cols["total_count_last"] = total
        self._set(name, **cols)
        if r.audit_interval_s and total is not None and mirror != total:
            request_audit(self.db, name, "drift")
        return {"resource": name, "total_count": total, "mirror_live": mirror,
                "delta": (mirror - total) if total is not None else None}

    def repair_incomplete(self, name: str, limit: int = 500,
                          min_age_seconds: int = 3600) -> int:
        """Queue a re-fetch for records the mirror holds thinner than PCO returns.

        A degraded record is invisible to every other check here. The incremental
        sweep only re-reads what `updated_at` moved, the audit only looks for
        deletions, and the drift probe only counts rows — so a person whose
        relationships were overwritten by a narrower payload stays wrong forever,
        because nothing about them will ever change again.

        That is not hypothetical: before the equal-timestamp guard existed, one
        pass-through of `/lists/{id}/people` — which PCO answers with `primary_campus`
        and nothing else — flattened 82 people in a live mirror, and their household
        edge was still missing days later. The guard stops it happening again; this
        is what puts the records back.

        Thinness is measured against the resource's declared `includes` — the set
        every mirrored copy is fetched with, so a row missing one of them was
        written by something narrower. Measuring against the *other rows* instead
        would be self-calibrating but blind in the case that matters most: a
        pass-through of a whole collection flattens every row at once, and then
        there are no richer peers left to notice.

        `min_age_seconds` is what keeps that from becoming a loop. If PCO answers
        without a relationship the registry asks for, the re-fetch cannot fix it,
        and the row would otherwise be queued again on every pass. Records
        re-fetched recently are left alone, so the worst case is one re-read per
        record per interval rather than a spin.
        """
        r = registry.by_name(name)
        expected = [i for i in r.includes if i in r.relationships]
        if not expected:
            return 0
        missing = " OR ".join(
            "NOT EXISTS (SELECT 1 FROM json_each(t.raw,'$.relationships') k WHERE k.key=?)"
            for _ in expected)
        rows = self.db.query(
            f"SELECT pco_id FROM {r.table} t WHERE deleted_at IS NULL AND ({missing}) "
            f"  AND last_synced_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?) "
            f"ORDER BY CAST(pco_id AS INTEGER), pco_id LIMIT ?",
            (*expected, f"-{int(min_age_seconds)} seconds", limit))
        for row in rows:
            self.enqueue_hydration(name, row["pco_id"], reason="incomplete")
        return len(rows)

    def repair_dangling(self, name: str, limit: int = 500,
                        min_age_seconds: int = 3600) -> int:
        """Queue a re-fetch where a stored relationship names a record we cannot serve.

        `repair_incomplete` above asks whether a relationship *key* is there.
        This asks the next question: whether the ids under it resolve. They are
        not the same question, and the gap between them is a shape the mirror was
        serving live —

          * a person created minutes earlier whose `relationships.emails` named
            an address the mirror held no row for, so `include=emails` answered
            with the id in the relationship and nothing in `included[]`;
          * a person still listing three households that had been deleted at PCO,
            so the same read named three households and sideloaded none.

        Both are documents no caller can act on and no other check can see. The
        sweep filters on `updated_at`, and neither record's will ever move again:
        the person is not edited by their email arriving late, and PCO does not
        touch the members of a household it deletes. The drift probe counts rows
        and would find both counts fine. So this is the check that looks.

        Repairing means re-reading **both ends**, because either can be the wrong
        one. Re-reading the holder is what fixes an edge PCO has dropped — the
        person comes back with `households: []` and the dangling ids are simply
        gone. Re-reading the target is what fixes, or finally settles, the other
        direction: the email arrives, or `/emails/{id}` answers `404` and
        `hydrate` tombstones it. One of the two always converges.

        Scoped to the relationships in the resource's declared `includes`, for
        the same reason `repair_incomplete` is: those are the edges every
        mirrored copy is fetched with, so they are the ones the mirror has
        undertaken to be able to serve. `min_age_seconds` bounds the cost the
        same way — if PCO keeps naming an id it will not hand over, this is one
        re-read per record per interval, not a spin.
        """
        r = registry.by_name(name)
        edges = [(i, r.relationships[i]) for i in r.includes
                 if i in r.relationships and r.relationships[i].kind in ("many", "json")]
        if not edges:
            return 0
        clauses, params = [], []
        for rel_name, rel in edges:
            target = registry.by_name(rel.target)
            clauses.append(
                f"EXISTS (SELECT 1 FROM json_each(t.raw, ?) j "
                f"        WHERE j.value ->> '$.id' IS NOT NULL "
                f"          AND NOT EXISTS (SELECT 1 FROM {target.table} c "
                f"                          WHERE c.pco_id = j.value ->> '$.id' "
                f"                            AND c.deleted_at IS NULL))")
            params.append(f"$.relationships.{rel_name}.data")
        rows = self.db.query(
            f"SELECT pco_id, raw FROM {r.table} t WHERE deleted_at IS NULL "
            f"  AND ({' OR '.join(clauses)}) "
            f"  AND last_synced_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?) "
            f"ORDER BY CAST(pco_id AS INTEGER), pco_id LIMIT ?",
            (*params, f"-{int(min_age_seconds)} seconds", limit))
        queued = 0
        for row in rows:
            self.enqueue_hydration(name, row["pco_id"], reason="dangling")
            queued += 1
            queued += self._queue_dangling_targets(json.loads(row["raw"]), edges)
        return queued

    def repair_split_edges(self, limit: int = 200, min_age_seconds: int = 3600) -> int:
        """Queue a re-read where the two copies of the household edge disagree.

        PCO stores a family twice — the household's `people` array and each
        member's own `households` array — and the mirror learns the two sides
        from different requests at different moments, so they can disagree; the
        serving layer then answers from whichever side the caller happens to
        read.

        The live case: build a family, and the synchronous post-write re-read
        races PCO's own replication — the person comes back still
        household-less, at an `updated_at` the join did not move. From there
        nothing converges: no sweep re-collects a record whose timestamp never
        changes, the household webhook repairs only the household, and
        `repair_incomplete` sees the `households` key present-and-empty and is
        satisfied. An app reading the child kept answering "nobody can reach
        this family" for hours, while PCO showed the parent the whole time.

        Either half may be the stale one, so the record that *lacks* the edge
        is the one re-read, in both directions. Same bounds as the other
        repair passes: `min_age_seconds` makes a disagreement PCO itself
        serves cost one re-read per record per interval, not a spin.
        """
        cutoff = f"-{int(min_age_seconds)} seconds"
        queued = 0
        for row in self.db.query(
                """SELECT DISTINCT p.pco_id FROM household h,
                        json_each(h.raw, '$.relationships.people.data') m
                        JOIN person p ON p.pco_id = m.value ->> '$.id'
                     WHERE h.deleted_at IS NULL AND p.deleted_at IS NULL
                       AND p.last_synced_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)
                       AND NOT EXISTS (SELECT 1 FROM
                             json_each(p.raw, '$.relationships.households.data') e
                             WHERE e.value ->> '$.id' = h.pco_id)
                     ORDER BY CAST(p.pco_id AS INTEGER), p.pco_id LIMIT ?""",
                (cutoff, limit)):
            self.enqueue_hydration("person", row["pco_id"], reason="split_edge")
            queued += 1
        for row in self.db.query(
                """SELECT DISTINCT h.pco_id FROM person p,
                        json_each(p.raw, '$.relationships.households.data') e
                        JOIN household h ON h.pco_id = e.value ->> '$.id'
                     WHERE p.deleted_at IS NULL AND h.deleted_at IS NULL
                       AND h.last_synced_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)
                       AND NOT EXISTS (SELECT 1 FROM
                             json_each(h.raw, '$.relationships.people.data') m
                             WHERE m.value ->> '$.id' = p.pco_id)
                     ORDER BY CAST(h.pco_id AS INTEGER), h.pco_id LIMIT ?""",
                (cutoff, limit)):
            self.enqueue_hydration("household", row["pco_id"], reason="split_edge")
            queued += 1
        return queued

    def _queue_dangling_targets(self, resource: dict, edges) -> int:
        """Fetch the far end of each unresolvable edge, where it is fetchable."""
        queued = 0
        for rel_name, rel in edges:
            target = registry.by_name(rel.target)
            # A `nested_walk` child has no address of its own — `GET
            # /household_memberships/{id}` is a 404 — so there is nothing to ask
            # for, and the parent walk is what repairs those anyway.
            if target.method in ("nested_walk", "passthrough_only"):
                continue
            node = ((resource.get("relationships") or {}).get(rel_name) or {}).get("data") or []
            for item in (node if isinstance(node, list) else [node]):
                if not (isinstance(item, dict) and item.get("id")):
                    continue
                if self.db.query_one(
                        f"SELECT 1 FROM {target.table} WHERE pco_id=? AND deleted_at IS NULL",
                        (str(item["id"]),)):
                    continue
                self.enqueue_hydration(target.name, str(item["id"]), reason="dangling")
                queued += 1
        return queued

    # -- hydration ---------------------------------------------------------
    def enqueue_hydration(self, name: str, pco_id: str, reason: str = "thin_webhook",
                          includes: list[str] | None = None, delay_s: int = 0) -> None:
        """Queue one record for a re-read.

        `delay_s` holds the task back — for a re-read whose whole point is to
        arrive *after* PCO's replicas have caught up with a write, where
        fetching again immediately would collect the same stale copy the
        caller is trying to escape. On a key collision the earlier time wins:
        a delayed verify must never postpone a task something else needs now.
        """
        r = registry.by_name(name)
        inc = includes or list(r.includes)
        self.db.execute(
            "INSERT INTO hydration_task(resource_type,pco_id,includes,reason,not_before) "
            "VALUES(?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now',?)) "
            "ON CONFLICT(resource_type,pco_id) DO UPDATE SET reason=excluded.reason, "
            "not_before=min(not_before, excluded.not_before)",
            (name, pco_id, json.dumps(inc), reason, f"+{int(delay_s)} seconds"))

    def drain_hydration(self, limit: int = 100) -> int:
        # `not_before` is honoured, not merely sorted by: a delayed verify that
        # ran at the next tick anyway would collect exactly the stale copy it
        # was scheduled to outwait.
        rows = self.db.query(
            "SELECT * FROM hydration_task "
            "WHERE not_before <= strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "ORDER BY not_before LIMIT ?", (limit,))
        done = 0
        for row in rows:
            self.hydrate(row["resource_type"], row["pco_id"], json.loads(row["includes"] or "[]"))
            self.db.execute("DELETE FROM hydration_task WHERE resource_type=? AND pco_id=?",
                            (row["resource_type"], row["pco_id"]))
            done += 1
        return done

    def hydrate(self, name: str, pco_id: str, includes: list[str] | None = None) -> None:
        r = registry.by_name(name)
        inc = ",".join(includes if includes is not None else r.includes) or None
        resp = self.client.get(f"{r.endpoint}/{pco_id}",
                               {"include": inc} if inc else None, priority="webhook_hydrate")
        if resp.status == 404:
            self.writer.tombstone(r.table, pco_id, None, "audit_absent")
            return
        if not resp.ok:
            return
        body = resp.json() or {}
        obj = body.get("data")
        if obj:
            self.writer.confirm_live(r.table, pco_id, obj, "reconcile") \
                if name != "person" else self.writer.route(obj, "reconcile")
            for i in body.get("included", []) or []:
                self.writer.route(i, "reconcile", owner_hint={"type": r.type, "id": pco_id})
            if name == "person":
                self._include_diff(obj, body.get("included", []),
                                   includes if includes is not None else list(r.includes))

    def _include_diff(self, person_obj: dict, included: list, requested: list) -> None:
        """Tombstone local children of a person that are absent from the fetched
        include set — catches single-child hard deletes (DESIGN §7.2).

        Which children to diff comes from what was **asked for**, not from what
        came back. Those are the same question right up until the answer is
        "none", and that is exactly the case this is for: PCO returns
        `"included": []` both for a person whose emails were not requested and for
        a person who has no emails left. Reading it off the response meant a
        person's *last* email could be deleted at PCO and stay in the mirror for
        ever — the one shape of single-child delete nothing else catches either.
        The request is not ambiguous, so it is the thing to trust.

        What `included[]` is *not* is the only half of the answer. A compound
        document has two, and they can disagree: a person created three minutes
        earlier came back from `/people?include=emails` with
        `relationships.emails` naming an address that `included[]` did not carry
        — PCO's own sideload had not caught up with the record it was sideloading
        from. Read as a delete, that would have buried an email PCO had just said
        the person has. So the relationship is consulted too, and a child it still
        names is left alone; a genuine delete removes it from both halves.
        """
        pid = person_obj["id"]
        r = registry.by_name("person")
        asked = {rel.target for name in requested
                 if (rel := r.relationships.get(name)) and rel.kind == "many"}
        present: dict[str, set] = {}
        for inc in included or []:
            cr = registry.by_type(inc.get("type", ""))
            if cr and cr.owner_rel == "person":
                present.setdefault(cr.name, set()).add(inc["id"])
        claimed = _related_ids(person_obj)
        for child in registry.RESOURCES.values():
            if child.owner_rel != "person" or child.name not in asked:
                continue        # not requested -> unknown, which is not empty
            seen_ids = present.get(child.name, set())
            for row in self.db.query(
                    f"SELECT pco_id FROM {child.table} "
                    f"WHERE person_pco_id=? AND deleted_at IS NULL", (pid,)):
                if row["pco_id"] not in seen_ids \
                        and (child.type, row["pco_id"]) not in claimed:
                    self.writer.tombstone(child.table, row["pco_id"], None, "absent")

    def _max_uat(self, table: str) -> str | None:
        row = self.db.query_one(f"SELECT max(pco_updated_at) m FROM {table}")
        return row["m"] if row else None
