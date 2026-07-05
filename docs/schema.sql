-- =====================================================================
-- pcomirror — canonical PostgreSQL schema (single source of truth)
-- Target: PostgreSQL 15+  (generated columns, RLS, MERGE-free upserts)
-- API version pinned:  X-PCO-API-Version = 2026-06-04  (People spec info.version)
--
-- This file is the authoritative resolution of the cross-section design.
-- It encodes the canonical decisions from DESIGN.md §3:
--   * ONE guarded writer (mirror_upsert / mirror_upsert_untimed /
--     mirror_tombstone / mirror_confirm_live) that every path calls.
--   * ONE reconciliation state table (mirror_sync_state).
--   * ONE webhook inbox keyed per-event, with exact-bytes envelope storage.
--   * Singular physical table names; JSON:API `type` is a serialization concern.
--   * Sticky tombstones via tombstone_uat / tombstone_reason.
--   * Polymorphic field_datum owner (customizable_type/id).
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. Extensions, enums, immutable parsers
-- ---------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy local search on search_name
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- gen_random_uuid()

CREATE TYPE mirror_source AS ENUM ('webhook','backfill','reconcile','passthrough');

-- PCO always emits ISO-8601 timestamps with an explicit UTC offset ("...Z").
-- Under that invariant the text->timestamptz parse is deterministic, so we
-- may legally declare these wrappers IMMUTABLE and use them in GENERATED
-- columns (a plain ::timestamptz cast is only STABLE and Postgres rejects it
-- in a generated expression). Dates carry no zone and are immutable anyway.
CREATE FUNCTION pco_ts(t text) RETURNS timestamptz
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT nullif(t,'')::timestamptz $$;

CREATE FUNCTION pco_date(t text) RETURNS date
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT nullif(t,'')::date $$;

CREATE FUNCTION pco_int(t text) RETURNS bigint
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT nullif(regexp_replace(coalesce(t,''),'[^0-9-]','','g'),'')::bigint $$;

