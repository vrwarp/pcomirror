-- =====================================================================
-- pcomirror — SQLite schema (default store for a single small church)
-- Target: SQLite 3.45+  (generated columns, ->> operator, UPSERT ... WHERE)
-- Profile: ONE organization, ONE process, no Redis, no server.
-- API version pinned:  X-PCO-API-Version = 2026-06-04
--
-- This is the DEFAULT deployment. docs/schema.sql is the Postgres variant
-- for the scale-up / multi-tenant path (see DESIGN.md §0, §12).
--
-- What is the same as Postgres: raw JSON is the system of record; every
-- queryable column is a generated projection of it; ONE monotonic writer
-- with sticky tombstones. What is simpler here: no org_id, no RLS, no KMS,
-- timestamps are ISO-8601 TEXT (lexicographic compare == chronological, so
-- no parsing), and the four "writer functions" are application code that
-- issues the canonical statements in §7 (SQLite has no stored procedures).
--
-- Run in WAL mode so the webhook receiver, workers and reader can share the
-- one file (single writer, many readers):
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ---------------------------------------------------------------------
-- SHARED COLUMN CONTRACT (every mirrored table carries exactly this)
--   pco_id           TEXT PRIMARY KEY
--   raw              TEXT NOT NULL          -- the JSON:API resource verbatim
--   pco_created_at   TEXT GENERATED         -- raw.attributes.created_at (ISO-8601)
--   pco_updated_at   TEXT GENERATED         -- raw.attributes.updated_at  (the monotonic clock)
--   first_seen_at    TEXT
--   last_synced_at   TEXT                    -- ALWAYS bumped when PCO confirms the row
--   source           TEXT CHECK(...)
--   api_version      TEXT
--   deleted_at       TEXT                    -- NULL = live; else tombstone
--   tombstone_uat    TEXT                    -- pco_updated_at captured at tombstone (sticky guard)
--   tombstone_reason TEXT                    -- destroyed | merged | audit_absent | absent
--   merged_into_pco_id TEXT
-- ISO-8601 UTC strings (…Z, fixed width) sort chronologically as TEXT, so the
-- >= / > guards work directly with no date parsing.
-- ---------------------------------------------------------------------

-- ===== ANCHOR: person =====
CREATE TABLE person (
  pco_id TEXT PRIMARY KEY,
  raw    TEXT NOT NULL,
  -- scalar projections
  first_name  TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.first_name')  STORED,
  last_name   TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.last_name')   STORED,
  name        TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.name')        STORED,
  nickname    TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.nickname')    STORED,
  status      TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.status')      STORED,  -- active|inactive|pending
  membership  TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.membership')  STORED,
  gender      TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.gender')      STORED,
  grade       TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.grade')       STORED,
  child       INTEGER GENERATED ALWAYS AS (raw ->> '$.attributes.child')    STORED,  -- 0/1
  birthdate   TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.birthdate')   STORED,
  anniversary TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.anniversary') STORED,
  primary_email TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.primary_email_address') STORED,
  search_name   TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.search_name') STORED,
  remote_id     TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.remote_id')   STORED,
  -- relationship ids (join keys; no cross-table FKs — eventual integrity)
  primary_campus_id  TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.primary_campus.data.id')  STORED,
  marital_status_id  TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.marital_status.data.id')  STORED,
  name_prefix_id     TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.name_prefix.data.id')     STORED,
  name_suffix_id     TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.name_suffix.data.id')     STORED,
  -- shared bookkeeping contract
  pco_created_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.created_at') STORED,
  pco_updated_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.updated_at') STORED,
  first_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source         TEXT NOT NULL CHECK (source IN ('webhook','backfill','reconcile','passthrough')),
  api_version    TEXT NOT NULL,
  deleted_at     TEXT,
  tombstone_uat  TEXT,
  tombstone_reason TEXT,
  merged_into_pco_id TEXT
);
CREATE INDEX person_uat_idx    ON person (pco_updated_at);
CREATE INDEX person_name_idx   ON person (last_name, first_name) WHERE deleted_at IS NULL;
CREATE INDEX person_status_idx ON person (status)                WHERE deleted_at IS NULL;
CREATE INDEX person_email_idx  ON person (primary_email)         WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX person_remote_idx ON person (remote_id) WHERE remote_id IS NOT NULL AND deleted_at IS NULL;
-- Optional fuzzy local search: an FTS5 table over search_name kept in sync by triggers,
-- or just LIKE — at 300 people a scan is instant.

