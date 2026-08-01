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
watermarks, tails `/person_mergers`, and runs a periodic id-audit both ways — it
tombstones hard deletes and merges that `updated_at` filtering can't see, and
restores records PCO lists that the mirror has no row for, the gap the sweep's
watermark has already passed. Everything that calls PCO shares **one per-org
rate limiter**.

## Documentation

| Document | What's in it |
|---|---|
| [`DESIGN.md`](DESIGN.md) | The full design. Start with **§0 Deployment profile** for the concrete build (single org · SQLite · write-through · PAT), then the canonical write path, storage model, ingestion/rate-limiting, webhooks, reconciliation, serving/pass-through, ops, the resolved-review ledger, and the scale-up path. |
| [`docs/schema.sqlite.sql`](docs/schema.sqlite.sql) | **The store** — the SQLite schema: table contract, generated projections, and the four canonical writer statements. |
| [`docs/schema_test_sqlite.py`](docs/schema_test_sqlite.py) | Assertion harness verifying the writer semantics on SQLite (monotonic guard, same-second correction, sticky/merge tombstones, authoritative resurrection, polymorphic `field_datum`, untimed-tombstone terminality). |
| [`docs/schema.sql`](docs/schema.sql) · [`docs/schema_test.sql`](docs/schema_test.sql) | The PostgreSQL equivalent (schema + writer functions + test) — the **scale-up** target for many churches / a large org (`DESIGN.md` §12). |
| [`docs/mutation-testing.md`](docs/mutation-testing.md) | Verifying `POST`/`PATCH`/`DELETE` against a **live** organization: the procedure, the guard that makes it survivable, the last run's results, and what PCO's validation does and does not catch. |

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
  ingest.py     # backfill, incremental sweep, per-parent walk, merger poll, delete audit, drift, hydration
  webhooks.py   # HMAC verify, per-event inbox, dispatch, thin->hydrate, merge handling
  webhooklog.py # every call to the receiver, recorded verbatim and downloadable
  serving.py    # WSGI JSON:API drop-in: read/include/where/search/order/paginate, write-through, pass-through
  cors.py       # which browser origins may read the API plane (off unless configured)
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
    --event people.v2.events.person.created \
    --secret <authenticity_secret> --url-token person-events-01
python3 -m pcomirror list-subscriptions       # what is registered, and where it delivers
python3 -m pcomirror serve                    # JSON:API on :8080 + background scheduler
```

`--url-token` is optional but usually what you want: it fixes the receiver URL
(`<public-url>/pco/webhooks/<token>`) so you can register it at PCO *before* the
subscription exists there — otherwise you need PCO's `authenticity_secret` to run
this command, and PCO needs the URL this command prints. Omit it and a random
token is generated (or the existing one kept — re-running for the same
`--subscription-id` rotates the secret without changing the URL).

`--event` may be repeated. Planning Center makes **one subscription per event
name** — a `WebhookSubscription` carries a single `name`, a single `url` and its
own `authenticity_secret` — but nothing requires those URLs to differ, which is
why PCO's own console lets you tick a column of events under one webhook. So one
receiver URL here carries as many event types as you point at it, and the
receiver works out which subscription a delivery came from by **the secret that
signed it**. Mixed secrets on one URL work for the same reason.

`--secret` is optional. Leave it off — and `--url-token` off with it — and the
receiver authenticates on its minted URL instead of on a signature, which is a
bearer credential rather than a hole. See
[Receivers with no secret](#receivers-with-no-secret-the-url-as-the-credential).

Or skip the command line: **the operator page at `/` manages subscriptions**
(`/admin/webhooks`) with the same event picker Planning Center shows, and takes
over from `PCOMIRROR_SUBSCRIPTIONS` once you save anything there — see
[Subscriptions from the page](#subscriptions-from-the-page).

Local apps then point at `http://localhost:8080/people/v2/...` with only a
base-URL + credential swap. Writes (`POST`/`PATCH`/`DELETE`) proxy to PCO first
and fail if PCO fails (`DESIGN.md` §8.4) — verified against a live organization,
including that a rejected write leaves the mirror completely untouched
([`docs/mutation-testing.md`](docs/mutation-testing.md)).

**The other Planning Center products come along.** An app that reads People
rarely reads *only* People — a directory wants `/check-ins/v2/…` for attendance
and `/groups/v2/…` for membership — and a base-URL swap points all of that here,
not just the People half. Those paths used to be a `404`; they now resolve
against PCO through the shared rate limiter, so the swap stays a configuration
change rather than a code change. Three things to know about them:

* **Read-only.** `GET` only; a write to an unmirrored product is a `404`. The
  mirror would be lending out its credential with no record of what was done
  with it.
* **Nothing is stored.** A payload from another product is relayed, never
  written. Resource `type` is not unique across products — a Check-Ins `Person`
  is a different record, in a different id space, from a People `Person` — so
  warming the mirror from one would overwrite a mirrored person with a stranger
  who happens to share an id.
* **They cost rate budget.** Every one is a live PCO request. They need the
  `passthrough` scope on the API key, exactly like any other pass-through, and
  they are not cached — only People is mirrored.

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

