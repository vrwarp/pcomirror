"""SQLite storage: schema generation from the registry + a thread-safe handle.

The mirrored-resource tables are generated from `registry.RESOURCES` using the
shared column contract documented in docs/schema.sqlite.sql, so all resources get
an identical, correct shape and adding a resource never means hand-writing DDL.
Operational tables and the custom-fields view are static SQL below.
"""
from __future__ import annotations

import sqlite3
import threading

from . import registry

# Columns every mirrored table carries in addition to its projections (DESIGN §4.2).
_BOOKKEEPING = """
  pco_created_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.created_at') STORED,
  pco_updated_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.updated_at') STORED,
  first_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  source         TEXT NOT NULL CHECK (source IN ('webhook','backfill','reconcile','passthrough')),
  api_version    TEXT NOT NULL,
  deleted_at     TEXT, tombstone_uat TEXT, tombstone_reason TEXT, merged_into_pco_id TEXT
"""

_OPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS mirror_sync_state (
  resource_type TEXT PRIMARY KEY,
  phase TEXT NOT NULL DEFAULT 'idle',
  backfill_cursor_ts TEXT, backfill_seen_ids TEXT NOT NULL DEFAULT '[]', backfill_completed_at TEXT,
  reconcile_watermark TEXT NOT NULL DEFAULT '', reconcile_cursor TEXT NOT NULL DEFAULT '',
  last_sweep_started_at TEXT, last_sweep_completed_at TEXT,
  merger_watermark TEXT NOT NULL DEFAULT '',
  audit_cursor TEXT, last_audit_started_at TEXT, last_audit_completed_at TEXT,
  total_count_last INTEGER, mirror_count_last INTEGER, last_drift_at TEXT,
  consecutive_errors INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  next_run_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS webhook_delivery (
  delivery_id TEXT PRIMARY KEY, subscription_pco_id TEXT, signature TEXT NOT NULL,
  raw_body BLOB NOT NULL, attempt INTEGER,
  received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS webhook_event (
  event_id TEXT PRIMARY KEY, delivery_id TEXT REFERENCES webhook_delivery(delivery_id),
  event_name TEXT NOT NULL, resource_type TEXT, action TEXT, pco_id TEXT,
  payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  process_attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
  received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  processed_at TEXT, last_error TEXT
);
CREATE INDEX IF NOT EXISTS webhook_event_status_idx ON webhook_event (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS hydration_task (
  resource_type TEXT NOT NULL, pco_id TEXT NOT NULL, includes TEXT NOT NULL DEFAULT '[]',
  reason TEXT, not_before TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  enqueued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (resource_type, pco_id)
);
CREATE TABLE IF NOT EXISTS webhook_dead_letter (
  event_id TEXT PRIMARY KEY, event_name TEXT, payload TEXT, last_error TEXT, attempts INTEGER,
  died_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS reconcile_run (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT, kind TEXT,
  started_at TEXT, completed_at TEXT, status TEXT,
  watermark_before TEXT, watermark_after TEXT,
  rows_seen INTEGER, rows_upserted INTEGER, rows_tombstoned INTEGER, requests_used INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS webhook_subscription (
  subscription_pco_id TEXT PRIMARY KEY, event_name TEXT NOT NULL, resource TEXT, action TEXT,
  url_token TEXT UNIQUE NOT NULL,
  authenticity_secret TEXT NOT NULL,   -- encrypt at rest in production (keyring / KMS); see DESIGN §9.1
  active INTEGER NOT NULL DEFAULT 1, last_event_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS api_key (
  id TEXT PRIMARY KEY, prefix TEXT NOT NULL, key_hash BLOB NOT NULL, name TEXT,
  scopes TEXT NOT NULL DEFAULT 'read:people',
  rate_limit_per_min INTEGER NOT NULL DEFAULT 600, passthrough_quota_per_min INTEGER NOT NULL DEFAULT 60,
  disabled_at TEXT, last_used_at TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS admin_account (
  id INTEGER PRIMARY KEY CHECK (id = 1),          -- single operator account
  password_hash BLOB NOT NULL, password_salt BLOB NOT NULL, iterations INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS admin_session (
  token_hash BLOB PRIMARY KEY,                    -- sha256(token); the token itself is never stored
  csrf TEXT NOT NULL, must_change_password INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passthrough_cache (
  cache_key TEXT PRIMARY KEY, status INTEGER, body TEXT, headers TEXT,
  fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')), expires_at TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS person_custom_fields AS
SELECT fd.person_pco_id,
       json_group_object(def.slug,
         coalesce(fd.value_bool, fd.value_number, fd.value_date, fd.value_text, fd.value)) AS fields
FROM field_datum fd
JOIN field_definition def ON def.pco_id = fd.field_definition_id AND def.deleted_at IS NULL
WHERE fd.deleted_at IS NULL AND fd.person_pco_id IS NOT NULL
GROUP BY fd.person_pco_id;
"""


def norm_text(value) -> str | None:
    """Case- and whitespace-folded text, for PCO's `where[search_*]` matching.

    NULL stays NULL so `instr(pcm_norm(col), ?)` is falsy for a missing column
    rather than matching every row on the empty string.
    """
    if value is None:
        return None
    return " ".join(str(value).split()).lower()


def norm_digits(value) -> str | None:
    """Digits only — so `555-0100` and `(555) 0100` are the same phone number."""
    if value is None:
        return None
    return "".join(ch for ch in str(value) if ch.isdigit())


def _projection_sql(proj: registry.Projection) -> str:
    col, sqltype, kind, spec = proj
    if kind == "json":
        return f"  {col} {sqltype} GENERATED ALWAYS AS (raw ->> '{spec}') STORED"
    if kind == "expr":
        return f"  {col} {sqltype} GENERATED ALWAYS AS ({spec}) STORED"
    return f"  {col} {sqltype}"  # plain, writer-filled


def _table_ddl(r: registry.Resource) -> list[str]:
    cols = ["  pco_id TEXT PRIMARY KEY", "  raw TEXT NOT NULL"]
    cols += [_projection_sql(p) for p in r.projections]
    cols.append(_BOOKKEEPING.strip("\n"))
    ddl = [f"CREATE TABLE IF NOT EXISTS {r.table} (\n" + ",\n".join(cols) + "\n);"]
    ddl.append(f"CREATE INDEX IF NOT EXISTS {r.table}_uat_idx ON {r.table} (pco_updated_at);")
    proj_cols = {p[0] for p in r.projections}
    for fk in ("person_pco_id", "household_pco_id", "field_definition_id"):
        if fk in proj_cols:
            ddl.append(f"CREATE INDEX IF NOT EXISTS {r.table}_{fk}_idx "
                       f"ON {r.table} ({fk}) WHERE deleted_at IS NULL;")
    return ddl


def schema_sql() -> str:
    parts: list[str] = ["PRAGMA foreign_keys=ON;"]
    for r in registry.RESOURCES.values():
        parts += _table_ddl(r)
    parts.append(_OPS_SCHEMA)
    return "\n".join(parts)


class Database:
    """Thread-safe SQLite handle. All access is serialized under one lock — fine at
    this scale and keeps WAL semantics simple (single writer, many readers)."""

    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            # The normalisers behind `where[search_*]`. Python functions rather
            # than SQL expressions so the serving layer folds the needle with the
            # exact same code that folds the column — a search that disagrees with
            # itself about whitespace is worse than one that does not exist.
            self._conn.create_function("pcm_norm", 1, norm_text, deterministic=True)
            self._conn.create_function("pcm_digits", 1, norm_digits, deterministic=True)

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(schema_sql())
            self._conn.commit()

    def execute(self, sql: str, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executescript(self, sql: str):
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def transaction(self):
        return _Txn(self)

    def close(self):
        with self._lock:
            self._conn.close()


class _Txn:
    """Context manager exposing the raw connection under the DB lock for
    multi-statement atomic units (used by reconcile page-commits)."""

    def __init__(self, db: Database):
        self._db = db

    def __enter__(self):
        self._db._lock.acquire()
        return self._db._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._db._conn.commit()
            else:
                self._db._conn.rollback()
        finally:
            self._db._lock.release()
        return False