-- ===== CHILD PATTERN: email  (same for phone_number, address, social_profile, note, background_check) =====
CREATE TABLE email (
  pco_id TEXT PRIMARY KEY,
  raw    TEXT NOT NULL,
  person_pco_id TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.person.data.id') STORED,
  address    TEXT    GENERATED ALWAYS AS (raw ->> '$.attributes.address')  STORED,
  location   TEXT    GENERATED ALWAYS AS (raw ->> '$.attributes.location') STORED,
  is_primary INTEGER GENERATED ALWAYS AS (raw ->> '$.attributes.primary')  STORED,
  blocked    INTEGER GENERATED ALWAYS AS (raw ->> '$.attributes.blocked')  STORED,
  pco_created_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.created_at') STORED,
  pco_updated_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.updated_at') STORED,
  first_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source TEXT NOT NULL CHECK (source IN ('webhook','backfill','reconcile','passthrough')),
  api_version TEXT NOT NULL,
  deleted_at TEXT, tombstone_uat TEXT, tombstone_reason TEXT, merged_into_pco_id TEXT
);
CREATE INDEX email_person_idx ON email (person_pco_id) WHERE deleted_at IS NULL;

-- ===== CUSTOM FIELDS: field_datum (polymorphic owner) + schema tables =====
CREATE TABLE field_datum (
  pco_id TEXT PRIMARY KEY,
  raw    TEXT NOT NULL,
  customizable_type TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.customizable.data.type') STORED,
  customizable_id   TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.customizable.data.id')   STORED,
  person_pco_id     TEXT GENERATED ALWAYS AS (
      CASE WHEN raw ->> '$.relationships.customizable.data.type' = 'Person'
           THEN raw ->> '$.relationships.customizable.data.id' END) STORED,
  field_definition_id TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.field_definition.data.id') STORED,
  field_option_id     TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.field_option.data.id')     STORED,
  value    TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.value') STORED,
  file_url TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.file')  STORED,
  -- typed columns (need field_definition.data_type): filled by app, re-derived when the definition is (re)mirrored
  value_text TEXT, value_number REAL, value_date TEXT, value_bool INTEGER,
  pco_created_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.created_at') STORED,
  pco_updated_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.updated_at') STORED,
  first_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source TEXT NOT NULL CHECK (source IN ('webhook','backfill','reconcile','passthrough')),
  api_version TEXT NOT NULL,
  deleted_at TEXT, tombstone_uat TEXT, tombstone_reason TEXT, merged_into_pco_id TEXT
);
CREATE INDEX field_datum_person_idx ON field_datum (person_pco_id)       WHERE deleted_at IS NULL;
CREATE INDEX field_datum_def_idx    ON field_datum (field_definition_id) WHERE deleted_at IS NULL;

-- field_definition / field_option / tab are TIMESTAMP-LESS (no updated_at in the spec):
-- use the untimed writer + list-and-replace reconcile (DESIGN §7.1).
CREATE TABLE field_definition (
  pco_id TEXT PRIMARY KEY,
  raw    TEXT NOT NULL,
  name      TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.name')      STORED,
  slug      TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.slug')      STORED,
  data_type TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.data_type') STORED,
  tab_id    TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.tab.data.id') STORED,
  pco_created_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.created_at') STORED,
  pco_updated_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.updated_at') STORED, -- NULL: none in spec
  first_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source TEXT NOT NULL CHECK (source IN ('webhook','backfill','reconcile','passthrough')),
  api_version TEXT NOT NULL,
  deleted_at TEXT, tombstone_uat TEXT, tombstone_reason TEXT, merged_into_pco_id TEXT
);
CREATE INDEX field_definition_slug_idx ON field_definition (slug) WHERE deleted_at IS NULL;

-- Single source of truth for custom-field values (no denormalized blob).
CREATE VIEW person_custom_fields AS
SELECT fd.person_pco_id,
       json_group_object(def.slug,
         coalesce(fd.value_bool, fd.value_number, fd.value_date, fd.value_text, fd.value)) AS fields
FROM field_datum fd
JOIN field_definition def ON def.pco_id = fd.field_definition_id AND def.deleted_at IS NULL
WHERE fd.deleted_at IS NULL AND fd.person_pco_id IS NOT NULL
GROUP BY fd.person_pco_id;

-- ===== households + person_merger audit =====
CREATE TABLE household (
  pco_id TEXT PRIMARY KEY, raw TEXT NOT NULL,
  name TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.name') STORED,
  pco_created_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.created_at') STORED,
  pco_updated_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.updated_at') STORED,
  first_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source TEXT NOT NULL CHECK (source IN ('webhook','backfill','reconcile','passthrough')),
  api_version TEXT NOT NULL,
  deleted_at TEXT, tombstone_uat TEXT, tombstone_reason TEXT, merged_into_pco_id TEXT
);

