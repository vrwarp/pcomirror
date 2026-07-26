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