-- =====================================================================
-- 1. SHARED COLUMN CONTRACT
--
-- Every mirrored resource table MUST carry exactly this bookkeeping set so
-- the single canonical writer (§3) is table-agnostic:
--
--   org_id          bigint       -- tenant discriminator (0 = the single default org)
--   pco_id          text         -- the PCO resource id
--   raw             jsonb        -- {attributes, relationships, links} verbatim = system of record
--   pco_created_at  timestamptz  GENERATED from raw.attributes.created_at
--   pco_updated_at  timestamptz  GENERATED from raw.attributes.updated_at  (the monotonic clock)
--   first_seen_at   timestamptz  DEFAULT now()
--   last_synced_at  timestamptz  -- ALWAYS bumped whenever PCO confirms the row (even a losing write)
--   source          mirror_source
--   api_version     date         -- the X-PCO-API-Version that produced `raw`
--   deleted_at      timestamptz  -- NULL = live; non-NULL = tombstone
--   tombstone_uat   timestamptz  -- pco_updated_at captured at tombstone time (sticky-undelete guard)
--   tombstone_reason text        -- 'destroyed' | 'merged' | 'audit_absent' | 'absent'
--   merged_into_pco_id text      -- redirect target when tombstone_reason='merged'
--   PRIMARY KEY (org_id, pco_id)
--
-- Everything else on a table is a GENERATED projection of `raw` (queryable
-- columns) — a pure function of raw, so a version bump re-projects with zero
-- API calls. Writers never set projections; they only ever write raw + the
-- bookkeeping the functions manage.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 2. ANCHOR: person  (FULL tier)
-- ---------------------------------------------------------------------
CREATE TABLE person (
  org_id  bigint NOT NULL DEFAULT 0,
  pco_id  text   NOT NULL,
  raw     jsonb  NOT NULL,

  -- immutable scalar projections (can_query_by / can_order_by surface)
  first_name    text GENERATED ALWAYS AS (raw->'attributes'->>'first_name')  STORED,
  last_name     text GENERATED ALWAYS AS (raw->'attributes'->>'last_name')   STORED,
  name          text GENERATED ALWAYS AS (raw->'attributes'->>'name')        STORED,
  nickname      text GENERATED ALWAYS AS (raw->'attributes'->>'nickname')    STORED,
  given_name    text GENERATED ALWAYS AS (raw->'attributes'->>'given_name')  STORED,
  status        text GENERATED ALWAYS AS (raw->'attributes'->>'status')      STORED,
  membership    text GENERATED ALWAYS AS (raw->'attributes'->>'membership')  STORED,
  gender        text GENERATED ALWAYS AS (raw->'attributes'->>'gender')      STORED,
  grade         text GENERATED ALWAYS AS (raw->'attributes'->>'grade')       STORED,
  primary_email text GENERATED ALWAYS AS (raw->'attributes'->>'primary_email_address') STORED,
  search_name   text GENERATED ALWAYS AS (raw->'attributes'->>'search_name') STORED,
  remote_id     bigint GENERATED ALWAYS AS (pco_int(raw->'attributes'->>'remote_id')) STORED,
  child         boolean GENERATED ALWAYS AS ((raw->'attributes'->>'child')::boolean) STORED,
  graduation_year int   GENERATED ALWAYS AS (nullif(raw->'attributes'->>'graduation_year','')::int) STORED,

  -- temporal projections (immutable via pco_* wrappers)
  birthdate      date        GENERATED ALWAYS AS (pco_date(raw->'attributes'->>'birthdate'))      STORED,
  anniversary    date        GENERATED ALWAYS AS (pco_date(raw->'attributes'->>'anniversary'))    STORED,
  inactivated_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'inactivated_at'))   STORED,

  -- denormalized relationship ids (join keys; NO cross-table FK constraints, see DESIGN §3.4)
  primary_campus_id  text GENERATED ALWAYS AS (raw->'relationships'->'primary_campus'->'data'->>'id')  STORED,
  inactive_reason_id text GENERATED ALWAYS AS (raw->'relationships'->'inactive_reason'->'data'->>'id') STORED,
  marital_status_id  text GENERATED ALWAYS AS (raw->'relationships'->'marital_status'->'data'->>'id')  STORED,
  name_prefix_id     text GENERATED ALWAYS AS (raw->'relationships'->'name_prefix'->'data'->>'id')     STORED,
  name_suffix_id     text GENERATED ALWAYS AS (raw->'relationships'->'name_suffix'->'data'->>'id')     STORED,
  school_id          text GENERATED ALWAYS AS (raw->'relationships'->'school'->'data'->>'id')          STORED,

  -- === shared bookkeeping contract ===
  pco_created_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'created_at')) STORED,
  pco_updated_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'updated_at')) STORED,
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  last_synced_at timestamptz NOT NULL DEFAULT now(),
  source         mirror_source NOT NULL,
  api_version    date NOT NULL,
  deleted_at     timestamptz,
  tombstone_uat  timestamptz,
  tombstone_reason text,
  merged_into_pco_id text,
  PRIMARY KEY (org_id, pco_id)
);

CREATE INDEX person_uat_idx        ON person (org_id, pco_updated_at);              -- sweep/keyset
CREATE INDEX person_name_idx       ON person (org_id, lower(last_name), lower(first_name)) WHERE deleted_at IS NULL;
CREATE INDEX person_status_idx     ON person (org_id, status)        WHERE deleted_at IS NULL;
CREATE INDEX person_email_idx      ON person (org_id, lower(primary_email)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX person_remote_idx ON person (org_id, remote_id)  WHERE remote_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX person_campus_idx     ON person (org_id, primary_campus_id) WHERE deleted_at IS NULL;
CREATE INDEX person_search_trgm    ON person USING gin (search_name gin_trgm_ops);
CREATE INDEX person_raw_gin        ON person USING gin (raw jsonb_path_ops);

-- ---------------------------------------------------------------------
-- 3. CHILD PATTERN: email (identical shape for phone_number, address, social_profile)
--    person_pco_id is GENERATED from the owner relationship. The ingestion
--    layer normalizes every sideloaded `included` child so its raw carries
--    relationships.person before upsert, so the owner id is never NULL.
-- ---------------------------------------------------------------------
CREATE TABLE email (
  org_id bigint NOT NULL DEFAULT 0,
  pco_id text   NOT NULL,
  raw    jsonb  NOT NULL,
  person_pco_id text    GENERATED ALWAYS AS (raw->'relationships'->'person'->'data'->>'id') STORED,
  address       text    GENERATED ALWAYS AS (raw->'attributes'->>'address')  STORED,
  location      text    GENERATED ALWAYS AS (raw->'attributes'->>'location') STORED,
  is_primary    boolean GENERATED ALWAYS AS ((raw->'attributes'->>'primary')::boolean) STORED,
  blocked       boolean GENERATED ALWAYS AS ((raw->'attributes'->>'blocked')::boolean) STORED,
  pco_created_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'created_at')) STORED,
  pco_updated_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'updated_at')) STORED,
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  last_synced_at timestamptz NOT NULL DEFAULT now(),
  source mirror_source NOT NULL, api_version date NOT NULL,
  deleted_at timestamptz, tombstone_uat timestamptz, tombstone_reason text, merged_into_pco_id text,
  PRIMARY KEY (org_id, pco_id)
);
CREATE INDEX email_person_idx ON email (org_id, person_pco_id) WHERE deleted_at IS NULL;
CREATE INDEX email_uat_idx    ON email (org_id, pco_updated_at);
-- phone_number: projects number,e164,carrier,country_code,location,is_primary
-- address:      projects street_line_1/2,city,state,zip,country_code,location,is_primary
-- social_profile, note, background_check: same shared contract + their own projections.

