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


class IngestError(RuntimeError):
    """A sync step could not complete — raised rather than recorded as success."""


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
                # The parent went away between listing it and walking it; the
                # parent's own sweep will tombstone it.
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

    def _live_count(self, table: str) -> int:
        return self.db.query_one(f"SELECT count(*) c FROM {table} WHERE deleted_at IS NULL")["c"]

    # -- backfill ----------------------------------------------------------
    def backfill(self, name: str) -> int:
        r = registry.by_name(name)
        if r.method == "nested_walk":
            n = self.nested_walk(name, source="backfill")
            self._set(name, phase="streaming", backfill_completed_at=now_iso())
            return n
        if r.method == "reference_periodic":
            n = self.reference_refresh(name)
            self._set(name, phase="streaming", backfill_completed_at=now_iso())
            return n
        if not r.supports_uat_filter:
            n = self._offset_page_all(r, source="backfill")
            hi = self._max_uat(r.table)
            self._set(name, phase="streaming", backfill_completed_at=now_iso(),
                      reconcile_watermark=hi or "", reconcile_cursor=hi or "")
            return n

        st = self.state(name)
        cursor = st["backfill_cursor_ts"] or ""
        seen = set(json.loads(st["backfill_seen_ids"] or "[]"))
        include = ",".join(r.includes) or None
        applied: set[str] = set()
        while True:
            params = {"order": "updated_at", "per_page": 100, "include": include}
            if cursor:
                params["where[updated_at][gte]"] = cursor
            resp = self.client.get(r.endpoint, params, priority="backfill")
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

    def _offset_page_all(self, r, source: str) -> int:
        off, total = 0, 0
        while True:
            body = self.client.get(r.endpoint, {"per_page": 100, "offset": off,
                                                 "include": ",".join(r.includes) or None},
                                    priority="backfill").json() or {}
            data = body.get("data", [])
            total += self.writer.route_page(body, source)
            if len(data) < 100:
                break
            off += 100
        return total

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
                    self._include_diff(d, body.get("included", []))
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
    def delete_audit(self, name: str = "person") -> int:
        r = registry.by_name(name)
        # Snapshot the mirror's live ids BEFORE enumerating PCO, so rows created
        # during the audit are never candidates (avoids a second-precision race).
        candidates = {row["pco_id"] for row in
                      self.db.query(f"SELECT pco_id FROM {r.table} WHERE deleted_at IS NULL")}
        # Stamped before the work, not after: the scheduler reads the later of
        # started/completed to decide whether an audit is due, so an audit that
        # dies partway waits its interval instead of restarting on the next tick.
        self._set(name, last_audit_started_at=now_iso())
        live, cursor = set(), ""
        while True:
            params = {"order": "created_at", "per_page": 100,
                      f"fields[{r.type}]": "created_at"}
            if cursor:
                params["where[created_at][gte]"] = cursor
            body = self.client.get(r.endpoint, params, priority="backfill").json() or {}
            data = body.get("data", [])
            if not data:
                break
            for d in data:
                live.add(d["id"])
            cursor = data[-1]["attributes"]["created_at"]
            if len(data) < 100:
                break
        tombstoned = 0
        for pid in candidates - live:
            resp = self.client.get(f"{r.endpoint}/{pid}", priority="backfill")
            if resp.status == 404:
                self.writer.tombstone(r.table, pid, None, "audit_absent")
                tombstoned += 1
            elif resp.ok:
                obj = (resp.json() or {}).get("data")
                if obj:
                    self.writer.confirm_live(r.table, pid, obj, "reconcile")
        self._set(name, last_audit_completed_at=now_iso())
        return tombstoned

    # -- drift probe -------------------------------------------------------
    def drift_probe(self, name: str) -> dict:
        """Compare PCO's `total_count` with how many live rows the mirror holds.

        A `nested_walk` resource has no collection to count. `GET
        /household_memberships` is a 404 by design — the rows exist only under
        `/households/{id}/household_memberships` — so probing it spent a request
        every 15 minutes to write a permanent `404` into the diagnostics log, next
        to the real failures somebody is trying to read. Counting the mirror side
        is still worth doing: it is what `/admin` shows, and it costs nothing.
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

    # -- hydration ---------------------------------------------------------
    def enqueue_hydration(self, name: str, pco_id: str, reason: str = "thin_webhook",
                          includes: list[str] | None = None) -> None:
        r = registry.by_name(name)
        inc = includes or list(r.includes)
        self.db.execute(
            "INSERT INTO hydration_task(resource_type,pco_id,includes,reason) VALUES(?,?,?,?) "
            "ON CONFLICT(resource_type,pco_id) DO UPDATE SET reason=excluded.reason",
            (name, pco_id, json.dumps(inc), reason))

    def drain_hydration(self, limit: int = 100) -> int:
        rows = self.db.query("SELECT * FROM hydration_task ORDER BY not_before LIMIT ?", (limit,))
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
                self._include_diff(obj, body.get("included", []))

    def _include_diff(self, person_obj: dict, included: list) -> None:
        """Tombstone local children of a person that are absent from the fetched
        include set — catches single-child hard deletes (DESIGN §7.2)."""
        pid = person_obj["id"]
        present: dict[str, set] = {}
        for inc in included or []:
            cr = registry.by_type(inc.get("type", ""))
            if cr and cr.owner_rel == "person":
                present.setdefault(cr.table, set()).add(inc["id"])
        for child in registry.RESOURCES.values():
            if child.owner_rel != "person":
                continue
            seen_ids = present.get(child.table)
            if seen_ids is None and not any(i.get("type") == child.type for i in included or []):
                # this include was not requested/returned -> don't diff (avoid false deletes)
                continue
            for row in self.db.query(
                    f"SELECT pco_id FROM {child.table} "
                    f"WHERE person_pco_id=? AND deleted_at IS NULL", (pid,)):
                if row["pco_id"] not in (seen_ids or set()):
                    self.writer.tombstone(child.table, row["pco_id"], None, "absent")

    def _max_uat(self, table: str) -> str | None:
        row = self.db.query_one(f"SELECT max(pco_updated_at) m FROM {table}")
        return row["m"] if row else None
