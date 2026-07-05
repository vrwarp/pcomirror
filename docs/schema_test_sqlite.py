#!/usr/bin/env python3
"""
pcomirror — SQLite canonical-writer semantics regression test.

Verifies the correctness properties the design review flagged (DESIGN.md §3, §10),
against docs/schema.sqlite.sql. The four "writer functions" are application code
here (SQLite has no stored procedures); the SQL below is the canonical contract
documented in schema.sqlite.sql §7.

    python3 docs/schema_test_sqlite.py      # prints 11 PASS lines, exits 0 on success
"""
import sqlite3, json, os, sys

APIV = "2026-06-04"
NOW  = "2026-07-05T00:00:00Z"   # the app's ISO-8601 UTC clock (fixed for deterministic tests)

HERE = os.path.dirname(os.path.abspath(__file__))
db = sqlite3.connect(":memory:")
db.executescript(open(os.path.join(HERE, "schema.sqlite.sql")).read())

# ---- fixtures ----
def person(pid, fn, ln, uat, created="2020-01-01T00:00:00Z"):
    return json.dumps({"id": pid, "type": "Person", "attributes": {
        "first_name": fn, "last_name": ln, "status": "active",
        "created_at": created, "updated_at": uat},
        "relationships": {"primary_campus": {"data": {"type": "PrimaryCampus", "id": "3"}}}})

def fd(pid, ctype, cid, defid, val, uat):
    return json.dumps({"id": pid, "type": "FieldDatum", "attributes": {
        "value": val, "created_at": "2020-01-01T00:00:00Z", "updated_at": uat},
        "relationships": {"customizable": {"data": {"type": ctype, "id": cid}},
                          "field_definition": {"data": {"type": "FieldDefinition", "id": defid}}}})

def hm(pid, per, hh):
    return json.dumps({"id": pid, "type": "HouseholdMembership", "attributes": {"household_role": "adult"},
        "relationships": {"person": {"data": {"type": "Person", "id": per}},
                          "household": {"data": {"type": "Household", "id": hh}}}})