-- ---------------------------------------------------------------------
-- 4. CUSTOM FIELDS: field_datum (polymorphic owner) + schema tables
--    Fixes reviewer finding: field_datum owner is polymorphic (customizable).
-- ---------------------------------------------------------------------
CREATE TABLE field_datum (
  org_id bigint NOT NULL DEFAULT 0,
  pco_id text   NOT NULL,
  raw    jsonb  NOT NULL,
  customizable_type text GENERATED ALWAYS AS (raw->'relationships'->'customizable'->'data'->>'type') STORED,
  customizable_id   text GENERATED ALWAYS AS (raw->'relationships'->'customizable'->'data'->>'id')   STORED,
  -- convenience: person owner only when the customizable is a Person (else NULL, e.g. Organization-level)
  person_pco_id     text GENERATED ALWAYS AS (
      CASE WHEN raw->'relationships'->'customizable'->'data'->>'type' = 'Person'
           THEN raw->'relationships'->'customizable'->'data'->>'id' END) STORED,
  field_definition_id text GENERATED ALWAYS AS (raw->'relationships'->'field_definition'->'data'->>'id') STORED,
  field_option_id     text GENERATED ALWAYS AS (raw->'relationships'->'field_option'->'data'->>'id')     STORED,
  value    text GENERATED ALWAYS AS (raw->'attributes'->>'value') STORED,   -- canonical string form
  file_url text GENERATED ALWAYS AS (raw->'attributes'->>'file')  STORED,
  -- typed projections: NOT generated (need field_definition.data_type). Writer/reprojector fills them;
  -- re-derived by a job when the owning field_definition is (re)mirrored (see DESIGN §6.4).
  value_text text, value_number numeric, value_date date, value_bool boolean,
  pco_created_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'created_at')) STORED,
  pco_updated_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'updated_at')) STORED,
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  last_synced_at timestamptz NOT NULL DEFAULT now(),
  source mirror_source NOT NULL, api_version date NOT NULL,
  deleted_at timestamptz, tombstone_uat timestamptz, tombstone_reason text, merged_into_pco_id text,
  PRIMARY KEY (org_id, pco_id)
);
CREATE INDEX field_datum_person_idx ON field_datum (org_id, person_pco_id) WHERE deleted_at IS NULL;
CREATE INDEX field_datum_def_idx    ON field_datum (org_id, field_definition_id) WHERE deleted_at IS NULL;
CREATE INDEX field_datum_uat_idx    ON field_datum (org_id, pco_updated_at);

-- field_definition / field_option / tab are TIMESTAMP-LESS reference resources
-- (no updated_at in the spec). pco_updated_at is therefore always NULL and they
-- use mirror_upsert_untimed + list-and-replace reconcile (§6).
CREATE TABLE field_definition (
  org_id bigint NOT NULL DEFAULT 0, pco_id text NOT NULL, raw jsonb NOT NULL,
  name      text GENERATED ALWAYS AS (raw->'attributes'->>'name')      STORED,
  slug      text GENERATED ALWAYS AS (raw->'attributes'->>'slug')      STORED,
  data_type text GENERATED ALWAYS AS (raw->'attributes'->>'data_type') STORED,
  sequence  int  GENERATED ALWAYS AS (nullif(raw->'attributes'->>'sequence','')::int) STORED,
  tab_id    text GENERATED ALWAYS AS (raw->'relationships'->'tab'->'data'->>'id') STORED,
  pco_created_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'created_at')) STORED,
  pco_updated_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'updated_at')) STORED, -- NULL: none in spec
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  last_synced_at timestamptz NOT NULL DEFAULT now(),
  source mirror_source NOT NULL, api_version date NOT NULL,
  deleted_at timestamptz, tombstone_uat timestamptz, tombstone_reason text, merged_into_pco_id text,
  PRIMARY KEY (org_id, pco_id)
);
CREATE INDEX field_definition_slug_idx ON field_definition (org_id, slug) WHERE deleted_at IS NULL;
-- field_option (value, sequence) and tab (name, sequence): same shared contract.

