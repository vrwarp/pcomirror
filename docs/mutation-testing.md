# Verifying the write path against a live organization

Reads can be verified from recordings ([`tests/golden/`](../tests/golden/README.md)).
Writes cannot. `POST`/`PATCH`/`DELETE` proxy to Planning Center and only touch the
mirror afterwards, so the behaviour under test — *what the mirror does with what
PCO answers* — exists only where a real API answers. That means writing to a real
organization, which is why this is a documented manual procedure rather than a
test that runs on its own.

**`DELETE /people/{id}` is immediate.** It returns `204` and the record is gone;
this document assumes no undo.

## Results of the last run

Run against an organization of 1,915 people on 2026-07-26. Census before and
after: **1915**. Seven write requests in total, each against a record created by
the same session.

| Verb | PCO | What the mirror did |
|---|---|---|
| `POST /people` | `201` | Applied the returned resource; row stored with `source=passthrough`. |
| `PATCH /people/{id}` | `200` | Applied all seven changed attributes; every projection re-derived; `pco_updated_at` advanced, so the monotonic guard took the `>` branch. |
| `DELETE /people/{id}` | `204` | Tombstoned with `tombstone_reason=destroyed`; subsequent reads answer `410` with `deleted_at` in `meta`, not `404`. |
| `PATCH` with an out-of-range value | `422` | **Nothing.** Mirror still agreed with PCO on every field, the rejected value landed nowhere, and neither `pco_updated_at` nor `last_synced_at` moved — a failed write leaves no trace at all. |

The patch deliberately spanned attribute types — a string change, an absent
attribute becoming present, a boolean flip, an integer (`grade`), and a date —
and the patched record was then confirmed to answer every filter its new values
imply (`where[grade]=8`, `where[child]=true`, `where[gender]=…`, a birthdate
range, and `search_name` on both the new given name and the new nickname), on
both sides.

### What this says about `fail-if-PCO-fails`

`_write_through` returns on `not resp.ok` before the writer is reached, and
applies PCO's *response* rather than the caller's request body. The `422` case
confirms both halves live: the request tried to set `first_name` and `nickname`
to a sentinel string, and neither reached the mirror.

### A second run: child collections (2026-07-28)

Run against an organization of 1,925 people. Census before and after: **1925**.
Four writes, all against one sentinel person created and deleted by the session:
`POST /people`, two `POST /people/{id}/emails`, `DELETE /people/{id}`.

| Question | Answer |
|---|---|
| Does a child create echo its owning `person` relationship? | **Yes** — `relationships: ["person"]` is present, so the URL-derived owner hint is belt-and-braces here rather than load-bearing. It is still sent, because a mirror that *needs* the echo is one bad release away from orphaning every row. |
| Does setting `primary: true` demote the one that held it? | **Yes**, silently — PCO returns only the new record and says nothing about the old one. |
| Does the demoted record's `updated_at` move? | **No.** Both stamps were `2026-07-28T15:06:28Z`, before and after. |

That last row is the one that matters. A side effect PCO applies **without
moving `updated_at`** is invisible to everything the mirror uses to stay fresh:
the incremental sweep filters on `where[updated_at][gt]=<watermark>`, so the
demoted row never comes back, and the canonical writer's monotonic guard would
refuse it as not-newer even if it did. Nothing converges on it, ever. The mirror
would have gone on reporting two primary email addresses for that person
indefinitely — and a caller asking for "the" number would have had even odds of
the one nobody answers.

So the fix cannot be "wait for the sweep": the write path re-reads the owner with
its children after any nested write, which is the only moment the divergence is
knowable. Verified end to end in this run — PCO and the mirror agreed on both
records afterwards.

**A measurement trap worth recording**, because it produced a confident wrong
answer first time round: `SELECT "primary" FROM email` does not error. There is
no such column — the projection is `is_primary` — and SQLite falls back to
treating a double-quoted unknown identifier as a *string literal*, so every row
came back with the truthy value `'primary'` and the mirror appeared to have
missed the demotion when it had not. `PRAGMA table_info` will not help you spot
it either: it omits generated columns. Use `PRAGMA table_xinfo`, and prefer
single quotes for literals so a typo in a column name is an error rather than a
plausible result.

## A finding about Planning Center's validation

Three of four deliberately invalid values were **accepted with `200`** and the
value silently discarded:

| Sent | PCO | Effect |
|---|---|---|
| `birthdate: "not-a-date"` | `200` | birthdate nulled |
| `birthdate: "9999-99-99"` | `200` | stays null |
| `grade: "not-a-number"` | `200` | grade nulled |
| `grade: 1000000000000` | `422` | rejected — `must be between -1 and 12` |

Only the out-of-range integer was validated. A client sending a malformed date
gets a success response and **silent data loss** on a field that previously held
a value. Anything writing dates through the mirror should validate before it
sends, because the API will not.

This is PCO's behaviour, not the mirror's — the mirror faithfully applied what
PCO returned in every case, including the nulls.

