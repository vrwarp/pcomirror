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
for every mirrored type, **one subscription per event** — that is PCO's model, not
a choice: a `WebhookSubscription` carries a single `name`, a single `url`, and its
own `authenticity_secret`. Anything in `DESIRED \ AVAILABLE` is logged as a
capability gap so reconciliation knows those types have **no fast path**.

**Many subscriptions may share one receiver URL.** Nothing at PCO requires the
URLs to differ, and its own console points every event you tick at one address —
so `POST /pco/webhooks/<url_token>` serves as many event types as an operator
registers on it, and `webhook_subscription.url_token` is not unique. The lookup is
therefore *token → the few subscriptions on it*, and **the secret that signed the
body selects which one is delivering**: it is the one part of the exchange only
the right subscription can produce. Reading the event name out of the payload to
choose a key would let the attacker-supplied half of the request pick the key it
is checked against, which is not a check. Every candidate secret is compared even
after a match, so timing does not leak which one was right.

That the receiver already tries a *set* of secrets is what makes rotation cheap:
two live secrets during an overlap window is the same code path as two events.
Secrets are stored via the **version-pointer model**
(`webhook_subscription.secret_version → org_secret`, envelope-encrypted).
Re-checked hourly by a health job (re-create missing, re-activate any PCO reports
inactive).

**Who owns the subscription list.** `PCOMIRROR_SUBSCRIPTIONS` is applied on every
`serve` start, which is what makes a container need no follow-up command — and is
also what would silently undo an operator's fix on the next restart. So the
operator page takes precedence once it has been used: saving anything at
`/admin/webhooks` sets `mirror_meta.subscriptions_managed_here`, after which the
environment is reported-and-skipped rather than applied, until it is handed back.
Same shape as the divergence override (§10), for the same reason — whoever can
reach the page at 9pm is rarely whoever can edit the container's environment.

**Events with no table.** The console offers events for resources this mirror
does not hold, and an operator may reasonably subscribe to one. Those are
captured, marked `status='ignored'` with the payload intact, and counted — *not*
dead-lettered. The dead-letter queue is the thing an alert points at; filling it
with events that were only ever going to be filed is how it stops being read.

### 6.2 Receiver — verify, capture, ack fast (< 500 ms)