-- Single source of truth for custom-field values, served relationally (no denormalized
-- person.custom_fields blob — it cannot be maintained through the monotonic person upsert).
CREATE VIEW person_custom_fields AS
SELECT fd.org_id, fd.person_pco_id,
       jsonb_object_agg(def.slug,
         coalesce(to_jsonb(fd.value_bool), to_jsonb(fd.value_number),
                  to_jsonb(fd.value_date), to_jsonb(fd.value_text), to_jsonb(fd.value))) AS fields
FROM field_datum fd
JOIN field_definition def
  ON def.pco_id = fd.field_definition_id AND def.org_id = fd.org_id AND def.deleted_at IS NULL
WHERE fd.deleted_at IS NULL AND fd.person_pco_id IS NOT NULL
GROUP BY fd.org_id, fd.person_pco_id;

-- ---------------------------------------------------------------------
-- 5. HOUSEHOLDS (many-to-many) + person_merger audit
-- ---------------------------------------------------------------------
CREATE TABLE household (
  org_id bigint NOT NULL DEFAULT 0, pco_id text NOT NULL, raw jsonb NOT NULL,
  name          text GENERATED ALWAYS AS (raw->'attributes'->>'name') STORED,
  member_count  int  GENERATED ALWAYS AS (nullif(raw->'attributes'->>'member_count','')::int) STORED,
  primary_contact_id text GENERATED ALWAYS AS (raw->'relationships'->'primary_contact'->'data'->>'id') STORED,
  pco_created_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'created_at')) STORED,
  pco_updated_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'updated_at')) STORED,
  first_seen_at timestamptz NOT NULL DEFAULT now(), last_synced_at timestamptz NOT NULL DEFAULT now(),
  source mirror_source NOT NULL, api_version date NOT NULL,
  deleted_at timestamptz, tombstone_uat timestamptz, tombstone_reason text, merged_into_pco_id text,
  PRIMARY KEY (org_id, pco_id)
);

-- household_membership: TIMESTAMP-LESS join resource (no updated_at). list-and-replace reconcile.
CREATE TABLE household_membership (
  org_id bigint NOT NULL DEFAULT 0, pco_id text NOT NULL, raw jsonb NOT NULL,
  household_pco_id text GENERATED ALWAYS AS (raw->'relationships'->'household'->'data'->>'id') STORED,
  person_pco_id    text GENERATED ALWAYS AS (raw->'relationships'->'person'->'data'->>'id')    STORED,
  household_role text    GENERATED ALWAYS AS (raw->'attributes'->>'household_role') STORED,
  pending        boolean GENERATED ALWAYS AS ((raw->'attributes'->>'pending')::boolean) STORED,
  pco_created_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'created_at')) STORED,
  pco_updated_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'updated_at')) STORED, -- NULL
  first_seen_at timestamptz NOT NULL DEFAULT now(), last_synced_at timestamptz NOT NULL DEFAULT now(),
  source mirror_source NOT NULL, api_version date NOT NULL,
  deleted_at timestamptz, tombstone_uat timestamptz, tombstone_reason text, merged_into_pco_id text,
  PRIMARY KEY (org_id, pco_id)
);
CREATE INDEX hm_person_idx    ON household_membership (org_id, person_pco_id)    WHERE deleted_at IS NULL;
CREATE INDEX hm_household_idx ON household_membership (org_id, household_pco_id) WHERE deleted_at IS NULL;

