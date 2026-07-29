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
CREATE TABLE IF NOT EXISTS mirror_meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE IF NOT EXISTS passthrough_cache (
  cache_key TEXT PRIMARY KEY, status INTEGER, body TEXT, headers TEXT,
  fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')), expires_at TEXT NOT NULL
);
-- Which parents a `nested_walk` child has actually been walked for. A child PCO
-- only serves one parent at a time has no other way to tell "this parent has no
-- rows" from "nobody has ever asked about this parent", and those two answers
-- are opposites: the first means the household is empty, the second means the
-- mirror does not know yet. Serving an empty page for the second is how a
-- student's parent contact silently became "nobody can reach this family".
CREATE TABLE IF NOT EXISTS nested_walk_state (
  resource_type TEXT NOT NULL, parent_pco_id TEXT NOT NULL,
  walked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  row_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (resource_type, parent_pco_id)
);
-- What was asked of Planning Center and what came back, for the questions that
-- can only be asked after the fact. Every mutation, and every upstream failure
-- including ones a retry recovered from. No bodies, no headers beyond a chosen
-- few, and no query-parameter values — see diagnostics.py for why.
CREATE TABLE IF NOT EXISTS diagnostic_event (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,     -- also the tie-break: `at` is only second-precision
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  kind TEXT NOT NULL,                             -- write.applied | write.lost | upstream.error | …
  severity TEXT NOT NULL DEFAULT 'info',          -- info | warning | error
  method TEXT, target TEXT,                       -- target carries query *keys* only
  status INTEGER, duration_ms INTEGER, attempts INTEGER,
  pco_id TEXT,
  pco_request_id TEXT,                            -- PCO's x-request-id: what their support can look up
  detail TEXT, error_type TEXT, error_detail TEXT
);
CREATE INDEX IF NOT EXISTS diagnostic_event_kind_idx ON diagnostic_event (kind, event_id);
CREATE INDEX IF NOT EXISTS diagnostic_event_sev_idx  ON diagnostic_event (severity, event_id);
-- A live golden corpus: the distinct reads this mirror has actually been asked
-- for, kept so each can be re-asked of PCO and the two answers compared.
--
-- Every row is a request a caller really made. The checker never invents one —
-- a synthesised query tests something nobody does, and worse, spends the PCO
-- budget doing it. `shape` is the request with ids and paging removed, and is
-- only a grouping: it is what stops the busiest query in the building taking
-- every check, while the rows within it cover the records callers actually
-- touch.
CREATE TABLE IF NOT EXISTS shadow_sample (
  sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
  shape TEXT NOT NULL,
  path TEXT NOT NULL, query TEXT NOT NULL DEFAULT '{}',
  seen INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_checked_at TEXT, last_agreed_at TEXT,
  UNIQUE (shape, path, query)
);
CREATE INDEX IF NOT EXISTS shadow_sample_shape_idx ON shadow_sample (shape, last_checked_at);
-- Where the mirror and PCO disagreed. Both bodies are stored **pseudonymised**;
-- there is no unpseudonymised copy anywhere, so an export cannot forget to strip
-- one. `verdict` separates lag the sweep will fix from divergence nothing will.
CREATE TABLE IF NOT EXISTS shadow_report (
  report_id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  shape TEXT NOT NULL, path TEXT NOT NULL,
  verdict TEXT NOT NULL,                          -- divergence | staleness
  difference_count INTEGER NOT NULL DEFAULT 0, differences TEXT NOT NULL DEFAULT '[]',
  mirror_status INTEGER, pco_status INTEGER,
  mirror_body TEXT, pco_body TEXT,
  pco_request_id TEXT
);
CREATE INDEX IF NOT EXISTS shadow_report_verdict_idx ON shadow_report (verdict, report_id);
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


def name_matches(haystack, needle) -> int:
    """PCO's `where[search_name]` rule, measured against the live API.

    Anchored, word-wise prefixing. The needle's words must prefix the *leading*
    words of the haystack, so against `Ada Byron`: `ada`, `ada by` and `ada byron`
    match; `yron` and `byron ada` do not. `byron` matches too — not here, but
    against the `last_name` haystack, which is why the surname is searched as a
    field in its own right rather than by letting a match start anywhere.

    Both looser rules were tried and both were wrong in the permissive direction.
    Substring matching returned a hundred people where PCO returned nine; allowing
    the run to start at any word boundary still over-matched by three on a
    1,915-person organization, by finding a needle in the middle of a compound
    surname where PCO finds nothing.
    """
    if haystack is None:
        return 0
    words = (norm_text(haystack) or "").split()
    probe = (norm_text(needle) or "").split()
    if not probe:
        return 1                       # a blank needle filters nothing
    if len(probe) > len(words):
        return 0
    return int(all(words[i].startswith(probe[i]) for i in range(len(probe))))


def digits_suffix(haystack, needle) -> int:
    """`where[search_phone_number]`: the stored number *ends with* the digits typed.

    Which is how people search for a phone number — the last four, the last seven —
    and why the country code on the front of an E.164 value finds nothing.
    """
    stored, probe = norm_digits(haystack), norm_digits(needle)
    return int(bool(stored) and bool(probe) and stored.endswith(probe))


