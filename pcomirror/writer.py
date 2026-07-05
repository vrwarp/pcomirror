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
        t = self._table(table)
        now = now or now_iso()
        self.db.execute(
            f"""INSERT INTO {t}(pco_id,raw,first_seen_at,last_synced_at,source,api_version)
                VALUES(:pid,:raw,:now,:now,:source,:av)
                ON CONFLICT(pco_id) DO UPDATE SET
                  last_synced_at=:now, source=excluded.source,
                  raw=CASE WHEN excluded.pco_updated_at>=pco_updated_at THEN excluded.raw ELSE raw END,
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
        if t == "field_datum":
            self._project_field_datum(pco_id)

    # -- sideload router ---------------------------------------------------
    def route(self, resource: dict, source: str, owner_hint: dict | None = None) -> None:
        """Route one JSON:API resource (from data[] or included[]) to its table."""
        r = registry.by_type(resource.get("type", ""))
        if r is None:
            return  # unmirrored type in an include set — ignore
        # Ensure an owned child's back-reference is present so its owner id projects.
        if r.owner_rel and owner_hint:
            rels = resource.setdefault("relationships", {})
            if r.owner_rel not in rels or not rels[r.owner_rel].get("data"):
                rels[r.owner_rel] = {"data": owner_hint}
        if r.timestamped:
            self.upsert(r.table, resource["id"], resource, source)
        else:
            self.upsert_untimed(r.table, resource["id"], resource, source)

    def route_page(self, body: dict, source: str) -> int:
        """Apply a JSON:API list response's data[] + included[] to the mirror."""
        n = 0
        for item in body.get("data", []) if isinstance(body.get("data"), list) else [body.get("data")]:
            if item:
                self.route(item, source)
                n += 1
        for inc in body.get("included", []) or []:
            self.route(inc, source)
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