-- person_merger is an immutable append-only log (created_at only, no updated_at).
CREATE TABLE person_merger (
  org_id bigint NOT NULL DEFAULT 0, pco_id text NOT NULL, raw jsonb NOT NULL,
  person_to_keep_id   text GENERATED ALWAYS AS (raw->'attributes'->>'person_to_keep_id')   STORED,
  person_to_remove_id text GENERATED ALWAYS AS (raw->'attributes'->>'person_to_remove_id') STORED,
  pco_created_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'created_at')) STORED,
  applied_at timestamptz, first_seen_at timestamptz NOT NULL DEFAULT now(),
  source mirror_source NOT NULL, api_version date NOT NULL,
  PRIMARY KEY (org_id, pco_id)
);
CREATE INDEX person_merger_created_idx ON person_merger (org_id, pco_created_at);

-- ---------------------------------------------------------------------
-- 6. REFERENCE (LITE) example: campus  (has created_at/updated_at)
-- ---------------------------------------------------------------------
CREATE TABLE campus (
  org_id bigint NOT NULL DEFAULT 0, pco_id text NOT NULL, raw jsonb NOT NULL,
  name      text GENERATED ALWAYS AS (raw->'attributes'->>'name')      STORED,
  time_zone text GENERATED ALWAYS AS (raw->'attributes'->>'time_zone') STORED,
  city      text GENERATED ALWAYS AS (raw->'attributes'->>'city')      STORED,
  state     text GENERATED ALWAYS AS (raw->'attributes'->>'state')     STORED,
  pco_created_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'created_at')) STORED,
  pco_updated_at timestamptz GENERATED ALWAYS AS (pco_ts(raw->'attributes'->>'updated_at')) STORED,
  first_seen_at timestamptz NOT NULL DEFAULT now(), last_synced_at timestamptz NOT NULL DEFAULT now(),
  source mirror_source NOT NULL, api_version date NOT NULL,
  deleted_at timestamptz, tombstone_uat timestamptz, tombstone_reason text, merged_into_pco_id text,
  PRIMARY KEY (org_id, pco_id)
);
-- Additional FULL tables: phone_number, address, social_profile, note, background_check,
--   list_result, workflow_card, workflow_card_activity, workflow_card_note.
-- Additional LITE tables: tab, field_option, list, list_category, note_category,
--   marital_status, membership_type, name_prefix, name_suffix, inactive_reason,
--   school_option, carrier, app, form, form_category, form_field, form_submission, person_app.
-- All share the column contract above and one of the two upsert functions below.

-- =====================================================================
-- 7. THE CANONICAL WRITERS  (every ingestion path calls exactly these)
-- =====================================================================

-- 7a. mirror_upsert — for resources WITH updated_at (the monotonic clock).
--     * last_synced_at ALWAYS advances (freshness signal stays honest for the serving layer).
--     * data (raw + all generated projections) moves forward only: overwrite iff
--       incoming.pco_updated_at >= stored.pco_updated_at  ( >= lets reconcile repair a
--       within-same-second divergence and a same-second correction win ).
--     * sticky tombstones: a live payload un-deletes ONLY when strictly newer than the
--       tombstone clock AND the tombstone was not a merge (merges resurrect only via
--       mirror_confirm_live, i.e. an authoritative live GET).
CREATE FUNCTION mirror_upsert(p_table regclass, p_org bigint, p_pco_id text,
                              p_raw jsonb, p_source mirror_source, p_api_version date)
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
  EXECUTE format($q$
    INSERT INTO %s AS t (org_id, pco_id, raw, first_seen_at, last_synced_at, source, api_version)
    VALUES ($1, $2, $3, now(), now(), $4, $5)
    ON CONFLICT (org_id, pco_id) DO UPDATE SET
      last_synced_at = now(),
      source         = EXCLUDED.source,
      raw            = CASE WHEN EXCLUDED.pco_updated_at >= t.pco_updated_at
                            THEN EXCLUDED.raw ELSE t.raw END,
      api_version    = CASE WHEN EXCLUDED.pco_updated_at >= t.pco_updated_at
                            THEN EXCLUDED.api_version ELSE t.api_version END,
      deleted_at     = CASE
                         WHEN t.deleted_at IS NULL                     THEN NULL
                         WHEN t.tombstone_reason = 'merged'            THEN t.deleted_at
                         WHEN EXCLUDED.pco_updated_at > t.tombstone_uat THEN NULL
                         ELSE t.deleted_at END,
      tombstone_uat  = CASE
                         WHEN t.deleted_at IS NOT NULL AND t.tombstone_reason <> 'merged'
                              AND EXCLUDED.pco_updated_at > t.tombstone_uat THEN NULL
                         ELSE t.tombstone_uat END,
      tombstone_reason = CASE
                         WHEN t.deleted_at IS NOT NULL AND t.tombstone_reason <> 'merged'
                              AND EXCLUDED.pco_updated_at > t.tombstone_uat THEN NULL
                         ELSE t.tombstone_reason END
  $q$, p_table)
  USING p_org, p_pco_id, p_raw, p_source, p_api_version;