```
POST /pco/webhooks/<url_token>
  raw  = request.raw_body_bytes                      # EXACT pre-parse bytes
  subs = active_subscriptions_on(url_token)          # 1..n — a URL may carry many events
  if not subs:                                                 return 404   # unknown token — NOT 410
  sig  = header["X-PCO-Webhooks-Authenticity"]                              # may be absent
  signed = [s for s in subs if s.secret != ""]
  open   = [s for s in subs if s.secret == ""]       # §6.2.1 — checked against nothing
  sub = first s in signed with constant_time_eq(hmac_sha256(s.secret, raw), sig)  # all compared
  if sub is None and not open:                                 return 401   # bad sig — NOT 410
  try:
     env = json_parse(raw)
     sub = sub or by_event_name(open, env)           # attribution only, never acceptance
     upsert webhook_delivery(delivery_id=env.id, raw_body=raw, signature=sig or "")
                                                       ON CONFLICT DO NOTHING
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

#### 6.2.1 A subscription with no secret

A blank `authenticity_secret` means the signature is not checked for that
subscription. It is spelled as an *absent secret* rather than as a flag because
the secret is the only thing a check could be made of — a separate switch would
be a second setting that can disagree with the first, and the way that resolves
is a receiver verifying against the empty string.

Wanted for senders that cannot sign, and for a stand-in during a rebuild.

**It moves the authentication rather than removing it.** The check leaves the
body's signature and lands on the token in the URL, and a URL whose token is
unguessable is a bearer credential — the same model as an API key in a header,
and the model half the webhook senders in the world actually use. `mint_token`
issues 32 characters of base64url (192 bits), so *no secret + minted token* is an
ordinary configuration. What is left over is *no secret + a token somebody could
guess*, which authenticates nothing at all.

Those two are not the same thing and are not reported the same way. The rule is
`webhooks.token_is_credential`: at least `CREDENTIAL_MIN_LEN` (24) characters and
at least `CREDENTIAL_BITS` (100) bits by `len × log2(distinct symbols used)` —
scored over the alphabet the token *actually* uses, so a 32-character run of one
character is zero bits rather than 32. A minted token scores ~149; a typed name
like `person-events-01` scores ~55.

The estimate assumes the characters were chosen independently, which is true of a
minted token and false of a phrase — a long descriptive slug scores like
randomness and is not. That limit is documented on `token_bits` rather than
papered over, and the page steers an operator turning the secret off towards
leaving the token blank, which is the case the estimate is exactly right about.

The bar also has to be one a minted token never trips, or it fires at random.
That is not hypothetical: minting was `token_hex(16)`, whose 32 characters draw
on 16 symbols, and about one token in ten thousand used nine or fewer and scored
under 100 — the mirror minting a token and then calling it guessable on its own
page. `token_urlsafe(24)` is the same length over 64 symbols; the worst of 300k
draws scored 131.

The rest of the cost is unchanged by any of this and belongs written down:

- A receiver is only as checked as its **least**-checked subscription. Signed
  subscriptions on the same URL still verify and are still attributed to
  themselves, but the URL's authentication is now the token, and a body may claim
  any event name (§6.2 files events by the *item's* name, not the subscription's).
- Nothing else is relaxed: an unknown token is still 404, the token format is
  still enforced, and pausing the last unverified subscription closes the URL.
- If the URL is the credential it wants a password's handling: TLS, and out of
  anything that logs or forwards URLs.

Two consequences fall out of the code rather than the policy. `sig` is used for
*attribution* only once a match is found, so with no secrets there is nothing
that can name the sender — the delivery's own event name is the best available
guess and is only a label on the audit row. And `webhook_delivery.signature` is
`NOT NULL` while a sender with nothing to sign with sends no header at all, so
the absence is stored as `''`; letting that insert fail would have answered 503
and had the sender redeliver, for ever.

**Loud only for the case that warrants it.** `webhooks.unprotected_tokens` is the
single definition — no secret *and* no credential-grade token — read by the
`serve` log, the dashboard banner and the receivers page alike, because an alarm
computed in three places eventually disagrees with itself about when to go off.
It is said at every start rather than once at configuration time, because the
person reading the log on a Tuesday is not the person who ticked the box: same
treatment as `PCOMIRROR_ALLOW_ANONYMOUS` (§8.4), for the same reason. A
credential-grade receiver gets a note explaining what its URL now is, and
nothing else — banner every secretless receiver and an operator learns to scroll
past the banner, which costs more than it buys.

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

### 6.5 Recording what arrives (`webhook_call`)

§6.2 stores the exact bytes of every delivery the receiver **accepts**, which is
what re-verification and replay are made of. The gap that leaves is everything
before that point: a delivery refused at the signature check, a token nobody
owns, a body that would not parse — and the headers the decision was made from,
which were read, compared and dropped. "Planning Center says it delivered and the
mirror says nothing arrived" had no evidence on this side of the wire.

So `webhook_call` records the **request**, not the delivery: method, path, query,
`REMOTE_ADDR`, every header, the exact body, the status and note answered, and
how long it took. Written from the serving layer, after `receive` answers and
also when it *raises* — a delivery that crashed something is the one nobody can
otherwise reconstruct — and wrapped so a recording can never fail a delivery
(`diagnostics.Recorder`'s rule, for the same reason: PCO's answer to a 5xx is to
send it again).

**Verbatim, and unlike the `diagnostic_event` log that means unredacted.** That
log keeps filter names and drops filter values because it is meant to be safe to
paste into an issue. A recording is the opposite instrument: whether the bytes
the mirror hashed are the bytes Planning Center signed is not a question that can
be asked of a summary, and a signature over a re-serialized body is a signature
over a different body. The rows therefore hold whole payloads — names,
addresses, phone numbers — and the download hands them over as they are, with the
warning carried inside the file rather than only on the page that offered it.

Two bounds, neither a redaction:

- **Per call.** `body` keeps the leading `MAX_BODY` (256 KiB) bytes exactly, with
  `body_bytes` recording the true length and `truncated` saying so. The receiver
  answers before it knows who is calling, so an endpoint that writes whatever it
  is handed straight to disk is a way to fill the disk.
- **In total.** A ring buffer of `PCOMIRROR_WEBHOOK_RECORD_KEEP` rows (default
  500; `0` is off), overridable from `/admin/webhooks/calls` without a restart —
  the delivery an operator wants recorded is the next one, and restarting to turn
  recording on loses it. The consequence worth knowing before it matters: a flood
  of junk to an unknown token evicts real history. That is the price of recording
  the rejects, which is where the diagnostic value is.

The console reads them, filters accepted from rejected, and downloads either the
whole log as JSON, one call, or one call's **exact bytes** — the last of those
being what re-hashing a refused delivery actually needs.

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

`where[updated_at]` cannot see a vanished id. Four mechanisms, fast→slow:

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
   request *per diff member only* (normally ≈0). **Every resource that declares
   `audit_interval_s` gets one**, not `person` alone: a household is hard-deleted
   by the same click, and a mirror auditing only people kept three abandoned
   households live — and on their members' `households` arrays — indefinitely.
   Enumerating a few hundred households is four requests a day.
4. **A `nested_walk` parent that 404s** — `GET
   /households/{id}/household_memberships` answering `404` is PCO stating that the
   household is gone, and for a collection served only under its parent it is the
   *only* such statement PCO makes. Confirm it with a `GET` on the parent itself
   (a 404 on the collection is evidence, not proof, and the cascade takes every
   member's family with it) and tombstone.

**Child deletes ride on include-diff (zero marginal cost):** whenever a person is
hydrated `include=…`, the `included[]` is the current child set; tombstone any
local child of that person not present. This catches single-child hard-deletes no
`updated_at` sweep can see, piggybacking on requests we already make.

**But include-diff rides on hydration, and hydration rides on traffic.** A person
nobody writes to and no webhook touches is never hydrated again, so their deleted
email stayed live in the mirror indefinitely — and an email is not only served, it
is *matched*: a live divergence export showed `where[search_name_or_email]`
answering with people PCO no longer matched, and missing ones it did, because the
email table had drifted in both directions. So the child contact tables (`email`,
`phone_number`, `address`, `social_profile`, `field_datum`) declare
`audit_interval_s` too: the same id-set enumeration, a handful of requests a day,
as the backstop for the quiet records traffic never repairs. Include-diff remains
the fast path; the audit is the floor under it.

**But `included[]` is only half of PCO's answer, and the halves can disagree.**
Measured minutes after a parent was added to a live organization:
`/people?include=emails` returned that person with `relationships.emails` naming
an address and `included[]` not carrying it. So the diff tombstones a child only
when **both** halves have dropped it — `included[]` did not sideload it *and* the
person's own relationships no longer name it. A real delete removes it from both;
a sideload that has not caught up removes it from one.

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
(missed delete/merge); `mirror_live < total_count` ⇒ missing rows. Either way the
id-set audit is the mechanism that settles it — it tombstones ghosts *and*
restores missing rows the sweep's watermark has already passed — so a nonzero
delta on an audited resource **requests an audit** (`mirror_meta`
`audit_requested:<resource>`, cleared when one completes). The scheduler honours
the request once the previous audit is at least an hour old
(`Scheduler.DRIFT_AUDIT_MIN_INTERVAL_S`): a ghost no longer waits out the
nightly cadence being served — and offered back by every duplicate-check
search — while a delta the audit cannot close (a population-semantics
difference, not drift) costs one audit an hour, not one per probe. This stays
strictly the probe *asking*: only the scheduler runs audits, and
`PCOMIRROR_AUDIT_INTERVAL_HOURS=0` still switches the whole mechanism off,
requests included.

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

The same rule has to hold for a **pass-through**, and there the payload is not a
sideload but an ordinary primary resource. `GET /lists/{id}/people` answers with a
Person carrying `primary_campus` and nothing else, whatever that person actually
has, and the mirror stores what a pass-through returns. In a live mirror one such
read flattened 82 people, took their household edge with it, and the app reading
the mirror told a room of youth workers that nobody could reach those families.

So the comparison is a **superset of relationship keys first, count second**.
Counting alone was not enough: a narrower `include=` returns a *different* set,
not merely a smaller one, so an equal-sized payload could still drop the one
relationship that mattered. The count survives as the tiebreak because a superset
test on its own is a one-way door — a flattened record holding `primary_campus`
could never be replaced by a richer payload that does not carry that key, and
would stay wrong forever. Either way `raw` stays verbatim: one payload is stored
whole, never a merge of two.

**A degraded record has to be repairable, and nothing else looks.** Stopping the
flattening does not undo it, and no other check here would ever notice: the
incremental sweep is keyed on `updated_at`, which will not move again for a record
whose only change was ours; the audit looks for deletions; the drift probe counts
rows, and a hollow record still counts as one. That made a degraded record a
one-way door — the 82 were still wrong days later.

`repair_incomplete` closes it. A row missing a relationship the resource's
`includes` asks for was written by something narrower than a mirrored fetch, so it
is queued through the existing hydration path and re-read whole. The expectation
comes from the registry rather than from comparing rows against each other,
because the case that matters most — a pass-through of an entire collection —
flattens every row at once and leaves no richer peer to compare against. A
recency floor keeps it from spinning on a record PCO will not answer more fully.
It runs on the scheduler beside the drift probe, and `pcomirror repair` runs it
now for a mirror that was damaged before the guard existed.

**An edge that does not resolve is the same bug one question further on.**
`repair_incomplete` asks whether a relationship *key* is present; `repair_dangling`
asks whether the ids under it name records the mirror can actually serve. Two of
these were live at once in one divergence report: a new parent whose `emails`
named an address the mirror held no row for, and a person still listing three
households PCO had deleted. Both are documents no caller can act on, and both are
invisible to everything above — the sweep needs an `updated_at` that will never
move again, and the drift probe counts rows that are all present. The repair
re-reads **both ends**: the holder, which is what drops an edge PCO has dropped,
and the target, which either arrives or answers `404` and is tombstoned. Same
schedule and same recency floor as `repair_incomplete`.

**Include-synthesized relationships are not stored, and are regenerated exactly.**
PCO answers `include=households.people` by adding a `people` relationship to the
*Person*. It is an artefact of the request rather than part of the record, so the
writer strips it before storing (on a copy — on a pass-through that same object is
the caller's response, and they are entitled to PCO's answer verbatim) and the
serving layer regenerates it whenever a nested include asks for it. Three rules,
each measured rather than assumed:

  * **An empty second level is still a relationship.** A household's memberships
    read with `include=person,person.emails` give *every* membership an `emails`
    key, `"data": []` for members with no address. Emitting it only when there
    were ids to put in it dropped the relationship for exactly the people a newly
    added family is made of.
  * **An empty first level is not.** A person with no field data gets no
    `field_definition` key at all — PCO synthesizes one key per first-level record
    it resolved.
  * **It is a concatenation, not a set.** PCO joins each first-level record's
    array end to end, so a person in two households that share a member is handed
    that member's id twice, in household order.

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

#### 8.3.1 The other Planning Center products

A path outside `/people/v2` — `/check-ins/v2/…`, `/groups/v2/…`, `/services/v2/…` —
is served the same way, by pass-through. The drop-in promise is a base-URL swap,
and an app that reads People rarely reads only People; refusing everything else
would have made the swap a code change for anyone reading a second product.

Three constraints, all of which the People path does not need and this one does:

* **`GET` only.** A write to an unmirrored product is a `404` — it would be the
  mirror spending its credential on a mutation it keeps no record of.
* **Addressed from the API root.** `pco_base_url` ends in `/people/v2`, so a
  foreign path relayed against it would ask PCO for `/people/v2/check-ins/v2/…`.
* **No version pin.** `api_version` is a dated revision *of People*. A version
  string is only valid for its own product, so the header is omitted and PCO
  answers at the organization's default.

And the one that is a correctness constraint rather than a plumbing one:
**nothing from a foreign product is written to the mirror.** The registry routes
a payload to a table by its JSON:API `type`, and `type` is not unique across
products — a Check-Ins `Person` is a different record, in a different id space,
from a People `Person`. Read-through on such a payload would overwrite a mirrored
person with a stranger sharing an id, and because the monotonic guard compares
`updated_at`, a newer foreign record would be *accepted*. So the read-through
above is conditioned on the path being a People one; foreign responses are
relayed and forgotten. Their `links` are still rewritten onto the mirror, since a
caller holding a pcomirror key cannot follow an absolute PCO URL.

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
  #    Sent ONCE. A mutation is never replayed: see "Indeterminate writes" below.
  try:
      resp = pco.request(req.method, pco_path, body=req.body, auth=PCO_PAT)
  except (Timeout, ConnError):                      # answer lost — MAY have applied
      return 504, mirror_error("upstream_response_lost",   # nothing written locally
                               write_indeterminate=True, safe_to_retry=False)

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
- **Read-your-writes covers what the write *affected*, not only what it
  returned.** `route_page` applies the resource PCO sent back, and for a nested
  write that is not the only record that moved. `POST /households/{h}/household_memberships`
  returns a membership; the household it joined is now wrong in two ways the
  response says nothing about, and neither self-repairs on read — its
  `relationships.people` array (which `include=households.people` is served
  from) still lists the old members, and its membership collection is a
  `nested_walk` whose ledger already says it was walked, so a read will not
  re-fetch it. So the walk is redone synchronously as part of the write, and the
  parent record is queued for hydration. Failing to re-walk leaves the rows
  already held rather than failing the write: stale beats absent, and the sweep
  converges. (The walk is *redone* rather than the ledger *dropped* — dropping it
  would turn every household read into a `503` for as long as PCO was
  unreachable, trading a staleness bug for an availability one.)
- **A top-level write can move records too.** `POST /households` builds a whole
  family in one call: the members are named in the request's
  `relationships.people`, and PCO stores that edge on each of them as well —
  `person.relationships.households` is a second copy, not a view. The response
  describes only the household, so the people it moved are re-read before the
  response, capped, with any tail queued. Only the edges the *request* names are
  followed: PCO's reply echoes a record's whole relationship set, and chasing
  those would make an ordinary `PATCH` pay for edges it never touched. Two gaps
  are left to the sweep on purpose — a member a `PATCH` *removes* is named by
  neither the request nor the mirror by then, and a `DELETE` names nobody.
- **Read-your-writes**, *best-effort*. A `POST` inserts the new `pco_id`
  immediately, so the very next local read (even before the webhook lands) sees
  the change. If that local insert itself fails, the request still succeeds —
  PCO has already accepted the write, and failing the response would invite a
  retry that creates a second record. The failure is logged and the mirror
  converges on the next reconcile or webhook. Read-your-writes is the weaker
  promise of the two, and it is the one that yields.

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
  caller gets PCO's status (or a `504`); the mirror never diverges from PCO.

**Indeterminate writes — a mutation is sent exactly once.**

"Failure = safe" is a statement about the *mirror*, not about PCO. There is one
case where the mirror stays clean and PCO does not: a write that arrived, was
applied, and whose response was lost on the way back. A dropped socket, a read
timeout, and a `502`/`504` from whatever sits in front of PCO all look identical
from here — and in every one of them the record may already exist.

PCO has no idempotency key to send, so that ambiguity cannot be resolved by
retrying; it can only be *duplicated* by retrying. Therefore:

- **`GET` is retried; `POST`/`PATCH`/`DELETE` are not.** Neither on a transport
  error nor on a `5xx`, which is the same rule stated in two places that must
  agree. The one exception is `429`: a limiter refuses *before* the request
  reaches anything that could apply it, so a replay cannot create a second
  record whatever the verb.
- **The caller is told, in terms it can act on.** `504` with
  `meta.write_indeterminate: true` and `meta.safe_to_retry: false`. This is the
  one failure a status code alone describes wrongly — every `5xx` reads as "try
  again" to a well-behaved client, and here that is the wrong move. The
  resolution is to read the record back, not to send the write again.

*Why this is spelled out at this length:* it was not, and both halves regressed
at once. The status path retried writes the transport path was written to
protect, and the lost-response case fell through to a bare `500` — which told an
otherwise-correct caller to retry, five times, creating five copies of one
parent on a real family's record before reporting that PCO could not be reached
at all.

**Two strictly separated credential planes:** local `api_key` (hashed, scoped —
`read:*` / `passthrough` / `write` — with a per-key local rate + pass-through
quota) authenticates apps to pcomirror; the upstream **PCO PAT** lives only
server-side and is never exposed to or selectable by callers.

### 8.5 Cross-origin access (CORS)

**The base-URL swap has one caller it cannot serve unaided: a browser.** A page
served from anywhere else has its `fetch` of `/people/v2/…` refused *before it is
sent*, whatever credential it holds, until this service states which origins may
read its answers. That statement is the deployment's, not the design's, so it is
**configuration** — `PCOMIRROR_CORS_ORIGINS` for the default, `/admin/cors` for
the override that wins — and it is **off by default**: a mirror of a church's
people database has no default set of websites that may read it, and a permissive
one is not a default so much as a decision made on somebody else's behalf.

Off is **silent**: no `Access-Control-*` header on any response, and `OPTIONS`
left as the `405` it already was. That is what a browser needs in order to
conclude the service is not for it.

| Decision | Why it is this way |
| --- | --- |
| **Preflight is answered before authentication** | Browsers strip `Authorization` from the `OPTIONS` probe. A preflight through `_authenticate` would `401`, and the browser would then report the *actual* request as an opaque CORS failure with that 401 nowhere in sight. The probe reaches no data: it answers with the allowed methods and headers, nothing else. |
| **Every response carries the headers, failures included** | A `401`/`403`/`400` without `Access-Control-Allow-Origin` cannot be read by the page that caused it, so the developer sees "CORS error" where the server said *key lacks the `write` scope*. |
| **A refusal is a `200` with the permission missing** | A browser only reads a `2xx` preflight; its own message ("Method `DELETE` is not allowed by `Access-Control-Allow-Methods`") is the most precise sentence in the exchange, and any non-2xx replaces it with "does not have HTTP ok status", which names nothing. `X-Mirror-Cors` carries the reason in words for whoever is holding `curl`, naming the variable to change. |
| **`Vary: Origin` whenever the answer is origin-dependent** | Including on responses to refused origins. Without it a shared cache in front of the service hands one origin's response — headers and all — to a page from another. `*` alone does not vary; with credentials it does, because the echo becomes concrete. |
| **The operator console and the webhook receiver are excluded, permanently** | `/` and `/admin/**` authenticate with a `SameSite=Strict` session cookie and run no JavaScript, so cross-origin access has no legitimate caller and one obvious illegitimate one; the receiver authenticates a delivery from Planning Center, which is not a browser. Not settings — §8.5 does not reach either plane. |
| **A malformed origin can never be echoed** | The `Origin` is attacker-chosen text that ends in a response header. Only a value that parses as `scheme://host[:port]` may be *allowed*, and every reason string is stripped to printable ASCII — otherwise `Origin: https://a\r\nSet-Cookie: …` is response splitting rather than a mismatch. |
| **What `Access-Control-Allow-Headers` says is the whole answer** | The browser compares its `Access-Control-Request-Headers` against that value and nothing else, so server-side leniency the value does not mention is leniency the deciding party never hears: the preflight is refused *before the request*, while `X-Mirror-Cors` says the header was fine. Anything `allows_header` will not refuse is therefore named in the advertised list. |
| **The caching headers an HTTP library adds are always allowed** | `Cache-Control`, `Pragma`, `Expires`, `If-None-Match`, `If-Modified-Since` — none safelisted by the browser, so all preflighted; none carrying authority a read could act on. `axios`'s cache interceptor sends the first three on *every* request (its default `cacheTakeover`) and the conditional pair on every revalidation, so a policy listing only `Authorization,Content-Type` — the default, and what the form offers — refused every request such a page made. `PCOMIRROR_CORS_HEADERS` is about the headers an *app* chooses; an operator naming those is not thinking about `Pragma`. Same rule, same reason, as the browser's own safelist. |
| **A reason is a header value, and a header value is latin-1** | PEP 3333 sends headers as latin-1, and a character outside it does not arrive mangled: it raises inside the WSGI server *midway through the header block*, so the response keeps the headers already written and loses the rest. The em dash these reasons were first written with therefore cost each refusal its `Access-Control-Allow-Origin` — the browser reported "no `Access-Control-Allow-Origin` header is present" for what was actually one disallowed request header, and the sentence explaining that never arrived. Reasons go out through `_explain`, and `_respond` makes the guarantee for every header including relayed ones: a `?` in a value is a response, a half-written header block is not. |
| **Malformed configuration is refused where it was set** | Same rule as `PCOMIRROR_SUBSCRIPTIONS` (§6.1): `https://app.church.org/`, with the slash the address bar leaves behind, would otherwise match nothing and be diagnosed only from inside a browser somebody else is holding. From the environment that means startup fails; from the page it means the form comes back with the reason and everything else still typed in. `*` beside a named origin is a contradiction; `*` with credentials is a combination browsers reject, so it is refused rather than emitted. |
| **The page wins over the environment** | Same shape as the subscription list (§6.1) and the divergence rate (§10), and for the same reason: whoever can reach the console when a browser app stops working is rarely whoever can edit the container's environment and restart it, and re-applying the environment on the next start would silently undo the fix at the hour nobody is watching. Stored in `mirror_meta`, read per request so a save applies to the next one; *hand back* clears it. **`build` is the single validator both go through**, so the two cannot come to mean different things by the same words — the same reason `divergence/rules.py` was lifted out of the golden test. A stored policy that cannot be re-validated falls back to the environment and says so, rather than serving a policy nobody can read. |
| **Saving nothing ≠ handing it back** | An empty origin list saved from the page is an override that is *off*; handing back restores `PCOMIRROR_CORS_*`. Conflating them would mean an operator who turned cross-origin access off got the environment's origins again on the next restart. |

**What CORS is not.** It is a rule enforced by browsers, not a boundary: `curl`
ignores it, so the API-key plane (§8.4) remains the only thing standing between a
caller and the data. And a key shipped to browser JavaScript is readable by
anyone who opens the page — which makes a narrowly scoped key
(`read:people,read:emails`) the right credential for a browser app, and `write` /
`passthrough` the wrong ones.

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
| 21 | "A unique receiver URL per subscription" read PCO's *model* (one subscription per event) as a *constraint* on URLs, which it is not — its own console points every ticked event at one URL. `url_token UNIQUE` made the normal setup impossible to register | `url_token` not unique; the receiver resolves the delivering subscription by **the secret that signed the body**, never by the event name in the payload (§6.1–6.2) |
| 22 | `PCOMIRROR_SUBSCRIPTIONS` re-applied on every start would silently overwrite a webhook fixed from the operator page, at the moment nobody is watching | The page takes precedence once used (`mirror_meta.subscriptions_managed_here`); the environment is reported-and-skipped, and handed back explicitly (§6.1) |
| 23 | An event for a resource with no table dead-lettered, so subscribing to the whole console list filled the queue an alert points at with events that were only ever going to be filed | Captured and marked `ignored`, payload intact, counted on the page; dead letters keep meaning "something broke" (§6.1) |
| 24 | A secret was mandatory, which shut out senders that cannot sign — and `webhook_delivery.signature NOT NULL` would have turned the unsigned delivery into a 503 and an endless redelivery loop rather than a stored one | A blank secret means no check (§6.2.1); the absent header stores as `''` |
| 25 | Every secretless receiver was reported as a hole, including ones whose minted 192-bit URL *is* a bearer credential — an alarm that fires on the ordinary case is one an operator learns to scroll past, and it would then be scrolled past for the receiver that mattered | Report the combination that authenticates nothing: no secret **and** no credential-grade token, defined once in `unprotected_tokens` (§6.2.1) |
| 26 | The credential-grade bar was measured against `token_hex(16)`, which draws 32 characters from 16 symbols — roughly 1 in 10⁴ minted tokens scored under it, so the mirror would mint a token and then call it guessable on its own page | `mint_token` is `token_urlsafe(24)`: same length, 64 symbols, worst of 300k draws 131 bits against a bar of 100 (§6.2.1). Caught by a test that mints thousands rather than one |

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

*Built so far*, in tier order: `person`, `email`, `phone_number`, `address`,
`social_profile`, `field_datum`, `note`, `household`, `list`, `form` (FULL);
`field_definition`, `household_membership`, `list_result`, `form_submission`,
`campus`, `marital_status`, `name_prefix`, `name_suffix`, `inactive_reason`
(LITE); `person_merger` (derived side-effect). That set is exactly the resources
the People webhook console emits events for, so every offered event has a table
to land in. The rest of the tiers above remain pass-through, and an event for one
of them is captured and marked `ignored` rather than dead-lettered (§6.1).

## Appendix B — Per-resource sync policy (seed)

| resource | endpoint | method | uat filter | incr | audit | pri | include |
|---|---|---|---|---|---|---|---|
| person | `/people` | incremental + audit + merger | yes | 60 s | weekly | P1 | emails,phone_numbers,addresses,field_data,households |
| email | `/emails` | incremental + audit | yes | 120 s | daily (+ cascade + include-diff) | P1 | — |
| phone_number | `/phone_numbers` | incremental + audit | yes | 120 s | daily (+ cascade + include-diff) | P1 | — |
| address | `/addresses` | incremental (descending-walk) + audit | **no** | 300 s | daily (+ cascade + include-diff) | P2 | — |
| field_datum | `/field_data` | incremental + audit | yes | 300 s | daily (+ cascade + include-diff) | P2 | field_definition |
| household | `/households` | incremental | yes | 600 s | monthly | P2 | — |
| household_membership | (via household, per parent) | nested walk, list-and-replace per parent | n/a (untimed) | 86400 s + filled on read for an unwalked parent | — | P3 | — |
| note | `/notes` | incremental | yes | 600 s | monthly | P2 | note_category |
| social_profile | `/social_profiles` | incremental + audit | yes | 600 s | daily (+ cascade + include-diff) | P3 | — |
| list | `/lists` | incremental | yes | 900 s | — | P3 | — |
| list_result | (via list, per parent) | nested walk, list-and-replace per parent — `GET /list_results` does not exist | **no** | 86400 s + filled on read for an unwalked parent, and on `list.refreshed` | — | P3 | — |
| form | `/forms` | incremental (descending-walk) — no `where[updated_at]` on `/forms` | **no** | 3600 s | — | P3 | — |
| form_submission | (via form, per parent) | nested walk, list-and-replace per parent | **no** | 86400 s + filled on read for an unwalked parent | — | P3 | — |
| background_check | `/background_checks` | incremental | yes | 900 s | monthly | P3 | — |
| person_merger | `/person_mergers` | merger_poll (created_at) | n/a | 120 s | n/a | P1 | — |
| reference/config (campus, field_definition, tab, list, marital_status, …) | `/…` | reference_periodic | mixed | 6–24 h | — | P3 | small |
| stats, birthday_people, report, people_import, … | — | passthrough_only | — | — | — | — | — |
