"""The canonical writer — the one path every mutation goes through (DESIGN §3).

These four operations issue exactly the SQL verified in docs/schema_test_sqlite.py.
Table names are validated against the registry (never interpolated from user
input) so the dynamic-table SQL is injection-safe.
"""
from __future__ import annotations

import json
from typing import Any

from . import registry
from .config import now_iso
from .db import Database


def _stable_ts(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


class Writer:
    def __init__(self, db: Database, api_version: str):
        self.db = db
        self.api_version = api_version

    def _table(self, table: str) -> str:
        if table not in registry._BY_TABLE and table not in ("person_merger",):
            raise ValueError(f"unknown table {table!r}")
        return table

    # -- 7a. resources WITH updated_at -------------------------------------
    def upsert(self, table: str, pco_id: str, raw: dict, source: str, now: str | None = None) -> None:
        """At an equal `updated_at`, a write may not make a record poorer.

        Two payloads carrying the same timestamp describe the same state, so
        neither is newer and "last one wins" is arbitrary — but they are not
        equally complete. PCO returns a resource in `included[]` with only the
        relationships that document needed, and returns different sets for
        different requests, so a person sideloaded as a member of somebody else's
        household, or fetched with a narrower `include=`, is a strictly thinner
        record than the same person fetched with a wider one. Letting the thinner
        one land deleted their emails and phone numbers until the next sweep.

        A genuinely newer payload (`>`) always wins, so a relationship that really
        was removed still lands — removing it moves `updated_at`.

        The test is a **superset first**, then a count. Two payloads can carry the
        same number of relationships and different ones — `/lists/{id}/people`
        returns a Person with `primary_campus` alone, and a narrower `include=`
        returns a different set again — so counting alone let an equal-sized
        payload drop the one relationship the caller needed.

        The count is kept as the tiebreak because a superset on its own is a
        one-way door: a flattened record holding `primary_campus` could never be
        replaced by a richer payload that happens not to carry that one key, and
        the record would be stuck wrong forever. Strictly-more-relationships is
        allowed through, which is what lets a re-fetch repair one.

        Either way `raw` stays verbatim — one of the two payloads is stored whole,
        never a merge of both.
        """
        t = self._table(table)
        now = now or now_iso()
        self.db.execute(
            f"""INSERT INTO {t}(pco_id,raw,first_seen_at,last_synced_at,source,api_version)
                VALUES(:pid,:raw,:now,:now,:source,:av)
                ON CONFLICT(pco_id) DO UPDATE SET
                  last_synced_at=:now, source=excluded.source,
                  raw=CASE WHEN excluded.pco_updated_at>pco_updated_at THEN excluded.raw
                           WHEN excluded.pco_updated_at<pco_updated_at THEN raw
                           WHEN NOT EXISTS (SELECT 1 FROM json_each({t}.raw,'$.relationships') held
                                            WHERE held.key NOT IN
                                              (SELECT key FROM json_each(excluded.raw,'$.relationships')))
                                THEN excluded.raw
                           WHEN (SELECT count(*) FROM json_each(excluded.raw,'$.relationships'))
                              > (SELECT count(*) FROM json_each({t}.raw,'$.relationships'))
                                THEN excluded.raw
                           ELSE raw END,
                  api_version=CASE WHEN excluded.pco_updated_at>=pco_updated_at THEN excluded.api_version ELSE api_version END,
                  deleted_at=CASE WHEN deleted_at IS NULL THEN NULL
                                  WHEN tombstone_reason='merged' THEN deleted_at
                                  WHEN excluded.pco_updated_at>tombstone_uat THEN NULL ELSE deleted_at END,
                  tombstone_uat=CASE WHEN deleted_at IS NOT NULL AND tombstone_reason<>'merged'
                                      AND excluded.pco_updated_at>tombstone_uat THEN NULL ELSE tombstone_uat END,
                  tombstone_reason=CASE WHEN deleted_at IS NOT NULL AND tombstone_reason<>'merged'
                                      AND excluded.pco_updated_at>tombstone_uat THEN NULL ELSE tombstone_reason END""",
            {"pid": pco_id, "raw": json.dumps(raw, separators=(",", ":")),
             "now": now, "source": source, "av": self.api_version},
        )
        # The realistic way a wrongly-buried person comes back: a webhook or a
        # sweep carrying a strictly newer payload. `confirm_live` is the other,
        # and only the audit's `200` branch reaches it — which never sees a
        # tombstoned row, because it only considers rows the mirror thinks live.
        self._uncascade(t, pco_id, now)
        if t == "field_datum":
            self._project_field_datum(pco_id)

    # -- 7b. timestamp-less resources -------------------------------------
    def upsert_untimed(self, table: str, pco_id: str, raw: dict, source: str, now: str | None = None) -> None:
        t = self._table(table)
        now = now or now_iso()
        self.db.execute(
            f"""INSERT INTO {t}(pco_id,raw,first_seen_at,last_synced_at,source,api_version)
                VALUES(:pid,:raw,:now,:now,:source,:av)
                ON CONFLICT(pco_id) DO UPDATE SET last_synced_at=:now, source=excluded.source,
                  raw=CASE WHEN deleted_at IS NULL THEN excluded.raw ELSE raw END,
                  api_version=CASE WHEN deleted_at IS NULL THEN excluded.api_version ELSE api_version END""",
            {"pid": pco_id, "raw": json.dumps(raw, separators=(",", ":")),
             "now": now, "source": source, "av": self.api_version},
        )

    #: Reasons that mean the record is *gone*, as opposed to folded into another
    #: one. A merge does not delete a person's emails — PCO moves them to the
    #: survivor — so cascading on `merged` would tombstone rows PCO had just
    #: reassigned, and the survivor's hydration re-routes them at their existing
    #: `updated_at`, which the monotonic guard would refuse to resurrect.
    GONE = frozenset({"destroyed", "audit_absent", "absent"})
    #: Written on a child so the cascade is legible in the log and reversible as a
    #: unit: these rows were never independently deleted.
    OWNER_DELETED = "owner_deleted"

    # -- 7c. tombstone (destroyed / merge / audit-absent) -----------------
    def tombstone(self, table: str, pco_id: str, uat: str | None, reason: str,
                  merged_into: str | None = None, now: str | None = None) -> None:
        t = self._table(table)
        now = now or now_iso()
        self.db.execute(
            f"""UPDATE {t} SET deleted_at=:now, tombstone_uat=coalesce(:uat,pco_updated_at,:now),
                  tombstone_reason=:reason, merged_into_pco_id=coalesce(:merged,merged_into_pco_id),
                  last_synced_at=:now, source='reconcile'
                WHERE pco_id=:pid AND (:reason='merged' OR deleted_at IS NOT NULL OR pco_updated_at IS NULL
                                       OR coalesce(:uat,pco_updated_at)>=pco_updated_at)""",
            {"pid": pco_id, "uat": _stable_ts(uat), "reason": reason, "merged": merged_into, "now": now},
        )
        if reason in self.GONE:
            self._cascade(t, pco_id, now)

    def _cascade(self, table: str, pco_id: str, now: str) -> None:
        """A child cannot outlive the record that owns it.

        Every other way a child gets tombstoned needs the owner to still be there
        to ask about: `_include_diff` compares a fetched person's `include=` set
        against what the mirror holds, and `_walk_one` re-reads a live household's
        memberships. When the owner itself is gone — a `404` on hydration, an
        absence the audit confirmed, a `destroyed` webhook — neither can run
        again, and nothing else looks: a child's own sweep filters on
        `where[updated_at]`, which cannot return a row that no longer exists.

        So the emails, phone numbers and addresses of a hard-deleted person
        stayed live in the mirror **for ever**, and `GET /emails` kept serving
        that person's address after `GET /people/{id}` had started answering 404.
        """
        owner = registry.by_table(table)
        for child, fk in registry.owned_children(owner.name) if owner else ():
            self.db.execute(
                f"""UPDATE {child.table}
                      SET deleted_at=:now, tombstone_uat=coalesce(pco_updated_at,:now),
                          tombstone_reason=:reason, last_synced_at=:now, source='reconcile'
                    WHERE {fk}=:pid AND deleted_at IS NULL""",
                {"pid": pco_id, "now": now, "reason": self.OWNER_DELETED})

    # -- 7d. authoritative live confirmation (audit 200 / list-replace) ---
    def confirm_live(self, table: str, pco_id: str, raw: dict, source: str, now: str | None = None) -> None:
        t = self._table(table)
        now = now or now_iso()
        self.db.execute(
            f"""INSERT INTO {t}(pco_id,raw,first_seen_at,last_synced_at,source,api_version)
                VALUES(:pid,:raw,:now,:now,:source,:av)
                ON CONFLICT(pco_id) DO UPDATE SET raw=excluded.raw, api_version=excluded.api_version,
                  source=excluded.source, last_synced_at=:now,
                  deleted_at=NULL, tombstone_uat=NULL, tombstone_reason=NULL, merged_into_pco_id=NULL""",
            {"pid": pco_id, "raw": json.dumps(raw, separators=(",", ":")),
             "now": now, "source": source, "av": self.api_version},
        )
        self._uncascade(t, pco_id, now)
        if t == "field_datum":
            self._project_field_datum(pco_id)

    def _uncascade(self, table: str, pco_id: str, now: str) -> None:
        """The other half of `_cascade`: this record is authoritatively alive, so
        the children that were only ever tombstoned *because it wasn't* are too.

        Only rows carrying `owner_deleted` — a child deleted on its own evidence
        keeps its tombstone. Without this, putting the owner back would leave its
        emails buried: the re-fetched copies carry their existing `updated_at`,
        and the monotonic guard resurrects only on strictly newer.

        The `EXISTS` is what makes this safe to call after *every* write rather
        than only after one known to have resurrected something. An ordinary
        upsert of a still-tombstoned person — a sweep re-reading a record it
        already has, at a timestamp too old to revive it — would otherwise dig up
        children whose owner is still buried.
        """
        owner = registry.by_table(table)
        for child, fk in registry.owned_children(owner.name) if owner else ():
            self.db.execute(
                f"""UPDATE {child.table}
                      SET deleted_at=NULL, tombstone_uat=NULL, tombstone_reason=NULL,
                          last_synced_at=:now
                    WHERE {fk}=:pid AND tombstone_reason=:reason
                      AND EXISTS (SELECT 1 FROM {owner.table}
                                   WHERE pco_id=:pid AND deleted_at IS NULL)""",
                {"pid": pco_id, "now": now, "reason": self.OWNER_DELETED})

    # -- sideload router ---------------------------------------------------
    def route(self, resource: dict, source: str, owner_hint: dict | None = None,
              synthesized: frozenset = frozenset()) -> None:
        """Route one JSON:API resource (from data[] or included[]) to its table."""
        r = registry.by_type(resource.get("type", ""))
        if r is None:
            return  # unmirrored type in an include set — ignore
        if r.timestamped and not (resource.get("attributes") or {}).get("updated_at"):
            # Not a record — a *partial representation* of one. A caller passing
            # `fields[Person]=first_name` through to PCO gets exactly that back, and
            # storing it replaced the mirrored person with a single attribute and a
            # NULL `pco_updated_at`. Worse, the monotonic guard compares against
            # that NULL and every comparison is false, so the record could never be
            # repaired by a later write. Warm the mirror only from something that
            # carries the timestamp the guard is built on.
            return
        # Ensure an owned child's back-reference is present so its owner id projects.
        if r.owner_rel and owner_hint:
            rels = resource.setdefault("relationships", {})
            if r.owner_rel not in rels or not rels[r.owner_rel].get("data"):
                rels[r.owner_rel] = {"data": owner_hint}
        stored = resource
        rels = resource.get("relationships")
        if synthesized and isinstance(rels, dict):
            # PCO answers `include=households.people` by adding a `people`
            # relationship to the *Person*. It is an artefact of the request, not
            # part of the record, and storing it made every later read of that
            # person claim a relationship PCO does not have.
            #
            # Copied rather than popped in place: on a pass-through this same
            # object is the response being handed back to the caller, who asked
            # for that include and is entitled to PCO's answer verbatim.
            drop = {n for n in synthesized if n not in r.relationships and n in rels}
            if drop:
                stored = dict(resource)
                stored["relationships"] = {k: v for k, v in rels.items() if k not in drop}
        if r.timestamped:
            self.upsert(r.table, stored["id"], stored, source)
        else:
            self.upsert_untimed(r.table, stored["id"], stored, source)

    @staticmethod
    def synthesized_rels(include: str | None) -> frozenset:
        """The relationship names PCO invents in response to a nested `include`."""
        if not include:
            return frozenset()
        return frozenset(tok.partition(".")[2] for tok in include.split(",") if "." in tok)

    def route_page(self, body: dict, source: str, synthesized: frozenset = frozenset(),
                   data_owner_hint: dict | None = None) -> int:
        """Apply a JSON:API list response's data[] + included[] to the mirror.

        `included[]` is applied *first*, so the primary representation wins.

        A compound document can carry the same resource twice, and the two copies
        are not equally good. `GET /people/X?include=households.people` returns X
        in `data` with every requested relationship resolved, and returns X again
        in `included` — as a member of their own household — carrying almost none.
        Applying `data` first let the thin sideloaded copy overwrite it a moment
        later (same `updated_at`, so the monotonic guard correctly allows the
        write), and the person lost their emails, phone numbers and households.
        The primary resource is the one the request was about; it goes last.
        """
        n = 0
        # `included[]` first, so that when two copies are equally complete the
        # primary one — the resource the request was actually about — lands last.
        for inc in body.get("included", []) or []:
            self.route(inc, source, synthesized=synthesized)
        for item in body.get("data", []) if isinstance(body.get("data"), list) else [body.get("data")]:
            if item:
                # The hint applies to the primary resource only. `POST
                # /people/{id}/emails` names the owner in the URL and PCO's reply
                # need not repeat it; without the hint the email lands with a NULL
                # `person_pco_id` and is invisible under the person who has it.
                # An `included[]` resource is somebody else's by definition, so it
                # must never inherit this — that would attach one person's contact
                # details to another.
                self.route(item, source, owner_hint=data_owner_hint, synthesized=synthesized)
                n += 1
        return n

    # -- field_datum typed columns ----------------------------------------
    def _project_field_datum(self, pco_id: str) -> None:
        row = self.db.query_one(
            "SELECT fd.value AS value, def.data_type AS data_type "
            "FROM field_datum fd LEFT JOIN field_definition def "
            "  ON def.pco_id = fd.field_definition_id "
            "WHERE fd.pco_id=?", (pco_id,))
        if row is None:
            return
        val, dt = row["value"], (row["data_type"] or "")
        v_text = v_num = v_date = v_bool = None
        if val is not None:
            if dt in ("number", "integer", "float", "currency"):
                try:
                    v_num = float(val)
                except (TypeError, ValueError):
                    pass
            elif dt in ("date", "datetime"):
                v_date = val
            elif dt in ("boolean", "checkbox"):
                v_bool = 1 if str(val).lower() in ("true", "1", "yes", "t") else 0
            else:
                v_text = val
        self.db.execute(
            "UPDATE field_datum SET value_text=?, value_number=?, value_date=?, value_bool=? WHERE pco_id=?",
            (v_text, v_num, v_date, v_bool, pco_id))

    def reproject_field_data(self, field_definition_id: str) -> None:
        """Re-derive typed columns for all field_data of a definition (DESIGN §4.3)."""
        for row in self.db.query(
                "SELECT pco_id FROM field_datum WHERE field_definition_id=?", (field_definition_id,)):
            self._project_field_datum(row["pco_id"])