END $fn$;

-- 7b. mirror_upsert_untimed — for TIMESTAMP-LESS resources (field_definition,
--     field_option, tab, household_membership). Data is last-write-wins on `raw`,
--     but a create/update NEVER clears a tombstone (destroyed is terminal until a
--     list-and-replace reconcile re-observes the row, which calls mirror_confirm_live).
CREATE FUNCTION mirror_upsert_untimed(p_table regclass, p_org bigint, p_pco_id text,
                                      p_raw jsonb, p_source mirror_source, p_api_version date)
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
  EXECUTE format($q$
    INSERT INTO %s AS t (org_id, pco_id, raw, first_seen_at, last_synced_at, source, api_version)
    VALUES ($1, $2, $3, now(), now(), $4, $5)
    ON CONFLICT (org_id, pco_id) DO UPDATE SET
      last_synced_at = now(),
      source         = EXCLUDED.source,
      raw            = CASE WHEN t.deleted_at IS NULL THEN EXCLUDED.raw ELSE t.raw END,
      api_version    = CASE WHEN t.deleted_at IS NULL THEN EXCLUDED.api_version ELSE t.api_version END
  $q$, p_table)
  USING p_org, p_pco_id, p_raw, p_source, p_api_version;
END $fn$;

-- 7c. mirror_tombstone — provisional (webhook destroyed) or authoritative (merge/audit) delete.
--     Merges are authoritative (always applied); destroyed/absent apply when not superseded
--     by a strictly-newer live row already stored.
CREATE FUNCTION mirror_tombstone(p_table regclass, p_org bigint, p_pco_id text,
                                 p_uat timestamptz, p_reason text, p_merged_into text DEFAULT NULL)
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
  EXECUTE format($q$
    UPDATE %s AS t SET
      deleted_at       = now(),
      tombstone_uat    = coalesce($3, t.pco_updated_at, now()),
      tombstone_reason = $4,
      merged_into_pco_id = coalesce($5, t.merged_into_pco_id),
      last_synced_at   = now(),
      source           = 'reconcile'
    WHERE t.org_id = $1 AND t.pco_id = $2
      AND ( $4 = 'merged'                                   -- merges always win
            OR t.deleted_at IS NOT NULL                     -- already dead: refresh reason/uat
            OR t.pco_updated_at IS NULL                     -- timestamp-less: no clock to supersede, apply
            OR coalesce($3, t.pco_updated_at) >= t.pco_updated_at )  -- not superseded by newer live data
  $q$, p_table)
  USING p_org, p_pco_id, p_uat, p_reason, p_merged_into;
END $fn$;

-- 7d. mirror_confirm_live — authoritative resurrection. A live GET->200 (audit confirmation,
--     survivor-hydration reassigning a moved child, or list-and-replace re-observation) is
--     ground truth and force-clears any tombstone, then applies the fresh raw.
CREATE FUNCTION mirror_confirm_live(p_table regclass, p_org bigint, p_pco_id text,
                                    p_raw jsonb, p_source mirror_source, p_api_version date)
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
  EXECUTE format($q$
    INSERT INTO %s AS t (org_id, pco_id, raw, first_seen_at, last_synced_at, source, api_version)
    VALUES ($1, $2, $3, now(), now(), $4, $5)
    ON CONFLICT (org_id, pco_id) DO UPDATE SET
      raw = EXCLUDED.raw, api_version = EXCLUDED.api_version, source = EXCLUDED.source,
      last_synced_at = now(),
      deleted_at = NULL, tombstone_uat = NULL, tombstone_reason = NULL, merged_into_pco_id = NULL
  $q$, p_table)
  USING p_org, p_pco_id, p_raw, p_source, p_api_version;
END $fn$;

