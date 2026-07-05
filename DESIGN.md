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
> and [§10](#10-canonical-decisions--resolved-review-ledger). Concrete DDL and
> the four canonical writer functions live in [`docs/schema.sql`](docs/schema.sql).

---

## Contents

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
11. [Open questions for you](#11-open-questions-for-you)
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

```
                         PCO People API  (people/v2)        PCO Webhooks (/webhooks/v2)
                              ▲     ▲                                │  signed deliveries
              GET (keyset,    │     │  GET (hydrate,                 ▼
              includes)       │     │  audit, pass-through)   ┌──────────────┐
                              │     │                         │ webhook-     │ verify HMAC(raw)
   ┌───────────┐   ┌──────────┴─────┴────────┐               │ receiver     │ → durable insert
   │ scheduler │──▶│  fetch-workers          │◀──────────────│ (ack < 500ms)│ → 2xx fast
   │ (leader)  │   │  backfill / reconcile / │  hydration    └──────┬───────┘
   └───────────┘   │  hydrate / pass-through │  queue               │ enqueue
        │          └───────────┬─────────────┘                      ▼
        │ enqueues             │  ALL calls go through        webhook_event (per-event inbox)
        │ sweeps               ▼                                     │ async worker
        │            ┌───────────────────────┐                      │
        └───────────▶│  SHARED RATE LIMITER   │  Redis GCRA          │
                     │  keyed by org_id       │  (per-token budget)  │
                     └───────────────────────┘                      │
                              │ every writer calls the ONE canonical writer (§3)
                              ▼                                      ▼
                     ┌──────────────────────────────────────────────────────┐
                     │   PostgreSQL  (raw JSONB system-of-record +           │
                     │   generated projections + mirror_sync_state)         │
                     └───────────────────────┬──────────────────────────────┘
                                             │ RLS-scoped reads
                                             ▼
                                   ┌────────────────────┐   passthrough (opt-in,
        local apps ──JSON:API────▶ │  api-server        │──▶ through shared limiter,
        (host + key swap)          │  (pcomirror-serve) │    read-through upsert)
                                   └────────────────────┘
```

Five stateless component classes over Postgres (+ Redis): **webhook-receiver**,
**fetch-workers**, **scheduler** (leader-elected singleton), **api-server**, and
the **stores**. Everything that calls PCO — backfill, reconcile, webhook
hydration, and pass-through — funnels through **one shared per-org rate limiter**
and writes through **one canonical writer**. Those two "one" statements are what
make the system correct and rate-safe; the rest is detail.

---

## 3. The canonical write path

> This is the heart of the design and the single most important section. Every
> ingestion path — backfill, reconcile, webhook, pass-through read-through —
> mutates the mirror **only** through the four functions in
> [`docs/schema.sql`](docs/schema.sql). The adversarial review found the original
> per-subsystem drafts specified the writer *four incompatible ways*; the rules
> below are the reconciled, single specification.

### 3.1 Storage invariant

`raw jsonb` is the **system of record** — the resource stored verbatim. Every
queryable column is a **`GENERATED ALWAYS AS (…) STORED`** projection of `raw`
(including `pco_created_at` / `pco_updated_at`, via the `IMMUTABLE` `pco_ts()` /
`pco_date()` parsers). Consequences:

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

> The four functions and these exact semantics are implemented in
> [`docs/schema.sql`](docs/schema.sql) and verified by
> [`docs/schema_test.sql`](docs/schema_test.sql) (11 assertions, all passing on
> PostgreSQL 16) — monotonic guard, same-second `≥` correction, sticky/merge
> tombstones, authoritative resurrection, polymorphic `field_datum`, and
> untimed-tombstone terminality.

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

Full DDL in [`docs/schema.sql`](docs/schema.sql). Highlights:

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

`PRIMARY KEY (org_id, pco_id)`; `(org_id, pco_updated_at)` for keyset/sweep on
every FULL table; live-row **partial** indexes (`WHERE deleted_at IS NULL`);
`GIN (raw jsonb_path_ops)` for ad-hoc containment; denormalized-FK btrees on join
keys; projected-column indexes mirroring `can_query_by`/`can_order_by`; `pg_trgm`
on `search_name` for local fuzzy search. Don't over-index tiny LITE tables.

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

**One limiter per org token, shared by all consumers.** Because PCO enforces
limits per token and the standard deployment runs multiple processes
(autoscaled fetch-workers **plus** the api-server doing pass-through), the
sanctioned limiter is a **Redis GCRA keyed `ratelimit:{org_id}`**, executed as an
atomic Lua script. *(An in-process token bucket is dev/single-process only — with
N processes each enforcing its own 5 req/s you get N×5 and a 429 storm; a startup
assertion checks all PCO-calling processes point at the same key.)*

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
`(org_id, resource_type, pco_id)` so a burst of child events for one person folds
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
rows → force a sweep. *(Contract: the mirror's `WHERE` must count exactly the
population `total_count` reflects — e.g. does `/people` count inactive people by
default? One empirical check against the target org, see §11.)*

---

## 8. Serving API & live pass-through

`pcomirror-serve` is a stateless HTTP service in front of Postgres whose prime
directive is to be a **drop-in for `…/people/v2` read paths** — an existing PCO
client works after only a base-URL + credential swap. The mirror *is* the cache;
there's no second cache to invalidate for mirrored types.

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

**Cannot be replicated locally → pass-through or degrade:** `search_*` virtual
fields, dynamic **List rule evaluation** (`list_result` is PCO's last materialized
membership; we serve last-known, live eval → pass-through), permission-derived
`filter=admins`, and all aggregates/reports/`/me`.

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

### 8.4 Writes & auth

**Read-only by default** (`405` on writes; PCO is the system of record — local
writes would split-brain). Optional opt-in **write-through** proxies to PCO then
applies the returned resource via `mirror_upsert` (the follow-up webhook dedups
idempotently). **Two strictly separated credential planes:** local `api_key`
(hashed, scoped, per-key local rate + pass-through quota, `org_id`-stamped on
every query) vs. server-only PCO creds never exposed to callers.

---

## 9. Auth, versioning, multi-tenancy & operations

### 9.1 Auth to PCO

Behind one `resolve_auth(org_id)` seam. **PAT (HTTP Basic) is the single-org
default** — zero refresh machinery. **OAuth 2.0 + PKCE** (scope `people`) is the
multi-org path: a **leader-elected, single-flight** refresh loop (per-org advisory
lock, refresh at T-10 min, persist the **rotated** refresh token in the success
transaction — two concurrent refreshes race and one loses its token). Lapse
(`invalid_grant`, >90 d idle) → `status='reauth_required'`, **writes stop, reads
never stop**, page on-call; a human re-runs consent, then a catch-up reconcile.
All secrets (PAT, client secret, tokens, webhook `authenticity_secret`) are
**envelope-encrypted** (AES-256-GCM under a KMS-wrapped per-org DEK), versioned in
`org_secret` for zero-downtime rotation — **not** pgcrypto.

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

**webhook-receiver** (autoscaled, public TLS), **fetch-workers** (scale on queue
depth, throughput-capped by per-org limit), **scheduler** (leader singleton:
sweeps, OAuth refresh, rotation, drift), **api-server** (RLS-scoped serving +
pass-through), **Postgres** (primary + read replicas) + **Redis** (limiter GCRA +
optional job queue; at small N, Postgres `SKIP LOCKED` + `LISTEN/NOTIFY` avoids a
second datastore). Only the receiver and api-server are internet-facing.

---

## 10. Canonical decisions & resolved review ledger

The six subsystems were designed in parallel, then adversarially reviewed. The
review's central finding was **cross-section divergence** — the "shared" writer,
state table, inbox, and limiter were each specified several incompatible ways.
This section is the reconciliation; each row is a settled decision baked into
[`docs/schema.sql`](docs/schema.sql) and the sections above.

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

## 11. Open questions for you

A few decisions genuinely need your input or an empirical check before build:

1. **Tenancy scope.** The design defaults to **single-org (PAT)** with an
   OAuth/multi-org path fully seamed in (every table already has `org_id`). If
   this mirror will ever serve multiple churches, confirm now so we finalize
   OAuth onboarding and RLS wiring. *(Recommended: single-org PAT to start.)*
2. **Read-only vs write-through.** Recommended **read-only** (PCO is the system of
   record). Enable opt-in write-through only if local apps must mutate PCO data.
3. **`total_count` population parity.** Does `/people`'s `meta.total_count` include
   inactive people (and any token-visibility filter) by default? The drift probe's
   `WHERE` must match exactly or it false-alarms — one empirical check against the
   real org.
4. **Audit cadence.** Weekly full id-audit is the default; orgs with heavy
   hard-delete usage or a compliance-driven deletion-latency SLA may want nightly.
5. **`search_*`.** Pass-through only (faithful), or invest in a local
   `pg_trgm`/FTS approximation (flagged `X-Mirror-Approximate`, semantics differ)?
6. **Language/stack.** The design is stack-agnostic; the store is fixed
   (PostgreSQL 15+, optional Redis). Confirm the service language so the reference
   pseudocode becomes real code. *(No blocker — any async-capable stack fits.)*

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
| household_membership | (via household) | list-and-replace | n/a (untimed) | 600 s | — | P2 | — |
| note | `/notes` | incremental | yes | 600 s | monthly | P2 | note_category |
| social_profile | `/social_profiles` | incremental | yes | 600 s | — | P2 | — |
| background_check | `/background_checks` | incremental | yes | 900 s | monthly | P3 | — |
| person_merger | `/person_mergers` | merger_poll (created_at) | n/a | 120 s | n/a | P1 | — |
| reference/config (campus, field_definition, tab, list, marital_status, …) | `/…` | reference_periodic | mixed | 6–24 h | — | P3 | small |
| stats, birthday_people, report, people_import, … | — | passthrough_only | — | — | — | — | — |