-- household_membership: TIMESTAMP-LESS join resource → untimed writer + list-and-replace.
CREATE TABLE household_membership (
  pco_id TEXT PRIMARY KEY, raw TEXT NOT NULL,
  household_pco_id TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.household.data.id') STORED,
  person_pco_id    TEXT GENERATED ALWAYS AS (raw ->> '$.relationships.person.data.id')    STORED,
  household_role TEXT    GENERATED ALWAYS AS (raw ->> '$.attributes.household_role') STORED,
  pending        INTEGER GENERATED ALWAYS AS (raw ->> '$.attributes.pending')        STORED,
  pco_created_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.created_at') STORED,
  pco_updated_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.updated_at') STORED, -- NULL
  first_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source TEXT NOT NULL CHECK (source IN ('webhook','backfill','reconcile','passthrough')),
  api_version TEXT NOT NULL,
  deleted_at TEXT, tombstone_uat TEXT, tombstone_reason TEXT, merged_into_pco_id TEXT
);
CREATE INDEX hm_person_idx    ON household_membership (person_pco_id)    WHERE deleted_at IS NULL;
CREATE INDEX hm_household_idx ON household_membership (household_pco_id) WHERE deleted_at IS NULL;

CREATE TABLE person_merger (
  pco_id TEXT PRIMARY KEY, raw TEXT NOT NULL,
  person_to_keep_id   TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.person_to_keep_id')   STORED,
  person_to_remove_id TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.person_to_remove_id') STORED,
  pco_created_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.created_at') STORED,
  applied_at TEXT, first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source TEXT NOT NULL CHECK (source IN ('webhook','backfill','reconcile','passthrough')),
  api_version TEXT NOT NULL
);
CREATE INDEX person_merger_created_idx ON person_merger (pco_created_at);

-- ===== reference example: campus (LITE) =====
CREATE TABLE campus (
  pco_id TEXT PRIMARY KEY, raw TEXT NOT NULL,
  name TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.name') STORED,
  pco_created_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.created_at') STORED,
  pco_updated_at TEXT GENERATED ALWAYS AS (raw ->> '$.attributes.updated_at') STORED,
  first_seen_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source TEXT NOT NULL CHECK (source IN ('webhook','backfill','reconcile','passthrough')),
  api_version TEXT NOT NULL,
  deleted_at TEXT, tombstone_uat TEXT, tombstone_reason TEXT, merged_into_pco_id TEXT
);
-- Additional FULL: phone_number, address, social_profile, note, background_check, list_result, workflow_card.
-- Additional LITE: tab, field_option, list, marital_status, membership_type, name_prefix, name_suffix,
--   inactive_reason, school_option, carrier, app, form, form_category, form_submission, person_app.

