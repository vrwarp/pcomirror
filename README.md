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
| [`DESIGN.md`](DESIGN.md) | The full design. Start with **§0 Deployment profile** for the concrete build (single org · SQLite · write-through · PAT), then the canonical write path, storage model, ingestion/rate-limiting, webhooks, reconciliation, serving/pass-through, ops, the resolved-review ledger, and the scale-up path. |
| [`docs/schema.sqlite.sql`](docs/schema.sqlite.sql) | **The store** — the SQLite schema: table contract, generated projections, and the four canonical writer statements. |
| [`docs/schema_test_sqlite.py`](docs/schema_test_sqlite.py) | Assertion harness verifying the writer semantics on SQLite (monotonic guard, same-second correction, sticky/merge tombstones, authoritative resurrection, polymorphic `field_datum`, untimed-tombstone terminality). |
| [`docs/schema.sql`](docs/schema.sql) · [`docs/schema_test.sql`](docs/schema_test.sql) | The PostgreSQL equivalent (schema + writer functions + test) — the **scale-up** target for many churches / a large org (`DESIGN.md` §12). |

The schema and writer semantics are runnable and verified on **SQLite 3.45**:

```sh
python3 docs/schema_test_sqlite.py     # 11/11 assertions PASS
```

(And on **PostgreSQL 16** for the scale-up path: `psql -d pcomirror -f docs/schema.sql`
then `psql -d pcomirror -v ON_ERROR_STOP=1 -f docs/schema_test.sql`.)

## Status

Design phase. `DESIGN.md` was produced by fanning the work across six subsystem
designs and then adversarially reviewing the combined design for sync
correctness, rate-limit/scale math, and data-model/security; the result is
reconciled into the canonical decisions in `DESIGN.md` §3 and §10, then tuned to
the confirmed deployment profile in `DESIGN.md` §0.

## Deployment profile

Built for **one Planning Center organization** (a church of a few hundred people):

- **SQLite** (WAL mode) — one file, no DB server, no Redis; backup is a file copy.
- **Write-through** — local apps can create/update/delete; the mirror proxies to
  PCO, then applies the returned resource.
- **Personal Access Token** auth — no OAuth refresh loop.
- **One service process** with an in-process, header-adaptive rate limiter.
- Service language is open — any stack with an HTTP client, HMAC, and SQLite fits.

PostgreSQL + Redis + multi-tenancy remain the documented **scale-up** path
(`DESIGN.md` §12) if it ever grows to many churches or a large org — the data
model and writer are identical, so it's a migration, not a rewrite.