# ---- the canonical writer (schema.sqlite.sql §7), as the app issues it ----
def mirror_upsert(t, pid, raw, source="webhook", now=NOW):
    db.execute(f"""INSERT INTO {t}(pco_id,raw,first_seen_at,last_synced_at,source,api_version)
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
      {"pid": pid, "raw": raw, "now": now, "source": source, "av": APIV}); db.commit()

def mirror_upsert_untimed(t, pid, raw, source="webhook", now=NOW):
    db.execute(f"""INSERT INTO {t}(pco_id,raw,first_seen_at,last_synced_at,source,api_version)
      VALUES(:pid,:raw,:now,:now,:source,:av)
      ON CONFLICT(pco_id) DO UPDATE SET last_synced_at=:now, source=excluded.source,
        raw=CASE WHEN deleted_at IS NULL THEN excluded.raw ELSE raw END,
        api_version=CASE WHEN deleted_at IS NULL THEN excluded.api_version ELSE api_version END""",
      {"pid": pid, "raw": raw, "now": now, "source": source, "av": APIV}); db.commit()

def mirror_tombstone(t, pid, uat, reason, merged=None, now="2099-01-01T00:00:00Z"):
    db.execute(f"""UPDATE {t} SET deleted_at=:now, tombstone_uat=coalesce(:uat,pco_updated_at,:now),
        tombstone_reason=:reason, merged_into_pco_id=coalesce(:merged,merged_into_pco_id),
        last_synced_at=:now, source='reconcile'
      WHERE pco_id=:pid AND (:reason='merged' OR deleted_at IS NOT NULL OR pco_updated_at IS NULL
                             OR coalesce(:uat,pco_updated_at)>=pco_updated_at)""",
      {"pid": pid, "uat": uat, "reason": reason, "merged": merged, "now": now}); db.commit()

def mirror_confirm_live(t, pid, raw, source="reconcile", now=NOW):
    db.execute(f"""INSERT INTO {t}(pco_id,raw,first_seen_at,last_synced_at,source,api_version)
      VALUES(:pid,:raw,:now,:now,:source,:av)
      ON CONFLICT(pco_id) DO UPDATE SET raw=excluded.raw, api_version=excluded.api_version,
        source=excluded.source, last_synced_at=:now,
        deleted_at=NULL, tombstone_uat=NULL, tombstone_reason=NULL, merged_into_pco_id=NULL""",
      {"pid": pid, "raw": raw, "now": now, "source": source, "av": APIV}); db.commit()

def one(sql): return db.execute(sql).fetchone()
def check(cond, label):
    print(("PASS" if cond else "FAIL") + ": " + label)
    if not cond: sys.exit(1)

# T1 generated projections
mirror_upsert("person", "100", person("100", "Ada", "Lovelace", "2026-01-01T00:00:00Z"), "backfill")
check(one("SELECT first_name,last_name,primary_campus_id,pco_updated_at FROM person WHERE pco_id='100'")
      == ("Ada", "Lovelace", "3", "2026-01-01T00:00:00Z"), "T1 generated projections")

# T2 older write: data no-op, last_synced_at still advances
db.execute("UPDATE person SET last_synced_at='2000-01-01T00:00:00Z' WHERE pco_id='100'")
mirror_upsert("person", "100", person("100", "STALE", "STALE", "2025-01-01T00:00:00Z"))
check(one("SELECT last_name,last_synced_at FROM person WHERE pco_id='100'") == ("Lovelace", NOW),
      "T2 older write: data no-op, last_synced_at advances")

# T3 newer wins
mirror_upsert("person", "100", person("100", "Ada", "Byron", "2026-02-01T00:00:00Z"))
check(one("SELECT last_name FROM person WHERE pco_id='100'")[0] == "Byron", "T3 newer write wins")

# T4 same-second correction (>=)
mirror_upsert("person", "200", person("200", "Grace", "WRONG", "2026-03-01T12:00:00Z"))
mirror_upsert("person", "200", person("200", "Grace", "Hopper", "2026-03-01T12:00:00Z"))
check(one("SELECT last_name FROM person WHERE pco_id='200'")[0] == "Hopper", "T4 same-second correction overwrites")

# T5 sticky tombstone survives reordered older update
mirror_tombstone("person", "200", "2026-03-02T00:00:00Z", "destroyed")
mirror_upsert("person", "200", person("200", "Grace", "Reorder", "2026-03-01T12:00:00Z"))
check(one("SELECT deleted_at FROM person WHERE pco_id='200'")[0] is not None,
      "T5 sticky tombstone survives reordered older update")

# T6 newer-than-tombstone update resurrects a non-merge tombstone
mirror_upsert("person", "200", person("200", "Grace", "Revived", "2026-03-03T00:00:00Z"))
check(one("SELECT deleted_at,last_name FROM person WHERE pco_id='200'") == (None, "Revived"),
      "T6 newer update resurrects non-merge tombstone")

# T7 merge tombstone is terminal under a later update
mirror_tombstone("person", "200", "2026-03-04T00:00:00Z", "merged", "999")
mirror_upsert("person", "200", person("200", "Grace", "ShouldNotRevive", "2026-03-05T00:00:00Z"))
r = one("SELECT deleted_at,merged_into_pco_id FROM person WHERE pco_id='200'")
check(r[0] is not None and r[1] == "999", "T7 merge tombstone terminal under update")

# T8 authoritative confirm_live overrides a merge
mirror_confirm_live("person", "200", person("200", "Grace", "Authoritative", "2026-03-06T00:00:00Z"))
check(one("SELECT deleted_at,merged_into_pco_id FROM person WHERE pco_id='200'") == (None, None),
      "T8 confirm_live overrides merge")

# T9 polymorphic field_datum owner
mirror_upsert("field_datum", "fd1", fd("fd1", "Person", "100", "def1", "baptized", "2026-01-01T00:00:00Z"), "backfill")
mirror_upsert("field_datum", "fd2", fd("fd2", "Organization", "1", "def2", "orgwide", "2026-01-01T00:00:00Z"), "backfill")
check(one("SELECT person_pco_id FROM field_datum WHERE pco_id='fd1'")[0] == "100"
      and one("SELECT person_pco_id FROM field_datum WHERE pco_id='fd2'")[0] is None,
      "T9 polymorphic field_datum owner (Person vs Organization)")

# T10 person_custom_fields view
db.execute("INSERT INTO field_definition(pco_id,raw,source,api_version) VALUES('def1',?,'backfill',?)",
           (json.dumps({"id": "def1", "type": "FieldDefinition",
                        "attributes": {"name": "Baptized", "slug": "baptized", "data_type": "string"}}), APIV))
db.execute("UPDATE field_datum SET value_text='baptized' WHERE pco_id='fd1'"); db.commit()
check(json.loads(one("SELECT fields FROM person_custom_fields WHERE person_pco_id='100'")[0]).get("baptized") == "baptized",
      "T10 person_custom_fields view resolves slug")

# T11 timestamp-less: destroyed terminal under at-least-once redelivered create
mirror_upsert_untimed("household_membership", "hm1", hm("hm1", "100", "h1"))
mirror_tombstone("household_membership", "hm1", None, "destroyed")
mirror_upsert_untimed("household_membership", "hm1", hm("hm1", "100", "h1"))
check(one("SELECT deleted_at FROM household_membership WHERE pco_id='hm1'")[0] is not None,
      "T11 untimed tombstone terminal under at-least-once redelivery")

print("\n--- all SQLite canonical-writer semantics verified (11/11) ---")