-- =====================================================================
-- 7. THE CANONICAL WRITER  (application code issues these exact statements —
--    SQLite has no stored procedures, so this block IS the contract)
-- =====================================================================
--
-- All four are parameterized; :now is the app's ISO-8601 UTC clock. The
-- monotonic guard rides on ISO-8601 TEXT comparison (chronological == lexical).
--
-- 7a. mirror_upsert(table, pco_id, raw, source, api_version)  -- resources WITH updated_at
--   INSERT INTO {table} (pco_id, raw, first_seen_at, last_synced_at, source, api_version)
--   VALUES (:pco_id, :raw, :now, :now, :source, :api_version)
--   ON CONFLICT(pco_id) DO UPDATE SET
--     last_synced_at = :now,
--     source         = excluded.source,
--     raw            = CASE WHEN excluded.pco_updated_at >= pco_updated_at THEN excluded.raw ELSE raw END,
--     api_version    = CASE WHEN excluded.pco_updated_at >= pco_updated_at THEN excluded.api_version ELSE api_version END,
--     deleted_at     = CASE
--                        WHEN deleted_at IS NULL                    THEN NULL
--                        WHEN tombstone_reason = 'merged'           THEN deleted_at
--                        WHEN excluded.pco_updated_at > tombstone_uat THEN NULL
--                        ELSE deleted_at END,
--     tombstone_uat  = CASE WHEN deleted_at IS NOT NULL AND tombstone_reason <> 'merged'
--                            AND excluded.pco_updated_at > tombstone_uat THEN NULL ELSE tombstone_uat END,
--     tombstone_reason = CASE WHEN deleted_at IS NOT NULL AND tombstone_reason <> 'merged'
--                            AND excluded.pco_updated_at > tombstone_uat THEN NULL ELSE tombstone_reason END;
--
-- 7b. mirror_upsert_untimed(table, pco_id, raw, source, api_version)  -- field_definition/field_option/tab/household_membership
--   INSERT INTO {table} (pco_id, raw, first_seen_at, last_synced_at, source, api_version)
--   VALUES (:pco_id, :raw, :now, :now, :source, :api_version)
--   ON CONFLICT(pco_id) DO UPDATE SET
--     last_synced_at = :now, source = excluded.source,
--     raw         = CASE WHEN deleted_at IS NULL THEN excluded.raw ELSE raw END,
--     api_version = CASE WHEN deleted_at IS NULL THEN excluded.api_version ELSE api_version END;
--   -- a create/update NEVER clears a tombstone; only mirror_confirm_live (list-and-replace) does.
--
-- 7c. mirror_tombstone(table, pco_id, :uat, :reason, :merged_into)
--   UPDATE {table} SET
--     deleted_at = :now, tombstone_uat = coalesce(:uat, pco_updated_at, :now),
--     tombstone_reason = :reason, merged_into_pco_id = coalesce(:merged_into, merged_into_pco_id),
--     last_synced_at = :now, source = 'reconcile'
--   WHERE pco_id = :pco_id
--     AND ( :reason = 'merged'                     -- merges always win
--           OR deleted_at IS NOT NULL              -- already dead: refresh reason/uat
--           OR pco_updated_at IS NULL              -- timestamp-less: no clock to supersede
--           OR coalesce(:uat, pco_updated_at) >= pco_updated_at );
--
-- 7d. mirror_confirm_live(table, pco_id, raw, source, api_version)  -- authoritative live GET -> 200
--   INSERT INTO {table} (pco_id, raw, first_seen_at, last_synced_at, source, api_version)
--   VALUES (:pco_id, :raw, :now, :now, :source, :api_version)
--   ON CONFLICT(pco_id) DO UPDATE SET
--     raw = excluded.raw, api_version = excluded.api_version, source = excluded.source,
--     last_synced_at = :now,
--     deleted_at = NULL, tombstone_uat = NULL, tombstone_reason = NULL, merged_into_pco_id = NULL;

