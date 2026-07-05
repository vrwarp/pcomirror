# pcomirror

A **layer service that mirrors Planning Center (PCO) People data** into a local
PostgreSQL store, so local applications can query it **without hitting the live
API** — with an explicit **pass-through** to PCO when they need live data.

The mirror stays fresh through **webhooks** (near-real-time fast path) plus a
**background reconciliation** refresh (the safety net that repairs anything a
missed, late, or lost webhook left stale). It respects PCO's rate limits at all
times and is designed to be correct under PCO's at-least-once, unordered webhook
delivery.

## Why

Local apps that read PCO People data repeatedly (directories, reporting,
integrations) otherwise burn the shared **100 requests / 20 s** rate budget and
couple their latency/availability to PCO. pcomirror turns that into fast local
Postgres queries, keeps a faithful copy up to date, and only reaches out to PCO
when data isn't mirrorable (stats, reports, live search) or a caller explicitly
demands live freshness.

## How it works (one paragraph)

Every PCO resource is stored **verbatim as JSONB** (the system of record) with
**generated columns** projected out for fast, indexed queries. A single
canonical, monotonic **upsert** makes all writes idempotent and correct under
duplicate/out-of-order delivery. An **initial backfill** keyset-walks
`updated_at` (never deep-offset paging, to dodge the >30k-offset penalty) and
sideloads children via `include=` to collapse N+1 into ~1 request per 100 people.
**Webhooks** apply changes in seconds; **reconciliation** sweeps `where[updated_at]`
watermarks, tails `/person_mergers`, and runs a periodic id-audit to catch hard
deletes and merges that `updated_at` filtering can't see. Everything that calls
PCO shares **one per-org rate limiter**.

## Documentation

| Document | What's in it |
|---|---|
| [`DESIGN.md`](DESIGN.md) | The full design: ground-truth API constraints, architecture, the canonical write path, storage model, ingestion/rate-limiting, webhooks, reconciliation, serving/pass-through, auth/tenancy/ops, the resolved-review ledger, and open questions. |
| [`docs/schema.sql`](docs/schema.sql) | The canonical PostgreSQL schema — table contract, generated projections, and the four canonical writer functions (`mirror_upsert`, `mirror_upsert_untimed`, `mirror_tombstone`, `mirror_confirm_live`). |
| [`docs/schema_test.sql`](docs/schema_test.sql) | An assertion harness that verifies the writer semantics (monotonic guard, same-second correction, sticky/merge tombstones, authoritative resurrection, polymorphic `field_datum`, untimed-tombstone terminality). |

The schema and writer semantics are runnable and verified on PostgreSQL 16:

```sh
createdb pcomirror
psql -d pcomirror -f docs/schema.sql
psql -d pcomirror -v ON_ERROR_STOP=1 -f docs/schema_test.sql   # all checks PASS
```

## Status

Design phase. `DESIGN.md` was produced by fanning the work across six subsystem
designs and then adversarially reviewing the combined design for sync
correctness, rate-limit/scale math, and data-model/security; the result is
reconciled into the canonical decisions in `DESIGN.md` §3 and §10. See
[`DESIGN.md` §11](DESIGN.md#11-open-questions-for-you) for the decisions that
need your input before implementation.

## Target stack

- **PostgreSQL 15+** (generated columns, row-level security) — the store.
- **Redis** — the shared per-org rate limiter (GCRA) and optional job queue
  (Postgres `SKIP LOCKED` + `LISTEN/NOTIFY` is a fine single-datastore
  alternative at small scale).
- Service language is intentionally open — the design is stack-agnostic.
