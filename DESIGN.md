# pcomirror — Design

A layer service that mirrors **Planning Center (PCO) People** data into a local
PostgreSQL store so local applications can query it **without hitting the live
API**, with an explicit **pass-through** to PCO when needed. Freshness is
maintained by **webhooks** (fast path) plus a **background reconciliation**
refresh (safety net) that repairs anything a missed/late/lost webhook left stale.

> **Status.** This is a design. It was produced by fanning the work across six
> subsystem designs, then adversarially reviewing the combined design for
> sync-correctness, rate-limit/scale math, and data-model/security. The result
> was unified into the canonical decisions in [§3](#3-the-canonical-write-path)
> and [§10](#10-canonical-decisions--resolved-review-ledger).

## 0. Deployment profile (decided)

pcomirror serves **one Planning Center organization** — a single church of a few
hundred people. The confirmed choices:

| Decision | Choice | Consequence |
|---|---|---|
| **Tenancy** | **Single org** | No `org_id`, no RLS, no multi-tenant machinery. |
| **Store** | **SQLite** ([`docs/schema.sqlite.sql`](docs/schema.sqlite.sql)) | One file, WAL mode, no DB server, no Redis. Backup = copy the file. |
| **Writes** | **Write-through** ([§8.4](#84-writes--write-through)) | Local apps can create/update/delete; the mirror proxies to PCO, then applies the returned resource. |
| **Auth** | **Personal Access Token** (HTTP Basic) | No OAuth refresh loop. |
| **Rate limiter** | **In-process** token bucket | One process → correct without Redis. |
| **Process model** | **One service** (or a few) sharing the SQLite file via WAT | Webhook receiver + fetch/reconcile workers + api-server. |

**Why SQLite is the right call here.** At ~300 people the entire dataset is a few
thousand rows across all tables; a full backfill is ~3–5 requests. The design was
built on portable primitives — **raw JSON as the system of record, generated
columns, and one monotonic UPSERT with a `WHERE` guard** — all of which SQLite
3.45 supports natively. ISO-8601 UTC timestamps even compare correctly as TEXT,
so the monotonic guard needs no date parsing. The schema and all four writer
semantics are **verified on SQLite 3.45** by
[`docs/schema_test_sqlite.py`](docs/schema_test_sqlite.py) (11/11 assertions).

**What the single-file / single-org profile lets us drop** vs. the general design
in the sections below: Redis and the GCRA limiter (→ in-process token bucket),
row-level security and `org_id` (→ single tenant), KMS envelope encryption (→ the
PAT and webhook secret live in an OS keyring or a `0600` file, decrypted by the
app), leader election (→ one process), and PL/pgSQL writer functions (→ one
application-level writer module issuing the canonical SQL). The **sync logic is
unchanged** — it's storage-agnostic.

**The rest of this document is the reference architecture.** Where it describes
PostgreSQL, Redis, RLS, OAuth, KMS, or multi-tenancy, read that as the
**scale-up path** ([§12](#12-scaling-up-postgres--multi-org)) — the machinery you
adopt if this ever grows to many churches or a large org. The Postgres schema
([`docs/schema.sql`](docs/schema.sql)) is kept as that target. Everything about
the data model, the canonical writer, ingestion, webhooks, and reconciliation
applies verbatim to both.

---

## Contents

0. [Deployment profile (decided)](#0-deployment-profile-decided)
1. [Ground truth: the PCO People API constraints](#1-ground-truth-the-pco-people-api-constraints)
2. [Architecture at a glance](#2-architecture-at-a-glance)
3. [The canonical write path](#3-the-canonical-write-path) — *the correctness core*
4. [Data model & storage](#4-data-model--storage)
5. [Ingestion & the rate-limited fetch engine](#5-ingestion--the-rate-limited-fetch-engine)
6. [Webhooks: real-time updates](#6-webhooks-real-time-updates)
7. [Background reconciliation & drift repair](#7-background-reconciliation--drift-repair)
8. [Serving API & live pass-through](#8-serving-api--live-pass-through)
9. [Auth, versioning, multi-tenancy & operations](#9-auth-versioning-multi-tenancy--operations)
10. [Canonical decisions & resolved review ledger](#10-canonical-decisions--resolved-review-ledger)
11. [Decisions & the one calibration step](#11-decisions--the-one-calibration-step)
12. [Scaling up (Postgres / multi-org)](#12-scaling-up-postgres--multi-org)
- [Appendix A — Entity tiers](#appendix-a--entity-tiers-what-we-mirror)
- [Appendix B — Sync policy seed](#appendix-b--per-resource-sync-policy-seed)

---

## 1. Ground truth: the PCO People API constraints

Everything below is derived from the docs and the `2026-06-04` People OpenAPI
spec (`x-pco-api-version: 16.2.0`, 35 resource types, 288 paths, 663 schemas).
These constraints *are* the design drivers.

**Shape — JSON:API 1.0.** Base URL `https://api.planningcenteronline.com/people/v2`.
Every response is `{ data, included?, meta, links }`. A resource is
`{ id, type, attributes, relationships:{<rel>:{data:{type,id}|[…], links}}, links }`.
**Every resource carries `attributes.created_at` and `attributes.updated_at`** —
the backbone of all incremental sync.

**Pagination is offset-based.** `per_page` (max **100**, default 25) + `offset`
(default 0). `meta` = `{ total_count, count, next:{offset}, prev:{offset}, can_include[],
can_query_by[], can_order_by[], can_filter[] }`.

**Ordering & filtering.** `order=updated_at` (asc) or `order=-updated_at` (desc,
`-` prefix). `where[attr]=v` (exact, case-insensitive; `%` wildcard). Date/time
operators `where[attr][gt|gte|lt|lte]=v`. Person is queryable by `created_at`,
`updated_at`, `id`, `remote_id`, `primary_campus_id`, `status`, name fields, etc.

**Sideloading.** `?include=emails,phone_numbers,addresses,field_data,households,…`
returns children in one `included[]` array — up to **two levels** (`field_data.field_definition`).
This collapses N+1 into ~1 request per 100 people.

**Rate limits (the hard ceiling).** **100 requests / 20 s per token** (≈5 req/s).
Drops to **75/20s when `offset > 30,000`** — a strong signal to *never deep-offset
page*. Headers on every response: `X-PCO-API-Request-Rate-Limit` / `-Period` /
`-Count`. On breach: **HTTP 429** + `Retry-After`. *Never hard-code the numbers* —
individual endpoints may differ; read the headers.

**Auth.** **Personal Access Token** (HTTP Basic `app_id:secret`) — single org,
simplest, no expiry. **OAuth 2.0 + PKCE** (scope `people`) — required for
multi-org; access token 2 h, refresh token valid 90 days from last issuance
(each refresh re-arms the clock, and PCO *rotates* the refresh token on use).
Every request must send a `User-Agent`.

**Versioning.** `X-PCO-API-Version: YYYY-MM-DD` (equal-or-less-than matching).
**Pin it** (`2026-06-04`); never send `LATEST` (silent breaking changes).

**Webhooks.** Managed at `/webhooks/v2`. Events `people.v2.events.<resource>.<action>`,
`action ∈ {created, updated, destroyed}`. Signed: `X-PCO-Webhooks-Authenticity =
HMAC-SHA256(key=authenticity_secret, msg=raw_body)`. Payload wraps a JSON:API
resource in `data[].attributes.payload`. Delivery is **at-least-once**, **no
ordering guarantee**; failures retried ~16× over ~5 days; **HTTP 410 deactivates
the subscription**. → A webhook *can* be lost (endpoint down past the window,
subscription gap), which is exactly why reconciliation must exist.

**The two structural consequences that shape the whole design:**

1. **Never trust webhook delivery for correctness, and never deep-offset page.**
   Correctness rests on `updated_at`-keyset reconciliation + idempotent writes;
   webhooks are a latency optimization on top.
2. **Deletes are (partly) invisible to `updated_at` filtering.** A hard-deleted
   or merged-away id simply *stops appearing*. `person_mergers` (which records
   `person_to_keep_id` / `person_to_remove_id` / `created_at`) and a periodic
   id-audit are how we catch them.

---

## 2. Architecture at a glance

The single-org SQLite profile: **one process** holding one SQLite file, with three
internal roles. Everything that calls PCO funnels through **one in-process rate
limiter**; everything that writes goes through **one canonical writer**.

```
                     PCO People API (people/v2)          PCO Webhooks (/webhooks/v2)
                          ▲     ▲                                 │ signed deliveries
          GET (keyset,    │     │  GET (hydrate,                  ▼
          includes,       │     │  audit,               ┌───────────────────┐
          write-through)  │     │  pass-through)        │ webhook-receiver  │ verify HMAC(raw)
   ┌───────────┐  ┌───────┴─────┴───────────┐           │ (ack < 500 ms)    │ → durable insert
   │ scheduler │─▶│  fetch + reconcile      │◀──────────│                   │ → 2xx fast
   │  (thread) │  │  workers                │ hydration └─────────┬─────────┘
   └───────────┘  │  backfill / reconcile / │ queue               │ enqueue
        │ sweeps  │  hydrate / pass-through │                     ▼
        │ merger  └───────────┬─────────────┘            webhook_event (per-event inbox)
        │ drift               │  ALL PCO calls →                  │ async
        │           ┌─────────────────────────┐                  │
        └──────────▶│ IN-PROCESS RATE LIMITER  │ token bucket,    │
                    │ (header-adaptive, 80%)   │ single token     │
                    └─────────────────────────┘                  │
                              │  every writer → the ONE canonical writer (§3)
                              ▼                                   ▼
                    ┌──────────────────────────────────────────────────┐
                    │   SQLite  (raw JSON system-of-record + generated  │
                    │   columns + mirror_sync_state · WAL mode)         │
                    └───────────────────────┬──────────────────────────┘
                                            ▼
                                  ┌────────────────────┐   pass-through / write-through
        local apps ──JSON:API───▶ │  api-server        │──▶ (opt-in, scope-gated,
        (host + key swap)         │  (pcomirror-serve) │    through the shared limiter)
                                  └────────────────────┘
```

**One service, one file.** The receiver, the fetch/reconcile workers, the
scheduler, and the api-server are roles inside one process sharing the SQLite file
in WAL mode (they can be split into separate processes later — WAL allows one
writer + many readers). Everything that calls PCO — backfill, reconcile, webhook
hydration, pass-through, and write-through — funnels through **one shared rate
limiter**, and every mutation goes through **one canonical writer**. Those two
"one" statements are what make the system rate-safe and correct; the rest is
detail. *(At scale this same shape fans into five independent services over
Postgres + Redis — see [§12](#12-scaling-up-postgres--multi-org).)*

---

## 3. The canonical write path

> This is the heart of the design and the single most important section. Every
> ingestion path — backfill, reconcile, webhook, pass-through read-through —
> mutates the mirror **only** through four canonical operations. On SQLite these
> are an **application-level writer module** issuing the statements in
> [`docs/schema.sqlite.sql`](docs/schema.sqlite.sql) §7; on Postgres they are the
> SQL functions in [`docs/schema.sql`](docs/schema.sql). The adversarial review
> found the original per-subsystem drafts specified the writer *four incompatible
> ways*; the rules below are the reconciled, single specification — identical on
> both engines and verified on each.

### 3.1 Storage invariant

`raw` is the **system of record** — the resource stored verbatim (SQLite `TEXT`;
Postgres `jsonb`). Every queryable column is a **generated projection** of `raw`
(SQLite: `raw ->> '$.attributes.first_name'`; Postgres: `GENERATED ALWAYS AS (…)
STORED` via the `IMMUTABLE` `pco_ts()`/`pco_date()` parsers). Timestamps are
ISO-8601 UTC — on SQLite they stay `TEXT` and compare chronologically as-is, so
`pco_updated_at` needs no parsing at all. Consequences:

- Writers only ever set `raw` + a fixed bookkeeping set; projections recompute
  themselves. The upsert is therefore **table-agnostic** (one function, dynamic
  table name).
- A PCO **version bump re-projects with zero API calls** — change the generated
  expression, let Postgres recompute from retained `raw`.

### 3.2 The four writers and their exact semantics

| Function | Used by | Rule |
|---|---|---|
| `mirror_upsert` | backfill, reconcile sweep, webhook create/update, pass-through read-through — **for resources that have `updated_at`** | `last_synced_at` **always** advances (freshness signal stays honest). Data overwrites **iff `incoming.updated_at ≥ stored.pco_updated_at`** (`≥`, so reconcile repairs a same-second divergence and a same-second correction wins). Un-deletes a tombstone **only** when `incoming.updated_at > tombstone_uat` **and** the tombstone was not a merge. |
| `mirror_upsert_untimed` | **timestamp-less** resources (`field_definition`, `field_option`, `tab`, `household_membership`) | Last-write-wins on `raw`, but a create/update **never clears a tombstone** — destroyed is terminal until a list-and-replace reconcile re-observes the row (via `mirror_confirm_live`). |
| `mirror_tombstone` | webhook `destroyed`, merge, audit-absent | Sets `deleted_at`, records `tombstone_uat` + `tombstone_reason` (+ `merged_into_pco_id`). Merges are authoritative; `destroyed`/`absent` apply unless superseded by strictly-newer stored live data. |
| `mirror_confirm_live` | audit confirmation GET→200, survivor-hydration reassigning a moved child, list-and-replace re-observation | An **authoritative live GET is ground truth**: force-clears any tombstone (including merges) and applies fresh `raw`. |

> These exact semantics are verified on **both** engines: SQLite 3.45 via
> [`docs/schema_test_sqlite.py`](docs/schema_test_sqlite.py) and PostgreSQL 16 via
> [`docs/schema_test.sql`](docs/schema_test.sql) — 11 assertions each covering the
> monotonic guard, same-second `≥` correction, sticky/merge tombstones,
> authoritative resurrection, polymorphic `field_datum`, and untimed-tombstone
> terminality.

**Why this exact shape (each clause fixes a concrete failure the review found):**

- **`last_synced_at` always advances** even on a losing write → the serving layer's
  freshness header (`X-Mirror-Oldest-Synced-At`) reflects "PCO confirmed this row
  at T", so a reconfirmed-but-unchanged row does **not** trigger a needless
  pass-through. (Bare-`WHERE` upserts skipped the whole row and broke this.)
- **`≥` not `>`** for data → a person edited twice in the same wall-clock second
  (both `updated_at = T`): the corrected v2 webhook, and reconcile's later GET,
  can both still overwrite the wrong v1. Strict `>` would freeze the mirror on v1
  **permanently**.
- **Sticky tombstones (`tombstone_uat`, merges terminal)** → a reordered
  pre-deletion `updated` arriving *after* a `destroyed` (same `updated_at`) can't
  resurrect the row; a reordered pre-merge `updated` can't un-retire a merged id.
  Resurrection is possible **only** via an authoritative live GET.

### 3.3 Idempotency & ordering, end to end

- **At-least-once webhooks** → deduped at the door on the per-event id
  (`webhook_event.event_id = data[].id`), and harmless anyway because re-applying
  the same/older payload is a no-op under the guard.
- **Unordered delivery / concurrent writers** (a webhook and a reconcile GET
  racing the same row) → the monotonic guard makes the outcome deterministic
  regardless of who wins the race; `source` is provenance only, never gates.
- **Crash safety** → reconcile commits each page's applies **and** its cursor
  advance in one transaction, so the watermark moves exactly-once even though HTTP
  is at-least-once.

---

## 4. Data model & storage

Full DDL in [`docs/schema.sqlite.sql`](docs/schema.sqlite.sql) (the chosen store;
[`docs/schema.sql`](docs/schema.sql) is the Postgres equivalent for scale-up).
Highlights:

### 4.1 Three mirror tiers

- **FULL** — own table, raw + rich projections, indexed, kept fresh by
  webhook + reconcile: `person` and its owned children (`email`, `phone_number`,
  `address`, `social_profile`, `field_datum`, `note`, `background_check`) + link
  resources (`household`, `household_membership`, `list_result`, `workflow_card`,
  `workflow_card_activity`, `workflow_card_note`).
- **LITE** — own table, raw + minimal projection, refreshed on a slow cadence by
  list-and-replace: reference/config (`campus`, `field_definition`, `field_option`,
  `tab`, `list`, `list_category`, `note_category`, `marital_status`,
  `membership_type`, `name_prefix`, `name_suffix`, `inactive_reason`,
  `school_option`, `carrier`, `app`, `form`(+fields/conditions), `form_category`,
  `form_submission`, `person_app`).
- **PASS-THROUGH / DERIVED** — never persisted as primary; proxied live:
  `birthday_people`, `/stats`, `workflow_assignee_summary`, `report`,
  `people_import`, `spam_email_address`, `message`, `message_group`, `/me`.
  `person_merger` is a **derived side-effect**: stored as an audit log *and*
  applied (retire the removed id → tombstone + `merged_into_pco_id`).

Full table in [Appendix A](#appendix-a--entity-tiers-what-we-mirror).

### 4.2 Hybrid raw-JSONB + generated projections

Shown in `schema.sql` for `person`. The split:

- **Generated** (pure function of `raw`, zero writer code): all scalars,
  relationship ids, and — via the `IMMUTABLE` `pco_ts()`/`pco_date()` wrappers —
  temporals and `pco_created_at`/`pco_updated_at`. *Immutability is legal because
  PCO always emits explicit-UTC ISO-8601, making the parse deterministic.*
- **Relationships & `links` stay inside `raw`** (never shredded away), so
  serialization is byte-faithful to PCO. We denormalize the **ids we join on**
  into generated columns, but declare **no cross-table foreign keys** —
  referential integrity is *eventual* (children/parents arrive out of order under
  at-least-once ingestion); reconciliation repairs dangling ids, not the DB.
- **Sideloaded `included[]`** is split per element and routed to its type's table
  through the same writer, so one include-page populates 5+ tables in a round
  trip. The ingestion layer **normalizes each included child's `raw`** to carry
  its owner relationship first, so owner-id generated columns are never NULL.

### 4.3 Custom fields

Two layers: **schema** (`tab`, `field_definition`, `field_option` — LITE,
timestamp-less) and **values** (`field_datum` — FULL). `field_datum`'s owner is
**polymorphic** (`relationships.customizable` may be a Person *or* the
Organization), so we store `customizable_type` + `customizable_id` and derive
`person_pco_id` only when the type is `Person` — a hard `person_pco_id NOT NULL`
would silently drop org-level custom values. Serve custom fields **relationally**
via the `person_custom_fields` view (join `field_datum → field_definition` on
slug) — the single source of truth. We deliberately do **not** keep a
denormalized `person.custom_fields` blob: it can't be maintained through the
monotonic person upsert (a field change doesn't bump `person.updated_at`) and
invites lost-update races.

Typed columns (`value_number/date/bool`) need the definition's `data_type`, so
they're writer-filled and **re-projected by a job whenever the owning
`field_definition` is (re)mirrored** — from retained `raw`, no API calls.

### 4.4 Soft-delete / tombstones

Deletes are never physical (`deleted_at`; keep `raw` for audit). Three sources,
all funneled through `mirror_tombstone` / `mirror_confirm_live` (§3): `destroyed`
webhooks (provisional), reconcile id-audit disappearance (authoritative on a
confirming `GET→404`), and merges (`merged_into_pco_id`, terminal). Local reads
filter `WHERE deleted_at IS NULL` (partial indexes make it free). A read on a
merged id can transparently follow `merged_into_pco_id` to the survivor.

### 4.5 Indexing

`PRIMARY KEY (pco_id)`; `(pco_updated_at)` for keyset/sweep on every FULL table;
live-row **partial** indexes (`WHERE deleted_at IS NULL`); denormalized-FK indexes
on join keys; projected-column indexes mirroring `can_query_by`/`can_order_by`; and
for fuzzy local search either SQLite **FTS5** over `search_name` or just `LIKE` (a
scan of a few hundred rows is instant). *(Scale-up adds `org_id` to every key, a
`GIN (raw jsonb_path_ops)` for ad-hoc JSONB containment, and a `pg_trgm` index on
`search_name`.)* Don't over-index tiny LITE tables.

### 4.6 Version evolution

`raw` is retained and projections are a pure function of it, so a
`X-PCO-API-Version` bump is a **re-projection, never a data migration**: add/alter
a generated column and back-fill from `raw`; rollback = revert the expression.
Per-row `api_version` records which extraction rule a given `raw` needs, so
mixed-version corpora reproject correctly.

---

## 5. Ingestion & the rate-limited fetch engine

One layer through which **every** PCO request passes; it moves data in as fast as
the org's ceiling allows, never exceeds it, and never trips the high-offset penalty.

### 5.1 HTTP client

Central header injection: `Authorization` (PAT Basic / OAuth Bearer), **required**
`User-Agent`, pinned `X-PCO-API-Version: 2026-06-04` (never `LATEST`),
`Accept: application/json`, `Accept-Encoding: gzip` (include-pages get large). On
**every** response (success or error) it parses the three rate headers and feeds
them to the shared limiter *before* returning.

### 5.2 The shared rate limiter (canonical)

**One limiter, shared by all consumers** (backfill, reconcile, webhook hydration,
pass-through). PCO enforces limits per token, so for our **single org / single
process** this is a plain **in-process token bucket** — correct by construction,
no Redis. *(The scale-up path, where multiple processes share one token, replaces
this with a Redis GCRA keyed `ratelimit:{org_id}`; see [§12](#12-scaling-up-postgres--multi-org).
An in-process bucket across N processes would let each enforce its own 5 req/s and
cause a 429 storm — which is exactly why the multi-process profile needs Redis.)*
At ~300 people the limiter is barely load-bearing — a full backfill is a handful
of requests — but it stays correct for webhook-hydration bursts and mass imports.

- **Adaptive, not hard-coded.** `target = floor(TARGET_UTIL · observed_Limit)` per
  observed `Period`, seeded and continuously corrected from response headers so it
  follows the 100→75 drop or any per-endpoint limit automatically.
- **One utilization constant: `TARGET_UTIL = 0.80`** (read from shared config by
  every subsystem — the drafts disagreed at 0.85/0.80/0.90; 0.80 preserves burst
  headroom so an in-flight burst never clips 100).
- **Pre-emptive derate:** any request that would cross `offset > 30,000` is capped
  at the 75/20s tier. (In practice keyset paging keeps `offset ≈ 0`, so this is a
  guardrail, not a normal path.)
- **429:** honor `Retry-After` verbatim, drain the bucket, exponential backoff +
  jitter, per-token circuit breaker.
- **Priority classes** drained strictly, with a small reserved slice pass-through
  can always use:

  | Priority | Class | Budget policy |
  |---|---|---|
  | P0 | pass-through, webhook hydration | latency-sensitive; reserved slice |
  | P1 | hot sweeps (`person`/`email`/`phone`), merger poll | steady |
  | P2–P3 | warm/cold sweeps, reference pulls, drift probes | background |
  | P4 | initial backfill, delete audit | **preemptible**; drains *all remaining slack* |

### 5.3 Initial backfill — keyset on ascending `updated_at`, never deep offset

**Decision (canonical): backfill keysets on ascending `order=updated_at`,
`per_page=100`, `where[updated_at][gte]=cursor`** — offset never grows, so we keep
the full 100/20s budget the whole way. *(The drafts contradicted here — §4 claimed
`created_at`; `updated_at` is correct because its completion cursor is directly
reusable as reconcile's watermark, and edits made mid-backfill get a new
`updated_at > cursor` and are naturally re-emitted at the tail, so a long backfill
is self-healing.)*

The tie problem (second-resolution timestamps; `id` isn't in `can_order_by`) is
solved with a **`gte` cursor + a persisted "seen ids at this second" set**, plus a
**bounded within-second offset drain** for the rare >100-row second, hard-bounded
by `where[updated_at][lt]=second+1s`:

```
cursor, seen = load_checkpoint() or (EPOCH, {})
INCLUDES = "emails,phone_numbers,addresses,field_data,field_data.field_definition,\
            social_profiles,households,marital_status,name_prefix,name_suffix,\
            primary_campus,school,inactive_reason"
loop:
  page = GET /people?order=updated_at&per_page=100
                    &where[updated_at][gte]=iso(cursor)&include=INCLUDES     # priority=P4
  if page.data == []: break                                                 # → hand off to reconcile
  fresh = [r for r in page.data if r.id not in seen]
  apply page.data + page.included via mirror_upsert                         # §3, idempotent
  max_ts = max(r.updated_at for r in page.data)
  if max_ts == cursor and len(page.data)==100:                             # a saturated single second
     drain_second(cursor, seen)                                            # offset paging, bounded by [T, T+1s)
     cursor, seen = cursor+1s, {}
  else:
     seen   = {r.id for r in page.data if r.updated_at==max_ts}
     cursor = max_ts
  checkpoint(cursor, seen)                                                   # mirror_sync_state.backfill_*
```

**Completion** hands the final (max) `updated_at` to reconcile as
`reconcile_watermark` — so the first sweep is a controlled no-op, not a full
re-scan. *(Caveat: a >100-row single second still being written during backfill
isn't guaranteed complete in one pass; the `total_count` drift probe (§7.4)
detects the shortfall and re-sweeps that window. A single second holding >30,000
rows is a documented pathological corner.)*

### 5.4 Sideload hydration collapses N+1 → 1

One include-page returns people **plus** their children in `included[]`.

| Strategy | Requests for *P* people |
|---|---|
| N+1 (list page + ~6 child GETs/person) | `P/100 + 6P ≈ 6P` |
| Single include page | `≈ P/100` |

| Org size | N+1 | Include | Backfill wall-clock @ ~4 req/s (token exclusive) |
|---|---|---|---|
| 50,000 | ~300,500 | **500** | ~2 min |
| 500,000 | ~3,005,000 | **5,000** | ~20 min |

A ~600× request reduction. (Request count is the binding constraint; gzip absorbs
the larger bodies.) These figures assume the token is exclusive to the mirror — a
co-tenant on the same token roughly halves throughput, and the limiter derates
when observed `Count` exceeds our own issued tally.

### 5.5 Concurrency

Throughput is limiter-bound (~4 req/s), not CPU-bound; ~1.3 requests in flight
saturate it, so **3–4 fetch-workers** is the sweet spot. More workers add queue
latency, not speed. All pools draw from the one limiter, so total in-flight is
bounded by the budget regardless of pool sizes.

---

## 6. Webhooks: real-time updates

The **fast path**: thin at the edge (verify + durably capture + ack), all real
work async, so PCO never sees a slow or failing endpoint.

### 6.1 Subscription bootstrap

`GET /webhooks/v2/available_events` → subscribe to `people.v2.events.<resource>.<action>`
(create/update/destroy) for every mirrored type, **one subscription per event**.
Anything in `DESIRED \ AVAILABLE` is logged as a capability gap so reconciliation
knows those types have **no fast path**. Each subscription gets a **unique
receiver URL** `POST /pco/webhooks/<url_token>` so the authenticity secret is an
O(1) lookup (the auth header carries only the HMAC, not the subscription id).
Secrets are stored via the **version-pointer model** (`webhook_subscription.secret_version
→ org_secret`, envelope-encrypted) so rotation can accept **two live secrets**
during an overlap window. Re-checked hourly by a health job (re-create missing,
re-activate any PCO reports inactive).

### 6.2 Receiver — verify, capture, ack fast (< 500 ms)

```
POST /pco/webhooks/<url_token>
  raw = request.raw_body_bytes                       # EXACT pre-parse bytes
  sub = subscription_cache[url_token]  or  return 404          # unknown token — NOT 410
  sig = header["X-PCO-Webhooks-Authenticity"]        or  return 401
  if not constant_time_eq(hmac_sha256(sub.secret, raw), sig):  return 401   # bad sig — NOT 410
  try:
     env = json_parse(raw)
     upsert webhook_delivery(delivery_id=env.id, raw_body=raw, signature=sig)  ON CONFLICT DO NOTHING
     for item in env.data:                            # a delivery may BATCH several events
        insert webhook_event(event_id=item.id, payload=json_parse(item.attributes.payload), …)
                                                       ON CONFLICT (event_id) DO NOTHING
     notify_workers()
  except: return 503                                  # pre-capture failure only; PCO retries safely
  return 204                                          # ack AFTER durable commit
```

Non-negotiables: **HMAC over the raw bytes** (never re-serialized JSON),
timing-safe compare; **durable insert before ack**; **exact bytes stored** in
`webhook_delivery.raw_body` (bytea) for replay/re-verification; dedup per **event
id** (handles batched deliveries — a delivery batching `person.updated` +
`email.created` yields two inbox rows, not one). **Never emit 410** — an explicit
response allow-list `{200,204,401,404,503}` plus a catch-all coercion to 204 (post-
capture) / 503 (pre-capture), enforced by a contract test, guards the 410 footgun.

### 6.3 Async worker — dispatch, guard, hydrate

Workers pull with `FOR UPDATE SKIP LOCKED`, parse `event_name` → `(resource, action)`
via a static registry that maps to **singular physical table names** (`person`,
`email`, `field_datum`, …). Then:

- `person_merger` → `merge_handler` (§7.3).
- `destroyed` → `mirror_tombstone(table, id, updated_at ?? received_at, 'destroyed')`.
- otherwise → `mirror_upsert(table, id, payload, 'webhook')`; if the payload is
  **thin** (omits a projected relationship/child, or the type needs includes),
  also **enqueue a hydration task**.

**Thin-payload hydration is coalesced and burst-guarded.** Tasks key on
`(resource_type, pco_id)` so a burst of child events for one person folds
into a single follow-up GET (debounced ~2–5 s), fetched **through the shared
limiter** (webhooks aren't rate-limited but our GETs are). **Critically**, a mass
import (say 50k new people, each firing a thin `created`) would otherwise fan out
to 50k per-id GETs (~3.3 h at 4 req/s) versus the same data via a 500-request
include-sweep (~2 min). So the hydration queue is **burst-detected**: above a
depth threshold for a resource, per-id hydration is suspended and an **incremental
include-sweep** is triggered/advanced instead (100 people+children per request),
dropping the individual tasks it will cover. This keeps a bulk change on the
~100× cheaper path.

### 6.4 Failure handling

Because the event was durably captured *before* the ack, processing failures are
**ours**, never propagated to PCO (which would spawn ~16 retries and risk the 410
footgun). Transient → `schedule_retry` with exponential backoff; permanent (unmapped,
unparseable, or failing after ~8 attempts/2 h) → `webhook_dead_letter` + alert. A
dead-letter is a latency/alert event, **not data loss** — reconciliation
independently re-derives the resource.

---

## 7. Background reconciliation & drift repair

The **backstop that makes the mirror eventually-correct** regardless of what
webhooks drop, reorder, or never deliver. Four cooperating jobs, all through the
shared limiter and the canonical writers, all page-granular and checkpointed. A
single **leader-elected reconciler** drives a `(priority, next_run_at)` queue,
executing one page per tick so a long audit cooperatively interleaves with hot
60 s sweeps.

### 7.1 Incremental catch-up sweep

Per resource, `order=updated_at` asc, `where[updated_at][gte]=reconcile_watermark`,
`per_page=100`, apply via `mirror_upsert`, advance the watermark to the last
applied `updated_at`. Cost per cycle ≈ `ceil(changed/100)+1` (normally 1 request
returning 0 rows). Offset stays 0 forever → never the 75/20s penalty.

- **`gte` not `gt`** so a mid-second-bucket crash can't skip un-applied peers; the
  monotonic guard no-ops the re-seen rows.
- **Watermark ownership (canonical):** `reconcile_watermark` is advanced **only by
  this ordered sweep**. The webhook worker **never** touches `mirror_sync_state` —
  letting an unordered, lossy webhook move the reconcile cursor would advance it
  past records whose webhook was never delivered, defeating the entire safety net.
- **Resources without `updated_at` filtering** (e.g. `/addresses` exposes
  `order=updated_at` but no `where[updated_at]`): **descending walk** from
  `offset=0`, stop the instant a row's `updated_at ≤ watermark_at_start`.
- **Timestamp-less resources** (`field_definition`, `field_option`, `tab`,
  `household_membership`): **list-and-replace** — within one transaction,
  `mirror_confirm_live` every fetched row and `mirror_tombstone(reason='absent')`
  every stored row of that scope **not** in the fetched set (the drafts omitted
  this "replace half", so deleted options/memberships would linger forever).

### 7.2 Delete/merge detection (the hard part)

`where[updated_at]` cannot see a vanished id. Three mechanisms, fast→slow:

1. **`destroyed` webhooks** — seconds, but lossy → provisional tombstone.
2. **Merger poll** — tail `/person_mergers` by `created_at` every ~120 s. It's an
   immutable append-log with **`created_at` only**, so poll
   `where[created_at][gte]=merger_watermark` (**`gte`, not `gt`** — two merges in
   one second + a crash between them would permanently skip the second under `gt`;
   re-applying a merge is idempotent). For each: `mirror_tombstone(person,
   remove_id, reason='merged', merged_into=keep_id)`, then **enqueue survivor
   hydration** so children PCO moved to the survivor are reassigned. Merges are
   *the* normal PCO delete path, so this cheap poll is the primary durable delete
   signal.
3. **Full-id delete audit** — infrequent (weekly; nightly for small/churny orgs).
   Enumerate all live ids keyset on **immutable `created_at`** with a **non-empty**
   sparse fieldset `fields[Person]=created_at` (so the keyset field is present in
   the payload — an *empty* fieldset strips `created_at` and the cursor can never
   advance), diff against the mirror, and for each mirrored-but-absent id issue a
   **confirming `GET`**: `404` → `mirror_tombstone(reason='audit_absent')`; `200`
   → `mirror_confirm_live` (false positive from a race). The confirm GET costs one
   request *per diff member only* (normally ≈0).

**Child deletes ride on include-diff (zero marginal cost):** whenever a person is
hydrated `include=…`, the `included[]` is the authoritative current child set;
tombstone any local child of that person not present. This catches single-child
hard-deletes no `updated_at` sweep can see, piggybacking on requests we already
make — which is why child types need no full audit of their own.

**Merge-child cascade caveat:** don't blindly cascade-tombstone the merged
person's children — some were *moved* to the survivor (same child id, now
`relationships.person = survivor`). The survivor-hydration include-diff reassigns
those via `mirror_confirm_live` (authoritative, resurrects even at equal
`updated_at`); only children truly absent from the survivor are tombstoned.

### 7.3 Bounding the audit cost

Keyset on `created_at` keeps `offset ≈ 0` (full 100/20s tier); sparse fieldset
shrinks rows from ~2 KB to ~60 B (bandwidth, not request count):

| Org size | Enum requests | Wall-clock (P4 draining ~4 req/s of slack) |
|---|---|---|
| 50,000 | 500 | ~2 min |
| 250,000 | 2,500 | ~10 min |
| 1,000,000 | 10,000 | ~42 min |

(P4 is slack-draining; if the token is busy with hot sweeps the audit stretches
proportionally — worst case ~2.8 h at a throttled 1 req/s for 1 M. Either way it
runs comfortably overnight while hot sweeps keep P1 freshness.)

### 7.4 Drift probe

One request (`per_page=1`) reads `meta.total_count`; compare to the mirror's live
count and record both into `mirror_sync_state.{total_count_last, mirror_count_last}`
— **the columns the ops `mirror_drift_ratio` alarm actually reads** (the drafts
had the alarm reading a column no job wrote). `mirror_live > total_count` ⇒ ghosts
(missed delete/merge) → schedule an audit; `mirror_live < total_count` ⇒ missing
rows → force a sweep.

**`total_count` population parity (resolved).** The alarm compares two counts, so
they must count the *same* population. PCO's docs don't state whether an
unfiltered `GET /people` (and thus `meta.total_count`) includes inactive/pending
people — and long-standing API behavior is that the list endpoint returns **all
statuses** by default (unlike the web UI, which hides inactive), with
`total_count` reflecting whatever `where[...]` filter is applied. Rather than
depend on that undocumented default, the design **pins the population in both
places and calibrates once**:

1. The probe issues a fixed query shape (default: **no `status` filter**), and the
   mirror counts the identical predicate (default: all live rows, any status —
   we mirror every status).
2. **Onboarding calibration:** right after the first full backfill, assert
   `total_count` (unfiltered probe) equals the mirror's full live count. If they
   differ, the delta *is* PCO's default population for this org — pin the matching
   `where[status]=…` on the probe and the same `status=…` on the mirror count.
   For a single org this is a one-time measured constant, not a guess.

(`status` is tri-state — `active`/`inactive`/`pending` — and we mirror all three,
so `where[status]` is fully serveable locally.)

---

## 8. Serving API & live pass-through

`pcomirror-serve` is an HTTP service in front of the mirror whose prime directive
is to be a **drop-in for `…/people/v2`** — an existing PCO client works after only
a base-URL + credential swap. **Reads** are served from SQLite; **writes** are
proxied to PCO and applied back ([§8.4](#84-writes--write-through)); anything not
mirrorable, or a caller demanding live freshness, is proxied through the shared
limiter. The mirror *is* the cache; there's no second cache to invalidate for
mirrored types.

### 8.1 Read surface

Same paths (`GET /people/v2/:type`, `/:type/:id`, `/people/:id/:rel`) and
`Content-Type: application/vnd.api+json`. Query grammar translates to SQL against
**typed projected columns** (never blind JSONB scans): `where[col]`,
`where[col][gt|gte|lt|lte]`, `%`→`ILIKE`; `order`/`-order`; `include` (2 levels,
joined from local child tables, de-duped by `(type,id)`); sparse fieldsets. The
allowed `col` set is a per-type **allowlist mirroring `can_query_by`/`can_order_by`**;
off-list → `400`. `data` is reconstructed from stored `raw`, so it's byte-faithful
to PCO; `meta.can_*` advertises exactly which grammar the mirror honors.
Pagination supports PCO-parity `per_page`/`offset` **plus** a recommended keyset
`page[after]=<cursor>` over `(sort_col, pco_id)`.

**`where[search_*]` is served locally** (§11.3, decided). PCO's search filters are
normalised **substring** matches, not equality, so they are declared in the
registry as a set of haystacks (`Search`) rather than as queryable columns:
`search_name` over PCO's own `search_name` plus `first last` and `nickname last`,
`search_name_or_email` widening the same needle to the person's emails via
`EXISTS`, and the phone variants matching on digits only so formatting is
irrelevant. Folding is one Python function used on both sides — as a SQLite UDF
for the column and directly for the needle — so a search cannot disagree with
itself about case or whitespace. Matching uses `instr`, not `LIKE`, so `%` and `_`
in a needle are literal and there is no escaping to get wrong. A needle that
normalises away for every haystack a filter covers (a name typed into a
phone-number search) matches **nobody**; only a genuinely blank value filters
nothing, which is how PCO reads it.

**Cannot be replicated locally → pass-through or degrade:** dynamic **List rule
evaluation** (a List is a saved *query*, and `list_result` is only PCO's last
materialized membership — mirroring it would serve a stale answer with no way to
tell; live eval → pass-through), **household memberships**, permission-derived
`filter=admins`, and all aggregates/reports/`/me`.

**Household membership, specifically: walked, not proxied.**
`GET /household_memberships` is a **404** — PCO exposes the rows only under
`/households/{id}/household_memberships`, one household at a time, and the payload
carries no `household` relationship. The household id appears only inside
`links.self`, so it is projected back out of the URL PCO sent rather than injected
into `raw`, which stays verbatim.

The refresh is a **periodic full walk**, a new `method="nested_walk"`. There is no
`updated_at` on a membership, and joining a household does not reliably move the
household's own — measured: 6% of households hold a member created after the
household was last touched — so no watermark can drive it. That rules out
event-driven refresh, not mirroring: a scheduled full walk is the standard
treatment for a slowly-changing dimension, and `reference_periodic` already
applies it to the reference tables. The cost is one request per parent, which at a
few hundred households is ~3 minutes daily and ~0.1% of the rate budget.

Two properties make the walk trustworthy. Each parent's answer is **authoritative
for that parent**, so anything the mirror still holds for it and PCO no longer
returns is tombstoned — otherwise a walk could only ever add, and a parent leaving
a household would never be noticed. And a parent that cannot be reached does not
abort the sweep or silently pass: its rows stay as they were, and the walk raises
rather than recording completion, so it runs again instead of leaving a gap nothing
knows about.

The staleness window is therefore the sweep interval rather than unbounded, which
is the trade this makes against pass-through: a day-old `household_role` against a
parent's phone number being unavailable whenever PCO is. For the read a counselor
waits on at a door, bounded staleness is the better failure.

**"Not walked yet" is not "empty", and the read path has to know the difference.**
A resource collected one parent at a time has a third state the other resources do
not: a parent whose collection has never been fetched. Its table rows are absent,
and absent rows serialize to an empty page — which is not a weaker answer than the
truth but the *opposite* one. For household memberships it reads as "this student
has no parent", and a caller that trusts it says so in the words a family would
read: *nobody can reach this family in an emergency*.

So the walk keeps a ledger, `nested_walk_state`, of which parents it has actually
visited, and a read of an unvisited parent does not answer from the empty table.
It fills that one parent first — a single upstream request, once ever per parent,
after which the periodic walk keeps it current — and if PCO cannot be reached it
returns **503**. An empty collection is then only ever a statement about the
family; an error is the only way the mirror says "I do not know".

The fill is bounded per read. One nested read needs one parent; only a page-wide
`include` can want one per row, and a hundred serial upstream requests is not a
response but a timeout — past the budget the read answers 503 with the same
distinction intact. The ledger is also seeded from rows already held on first
open, because a row can only exist if its parent was walked: a mirror that has
been walking for months does not re-fetch 500 households to learn what it knows.

This is also what makes the resource safe to *add*. Every sweep in the scheduler is
gated on the resource having been backfilled, and a resource declared after a
mirror was first built has no backfill — so on an existing deployment the walk
never ran at all, and every household read empty indefinitely. The scheduler now
adopts a newly declared resource by backfilling it once; the read-time fill covers
the interval before that lands, and the ledger keeps the cost at one request per
parent rather than one per read.

**The mirror does not invent the top-level collection either.** `GET
/household_memberships` is a 404 at PCO, and so is `GET
/household_memberships/{id}` — the row is addressable only through its household.
Both are 404 here. Serving them would put paths in front of clients that no other
backend has, and a client that came to depend on one would break on the API this
is a drop-in for.

The link map follows from that: a parented resource is linked **through** its
parent (`/households/{h}/household_memberships/{m}`), which is also the form PCO
returns and the one the owning id is parsed back out of. And because the mirror
generates a relationship link per declared relationship, the router serves the
segment past a parented record too — publishing a link it would then refuse is its
own bug, and PCO answers those paths.

**A sideloaded copy may not make a record poorer.** A compound document can carry
the same resource twice — `GET /people/X?include=households.people` returns X in
`data` with every requested relationship resolved and again in `included`, as a
member of their own household, carrying almost none. Both have the same
`updated_at`, so the monotonic guard admits both. `route_page` therefore applies
`included[]` first and `data[]` last, and `upsert` takes a `primary` flag: at an
equal timestamp a sideload may refresh a record but may not replace it with one
carrying fewer relationships.

**Include-synthesized relationships are not stored.** PCO answers
`include=households.people` by adding a `people` relationship to the *Person*.
It is an artefact of the request rather than part of the record, so the writer
strips it before storing (on a copy — on a pass-through that same object is the
caller's response, and they are entitled to PCO's answer verbatim) and the
serving layer regenerates it whenever a nested include asks for it.

**Page links carry the whole query.** `links.self`/`next`/`prev` are rebuilt from
the caller's own query string with only `offset`/`per_page` replaced, and
`meta.next`/`meta.prev` carry the same cursor for clients that read it there. A
link that dropped `where`/`order`/`include` would not be a smaller answer — it
would be a *different* query wearing the same URL, duplicating and skipping rows
with nothing to signal it.

**Sparse fieldsets** (`fields[Type]=a,b`) are honoured with PCO's semantics: the
named set limits attributes *and* relationships for that type, applies to
sideloaded resources by their own type, leaves `links` alone, and treats an
unknown name as selecting nothing rather than as an error. `include=` still
sideloads a relationship the fieldset does not name.

**A record has one shape regardless of the request.** PCO varies a resource's
relationship set per call — a bare `/people` read carries only `primary_campus`;
`emails` appears only under `include=emails`. The mirror serves what it holds,
which a backfill always fetched with includes, so its set is a superset. This is
the same decision as the generated `links` map and is strictly additive: nothing
PCO would have sent is omitted, and nothing PCO does not have is invented. The
golden suite asserts exactly those two properties rather than equality.

**Filters may reach through relationships.** `where[<rel>][<attr>]`, to any depth
the registry models, compiles to a chain of correlated `EXISTS` subqueries — one
hop per relationship, each joined by the same rule the relationship is served
with. PCO documents ~100 such filters and applies none of them (measured: a value
that cannot match anything still returns the whole collection), so this is a
deliberate divergence, recorded in `tests/golden/`.

**Nothing given is silently ignored** (the rule behind that divergence and the
`refuses` ones). An unsupported filter, order key, uncoercible value or
unmirrored `include` is a `400`. PCO answers `200` having dropped it, which a
caller cannot distinguish from a correct answer; between a loud error and a
silently wrong page, the error is the safe failure.

**To-one relationships are resources, not collections.** `/:type/:id/:rel` for a
`one` relationship returns a single object and `404`s when the foreign key is
unset, which is what PCO does — the earlier form answered with an empty page.

**Nested collections get the top-level surface.** `/:type/:id/:rel` runs the same
where/order/include/pagination path as the collection read, restricted to the
relationship — because that is what PCO serves at
`/households/{id}/household_memberships`, includes and all.

### 8.2 Freshness is first-class

Model: **eventually consistent, bounded staleness** = `max(webhook lag, reconcile
interval for the row's class)`. Every row carries `last_synced_at`/`pco_updated_at`/
`source`/`deleted_at`; surface them in `data[].meta.mirror` and headers
(`X-Mirror-Source`, `X-Mirror-Oldest-Synced-At`, `X-Mirror-Staleness-Bound-Seconds`,
`X-Mirror-Reconcile-Age-Seconds` — the last exposes a *stalled reconciler* even
when rows look recent). A caller may demand freshness with
`X-Mirror-Max-Staleness-Seconds: N`; if any served row (or the class's reconcile
age) exceeds `N`, serve transparently upgrades to pass-through. Tombstoned rows are
hidden by default; a single GET on a destroyed/merged id returns **410 Gone**
(+ `Location` and `meta.merged_into` for merges).

### 8.3 Pass-through

Explicit opt-in (`?passthrough=on|auto|refresh`, scope-gated) or automatic
fallback (miss, freshness violation, non-mirrorable route). **Always through the
shared limiter** (it spends the same per-org budget as sync — a per-key
`passthrough_quota` stops one caller starving reconciliation). Mirrorable results
are **read-through** via `mirror_upsert(source='passthrough')` (never regresses a
newer webhook write); non-mirrorable results (stats/reports/search) go to a
short-TTL `passthrough_cache`.

### 8.4 Writes — synchronous write-through (PCO-first, fail-if-it-fails)

**The mirror is never the authority for a write.** A local `POST`/`PATCH`/`DELETE`
is a **synchronous proxy to PCO**: we make the equivalent PCO call *first*, and
**the caller's write succeeds only if PCO's does**. The mirror is touched **only
after** PCO returns success, using PCO's own returned resource. There is no local
write buffer and no "accept now, sync later" — if PCO is unreachable or rejects
the write, the local write fails and the mirror is left exactly as it was.

```
def write_through(req, ctx):                        # caller's api-key must hold the `write` scope
  require_scope(ctx.key, 'write')
  limiter.acquire(priority='passthrough')           # writes spend the shared PCO budget too

  # 1) The equivalent PCO RPC — same method + path + JSON:API body, host-swapped.
  try:
      resp = pco.request(req.method, pco_path, body=req.body, auth=PCO_PAT)
  except (Timeout, ConnError):                      # PCO unreachable
      return 504, mirror_error("upstream_unreachable")     # nothing written locally

  # 2) FAIL IF IT FAILS — any non-2xx is the caller's failure; the mirror is untouched.
  if not resp.ok:                                   # 400/401/403/404/409/422/429/5xx…
      return relay(resp)                            # PCO's status + JSON:API errors, verbatim

  # 3) Success only: apply PCO's canonical resource so the write is read-your-writes.
  if req.method == 'DELETE':
      mirror_tombstone(table, pco_id, now, 'destroyed')             # PCO confirmed the delete
  else:
      for r in [resp.data] + resp.get('included', []):
          mirror_upsert(table_of(r), r.id, r, source='passthrough') # POST → new pco_id inserted
  return relay(resp)                                # 200/201 + Location, exactly what PCO returned
```

**Guarantees this gives you:**

- **PCO-first, mirror-second.** The mirror can never hold a write PCO rejected,
  and the caller is never told "success" unless PCO accepted it. Ordering is
  strict: PCO call → (on 2xx) mirror update → response.
- **Fail-closed on every failure.** Validation (`422`), optimistic-concurrency
  conflict (`409`), auth (`401/403`), not-found (`404`), rate-limit (`429`), PCO
  `5xx`, or a network timeout (`504`) all abort the write and relay PCO's status;
  the mirror is not modified. Reads keep working from the mirror throughout.
- **No split-brain.** Applying PCO's *returned* resource (not the request body)
  means the mirror matches what PCO actually stored. PCO **also** fires a
  `created`/`updated`/`destroyed` webhook for the same change; because it carries
  the same-or-newer `updated_at`, the canonical monotonic writer makes it an
  idempotent no-op — belt-and-suspenders, never a double-apply.
- **Read-your-writes.** A `POST` inserts the new `pco_id` immediately, so the very
  next local read (even before the webhook lands) sees the change.

*(This deliberately couples writes to PCO availability: if PCO is down, writes
fail — which is what "always call PCO and fail if it fails" requires. If you ever
want writes to survive a PCO outage, that would need an explicit outbox/retry
queue and a documented weaker guarantee; it is intentionally **not** in this
design.)*

- **PCO is authoritative for validation and conflicts.** A rejected write (`409`
  stale update, `422` invalid) is relayed verbatim; the mirror is untouched.
- **No split-brain.** The write applies PCO's *returned* resource through the same
  monotonic `mirror_upsert`, and the follow-up `created`/`updated` webhook that
  PCO also fires is an idempotent no-op (same or older `updated_at`). A `POST`
  yields the new `pco_id`, inserted immediately so the very next local read sees it.
- **Failure = safe.** If the PCO call errors, nothing is written locally and the
  caller gets PCO's status (or a `502`); the mirror never diverges from PCO.

**Two strictly separated credential planes:** local `api_key` (hashed, scoped —
`read:*` / `passthrough` / `write` — with a per-key local rate + pass-through
quota) authenticates apps to pcomirror; the upstream **PCO PAT** lives only
server-side and is never exposed to or selectable by callers.

---

## 9. Auth, versioning & operations

> This section covers the general architecture. For our **single-org SQLite
> profile** ([§0](#0-deployment-profile-decided)): auth is the **PAT** (no OAuth,
> no refresh loop); there is no multi-tenancy (skip §9.2); and secrets are stored
> as noted below. The OAuth/KMS/RLS machinery here is the
> [scale-up path](#12-scaling-up-postgres--multi-org).

### 9.1 Auth to PCO

**PAT (HTTP Basic `app_id:secret`) is the chosen auth** — zero refresh machinery,
no expiry, no consent flow. The two secrets we hold (the **PAT** and each
subscription's webhook **`authenticity_secret`**) live encrypted at rest: an OS
keyring, or a `0600` secrets file decrypted with a key from the environment — no
KMS needed at this scale. (Scale-up swaps this for **OAuth 2.0 + PKCE** with a
leader-elected single-flight refresh loop and KMS envelope encryption; see §12.)

### 9.2 Multi-tenancy

**Single DB, `org_id` discriminator on every table + PK, with RLS** (`USING
(org_id = current_setting('app.org_id')::bigint)`) — the sweet spot for tens–low-
hundreds of orgs, and a forgotten `WHERE org_id=` becomes a non-event, not a leak.
Limits are **per token**, so each org has its own budget and the **limiter is
keyed per `org_id`**. Watermarks/subscriptions are per `(org_id, resource)`.
Schema-per-org / db-per-org are reserved for a residency/isolation mandate; the
`org_id` + `resolve_auth` + limiter seams make promoting a whale tenant a data-
move, not a rewrite.

### 9.3 Versioning ops

Pin `X-PCO-API-Version` per org (`org.api_version`, default `2026-06-04`) so a
risky bump is canaried on one tenant first. Upgrade playbook: download+diff the new
spec, re-project generated columns from retained `raw` (no API calls), test against
golden fixtures, canary, fleet-roll; rollback = flip the date back. A projection-
contract hash + a runtime canary (sampled `raw` key-sets ⊆ the version's declared
set) emit `spec_drift`/`unmapped_attribute` — a "you're missing a column" signal,
never data loss.

### 9.4 Observability

Prometheus + structured logs, labeled `org_id` × `resource` × `source` (never
`pco_id` — cardinality). Key signals:

| Metric | Alert |
|---|---|
| `pco_ratelimit_utilization` | >0.85 5 m (warn); sustained 1.0 (crit) |
| `pco_429_total` | rate >1/min for 10 m |
| `webhook_lag_seconds` (`received_at − payload.updated_at`) | p95 >60 s (warn), >300 s (crit) |
| `webhook_signature_fail_total` | any >0 (rotated secret / attack) |
| `reconcile_lag_seconds` | >2× configured sweep interval |
| `deadletter_depth` | >0 (warn), >100 (crit) |
| `mirror_drift_ratio` (`mirror_count_last` vs `total_count_last`) | \|1 − ratio\| > 0.005 — **primary silent-miss alarm** |
| `hydration_queue_depth` | above burst threshold → triggers sweep (§6.3) |
| `oauth_token_expiry_seconds` / `oauth_reauth_required` | <900 & not refreshing / ==1 (page) |
| `backfill_progress_ratio` | stalled >15 m |

Health endpoints: `/healthz`, `/readyz` (DB + KMS + Redis + migrations at head),
`/metrics`, and an ops `/status/orgs`.

### 9.5 Failure modes (reconciliation is the universal cover)

| Failure | Immediate mitigation | Structural cover |
|---|---|---|
| PCO outage | backoff + breaker; **serve stale reads**; pause pass-through with 503 | reconcile replays the gap on recovery |
| 429 | honor `Retry-After`, shed P4 first | per-org GCRA + 0.80 target + no deep offset |
| Access token expired | proactive T-10min refresh; refresh-then-retry on 401 | refresh loop |
| Refresh lapsed >90 d | `reauth_required`, stop writes, page | reads unaffected; post-reauth reconcile |
| Webhook endpoint down (in window) | fix receiver | PCO at-least-once redelivery |
| Webhook lost (past window / gap) | targeted reconcile | **reconciliation is the designed cover** |
| Poison webhook | dead-letter after N; always 2xx post-capture | reconcile re-derives truth |
| Out-of-order / duplicate | monotonic guard + per-event dedup | idempotent by `(org_id,pco_id)` |
| Partial backfill crash | resume from `mirror_sync_state` keyset cursor | idempotent replay |
| Merge / destroy | tombstone + `merged_into_pco_id` | merger poll + audit catch misses |

### 9.6 Deployment

**Single-org SQLite profile (chosen):** **one service process** holding the SQLite
file in WAL mode, with three concerns as internal loops/threads — the
**webhook-receiver** (the only internet-facing part, behind TLS), the **fetch +
reconcile workers**, and the **api-server** for local apps. A single scheduler
thread drives the reconcile sweeps, merger poll, and drift probe. No Redis, no DB
server, no leader election. Backups are a periodic `VACUUM INTO` / file copy (WAL
checkpoint first). If you ever want the receiver isolated (public exposure), split
it into its own process — WAL lets both share the one file (single writer, many
readers); coordinate the writer with `busy_timeout`.

Packaged as a **single Docker container** (`Dockerfile` / `docker-compose.yml`):
non-root, binds `0.0.0.0:8080`, the SQLite file on a `/data` volume (the only
state to back up), `SIGTERM`-graceful for clean `docker stop`, and a `/healthz`
healthcheck. Webhooks require a public HTTPS endpoint, so a TLS-terminating
reverse proxy / tunnel sits in front and forwards to `:8080`. See the README's
"Run it in Docker".

*(Scale-up deployment — many orgs or a large org — fans this into autoscaled
receivers, queue-scaled fetch-workers, a leader-elected scheduler, an RLS-scoped
api-server, Postgres + read replicas, and Redis for the shared limiter/queue; see
§12.)*

---

## 10. Canonical decisions & resolved review ledger

The six subsystems were designed in parallel, then adversarially reviewed. The
review's central finding was **cross-section divergence** — the "shared" writer,
state table, inbox, and limiter were each specified several incompatible ways.
This section is the reconciliation; each row is a settled decision baked into the
schema files ([`docs/schema.sqlite.sql`](docs/schema.sqlite.sql) /
[`docs/schema.sql`](docs/schema.sql)) and the sections above. Rows 4/5/18 (Redis
GCRA, KMS) describe the multi-process/scale-up resolution; the single-org profile
uses the simpler equivalents from [§0](#0-deployment-profile-decided).

| # | Divergence / bug found | Canonical decision |
|---|---|---|
| 1 | Webhook path advanced the reconcile watermark → a lost/out-of-order webhook permanently skips an update | **Only** the ordered reconcile sweep advances `reconcile_watermark`; webhooks never write `mirror_sync_state` (§7.1) |
| 2 | Monotonic guard specified 4 ways (`>` / `>=` / bare-WHERE / CASE-GREATEST) | **One** `mirror_upsert`: data on `≥`, `last_synced_at` **always** advances, sticky-tombstone un-delete on `> tombstone_uat` (§3) |
| 3 | Tombstones cleared unconditionally → resurrection of destroyed/merged ids | Sticky `tombstone_uat` + merges terminal; resurrection only via `mirror_confirm_live` (authoritative live GET) (§3.2) |
| 4 | Rate limiter: in-process object incompatible with multi-process deploy | **Redis GCRA keyed `org_id`** is the only sanctioned limiter when >1 process calls PCO; startup assertion (§5.2) |
| 5 | Three utilization targets (0.85/0.80/0.90), two algorithms | **One** GCRA, **`TARGET_UTIL=0.80`** from shared config (§5.2) |
| 6 | Thin-webhook hydration amplified a mass import ~100× (50k GETs vs 500-req sweep) | Burst-detect hydration depth → switch to include-sweep, drop covered tasks (§6.3) |
| 7 | Backfill sort key contradicted (`updated_at` vs `created_at`); watermark seeded to `min` | Backfill keysets **ascending `updated_at`**; seed `reconcile_watermark = max` at completion (§5.3) |
| 8 | Merger poll used `gt` → same-second + crash skips a merge forever | `where[created_at][gte]`, idempotent re-apply (§7.2) |
| 9 | Delete audit used empty `fields[Person]=` → keyset cursor field is NULL, audit never advances | `fields[Person]=created_at` (non-empty) (§7.2) |
| 10 | Four divergent state tables; drift alarm read a column no job wrote | **One** `mirror_sync_state`; reconcile writes `total_count_last`/`mirror_count_last` the alarm reads (schema §8, §7.4) |
| 11 | Two webhook inbox tables (delivery-id vs event-id grain); batched deliveries dropped events | **One** per-event `webhook_event` (`data[].id`) + `webhook_delivery` envelope-audit with exact bytes (schema §8) |
| 12 | `field_datum` hard-coded `person_pco_id NOT NULL`, but owner is polymorphic | Store `customizable_type`/`customizable_id`; derive `person_pco_id` only for Persons (§4.3) |
| 13 | Denormalized `person.custom_fields` blob unmaintainable + lost-update races | Drop it; serve via `person_custom_fields` view (single source of truth) (§4.3) |
| 14 | Timestamp-less reference reconcile had no "delete absent rows" step | List-and-replace tombstones rows absent from the fetched set, in one txn (§7.1) |
| 15 | Merge cascade tombstoned children PCO had *moved* to the survivor | Survivor-hydration include-diff reassigns moved children via `mirror_confirm_live` (§7.2) |
| 16 | Singular vs plural table names diverged; webhook writes would hit missing tables | Canonical **singular** physical names; JSON:API `type` is a serialization concern (schema, §6.3) |
| 17 | `merged_into` column named 4 ways | `merged_into_pco_id` everywhere (schema) |
| 18 | Webhook secret storage inline vs version-pointer; pgcrypto vs envelope | Version-pointer `org_secret` + envelope encryption (KMS) (§9.1, §6.1) |
| 19 | `field_datum` typed columns not re-derived when its definition arrives later | Re-projection job on any `field_definition` (re)mirror, from retained `raw` (§4.3) |
| 20 | Dropped `form_submission`/`workflow_card_activity`/`person_app` without rationale | Added to LITE/FULL tiers (Appendix A) |

---

## 11. Decisions & the one calibration step

The four open decisions are now settled ([§0](#0-deployment-profile-decided)):
**single org**, **SQLite**, **write-through**, **PAT**. That leaves exactly one
thing that must be *measured* rather than decided, plus two minor knobs:

1. **`total_count` calibration (one-time, at onboarding).** As in [§7.4](#74-drift-probe):
   after the first full backfill, assert the unfiltered `total_count` equals the
   mirror's full live count. If they differ, pin the matching `where[status]` on
   the probe. No decision needed — the org tells us the answer once.
2. **Audit cadence** (minor). At ~300 people the full id-audit is ~3 requests, so
   run it **nightly** — it's effectively free. (Default in Appendix B is weekly;
   nightly is the better fit here.)
3. **`search_*`** — **decided: served locally** (§8.1). At a few hundred people a
   scan is free, and the round trip was not the real cost: pass-through spends the
   PCO budget on the one call a human waits for, keystroke by keystroke. Semantics
   are matched to PCO's rather than approximated — substring, not equality, over
   the name/nickname/email/phone haystacks. FTS5 remains the scale-up answer
   (§12) if the person table ever gets big enough for the scan to show.

**Still useful to confirm:** the **service language** (the store is fixed —
SQLite; the writer/pseudocode is language-agnostic). Any language with an HTTP
client, an HMAC library, and SQLite bindings fits — including a single-binary Go
or a small Python/Node service.

---

## 12. Scaling up (Postgres / multi-org)

The single-org SQLite build is deliberately a **strict subset** of the reference
architecture, so growth is additive, not a rewrite. If pcomirror ever serves many
churches or one large org, adopt these — each already specified in the sections
above:

| Trigger | Swap in |
|---|---|
| Many orgs, or > ~100k people | **PostgreSQL** ([`docs/schema.sql`](docs/schema.sql)) — same tables, generated columns, and the four writer functions as SQL. |
| Multiple churches | **Multi-tenancy** ([§9.2](#92-multi-tenancy)): `org_id` column + RLS; OAuth 2.0 + PKCE with the refresh loop ([§9.1](#91-auth-to-pco)); per-org watermarks/subscriptions. |
| More than one PCO-calling process | **Redis GCRA limiter** keyed by `org_id` ([§5.2](#52-the-shared-rate-limiter-canonical)) — the in-process bucket can't coordinate across processes. |
| Horizontal scale | The five-component deployment ([§9.6](#96-deployment)): autoscaled receivers, queue-scaled workers, leader-elected scheduler, replicas. |
| Secret rotation at scale | KMS envelope encryption + version-pointer `org_secret` ([§9.1](#91-auth-to-pco)). |

Because the data model, the canonical writer, ingestion, webhooks, and
reconciliation are identical across both, migrating is: dump SQLite → load
Postgres (raw JSON is portable verbatim), point the app at the new store, and turn
on the multi-process machinery. The reviewed correctness properties carry over
unchanged.

---

## Appendix A — Entity tiers (what we mirror)

| Tier | Resources |
|---|---|
| **FULL** (own table, webhook + reconcile) | `person`; children `email`, `phone_number`, `address`, `social_profile`, `field_datum`, `note`, `background_check`; links `household`, `household_membership`, `list_result`, `workflow_card`, `workflow_card_activity`, `workflow_card_note` |
| **LITE** (own table, slow list-and-replace) | `campus`, `field_definition`, `field_option`, `tab`, `list`, `list_category`, `note_category`, `note_category_subscription`, `marital_status`, `membership_type`, `name_prefix`, `name_suffix`, `inactive_reason`, `school_option`, `carrier`, `app`, `form`, `form_category`, `form_field`, `form_submission`, `person_app` |
| **PASS-THROUGH / DERIVED** | `birthday_people`, `organization_statistics` (`/stats`), `workflow_assignee_summary`, `report`, `people_import`(+conflicts/histories), `spam_email_address`, `message`, `message_group`, `/me` |
| **DERIVED side-effect** | `person_merger` — stored as an audit log **and** applied (retire the removed id) |

*Timestamp-less resources* (`field_definition`, `field_option`, `tab`,
`household_membership`) have no `updated_at`, so they use `mirror_upsert_untimed`
+ list-and-replace reconcile.

## Appendix B — Per-resource sync policy (seed)

| resource | endpoint | method | uat filter | incr | audit | pri | include |
|---|---|---|---|---|---|---|---|
| person | `/people` | incremental + audit + merger | yes | 60 s | weekly | P1 | emails,phone_numbers,addresses,field_data,households |
| email | `/emails` | incremental | yes | 120 s | cascade + include-diff | P1 | — |
| phone_number | `/phone_numbers` | incremental | yes | 120 s | — | P1 | — |
| address | `/addresses` | incremental (descending-walk) | **no** | 300 s | — | P2 | — |
| field_datum | `/field_data` | incremental | yes | 300 s | — | P2 | field_definition |
| household | `/households` | incremental | yes | 600 s | monthly | P2 | — |
| household_membership | (via household, per parent) | nested walk, list-and-replace per parent | n/a (untimed) | 86400 s + filled on read for an unwalked parent | — | P3 | — |
| note | `/notes` | incremental | yes | 600 s | monthly | P2 | note_category |
| social_profile | `/social_profiles` | incremental | yes | 600 s | — | P2 | — |
| background_check | `/background_checks` | incremental | yes | 900 s | monthly | P3 | — |
| person_merger | `/person_mergers` | merger_poll (created_at) | n/a | 120 s | n/a | P1 | — |
| reference/config (campus, field_definition, tab, list, marital_status, …) | `/…` | reference_periodic | mixed | 6–24 h | — | P3 | small |
| stats, birthday_people, report, people_import, … | — | passthrough_only | — | — | — | — | — |