-- =====================================================================
-- 8. OPERATIONAL TABLES  (single org — no org_id)
-- =====================================================================
CREATE TABLE mirror_sync_state (
  resource_type TEXT PRIMARY KEY,
  phase TEXT NOT NULL DEFAULT 'idle',               -- idle|backfilling|streaming|reconciling
  backfill_cursor_ts   TEXT,
  backfill_seen_ids    TEXT NOT NULL DEFAULT '[]',   -- JSON array of ids at the cursor second
  backfill_completed_at TEXT,
  reconcile_watermark  TEXT NOT NULL DEFAULT '',     -- '' sorts before any ISO-8601 date; only the sweep advances it
  reconcile_cursor     TEXT NOT NULL DEFAULT '',
  last_sweep_started_at TEXT, last_sweep_completed_at TEXT,
  merger_watermark     TEXT NOT NULL DEFAULT '',
  audit_cursor         TEXT,
  last_audit_started_at TEXT, last_audit_completed_at TEXT,
  total_count_last  INTEGER, mirror_count_last INTEGER, last_drift_at TEXT,  -- drift probe writes these
  consecutive_errors INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  next_run_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE sync_policy (
  resource_type TEXT PRIMARY KEY,
  endpoint TEXT NOT NULL, method TEXT NOT NULL,
  supports_uat_filter INTEGER NOT NULL, timestamped INTEGER NOT NULL DEFAULT 1,
  incr_interval_s INTEGER, audit_interval_s INTEGER,
  priority INTEGER NOT NULL, per_page INTEGER NOT NULL DEFAULT 100,
  include TEXT, parent_type TEXT, enabled INTEGER NOT NULL DEFAULT 1
);

-- webhook envelope audit (exact bytes once per delivery)
CREATE TABLE webhook_delivery (
  delivery_id TEXT PRIMARY KEY,
  subscription_pco_id TEXT,
  signature TEXT NOT NULL,
  raw_body  BLOB NOT NULL,                          -- exact verified bytes (re-HMAC / replay)
  attempt INTEGER, received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- per-event inbox / idempotency ledger (one row per data[].id; handles batched deliveries)
CREATE TABLE webhook_event (
  event_id TEXT PRIMARY KEY,
  delivery_id TEXT REFERENCES webhook_delivery(delivery_id),
  event_name TEXT NOT NULL, resource_type TEXT, action TEXT, pco_id TEXT,
  payload TEXT NOT NULL,                            -- parsed JSON:API resource
  status TEXT NOT NULL DEFAULT 'pending',           -- pending|processing|done|skipped_stale|dead
  process_attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
  received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  processed_at TEXT, last_error TEXT
);
CREATE INDEX webhook_event_status_idx ON webhook_event (status, next_attempt_at);

CREATE TABLE hydration_task (
  resource_type TEXT NOT NULL, pco_id TEXT NOT NULL,
  includes TEXT NOT NULL DEFAULT '[]', reason TEXT,
  not_before TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  enqueued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (resource_type, pco_id)
);

CREATE TABLE webhook_dead_letter (
  event_id TEXT PRIMARY KEY, event_name TEXT, payload TEXT,
  last_error TEXT, attempts INTEGER, died_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- every call to the receiver, exactly as it arrived — including the ones nothing
-- was captured from, which is where the diagnostic value is. `webhook_delivery`
-- above holds the bytes of the deliveries that were *accepted*; a bad signature,
-- an unknown token or a body that would not parse left no trace at all, and the
-- header that decided it was never kept. Verbatim: every header, the exact body,
-- nothing redacted — see pcomirror/webhooklog.py for why, and what it costs.
-- A ring buffer of `keep` rows (PCOMIRROR_WEBHOOK_RECORD_KEEP); `body` holds the
-- first MAX_BODY bytes and `body_bytes` the true length, because the receiver
-- answers before it knows who is calling.
CREATE TABLE webhook_call (
  call_id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  method TEXT NOT NULL, path TEXT NOT NULL, query TEXT NOT NULL DEFAULT '',
  url_token TEXT, remote_addr TEXT,
  headers TEXT NOT NULL DEFAULT '{}',      -- every header, verbatim, as a JSON object
  body BLOB NOT NULL DEFAULT x'',          -- exact bytes, to MAX_BODY
  body_bytes INTEGER NOT NULL DEFAULT 0,   -- the true length, truncated or not
  truncated INTEGER NOT NULL DEFAULT 0,
  status INTEGER,                          -- NULL = the receiver raised rather than answered
  note TEXT, duration_ms INTEGER,
  delivery_id TEXT, event_name TEXT,       -- read out of the body, best effort, as an index
  event_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX webhook_call_status_idx ON webhook_call (status, call_id);

CREATE TABLE reconcile_run (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  resource_type TEXT, kind TEXT, started_at TEXT, completed_at TEXT, status TEXT,
  watermark_before TEXT, watermark_after TEXT,
  rows_seen INTEGER, rows_upserted INTEGER, rows_tombstoned INTEGER, requests_used INTEGER, error TEXT
);

-- webhook subscriptions: one row per event name, which is PCO's own unit — a
-- WebhookSubscription carries a single `name`, a single `url` and its own
-- `authenticity_secret`. `url_token` is deliberately NOT unique: several
-- subscriptions may point at one receiver URL, which is what PCO's console does
-- when you tick a column of events, and the receiver resolves which one is
-- delivering by the secret that signed the body (DESIGN §6.1-6.2).
-- secret_ref points at where the encrypted authenticity_secret is kept (OS keyring / a
-- 0600 secrets file / an app-held key) — NOT stored in plaintext here.
-- `managed` records who last wrote the row: 'env' (PCOMIRROR_SUBSCRIPTIONS) or
-- 'admin' (the operator page, which takes precedence once used).
CREATE TABLE webhook_subscription (
  subscription_pco_id TEXT PRIMARY KEY,
  event_name TEXT NOT NULL, resource TEXT, action TEXT,
  url_token TEXT NOT NULL,
  secret_ref TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  managed TEXT NOT NULL DEFAULT 'env',
  last_event_at TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX webhook_subscription_token_idx ON webhook_subscription (url_token, active);

-- Local API keys for the serving plane (separate from the upstream PCO PAT).
CREATE TABLE api_key (
  id TEXT PRIMARY KEY, prefix TEXT NOT NULL, key_hash BLOB NOT NULL,  -- sha256(secret)
  name TEXT,
  scopes TEXT NOT NULL DEFAULT 'read:people',        -- JSON/CSV: read:people|read:reference|read:deleted|passthrough|write
  rate_limit_per_min INTEGER NOT NULL DEFAULT 600,
  passthrough_quota_per_min INTEGER NOT NULL DEFAULT 60,
  disabled_at TEXT, last_used_at TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE passthrough_cache (
  cache_key TEXT PRIMARY KEY, status INTEGER, body TEXT, headers TEXT,
  fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), expires_at TEXT NOT NULL
);