## How Planning Center orders names (2026-07-29, read-only)

Measured by walking the whole 1925-person organization with `order=<attr>` and a
sparse fieldset, then re-sorting the same rows locally. **GET only** — no
mutation guard needed, nothing was written.

| Local rule | Positions differing from PCO, `order=last_name` |
|---|---|
| `COLLATE NOCASE, id` | **34** of 1925 |
| NFKD, drop combining marks, `lower()`, then `id` | **0** of 1925 |

`NOCASE` folds ASCII `A`–`Z` and nothing else, so every accented surname sorts
after `z`: PCO returns `Manríquez, Márquez, Martinez`, and the mirror returned
`Manríquez, Martinez, … Márquez`. Six of this organization's surnames are
non-ASCII, which is enough to displace a record by several positions in any page
that spans them — and that is the shape of the ordering difference a divergence
report showed, one record eight places out with every attribute agreeing.

Two more facts from the same walk:

- **Ties break on ascending `id`.** 224 runs of equal `last_name`, all 224 in
  ascending numeric id order. The mirror's existing `_ID_ORDER` tiebreak is right.
- **Nulls sort first**, contiguously, and then by ascending id.

`pcm_sortkey` implements the measured rule, and `order=last_name` and
`order=first_name` now reproduce PCO's order for all 1925 people exactly.
`lower()` rather than `casefold()` deliberately: `lower()` is what was measured
to agree, and casefolding (`ß`→`ss`) would be a guess.

### One residual, and why it is not chased

`order=nickname` still differs by exactly one record. 784 people have a null
nickname; PCO returns 783 of them in ascending id and puts `140380947` last of
the null block — stably, on repeated reads, and a direct `GET` on that person
also reports `nickname: null`. The position is exactly where an empty string
would sort under `ORDER BY nickname NULLS FIRST`, which is the likely
explanation, but PCO's API reports the value as null either way. **A mirror
cannot reproduce a distinction the API does not expose**, so this is recorded
rather than worked around.

## The safety model

The safety is in the transport, not in whoever is driving it being careful.
[`tools/mutation_guard.py`](../tools/mutation_guard.py) wraps the real transport
and refuses everything that is not the one operation it has been armed for:

* `GET` always passes, and never consumes an arming.
* `POST` only to `/people`, only while armed, only once, only with the sentinel
  surname.
* `PATCH` and `DELETE` only for the id *this session created* — checked against
  the URL **and** the body's `data.id` — within a per-operation limit.
* `PUT` and everything else are refused unconditionally.
* No body may grant permissions, set a login identifier, attach the record to
  another record, or remove the sentinel.

Arming is one operation at a time and is spent on use, so a stray call cannot
ride along behind an intended one. Every refusal path is covered by
[`tests/test_mutation_guard.py`](../tests/test_mutation_guard.py), which runs in
the ordinary suite against a transport that fails if it is ever reached — so the
blocking logic has regression coverage without any network, credential or risk.
Those tests never demonstrate a successful write.

## Running it

There is no script in this repository that performs the writes. That is
deliberate: a committed one-command "create and delete a person in production" is
a thing to fear, and the procedure needs a human at each step anyway. Assemble it
from the pieces:

1. Wrap the real transport and build a `Mirror` against a **scratch database**,
   never the production one — a failed run should not leave a tombstone in the
   store the service is reading from.

   ```python
   from pcomirror.app import Mirror
   from pcomirror.config import Settings
   from pcomirror.pcoclient import UrllibTransport
   from mutation_guard import MutationGuard

   guard = MutationGuard(UrllibTransport(None))
   mirror = Mirror(Settings(db_path="/tmp/mutation-check.db", pco_app_id=...,
                            pco_secret=..., allow_anonymous=True),
                   transport=guard)
   ```

2. **Census first.** Record `meta.total_count` for `/people`, and confirm no
   record already carries your sentinel. You are going to compare against this.

3. **Create.** Arm, send one `POST` through the WSGI app, and record the returned
   id on the guard (`guard.created_id = ...`) — nothing may be patched or deleted
   until you have.

4. **Patch.** Arm, send, then compare every changed attribute against what PCO
   returned, not against what you sent.

5. **Verify before deleting.** Re-read the record and check it is still yours:
   the sentinel surname, no permissions, and no emails, phone numbers, addresses,
   households or field data. If any check fails, stop — a wrong id must never
   reach a `DELETE`.

6. **Delete.** Arm, send, then confirm the census is back to its starting value
   and that no record answers any name the session used.

Confirm with the organization's owner at each step. Treat every count that does
not return to its starting value as an incident.

## One methodology note

During the last run, a filter check reported two apparent mismatches between PCO
and the mirror. Both were the check itself: it compared membership of the first
page for filters matching several hundred people. Paging fully showed agreement.

The same mistake is available in every comparison in this repository, and it is
worth naming: **a page cap looks exactly like a semantic difference.** Compare
`meta.total_count`, or page to exhaustion, before believing one.