-- =====================================================================
-- 8. OPERATIONAL TABLES (unified — replaces the 4 divergent state tables
--    and 2 divergent inbox tables from the section drafts)
-- =====================================================================

-- 8a. ONE reconciliation/backfill state row per (org, resource_type).
--     Owned by reconcile+backfill. The webhook worker NEVER writes reconcile_* here.
CREATE TABLE mirror_sync_state (
  org_id bigint NOT NULL DEFAULT 0,
  resource_type text NOT NULL,
  phase text NOT NULL DEFAULT 'idle',            -- idle|backfilling|streaming|reconciling

  -- initial backfill (ascending updated_at keyset + seen-ids tie handling)
  backfill_cursor_ts    timestamptz,
  backfill_seen_ids     text[] NOT NULL DEFAULT '{}',
  backfill_completed_at timestamptz,

  -- incremental sweep (ONLY advanced by reconcile's ordered ascending sweep)
  reconcile_watermark   timestamptz NOT NULL DEFAULT '-infinity',
  reconcile_cursor      timestamptz NOT NULL DEFAULT '-infinity',
  last_sweep_started_at timestamptz, last_sweep_completed_at timestamptz,

  -- merger poll (person only); person_merger has created_at ONLY
  merger_watermark      timestamptz NOT NULL DEFAULT '-infinity',

  -- delete audit (keyset on immutable created_at)
  audit_cursor          timestamptz,
  last_audit_started_at timestamptz, last_audit_completed_at timestamptz,

  -- drift probe (written by reconcile; read by the ops mirror_drift_ratio metric)
  total_count_last  bigint,
  mirror_count_last bigint,
  last_drift_at     timestamptz,

  -- health / backoff
  consecutive_errors int NOT NULL DEFAULT 0, last_error text,
  next_run_at timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, resource_type)
);

-- 8b. Per-resource sync policy (hot-editable config; seeded from DESIGN §6 appendix).
CREATE TABLE sync_policy (
  resource_type text PRIMARY KEY,
  endpoint text NOT NULL,
  method   text NOT NULL,               -- incremental | merger_poll | reference_periodic | passthrough_only
  supports_uat_filter boolean NOT NULL, -- true => where[updated_at]; false => descending-walk (e.g. address)
  timestamped boolean NOT NULL DEFAULT true, -- false => field_definition/field_option/tab/household_membership
  incr_interval_s int, audit_interval_s int,
  priority smallint NOT NULL,           -- 0=highest .. 4=lowest
  per_page smallint NOT NULL DEFAULT 100,
  include  text,                        -- e.g. 'emails,phone_numbers,addresses,field_data'
  parent_type text, enabled boolean NOT NULL DEFAULT true
);

-- 8c. Webhook envelope audit (exact bytes, once per delivery) ...
CREATE TABLE webhook_delivery (
  delivery_id text PRIMARY KEY,         -- top-level envelope id
  org_id bigint NOT NULL DEFAULT 0,
  subscription_pco_id text,
  signature text NOT NULL,              -- X-PCO-Webhooks-Authenticity (verified)
  raw_body  bytea NOT NULL,             -- EXACT verified bytes (re-HMAC / replay capable)
  attempt int, received_at timestamptz NOT NULL DEFAULT now()
);

-- 8d. ... and the per-event inbox / idempotency ledger (one row per data[].id).
--     Handles batched deliveries; dedup key is the per-event id, not the envelope id.
CREATE TABLE webhook_event (
  event_id text PRIMARY KEY,            -- data[].id  (idempotency key)
  delivery_id text REFERENCES webhook_delivery(delivery_id),
  org_id bigint NOT NULL DEFAULT 0,
  event_name text NOT NULL,             -- people.v2.events.person.updated
  resource_type text, action text,      -- parsed: 'person','updated'
  pco_id text,
  payload jsonb NOT NULL,               -- parsed JSON:API resource (data[].attributes.payload)
  status text NOT NULL DEFAULT 'pending', -- pending|processing|done|skipped_stale|dead
  process_attempts int NOT NULL DEFAULT 0, next_attempt_at timestamptz,
  received_at timestamptz NOT NULL DEFAULT now(), processed_at timestamptz, last_error text
);
CREATE INDEX webhook_event_status_idx ON webhook_event (status, next_attempt_at);
CREATE INDEX webhook_event_res_idx    ON webhook_event (org_id, resource_type, pco_id);