**Filters reach through relationships.** `where[emails][address]`,
`where[addresses][city]`, `where[field_data][field_definition][name]` and the rest
of PCO's documented nested filters are applied. PCO documents them and then
ignores them — the same request with a value that cannot match anything still
returns the whole collection — so this is one of the few places the mirror is
deliberately stricter than the API it mirrors.

**Nothing given is silently ignored.** An unknown filter or `order` key, a value
that cannot be coerced, an `include` naming a type the mirror does not hold: each
is a `400`. PCO answers `200` and quietly drops them, which is indistinguishable
from having applied them. The divergence is recorded and asserted in
[`tests/golden/`](tests/golden/README.md).

**A record has one shape, whatever the request.** PCO varies which relationships
it puts on a resource — a bare `/people` read carries only `primary_campus`, and
`emails` appears only when you `include` it. The mirror serves the fullest
representation it holds, so a plain read carries `emails`, `households`,
`addresses` and the rest as well. That is the same decision as the generated
`links` map below, and it is strictly additive: the mirror never omits a
relationship PCO would have sent, and never invents one PCO does not have.

**Ordering follows PCO's, not SQLite's.** Ids sort numerically, because they are
text columns holding numbers of different lengths and `/emails` carries both
8- and 9-digit ones. Text sorts under a **measured** fold — combining marks
stripped, then lowercased — because PCO folds accents as well as case and
SQLite does neither by default. `COLLATE NOCASE` fixes only half of that: it
folds ASCII `A`–`Z` and nothing else, so every accented surname sorts after `z`
and `Márquez` lands past all the `Mar…` names instead of among them. Walking a
real 1925-person organization, `NOCASE` disagreed with PCO in 34 positions and
the fold agreed in all 1925 ([the measurement](docs/mutation-testing.md#how-planning-center-orders-names-2026-07-29-read-only)).
Each of these is the difference between a page that matches PCO and a page that
quietly contains different rows.

Nested collections (`/people/{id}/emails`, `/households/{id}/people`, …) take the
same `where`/`order`/`include`/`per_page` grammar as the top-level ones, served
from the mirror. `meta.can_query_by`, `can_order_by`, `can_search_by` and
`can_include` advertise exactly what each endpoint honours, and `meta.parent`,
`meta.next`/`prev` and `meta.can_filter` mirror PCO's own contract.

**Household membership is mirrored by walking.** `GET /household_memberships` is a
404 — PCO exposes those rows only under `/households/{id}/household_memberships`,
one household at a time, and the payload carries no `household` relationship, so
the owning id is parsed out of `links.self`, the only place PCO puts it.

There is no `updated_at` on a membership, and joining a household does not
reliably move the household's own (measured: 6% of households hold a member
created after the household was last touched). So the refresh is a **periodic full
walk** rather than a watermark — the standard treatment for a slowly-changing
dimension, and the same one the reference tables get. One request per household:
at a few hundred households that is around three minutes daily, about a tenth of
a percent of the rate budget. Each household's answer is authoritative for that
household, so a membership PCO stops returning is tombstoned — without that a
walk could only ever add, and a parent leaving would never be noticed.

Worth the walk because `household_role` is the entire basis on which a caller
decides which adult in a household is the parent to telephone, and that lookup
sits on the path somebody waits on at a check-in door. With it mirrored, a client
reading parent contact needs **no `passthrough` scope and makes no upstream
request**: verified end to end by running Tally's own `getPersonDetails` against
the mirror and against PCO for 40 students — identical answers, zero requests to
PCO.

The `households` edge is local too: PCO returns a person's household identifiers
inline on the Person and the members inline on the Household, so
`include=households`, `include=households.people`, `/people/{id}/households` and
`/households/{id}/people` all answer from the mirror.

**Every event the People webhook console offers has somewhere to land.** The
console's list is `person`, `email`, `phone_number`, `address`, `field_datum`,
`field_definition`, `household`, `person_merger`, `note`, `list`, `list_result`
and `form_submission`; all twelve are mirrored resources with their own tables,
so an event for any of them is applied rather than filed.

The four that arrived with this coverage follow the patterns already here.
`note` and `social_profile` are person-owned children swept like `email`. `list`
is swept on its own `updated_at`; `form` has no `where[updated_at]` at all, so it
takes the descending walk `address` takes. `list_result` and `form_submission`
are served by PCO only under one parent at a time, so they are walked per parent
exactly as household memberships are — one request per list and per form, daily.

`people.v2.events.list.refreshed` is the one action that is not a record change:
it fires when a list is re-run, and the payload is the List itself, whose
attributes may be identical. It is handled by dropping that list's walk record,
so the next read re-fetches its results — otherwise a refresh would show up as
nothing but a `refreshed_at` that moved.

An event for a resource with **no** table here — a workflow card, say — is
captured, marked `ignored` in the inbox with its payload intact, and counted on
the admin page. Deliberately not dead-lettered: an event the mirror has no use
for is not a failure, and burying those among the ones that really did break is
how a dead-letter queue stops being read.

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

### Browser access (CORS)

A browser is the one caller that cannot take the base-URL swap up on its own.
`fetch('http://pcomirror.lan:8080/people/v2/people')` from a page served anywhere
else is refused *before it is sent*, whatever credential it holds, until this
service says which pages may read its answers. So name them — from the
environment:

```sh
PCOMIRROR_CORS_ORIGINS=https://directory.yourchurch.org,https://*.yourchurch.org
```

or from **`/admin/cors`**, which manages the same policy from the operator
console and **wins once you save anything there** — origins, methods, headers,
preflight lifetime and all. A save applies to the **next request**: no restart, so
a browser app that stops working at 9pm can be fixed by whoever is holding the
laptop rather than whoever can edit the container's environment. The same
*Hand back to the environment* button the webhooks page has reverses it. See
[Browser access from the page](#browser-access-from-the-page).

**Off unless one of the two names an origin, and off is silent** — no
`Access-Control-*` header anywhere and `OPTIONS` stays the `405` it already was,
which is exactly what a browser needs to see to conclude the service is not for
it. There is no permissive default, because no default could be both useful and
safe for a mirror of a church's people database.

| Variable | Default | What it says |
| --- | --- | --- |
| `PCOMIRROR_CORS_ORIGINS` | *(unset — off)* | The origins allowed. `*` for any; `https://*.church.org` for subdomains; `null` for `file://` pages and sandboxed iframes. |

| `PCOMIRROR_CORS_METHODS` | `GET,POST,PATCH,DELETE` | Which of them a page may use. `GET` alone for a read-only browser app. |
| `PCOMIRROR_CORS_HEADERS` | `Authorization,Content-Type` | Request headers a page may send — the API key, and a JSON:API body. Whatever is set, the caching headers an HTTP library adds for itself are allowed too; see below. |
| `PCOMIRROR_CORS_EXPOSE_HEADERS` | `X-Mirror-Source,Location` | Response headers a page may *read*: mirror-or-pass-through, and where a `201` put the new record. |
| `PCOMIRROR_CORS_MAX_AGE` | `600` | How long a browser may cache the preflight answer. |
| `PCOMIRROR_CORS_ALLOW_CREDENTIALS` | `0` | Let cross-origin requests carry cookies / browser-remembered Basic credentials. Rarely wanted: a key in an `Authorization` header needs none of it. |

Every one of those is also a field on `/admin/cors`, validated by the same code:
one validator, two vocabularies, so the page and the environment cannot come to
mean different things by the same words.

`PCOMIRROR_CORS_HEADERS` is about the headers *your app* chooses. Five more are
allowed whatever it says — `Cache-Control`, `Pragma`, `Expires`, `If-None-Match`
and `If-Modified-Since` — because an HTTP client library adds them on its own
account and none of them carries authority a read could act on. `axios`'s cache
interceptor, to name the one that prompted this, puts the first three on *every*
request under its default `cacheTakeover` and the conditional pair on every
revalidation; none is on the browser's safelist, so all five are preflighted, and
a policy listing only `Authorization,Content-Type` refused every request such a
page made. That is a header nobody typed failing a request nobody could see.

Origins are matched as browsers send them — `scheme://host[:port]`, nothing else —
so `https://app.church.org/` with the slash the address bar leaves behind **fails
startup** (or is refused by the form) rather than never matching. A wildcard
replaces the first label only:
`https://*.church.org` allows `app.church.org` and `a.b.church.org`, and does not
allow `church.org` (a different origin you did not name) or `evil-church.org`
(a different site). `*` beside a named origin is refused as a contradiction, and
`*` anywhere is refused alongside credentials, because a browser rejects that
combination outright — a service that emitted it would fail every request while
looking configured.

Four decisions in here are worth stating, because each is a failure a browser
reports as the same single line:

- **A preflight is answered without a credential.** Browsers strip
  `Authorization` from the `OPTIONS` probe, so a preflight run through the API-key
  check would `401` — and the browser would then report the *real* request as a
  CORS failure with that 401 nowhere in sight. The probe reaches no data: it
  answers only with the methods and headers the policy allows.
- **Errors carry the headers too.** A `401`, `403` or `400` a page cannot read is
  reported to its developer as "CORS error", and the sentence saying *key lacks
  the 'write' scope* never arrives. Every response gets them, not just the ones
  that worked.
- **`Vary: Origin` is set whenever the answer depends on the origin** — including
  on responses to origins that were refused, or a shared cache in front of this
  service hands one origin's response, headers and all, to a page from another.
- **The operator console and the webhook receiver are never cross-origin
  readable**, whatever is configured. The console authenticates with a
  `SameSite=Strict` session cookie and runs no JavaScript, so nothing legitimate
  would ask; the receiver authenticates deliveries from Planning Center, which is
  not a browser. Neither exclusion is a setting.

A refused preflight still answers `200` — a browser only reads a `2xx` preflight,
and its own message ("Method `DELETE` is not allowed by
`Access-Control-Allow-Methods`") is the most useful sentence anyone gets out of
the exchange. For whoever is holding `curl` instead, the reason is spelled out in
an `X-Mirror-Cors` header naming both places the policy can be set — it never
guesses which one is in force, since sending an operator to edit a variable
nothing is reading is worse than saying nothing:

```sh
curl -si -X OPTIONS http://localhost:8080/people/v2/people \
     -H 'Origin: https://elsewhere.example' -H 'Access-Control-Request-Method: GET'
# X-Mirror-Cors: origin 'https://elsewhere.example' is not allowed — set the
#                allowed origins at /admin/cors or in PCOMIRROR_CORS_ORIGINS
```

`serve` prints the policy at startup, and says which of the two sources it came
from.

#### Browser access from the page

`PCOMIRROR_CORS_*` is the *default*, not the last word. **`/admin/cors` manages the
same policy and the page wins**: save anything there and the environment stops
being consulted, so a restart cannot undo a policy fixed at 9pm — the same
arrangement, and the same reasoning, as
[subscriptions from the page](#subscriptions-from-the-page). `serve` says which
source is in force at every start rather than leaving an operator to wonder why
their variable appears to do nothing, and *Hand back to the environment* reverses
it — from then on, no restart needed.

The form is the six settings above: origins in one box, methods as tick-boxes,
the two header lists, the preflight lifetime, and credentials. It **applies to the
next request**. What it cannot reach immediately is a preflight a browser has
already cached — that lives for up to `Access-Control-Max-Age`, which is why that
number is on the form rather than buried, and why `0` is worth setting while
you are working something out.

Two distinctions the page is careful about, because conflating either would lose
somebody's intent:

- **Saving no origins turns cross-origin access off; handing back to the
  environment restores `PCOMIRROR_CORS_*`.** They are different wishes, and an
  operator who wanted the first must not get the environment's origins back on the
  next restart.
- **A refused save changes nothing and keeps what you typed.** A trailing slash in
  one field does not cost you the other five, and the error is worded for a form
  ("Origins: drop the trailing slash") rather than for a container log.

The dashboard's **Browser access** section shows the policy in force and where it
came from. It stays true that the console itself is never cross-origin readable,
whatever is saved there.

**The thing CORS cannot do for you:** an API key in browser JavaScript is
readable by anyone who opens the page, and CORS is a rule for *browsers*, not a
firewall — `curl` ignores it entirely. So mint a browser app a key scoped to what
it reads (`--scopes 'read:people,read:emails'`), and keep `write` and
`passthrough` for callers that can hold a secret.

### Admin page

The root path (`http://localhost:8080/`) serves an operator console: create and
revoke API keys, manage webhook subscriptions, and read cache statistics.
Server-rendered HTML, no JavaScript, no external assets.

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
  `/admin/webhooks` is where they are managed: the same event picker Planning
  Center's console shows, one receiver URL carrying as many event types as you
  tick, what the mirror will do with each, and a paste box that reads the
  `PCOMIRROR_SUBSCRIPTIONS` syntax. Saving there takes the list over from the
  environment — see
  [Subscriptions from the page](#subscriptions-from-the-page).
  `/admin/webhooks/calls` holds every call the receiver has been sent, verbatim
  and downloadable, refused ones included — see
  [Webhook recordings](#webhook-recordings).
- **Browser access** — the CORS policy in force: which origins, methods and
  headers, how long a preflight is cached, and whether it came from the
  environment or this console. `/admin/cors` is where it is changed, and takes
  over from `PCOMIRROR_CORS_*` once used — see
  [Browser access (CORS)](#browser-access-cors).
- **Diagnostics** — a summary, and `/admin/diagnostics` for the full log. See below.

### Diagnostics

`/admin/diagnostics` is a durable record of what the mirror asked Planning Center
and what came back. It exists because of a question that could not be answered
after the fact: a write reached PCO, PCO applied it, the response never made it
back, and the only account of why was a line on stderr in a container that had
since been replaced.

What is recorded:

- **Every mutation**, successful or not. `write.applied`, `write.refused` (PCO
  said no), `write.lost` (the response never came, so it may or may not have been
  applied), and `write.mirror_failed` (PCO applied it; the mirror could not
  record it). The last two are counted separately on the dashboard as
  **indeterminate writes** — each one needs checking upstream by hand.
- **Every upstream failure**, read or write, *including ones a retry recovered
  from* (`upstream.retry`). A read that needed three attempts is not a problem in
  itself, but it is often the reason the write beside it timed out, and a
  successful retry otherwise erases the only trace of it.
- **`x-request-id`** — the one field in the exchange Planning Center's own
  support can look up.

Each event carries the method, the path, the upstream status, how long it took,
how many sends it needed, and the record id where there is one.

What is **never** recorded: request or response bodies, any header beyond a
chosen few, and the *values* of query parameters. A mirror of a church's people
database has somebody's child's details in almost every payload, and a diagnostic
log is exactly the sort of thing that gets pasted into an issue. Filter names
survive (`where[search_name]=•`) because "that filter was in play" is the
diagnostic fact; what was typed into it is not.

Recording never fails a request — if it cannot write, the page says the log is
incomplete rather than quietly showing a short one. The table is capped at
`PCOMIRROR_DIAGNOSTIC_KEEP` rows (default 1000; `0` switches recording off).

### Webhook recordings

`/admin/webhooks/calls` keeps every call to the receiver **exactly as it
arrived**, and lets you download it. This is the log to reach for when Planning
Center's console says a delivery went out and the mirror has no sign of one.

The mirror already stored the bytes of every delivery it *accepted* — that is
what re-verifying a signature needs. What was missing is everything before that
point, which is where the failures are:

- a **401**: which signature, over which bytes, against which token;
- a **404**: PCO delivering to a subscription somebody removed, or a scanner
  working through guesses — the request is what tells them apart;
- a **503**: the body that would not parse, which PCO will keep redelivering;
- the **headers**, `X-PCO-Webhooks-Authenticity` above all. The receiver read it,
  compared it, and threw it away.

Each recording holds the method, path, query, caller address, every header, the
exact request bytes, the status and note answered, and how long it took —
including the call that made the receiver *raise*, which is otherwise the one
delivery nobody can reconstruct. Recording can never fail a delivery; if it fails
to write, the page says the log is incomplete.

Download the whole log as JSON, just the rejected calls, one call, or **one
call's exact bytes** — the last being what re-hashing a refused delivery
actually needs.

**Nothing is redacted, deliberately.** This is the opposite policy to
[Diagnostics](#diagnostics) above, which drops query values so it is safe to
paste into an issue. A recording that has been cleaned up cannot answer the
question it was kept for: a signature is over the bytes that were sent, and a
signature over a re-serialized body is a signature over a different body. So
these rows — and the download — hold whole webhook payloads: names, addresses,
phone numbers, whatever the event carried. **Handle the file like the database,
because it is a copy of part of it.**

Two bounds, neither of them a redaction. A body over 256 KiB is stored to that
length and marked clipped, with its true size recorded, because the receiver
answers before it knows who is calling. And the table is a ring buffer of
`PCOMIRROR_WEBHOOK_RECORD_KEEP` calls (default 500; `0` switches recording off),
changeable from the page without a restart — the delivery you want recorded is
the next one. A flood of junk to an unknown token evicts real history: that is
the cost of recording the rejects, and the reason the number is raisable while
you are chasing something.

### Divergence checking

`/admin/divergence` records where the mirror and Planning Center disagree, so a
wrong answer stops being something only a user notices. Off unless
`PCOMIRROR_SHADOW_PER_MINUTE` is set above zero — it spends real PCO budget, so
it is meant to be switched on while chasing something.

**Why it has to exist.** Every freshness mechanism in this design rests on
`updated_at` being truthful, and there is now a measured case where it is not:
PCO demotes a previous primary email **without moving it**
([`docs/mutation-testing.md`](docs/mutation-testing.md)). The sweep filters on
that timestamp so the record never comes back; the monotonic writer would refuse
it as not-newer if something did fetch it; drift counts rows and the count does
not change. Nothing converges on it, ever. Asking PCO is the only way to see it.

**How it works.** It keeps a **live golden corpus**: the distinct reads the
mirror has actually been asked for. The scheduler works through it under the rate
cap, replaying each request against the mirror *and* PCO back to back, then
comparing. `tests/golden/` is the same idea recorded by hand once; this is the
same idea kept current by the traffic itself.

Nothing is synthesised. Every request checked is one a caller really made — a
made-up query tests something nobody does, and spends the PCO budget doing it.

Replaying both sides at comparison time is what keeps them near-simultaneous: an
edit landing between a stored response and a later upstream read is
indistinguishable from a bug.

**Shape is a fairness unit, not the sample.** Requests are grouped by shape — the
path with ids and paging removed, so `/people/1` and `/people/99999` group
together. Checking takes the least-recently-checked *shape*, then the
least-recently-checked request within it. The grouping stops the busiest query in
the building taking every check; the several requests inside a group are what
cover the records callers actually touch, so a shape does not mean re-verifying
one person for ever. Up to `SAMPLES_PER_SHAPE` (25) requests are kept per shape,
the busiest ones — a request made once may never be made again, and the one made
constantly is the one whose breaking gets noticed.

The boundary this draws is deliberate: it verifies the mirror **against the
traffic it serves**. A record no caller has ever asked for is outside it, and the
reconcile sweep, drift probe and delete audit own that ground.

**A child cannot outlive the record that owns it.** Every other way a child gets
tombstoned needs the owner still to be there to ask about: the include-diff
compares a fetched person's `include=` set against what the mirror holds, and the
per-parent walk re-reads a live household's memberships. When the owner itself is
gone — a `404` on hydration, an absence the audit confirmed, a `destroyed`
webhook — neither can run again, and nothing else looks, because a child's own
sweep filters on `where[updated_at]` and that cannot return a row which no longer
exists. So the emails, phone numbers and addresses of a hard-deleted person
stayed live in the mirror **for ever**, and `GET /emails` kept serving that
person's address long after `GET /people/{id}` had started answering 404. The
canonical writer now cascades along the declared ownership edges, and revives
those children if the owner comes back.

Ownership, not reference: `person.primary_campus` points at a campus, and a
campus being deleted must never tombstone the people in it. A **merge** is
excluded too — PCO moves a merged person's children to the survivor rather than
deleting them.

The **delete audit** is the third and slowest of the delete mechanisms (DESIGN
§7.2) and the only one that needs no signal from PCO: webhooks are lossy and the
merger poll only covers the merge path, so a person hard-deleted in the UI is
invisible to everything else — `where[updated_at]` cannot return an id that no
longer exists. It runs on `PCOMIRROR_AUDIT_INTERVAL_HOURS` (default 24, `0` off),
timed from the *persisted* completion stamp rather than process start, because a
once-a-night check measured against a service that restarts more often than that
is a check that never happens.

**What fairness by shape does and does not buy.** Every shape gets an equal share
of the checks, whatever its traffic. Measured against deliberately lopsided
traffic:

| shape | share of traffic | share of checks |
|---|---|---|
| `/people` | 97.7% | 25% |
| `/people/{id}` | 2.0% | 25% |
| `/people?include` | 0.2% | 25% |
| `/people?where[child]` | 0.2% | 25% |

That is the intent, not a side effect. A hot query breaking is noticed in minutes
by whoever is using it; a filter run once a week breaking is silent for as long as
nobody runs it. Equal shares deliberately bias the budget towards the quiet
corners, because those are the ones nothing else will report.

The cost is coverage *within* a busy shape: with `S` shapes at `R` checks a
minute, a shape holding `N` distinct requests takes `N·S/R` minutes to work
through them. Twenty shapes at 6/min with 25 requests in one of them is about an
hour and a half for that shape's full cycle.

**Yielding to the foreground.** Background work — `divergence`, `reconcile`,
`backfill`, `webhook_hydrate` — only spends a token when the bucket has headroom
above a reserve, so it uses what the foreground demonstrably is not. When PCO is
quiet the bucket sits full and checks run freely; when callers are busy the tokens
stay low and checks stall, which is the point. A divergence check gives up after
five seconds rather than hold the scheduler thread, and the rate is a token bucket
started with **one** token rather than a full bucket: switching this on should do
something immediately, not fire a whole minute's allowance into a budget people
are waiting on.

**Two verdicts, and the difference is the point:**

| | means | action |
|---|---|---|
| **staleness** | PCO's `updated_at` is newer — the mirror is simply behind | none; the sweep collects it |
| **divergence** | they differ at the *same* `updated_at` | somebody has to look — nothing will fix this on its own |

Burying the second under the first is how this feature would fail quietly, so
they are counted and filtered separately.

**What is *not* a difference** is the part that takes the work. The mirror
differs from PCO on purpose — `links` are generated from the registry and
rewritten relative, `meta.can_filter` is deliberately empty, `meta.mirror` is its
own — and a naive comparison reports 100% divergence and teaches you nothing.
Those rules live in [`pcomirror/divergence/rules.py`](pcomirror/divergence/rules.py),
lifted out of `tests/test_golden.py` so the live check and the 81-response corpus
cannot drift apart. Live it is *stricter* in one respect: `meta.total_count` must
match, because the mirror holds the whole organization where the corpus is a
sample.

**Turning it on.** `/admin/divergence` has the switch: a checks-per-minute box
where `0` is off. `PCOMIRROR_SHADOW_PER_MINUTE` sets the default and the page
overrides it, persistently — the person who wants this on at 9pm while chasing
something is rarely the person who can edit the container's environment and
restart it. One number rather than a separate on/off toggle, so there are not two
settings that can disagree. "Back to the environment default" clears the
override.

The rate is a token bucket at N *per minute*, filled at startup so the first pass
does something immediately. It is genuinely per minute: the scheduler ticks every
few seconds, and a plain per-pass limit would have made the number mean twelve
times what it said.

Both responses are stored **pseudonymised**, so the log is safe to hand to
somebody. Download it as JSON or clear it from the page; the store is capped by
`PCOMIRROR_SHADOW_KEEP` (default 200 reports).

A report keeps the **concrete parameters**, not only the shape. That is what
makes an ordering difference readable: a real export showed one record eight
places out of position with every attribute agreeing, and nothing in the file
said which field the page had been sorted by. Filter *values* in those parameters
are pseudonymised like the attributes they filter on — `where[last_name]=` is as
identifying as `attributes.last_name` — while `order`, `include`, `per_page`,
`offset` and `fields[…]` survive verbatim, because they name schema and they are
the reason for storing the query at all.

### Pseudonyms

[`pcomirror/pseudonym/`](pcomirror/pseudonym/) replaces the people in a payload
with believable strangers, so a log can be read by somebody and handed to
somebody. It is the building block the divergence log is stored through.

Every real value becomes a plausible fake one, and *the same* fake one every
time, so what survives is the structure: which records share a surname, which
people are in which household, whether two responses differ in a flag. A family
still reads as a family. What does not survive is who they are.

| Kind | Becomes |
|---|---|
| names — first, last, nickname, `name` | a name from the pools, consistently: a person's `name` always agrees with their `first_name` and `last_name`, and `Reed Household` follows `Reed` |
| email | a different valid address; two identical addresses stay identical |
| phone | the same digit count and punctuation, dialling code kept — both phone filters turn on those |
| address | a plausible street, city, state and postcode |
| dates | the same year, a shifted month and day, so age and grade logic still lands |
| booleans, numbers, timestamps, ids, relationships | **untouched** — this is what a divergence is made of |
| free text (`medical_notes`, field values) | `«redacted:a3f9…»`, never fabricated |
| anything unclassified | `«redacted:a3f9…»` |

A redaction still carries a **keyed fingerprint of what it replaced**, because a
constant marker would make every hidden value equal to every other — and then the
one question the log is asked about those fields, *are these two the same*, could
never be answered. Equal values tag alike; different ones do not; the tag carries
none of the text. Free text is compared verbatim rather than case-folded, since a
mirror holding `EpiPen` where PCO holds `epipen` is a real difference.

Two properties are worth stating outright, because the package is worthless
without either:

- **Unclassified attributes are redacted, never passed through.** PCO adds
  fields; the day it adds one this package has not heard of, the failure has to
  be a redaction rather than a leak.
- **Selection is keyed.** A plain hash over a thousand-name pool is a lookup
  table anybody can rebuild. Selection is an HMAC under a key **derived from the
  PCO credential** — nothing is minted and nothing extra has to be kept, because
  anyone holding the token can read the real records anyway. The key is a
  derivative, never the token itself, and never appears in an export. Two
  organizations map the same person differently; one organization maps them
  identically for ever, and the mapping survives a rebuilt database.

  The consequence to know about: **rotating the PAT re-pseudonymises everything**,
  so logs from either side of a rotation cannot be compared.

Record **ids are kept**, deliberately: they are how two responses are lined up
against each other, and they identify nobody without access to the organization
they came from.

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

Several entries may share one `url_token`, which is the usual shape: one receiver
URL registered at PCO, carrying every event type you ticked there.

#### Receivers with no secret: the URL as the credential

The `authenticity_secret` field may be left empty
(`sub_123:people.v2.events.person.updated::`, or the checkbox on
`/admin/webhooks`). That subscription's signature is then not checked.

This is not the same as "unauthenticated". It **moves** the authentication from
the body's signature to the token in the URL — and a URL whose token is
unguessable is a bearer credential, exactly as an API key in a header is. A
minted token is 32 characters of base64url, 192 bits, so leaving the secret off
and the token blank gives you a perfectly ordinary security model, not a hole.

So the mirror distinguishes the two cases rather than shouting about both:

| | signature | guessable token, no secret | minted token, no secret |
| --- | --- | --- | --- |
| authenticated by | the body | **nothing** | the URL |
| the mirror | says nothing | names it at every start, banners it, marks it | says nothing; notes on the receiver what the URL now is |

`webhooks.token_is_credential` decides which, and the bar is deliberately
conservative: at least 24 characters, and at least 100 bits by
`len × log2(distinct symbols used)` — the alphabet the token *actually* uses, so
`aaaa…` 32 times scores zero rather than 32. A minted token scores ~149; the
kind of name somebody types, `person-events-01`, scores ~55.

**What that estimate cannot do**, since it decides whether anything shouts: it
assumes the characters were chosen independently. That is true of a minted token
and false of a phrase, so a long descriptive slug scores like randomness and is
not. If you pick a token by hand, do not also turn the secret off.

Two things stay true whatever you choose:

- A receiver is only as checked as its *least*-checked subscription. One
  secretless subscription moves the whole URL's authentication into the token,
  because a delivery may name any event; signed subscriptions on it still verify
  and are still attributed correctly.
- Only the signature check goes. An unknown token is still a `404`, the token
  format is still enforced, and a paused subscription still stops receiving.

And if the URL is the credential, treat it like a password: serve it over TLS,
keep it out of anything that logs or forwards URLs, and rotate it by moving the
events to a subscription on a fresh token.

There is no separate switch for any of this, on purpose: the secret is the only
thing a check could be made of, so "no secret" and "no signature check" are one
fact rather than two settings that can disagree.

#### Subscriptions from the page

`PCOMIRROR_SUBSCRIPTIONS` is the *default*, not the last word. `/admin/webhooks`
manages the same list from the operator console, and **the page wins**: the
moment you save anything there, the environment stops being applied on start, so
a restart cannot undo a webhook you fixed at 9pm — the person who can reach the
page is rarely the person who can edit the container's environment and restart
it. `serve` says so in its log rather than skipping silently, and a
*Hand back to the environment* button reverses it.

The page carries the same event picker Planning Center's console does: tick as
many events as you like, paste the secret, and it registers one subscription per
event, all pointing at one receiver URL that it then shows you to paste back into
PCO. Leaving the secret blank needs the *no secret* box ticked as well — an empty
field on its own is what a half-finished paste looks like. Tick it and leave the
token blank too, and the minted URL becomes the credential. It also states, per event, what the mirror will do with it — write it to a
table, run the merge path, or record it and apply it to nothing (which is what an
event for a resource with no table here means; those are kept in the inbox marked
`ignored` rather than dead-lettered, so the dead-letter queue keeps meaning
"something broke"). Already have a `PCOMIRROR_SUBSCRIPTIONS` value? Paste it into
the import box — it goes through the same parser.

The offered event list is the People console's, built in. **Refresh from Planning
Center** replaces it with whatever `GET /webhooks/v2/available_events` returns for
your organization, which is the only version that stays right when PCO adds an
event. Any event name can be typed in regardless; nothing here gates what the
receiver accepts.

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
- **Config is env-first** (see [`.env.example`](.env.example)): `PCO_APP_ID`,
  `PCO_SECRET`, `PCO_API_VERSION`, `PCO_USER_AGENT`, `PCOMIRROR_PUBLIC_URL`,
  `PCOMIRROR_BACKFILL_ON_START`, `PCOMIRROR_SUBSCRIPTIONS`, `PUID` / `PGID`,
  `PCOMIRROR_ALLOW_ANONYMOUS`, `PCOMIRROR_CORS_ORIGINS` (+ the other
  `PCOMIRROR_CORS_*` knobs), `PCO_CA_BUNDLE` (if PCO egress goes via a proxy),
  `PCOMIRROR_DIAGNOSTIC_KEEP`, `PCOMIRROR_SHADOW_PER_MINUTE` /
  `PCOMIRROR_SHADOW_KEEP`, `PCOMIRROR_WEBHOOK_RECORD_KEEP`,
  `PCOMIRROR_AUDIT_INTERVAL_HOURS`,
  `PCO_WEBHOOKS_BASE_URL`, and the container-friendly defaults
  `PCOMIRROR_DB` / `PCOMIRROR_HOST` / `PCOMIRROR_PORT`. Four of these are
  defaults the admin page can override and persist —
  `PCOMIRROR_SHADOW_PER_MINUTE`, `PCOMIRROR_WEBHOOK_RECORD_KEEP`,
  `PCOMIRROR_SUBSCRIPTIONS` and the `PCOMIRROR_CORS_*` set — because each is
  something an operator needs to change mid-incident, from a machine that cannot
  restart the container.
- **API keys live in the DB**, so create one against the same volume:
  `docker exec pcomirror python -m pcomirror create-api-key --name <app>`.
  They are deliberately not settable from the environment — that would mean
  storing them in plaintext instead of hashed.
- **Webhooks need a public HTTPS URL.** PCO must reach this service, so put a
  reverse proxy / tunnel (Caddy, nginx, Cloudflare Tunnel) in front that
  terminates TLS and forwards to the container's `:8080`. Set `PCOMIRROR_PUBLIC_URL`
  to that URL — `PCOMIRROR_SUBSCRIPTIONS`, `add-subscription` and
  `/admin/webhooks` all show the exact receiver URL to register at PCO.
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
python3 run_tests.py     # 615 end-to-end tests + 11 writer-semantics assertions
```

`tests/test_mutation_guard.py` covers the refusal logic behind the live write
check — offline, against a transport that fails if it is ever reached, so the
blocking rules have regression coverage without any network or credential. The
writes themselves are a manual procedure
([`docs/mutation-testing.md`](docs/mutation-testing.md)); there is deliberately no
committed script that creates and deletes a person in production.

The suite drives backfill, sideloading, incremental sweep, merger poll, delete
audit, include-diff child deletes, drift, webhook verify/dedup/dispatch/thin-
hydrate/merge, JSON:API reads (where/search/order/include/pagination), the
410-on-merge redirect, and write-through — including the **fail-if-PCO-fails**
guarantee — against an in-process fake PCO, so no network or live credentials are
needed. `tests/test_subscriptions.py` pins the three webhook behaviours whose
failure modes are silent: a receiver URL carrying several event types verifying
each against the right secret, the operator page taking precedence over
`PCOMIRROR_SUBSCRIPTIONS` so a restart cannot undo it, and an event for a
resource with no table being recorded rather than dead-lettered. It also runs the
schema migration off the old `url_token UNIQUE` table and asserts the existing
rows survive. `tests/test_search.py` covers the query surface a real PCO client sends:
every `where[search_*]` filter, typed/boolean filters, nested collections with
includes, and a full walk of `links.next` asserting each row is visited exactly
once.

`tests/test_cors.py` pins the cross-origin rules for the same reason: a browser
reports every one of them as the same single line in a console, so an
unauthenticated preflight, headers on the error responses, `Vary: Origin`, and
the two planes the policy must never reach are otherwise indistinguishable when
one breaks. It also asserts that an `Origin` carrying a newline is never echoed —
allowed origins are matched against validated patterns precisely so that
request-supplied text cannot reach a response header — and it drives the whole
`/admin/cors` lifecycle: that a save applies to the next request without a
restart, survives the process it was made in, that saving no origins turns CORS
off while *hand back* restores the environment, that a refused save stores
nothing and keeps what was typed, and that a stored policy which cannot be
re-validated falls back to the environment instead of being served.

[`tests/golden/`](tests/golden/README.md) holds **80** request/response pairs captured
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