def digits_equal(haystack, needle) -> int:
    """`where[search_phone_number_e164]`: exact, once punctuation is discounted."""
    stored, probe = norm_digits(haystack), norm_digits(needle)
    return int(bool(stored) and bool(probe) and stored == probe)


def _generated_body(kind: str, spec: str | None) -> str:
    """The `(...)` an ALTER needs, or "" for a plain writer-filled column."""
    if kind == "json":
        return f"(raw ->> '{spec}')"
    if kind == "expr":
        return f"({spec})"
    return ""


def _same_expression(stored: str, body: str) -> str | bool:
    """Whitespace-insensitive: SQLite reformats nothing, but the registry might."""
    squash = lambda t: " ".join(t.split())            # noqa: E731
    return squash(body) in squash(stored)


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
            self._conn.create_function("pcm_name_match", 2, name_matches, deterministic=True)
            self._conn.create_function("pcm_digits_suffix", 2, digits_suffix, deterministic=True)
            self._conn.create_function("pcm_digits_eq", 2, digits_equal, deterministic=True)

    def init_schema(self) -> list[str]:
        """Create anything missing, then reconcile columns on tables that already
        exist. Returns the columns it had to add.

        `CREATE TABLE IF NOT EXISTS` is a no-op once a table is there, so adding a
        projection to the registry used to leave every existing deployment with a
        table the serving layer would query and SQLite would reject. SQLite cannot
        add a STORED generated column to a populated table but it can add a
        VIRTUAL one, which queries identically — it is computed on read instead of
        on write. A fresh database still gets STORED columns throughout.
        """
        with self._lock:
            self._conn.executescript(schema_sql())
            added = self._reconcile_columns()
            self._seed_walk_ledger()
            self._conn.commit()
        return added

    def _seed_walk_ledger(self) -> None:
        """Credit parents whose rows prove they were already walked.

        The ledger arrived after the walk did, so a mirror that has been walking
        for a while has the rows and none of the record. Without this, every one of
        those parents would look unvisited and be re-fetched on first read — one
        request each, for work already done. A row can only exist because its
        parent was walked, so the rows are the record.
        """
        for r in registry.full_and_lite():
            if r.method != "nested_walk" or not r.parent_fk:
                continue
            have = self._conn.execute(
                "SELECT count(*) FROM nested_walk_state WHERE resource_type=?", (r.name,)).fetchone()[0]
            if have:
                continue
            self._conn.execute(
                "INSERT OR IGNORE INTO nested_walk_state(resource_type,parent_pco_id,row_count) "
                f"SELECT ?, {r.parent_fk}, count(*) FROM {r.table} "
                f"WHERE {r.parent_fk} IS NOT NULL GROUP BY {r.parent_fk}", (r.name,))

    def _reconcile_columns(self) -> list[str]:
        """Add columns the registry gained, and repair ones whose expression changed.

        The second half matters as much as the first. Changing what a projection is
        derived from leaves an existing table with the *old* expression — the column
        is there, so nothing was missing, and the value it computes is silently
        wrong or NULL for ever. That is a feature that looks like it works: rows
        arrive, the column stays empty, and nothing complains.
        """
        changes = []
        for r in registry.RESOURCES.values():
            have = {row["name"] for row in
                    self._conn.execute(f"PRAGMA table_xinfo({r.table})").fetchall()}
            stored = self._column_definitions(r.table)
            for col, sqltype, kind, spec in r.projections:
                body = _generated_body(kind, spec)
                if col not in have:
                    self._add_column(r.table, col, sqltype, body)
                    changes.append(f"+{r.table}.{col}")
                elif body and not _same_expression(stored.get(col, ""), body):
                    self._replace_generated_column(r.table, col, sqltype, body)
                    changes.append(f"~{r.table}.{col}")
        return changes

    def _column_definitions(self, table: str) -> dict[str, str]:
        """Each column's definition as SQLite stored it, keyed by column name.

        Line-based, which is safe because this schema is generated by
        `_table_ddl` — one column per line.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        out: dict[str, str] = {}
        for line in ((row["sql"] if row else "") or "").splitlines():
            parts = line.strip().rstrip(",").split(None, 1)
            if len(parts) == 2:
                out[parts[0]] = parts[1]
        return out

    def _add_column(self, table: str, col: str, sqltype: str, body: str) -> None:
        # VIRTUAL, because SQLite cannot add a STORED generated column to a
        # populated table. It computes on read instead of on write and queries
        # identically; a fresh database still gets STORED throughout.
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sqltype}"
                           + (f" GENERATED ALWAYS AS {body} VIRTUAL" if body else ""))

    def _replace_generated_column(self, table: str, col: str, sqltype: str, body: str) -> None:
        """Drop and re-add a generated column so it picks up its new expression.

        Safe by definition: a generated column holds nothing of its own. Any index
        over it has to go first, and is rebuilt from the DDL SQLite kept.
        """
        indexes = [(r["name"], r["sql"]) for r in self._conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL", (table,)).fetchall() if col in (r["sql"] or "")]
        for name, _ in indexes:
            self._conn.execute(f"DROP INDEX IF EXISTS {name}")
        self._conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        self._add_column(table, col, sqltype, body)
        for _, sql in indexes:
            self._conn.execute(sql)

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

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO mirror_meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                     "updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')", (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM mirror_meta WHERE key=?", (key,))
        return row["value"] if row else None

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