-- 8e. Hydration queue (thin-webhook + merge follow-up). (resource,pco_id) PK coalesces bursts.
CREATE TABLE hydration_task (
  org_id bigint NOT NULL DEFAULT 0,
  resource_type text NOT NULL,
  pco_id text NOT NULL,
  includes text[] NOT NULL DEFAULT '{}',
  reason text,                          -- 'thin_webhook' | 'merge_survivor' | 'passthrough_miss'
  not_before timestamptz NOT NULL DEFAULT now(),
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, resource_type, pco_id)
);

-- 8f. Dead-letter (diagnostic only — reconcile independently re-derives truth).
CREATE TABLE webhook_dead_letter (
  event_id text PRIMARY KEY, org_id bigint, event_name text, payload jsonb,
  last_error text, attempts int, died_at timestamptz NOT NULL DEFAULT now()
);

-- 8g. Append-only run log + drift log (observability; not used for resume).
CREATE TABLE reconcile_run (
  run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id bigint, resource_type text, kind text,          -- incremental|merger|audit|drift|backfill
  started_at timestamptz, completed_at timestamptz, status text,
  watermark_before timestamptz, watermark_after timestamptz,
  rows_seen int, rows_upserted int, rows_tombstoned int, requests_used int, error text
);

-- =====================================================================
-- 9. TENANCY, SECRETS, LOCAL AUTH
-- =====================================================================
CREATE TABLE org (
  org_id bigint PRIMARY KEY,
  name text NOT NULL,
  auth_kind text NOT NULL DEFAULT 'pat',   -- 'pat' | 'oauth'
  api_version date NOT NULL DEFAULT '2026-06-04',
  status text NOT NULL DEFAULT 'active',    -- active | reauth_required | paused
  last_refresh_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);

-- Envelope-encrypted secrets (AES-256-GCM under a KMS-wrapped per-org DEK). Version-pointer
-- model enables zero-downtime rotation (webhook secret rotation needs two live versions).
CREATE TABLE org_secret (
  org_id bigint NOT NULL REFERENCES org(org_id),
  kind text NOT NULL,   -- pat_app_id|pat_secret|oauth_client_id|oauth_client_secret
                        -- |oauth_access_token|oauth_refresh_token|webhook_secret
  version int NOT NULL,
  ciphertext bytea NOT NULL, nonce bytea NOT NULL, dek_wrapped bytea NOT NULL, kek_id text NOT NULL,
  expires_at timestamptz, rotated_at timestamptz, retired_at timestamptz,
  PRIMARY KEY (org_id, kind, version)
);

CREATE TABLE webhook_subscription (
  org_id bigint NOT NULL,
  subscription_pco_id text NOT NULL,   -- webhooks/v2 subscription id
  event_name text NOT NULL,
  resource text, action text,
  url_token text UNIQUE NOT NULL,      -- opaque 128-bit token in the receiver URL path (O(1) secret lookup)
  secret_version int NOT NULL,         -- -> org_secret(kind='webhook_secret'); resolves both versions mid-rotation
  active boolean NOT NULL DEFAULT true,
  last_event_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, subscription_pco_id)
);

-- Local API keys — the serving plane, strictly separate from upstream PCO creds.
CREATE TABLE api_key (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  prefix text NOT NULL, key_hash bytea NOT NULL,   -- sha256(secret); raw never stored
  name text, org_id bigint NOT NULL,
  scopes text[] NOT NULL DEFAULT '{read:people}',  -- read:people|read:reference|read:deleted|passthrough|write
  rate_limit_per_min int NOT NULL DEFAULT 600,
  passthrough_quota_per_min int NOT NULL DEFAULT 60,
  disabled_at timestamptz, last_used_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);

-- Short-TTL cache for non-mirrorable pass-through GETs (stats/reports/search).
CREATE TABLE passthrough_cache (
  cache_key text PRIMARY KEY, org_id bigint,
  status int, body jsonb, headers jsonb,
  fetched_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL
);

-- =====================================================================
-- 10. ROW-LEVEL SECURITY (tenant isolation; enable per mirrored table)
-- =====================================================================
-- Example for person; repeat for every mirrored table:
-- ALTER TABLE person ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY person_org_iso ON person
--   USING (org_id = current_setting('app.org_id')::bigint);
-- The api-server runs `SET LOCAL app.org_id = <caller org>` per request/txn.
