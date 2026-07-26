# pcomirror

[![CI](https://github.com/vrwarp/pcomirror/actions/workflows/ci.yml/badge.svg)](https://github.com/vrwarp/pcomirror/actions/workflows/ci.yml)

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
when data isn't mirrorable (stats, reports, live List membership) or a caller
explicitly demands live freshness.

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

## Implementation

A working, dependency-free implementation lives in [`pcomirror/`](pcomirror/)
(pure Python 3.11+ standard library — SQLite, `hmac`, `urllib`, `wsgiref`; no
`pip install`). It is a faithful build of the design: raw-JSON storage with
generated projections, the one canonical writer, the rate-limited PCO client, the
backfill/reconcile/merger/audit/drift ingestion, the webhook receiver + async
processing, and the JSON:API serving layer with write-through and pass-through.

```
pcomirror/
  registry.py   # the data-driven resource catalog (schema + behaviour, one source of truth)
  db.py         # SQLite schema generation (from the registry) + a thread-safe handle
  writer.py     # the 4 canonical writers (upsert / upsert_untimed / tombstone / confirm_live)
  ratelimit.py  # in-process, header-adaptive token bucket
  pcoclient.py  # PCO HTTP client (injectable transport), auth, version pin, 429 handling
  ingest.py     # backfill, incremental sweep, merger poll, delete audit, drift, hydration
  webhooks.py   # HMAC verify, per-event inbox, dispatch, thin->hydrate, merge handling
  serving.py    # WSGI JSON:API drop-in: read/include/where/search/order/paginate, write-through, pass-through
  scheduler.py  # one background loop: drain inbox + hydration, run due sweeps, poll mergers, drift
  app.py, cli.py
```

The running schema is generated from `registry.py` to the exact contract
documented in [`docs/schema.sqlite.sql`](docs/schema.sqlite.sql).

### Run it

```sh
export PCO_APP_ID=... PCO_SECRET=...          # your Personal Access Token
export PCOMIRROR_DB=pcomirror.db

python3 -m pcomirror init-db                  # create the SQLite schema
python3 -m pcomirror backfill                 # initial full load (once)
python3 -m pcomirror reconcile --audit        # sweep + merger poll + id audit
python3 -m pcomirror add-subscription \        # register a webhook (secret from PCO)
    --subscription-id <id> --event people.v2.events.person.updated \
    --secret <authenticity_secret> --url-token person-updated-01
python3 -m pcomirror serve                    # JSON:API on :8080 + background scheduler
```

`--url-token` is optional but usually what you want: it fixes the receiver URL
(`<public-url>/pco/webhooks/<token>`) so you can register it at PCO *before* the
subscription exists there — otherwise you need PCO's `authenticity_secret` to run
this command, and PCO needs the URL this command prints. Omit it and a random
token is generated (or the existing one kept — re-running for the same
`--subscription-id` rotates the secret without changing the URL).

Local apps then point at `http://localhost:8080/people/v2/...` with only a
base-URL + credential swap. Writes (`POST`/`PATCH`/`DELETE`) proxy to PCO first
and fail if PCO fails (`DESIGN.md` §8.4).

### The query surface

The read grammar is PCO's, served from SQLite. Two parts of it are easy to get
subtly wrong, so they are worth stating outright.

**`where[search_*]` does not mean one thing.** All five of PCO's search filters are
served locally, and each arm matches by its own rule — measured against the live
API, not assumed (see [`tests/golden/README.md`](tests/golden/README.md)):

| Filter | Rule | Matches |
| --- | --- | --- |
| `where[search_name]` | anchored word-prefix | `name`, `first last`, `first_name`, `last_name`, `nickname`, `given_name` |
| `where[search_name_or_email]` | + substring | the above, or anywhere inside an email address |
| `where[search_phone_number]` | digits suffix | a phone number ending in the digits typed |
| `where[search_phone_number_e164]` | digits exact | the E.164 value, punctuation discounted |
| `where[search_name_or_email_or_phone_number]` | all three | every arm above |

Concretely, for *Ada Byron*: `ada`, `byron`, `ada by` and `ADA   BYRON` all find
her; `yron` and `byron ada` find nobody, because the match is anchored at the
start of a name field and the words must be in order. Phone numbers match on a
suffix, so the last four digits find a person and a leading area code does not —
`(555) 010-1` and `5550101` are the same number. `%` in a needle is a literal `%`.
A needle that cannot apply to a filter — a name typed into a phone-number search —
matches nobody; only a blank value filters nothing.

Ordinary `where[attr]=v` stays exact and case-insensitive (with `%` as a wildcard),
and values are coerced to the column's type, so `where[child]=true` matches the
`1` a boolean is stored as instead of silently matching nothing.

**Page links carry the whole query.** `links.next` is your request with `offset`
advanced — `where`, `order` and `include` included — and `meta.next.offset` says
the same thing for clients that read it there. Following the link walks every
matching row exactly once.

**Sparse fieldsets work.** `fields[Person]=first_name,last_name` limits attributes
*and* relationships for that type, `fields[Email]=address` applies to sideloaded
emails, and an unknown field name selects nothing rather than erroring — the same
JSON:API semantics PCO implements. `include=` still sideloads a relationship even
when the fieldset does not name it.

**A record has one shape, whatever the request.** PCO varies which relationships
it puts on a resource — a bare `/people` read carries only `primary_campus`, and
`emails` appears only when you `include` it. The mirror serves the fullest
representation it holds, so a plain read carries `emails`, `households`,
`addresses` and the rest as well. That is the same decision as the generated
`links` map below, and it is strictly additive: the mirror never omits a
relationship PCO would have sent, and never invents one PCO does not have.

**Ordering follows PCO's, not SQLite's.** Ids sort numerically, because they are
text columns holding numbers of different lengths and `/emails` carries both
8- and 9-digit ones. Text sorts case-insensitively, because PCO folds case and
SQLite's default collation does not. Both are the difference between a page that
matches PCO and a page that quietly contains different rows.

Nested collections (`/people/{id}/emails`, `/households/{id}/people`, …) take the
same `where`/`order`/`include`/`per_page` grammar as the top-level ones, served
from the mirror. `meta.can_query_by`, `can_order_by`, `can_search_by` and
`can_include` advertise exactly what each endpoint honours, and `meta.parent`,
`meta.next`/`prev` and `meta.can_filter` mirror PCO's own contract.

**Household membership is deliberately not mirrored.** `GET /household_memberships`
is a 404 — PCO exposes those rows only under `/households/{id}/household_memberships`,
one household at a time, and the payload there carries no `household`
relationship, so there is no bulk source and no incremental signal. Those two
endpoints therefore pass through. The `households` edge itself *is* local: PCO
returns a person's household identifiers inline on the Person, so
`include=households`, `/people/{id}/households` and `/households/{id}/people` all
answer from the mirror at no upstream cost.

**URLs in responses point back at the mirror.** A caller holds a pcomirror API
key, not a PCO PAT, so a response must never hand back a URL only PCO can serve.
Every `api.planningcenteronline.com/people/v2/...` URL — in `links`, in
`relationships.<rel>.links`, and in pass-through and write-through responses — is
rewritten to a mirror-relative path. Two are deliberately left absolute because
they are not API endpoints: `links.html` (PCO's web UI for a record) and the
avatar URLs (PCO's image CDN; the mirror doesn't proxy blobs).

The `links` map is **generated from the registry**, not echoed from PCO. PCO
returns a smaller map for a list page than for a single-resource fetch, so
echoing it made a record's shape depend on whether it arrived via backfill or
reconcile. The generated map is the same either way; `html` is the one entry
passed through, since it needs PCO's account-prefixed id and can't be derived.

PCO exposes relationships the mirror doesn't cover (`notes`, `workflow_cards`,
`background_checks`, …). Those aren't advertised in the generated map, but
requesting one resolves against PCO via pass-through rather than failing, so no
mirror path is a dead end. That spends the server's PCO credential, so the
caller's key needs the `passthrough` scope.

### Authentication

Two strictly separated credential planes (`DESIGN.md` §8.4):

| Plane | Direction | Mechanism |
| --- | --- | --- |
| **PCO PAT** | pcomirror → PCO | HTTP Basic `app_id:secret` from `PCO_APP_ID` / `PCO_SECRET`. Server-side only; never exposed to or selectable by callers. |
| **Webhook secret** | PCO → pcomirror | HMAC-SHA256 over the raw body, keyed on the subscription's `authenticity_secret`, found by the `url_token` in the path. |
| **API key** | your apps → pcomirror | `Authorization: Bearer pcm_…` — or HTTP Basic with the key as the username or the password, since that is what an existing PCO client already sends. Hashed at rest, scoped. |

`/people/v2/**` requires an API key. Mint one — the secret is printed once and
only its SHA-256 digest is stored, so it cannot be recovered later:

```sh
python3 -m pcomirror create-api-key --name dashboard --scopes 'read:*'
python3 -m pcomirror list-api-keys          # prefixes, scopes, last-used, state
python3 -m pcomirror revoke-api-key --prefix bb2d7fbb
```

```sh
curl -H 'Authorization: Bearer pcm_bb2d7fbb_7187…' \
     http://localhost:8080/people/v2/people

# Or, for a client built against PCO — which authenticates with HTTP Basic —
# put the key where the app id or the secret goes and change nothing else:
curl -u 'pcm_bb2d7fbb_7187…:unused' http://localhost:8080/people/v2/people
```

A PCO PAT is never a way in: Basic is accepted only when one of its two fields is
a `pcm_` key. The planes stay separate.

**Scopes** are comma-separated:

- `read:*` — read every mirrored collection; `read:people`, `read:emails`, … grant
  one endpoint each.
- `write` — `POST` / `PATCH` / `DELETE`, which write through to PCO.
- `passthrough` — let the caller spend the server's PCO credential on requests the
  mirror can't answer (an unmirrored type, or `?passthrough=1` on a mirror miss).
  Deliberately separate from `read:*`: reading the local mirror is free, calling
  PCO is not.

Missing or invalid key → `401` with a `WWW-Authenticate: Bearer` challenge; valid
key without the right scope → `403`. `/healthz` and `/readyz` stay public so the
container healthcheck works, and the webhook receiver stays public because it
authenticates with its own HMAC.

**It fails closed.** With no keys created, `/people/v2/**` returns `401` (saying so,
with the command to fix it) rather than serving data. `serve` prints the same
warning at startup. To keep the old open behaviour on a trusted LAN, set
`PCOMIRROR_ALLOW_ANONYMOUS=1` — `serve` then warns on every start that the service
must not be exposed publicly.

Not yet enforced: the `rate_limit_per_min` / `passthrough_quota_per_min` columns
on `api_key` are part of the §8.4 design but nothing reads them today.

### Admin page

The root path (`http://localhost:8080/`) serves an operator console: create and
revoke API keys, and read cache statistics. Server-rendered HTML, no JavaScript,
no external assets.

**First login** uses your `PCO_SECRET` as the password. That is not a security
claim — anything that can read the container's environment already holds the PAT,
so the PAT is the weakest link and a separate bootstrap secret would be theatre.
The first login is therefore forced through a password change, and once you set a
password `PCO_SECRET` stops working as a login. Passwords are stored as
PBKDF2-HMAC-SHA256 (600k iterations, per-password salt); minimum 12 characters.

If no password has been set *and* `PCO_SECRET` is empty, the page says so and
admits nobody, rather than accepting an empty password.

What the console shows:

- **Cache** — live and tombstoned row counts per resource, oldest sync timestamp,
  backfill and last-sweep times, consecutive errors, on-disk size (DB + WAL), and
  **drift**: mirror count minus PCO's reported total at the last probe. Non-zero
  drift means a sweep is due.
- **API keys** — prefix, name, scopes, last used; create with scope checkboxes
  (the secret is displayed exactly once), and revoke inline.
- **Webhooks** — registered subscriptions with their receiver tokens and last
  event, delivery and event counts by status, and dead-letter count.

Session hardening: `HttpOnly` + `SameSite=Strict` cookies (`Secure` too when the
request arrives over HTTPS, including via `X-Forwarded-Proto` from a reverse
proxy), 12-hour expiry, tokens stored only as a SHA-256 digest, CSRF tokens on
every state-changing form, a 5-attempt/60-second login lockout, and
`Content-Security-Policy: default-src 'none'` since the page runs no scripts.
Changing the password invalidates every existing session.

The console is deliberately outside the API-key plane: an API key is for machines
and cannot reach `/admin/**`, and an admin session cannot read `/people/v2/**`.

### Run it in Docker

The service ships as a small, dependency-free image (`python:3.13-slim`, no build
step). It runs as a non-root user (`PUID`/`PGID`-configurable), binds
`0.0.0.0:8080`, persists the SQLite file to a `/data` volume, handles `SIGTERM`
for clean `docker stop`, and has a built-in healthcheck on `/healthz`.

```sh
cp .env.example .env          # fill in PCO_APP_ID / PCO_SECRET / PCOMIRROR_PUBLIC_URL
docker compose up -d --build  # build + start (JSON:API + scheduler) on :8080

# First-time full load (once). Either set PCOMIRROR_BACKFILL_ON_START=1 in .env,
# or run it as a one-shot against the same volume:
docker compose run --rm pcomirror backfill

# Register a webhook subscription (authenticity_secret comes from PCO):
docker compose run --rm pcomirror add-subscription \
    --subscription-id <id> --event people.v2.events.person.updated --secret <authenticity_secret>

docker compose logs -f pcomirror
```

#### Plain Docker, fully configured in one command

Every setup step is env-driven, so a fresh instance needs no follow-up commands —
handy where running one-shots is awkward (Synology Container Manager, Portainer,
a `docker run` unit file):

```sh
docker volume create pcomirror-data

docker run -d --name pcomirror --restart unless-stopped \
  -p 8080:8080 \
  -v pcomirror-data:/data \
  -e PCO_APP_ID=your_pat_app_id \
  -e PCO_SECRET=your_pat_secret \
  -e PCO_USER_AGENT="pcomirror/0.1 (+admin@yourchurch.org)" \
  -e PCOMIRROR_PUBLIC_URL=https://pcomirror.yourchurch.org \
  -e PCOMIRROR_BACKFILL_ON_START=1 \
  -e PCOMIRROR_SUBSCRIPTIONS="sub_123:people.v2.events.person.updated:person-updated-01:whsec_aaa,sub_124:people.v2.events.person.created:person-created-01:whsec_bbb" \
  pcomirror:latest
```

That covers `init-db` + `add-subscription` + `backfill` + `serve` in one shot:
the schema is created on every start (`CREATE TABLE IF NOT EXISTS`, so it's a
no-op after the first), `PCOMIRROR_BACKFILL_ON_START=1` only loads resources that
have never completed a backfill (safe to leave set — restarts won't re-load), and
the scheduler starts with `serve`, the image's default CMD.

**`PCOMIRROR_SUBSCRIPTIONS`** is a comma-separated list of
`<subscription_id>:<event>:<url_token>:<authenticity_secret>`. It's re-applied on
every start and keyed on the subscription id, so it's idempotent: repeats update
the event and secret but never change a token already registered at PCO. Because
you choose the tokens, you know the receiver URLs (`https://…/pco/webhooks/person-updated-01`)
before the container has ever run — register those at PCO, paste the secrets it
gives you back into this variable, and the deploy is genuinely one-shot. Leave the
token field empty (`sub_123:people.v2.events.person.updated::whsec_aaa`) to keep
an existing token or mint a random one. A malformed value fails startup loudly
rather than leaving webhooks silently 404ing. If a secret contains a `,`, use the
JSON form instead:

```sh
-e PCOMIRROR_SUBSCRIPTIONS='[{"id":"sub_123","event":"people.v2.events.person.updated","token":"person-updated-01","secret":"whsec_aaa"}]'
```

**On a Synology specifically:** paste the variables into Container Manager's
*Environment* tab (or import `docker-compose.yml` as a Project). If you bind-mount
a share instead of using the named volume, set `PUID`/`PGID` to the host user that
owns it (see below) — otherwise SQLite fails with `unable to open database file`.
For TLS, point *Control Panel → Login Portal → Advanced → Reverse Proxy* at
`localhost:8080` and set `PCOMIRROR_PUBLIC_URL` to the public hostname.

#### `PUID` / `PGID`

A bind-mounted directory keeps its *host* ownership, which is why the image's
default uid can't write to it. Point the container at the owning user instead:

```sh
docker run -d --name pcomirror -p 8080:8080 \
  -v /volume1/docker/pcomirror:/data \
  -e PUID=1026 -e PGID=100 \
  ... vrwarp/pcomirror:latest
```

On a Synology, `id <your-user>` over SSH gives those numbers (`1026`/`100` is a
typical first admin user / `users` group). Find the owner of an existing folder
with `stat -c '%u %g' /volume1/docker/pcomirror`.

How it works: the container starts as root just long enough to `chown` the data
directory (and the `.db` / `-wal` / `-shm` files in it) to `PUID:PGID`, then
permanently drops to that user before exec'ing the app — the service itself never
runs as root. The chown is skipped when ownership already matches, so restarts are
cheap. Defaults are `10001:10001`, identical to the image's previous fixed user, so
leaving both unset changes nothing.

Starting the container with `--user` (or compose's `user:`) still works and takes
precedence: the entrypoint sees it is already non-root, warns that `PUID`/`PGID`
can't be applied, and runs the command unchanged. In that mode the host directory
must already be writable by the uid you chose.

If the data directory still isn't writable, the entrypoint now says so directly
and exits, rather than surfacing a SQLite traceback:

```
[entrypoint] /data is not writable by uid 1000:1000 (owned by 0:0, mode 755).
[entrypoint] If it is a bind mount, either set PUID/PGID to the host user that owns it, ...
```

**Container specifics**

- **Persistence:** the DB lives at `/data/pcomirror.db` on the `pcomirror-data`
  volume — the only state to back up (`docker run --rm -v pcomirror-data:/data ...`
  or copy the file). Everything else is disposable.
- **Config is env-only** (see [`.env.example`](.env.example)): `PCO_APP_ID`,
  `PCO_SECRET`, `PCO_API_VERSION`, `PCO_USER_AGENT`, `PCOMIRROR_PUBLIC_URL`,
  `PCOMIRROR_BACKFILL_ON_START`, `PCOMIRROR_SUBSCRIPTIONS`, `PUID` / `PGID`,
  `PCOMIRROR_ALLOW_ANONYMOUS`, `PCO_CA_BUNDLE` (if PCO egress goes via a proxy),
  and the container-friendly defaults `PCOMIRROR_DB` / `PCOMIRROR_HOST` /
  `PCOMIRROR_PORT`.
- **API keys live in the DB**, so create one against the same volume:
  `docker exec pcomirror python -m pcomirror create-api-key --name <app>`.
  They are deliberately not settable from the environment — that would mean
  storing them in plaintext instead of hashed.
- **Webhooks need a public HTTPS URL.** PCO must reach this service, so put a
  reverse proxy / tunnel (Caddy, nginx, Cloudflare Tunnel) in front that
  terminates TLS and forwards to the container's `:8080`. Set `PCOMIRROR_PUBLIC_URL`
  to that URL — both `PCOMIRROR_SUBSCRIPTIONS` and `add-subscription` print the
  exact receiver URL to register at PCO.
- **One-shot commands** override the default `serve` CMD — with compose,
  `docker compose run --rm pcomirror reconcile --audit`; with plain Docker, pass
  the same env and volume and put the subcommand after the image name:
  ```sh
  docker run --rm --env-file .env -v pcomirror-data:/data pcomirror:latest reconcile --audit
  ```
  Or, against the already-running container (note the explicit `python -m
  pcomirror` — `docker exec` bypasses the image's ENTRYPOINT):
  ```sh
  docker exec pcomirror python -m pcomirror drift
  ```
  Doing this while `serve` is running is safe: the DB is WAL with a busy timeout.

### Test it

```sh
python3 run_tests.py     # 178 end-to-end tests + 11 writer-semantics assertions
```

The suite drives backfill, sideloading, incremental sweep, merger poll, delete
audit, include-diff child deletes, drift, webhook verify/dedup/dispatch/thin-
hydrate/merge, JSON:API reads (where/search/order/include/pagination), the
410-on-merge redirect, and write-through — including the **fail-if-PCO-fails**
guarantee — against an in-process fake PCO, so no network or live credentials are
needed. `tests/test_search.py` covers the query surface a real PCO client sends:
every `where[search_*]` filter, typed/boolean filters, nested collections with
includes, and a full walk of `links.next` asserting each row is visited exactly
once.

[`tests/golden/`](tests/golden/README.md) holds **40** request/response pairs captured
from the **live** Planning Center API and sanitized; `tests/test_golden.py`
replays them against the serving layer and asserts the mirror answers in PCO's
shape and PCO's order. That corpus is the only part of the suite that cannot be
wrong in the same direction as the code — every semantic rule above was corrected
because of it.

## Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every pull request,
on pushes to `main`, on `v*` tags, and on demand (**Actions → CI → Run workflow**):

| Job | What it does |
|---|---|
| `test` | `python -m compileall` + `python run_tests.py` on Python **3.11, 3.12, 3.13** (no `pip install` — the suite is stdlib-only). |
| `docker` | Builds the image and smoke-tests it: `init-db` as a one-shot command, then `serve --no-scheduler` polled until `/healthz` and `/readyz` answer 200. |
| `publish` | **Docker Hub push.** Runs only after `test` and `docker` pass, and only on a **push** to `main` or a `v*` tag. Never on a pull request (so a fork PR can't reach the registry credentials) and never on a manual dispatch — to re-publish by hand, re-run a `main` push run. Builds `linux/amd64` + `linux/arm64`. |

So `test` and `docker` gate every pull request; `publish` is push-only.

### Publishing to Docker Hub

Add two repository secrets (**Settings → Secrets and variables → Actions**):

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Your Docker Hub username. |
| `DOCKERHUB_TOKEN` | A Docker Hub **access token** with Read & Write scope (Docker Hub → Account Settings → Personal access tokens) — not your account password. |

The image is published as `<DOCKERHUB_USERNAME>/pcomirror`. To publish under a
different name (e.g. an organization), set the repository **variable**
`DOCKERHUB_IMAGE` to the full name. Tags pushed:

| Trigger | Tags |
|---|---|
| push to `main` | `latest`, `main`, `sha-<full-sha>` |
| tag `v1.2.3` | `1.2.3`, `1.2`, `1`, `sha-<full-sha>` |

So a release is just `git tag v1.2.3 && git push origin v1.2.3`. If the secrets
aren't configured the `publish` job fails with a message saying which are missing,
rather than skipping silently.

## Status

Design + reference implementation. `DESIGN.md` was produced by fanning the work
across six subsystem designs and then adversarially reviewing the combined design
for sync correctness, rate-limit/scale math, and data-model/security; the result
is reconciled into the canonical decisions in `DESIGN.md` §3 and §10, then tuned to
the confirmed deployment profile in `DESIGN.md` §0 and built out in `pcomirror/`.

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
