# Golden corpus — real Planning Center responses, sanitized

Eighty request/response pairs captured from the **live** Planning Center People API and
then sanitized. `tests/test_golden.py` loads the resources out of them, replays
the same requests against the serving layer, and asserts the mirror answers in
the same shape and the same order as PCO did.

## Why these exist

A hand-written fixture encodes what we *think* the API does, so it agrees with
the code even when the code is wrong. Every compatibility bug this corpus was
built to catch was invisible to the unit suite:

| What we believed | What the live API does |
|---|---|
| `where[search_name]` is a substring match | It is an **anchored word-prefix** match per name field. The substring reading returned 100 people where PCO returned 9. |
| `where[search_phone_number]` is a substring match | It matches a **digits suffix** — the last four, the last seven. An E.164 value with its country code finds nothing. |
| `where[search_phone_number_e164]` is fuzzy | It is **exact**, once punctuation is discounted. |
| `search_name` covers `nickname lastname` | It does not. A nickname alone finds the person; that pairing finds nobody. |
| `search_name` covers `middle_name` | It does not — but it does cover `given_name`. |
| Person has an `attributes.search_name` | There is no such attribute; the column projected from it was always NULL. |
| Collections sort by id as text | They sort **numerically**. `/emails` carries both 8- and 9-digit ids, so every row of page one was in the wrong place. |
| `order=last_name` sorts like SQLite | PCO folds case. SQLite's BINARY collation put every lowercase surname after every uppercase one. |
| `/household_memberships` is a collection | It is a **404**. The rows exist only under `/households/{id}/household_memberships`, carry no `household` relationship, and are mirrored by a periodic per-household walk that parses the owning id out of `links.self`. |
| `fields[Type]` was honoured | It was ignored entirely — a caller asking for two attributes got all thirty-one. |
| Any payload can warm the mirror | A `fields[]` response has no `updated_at`, and storing one replaced a person with a single attribute and a NULL timestamp that the monotonic guard could never repair. |
| `include=field_definition` on a FieldDatum worked | The registry declared no relationship, so it silently sideloaded nothing. |
| PCO applies the nested filters it documents | It **ignores** all of them. `where[emails][address]=nobody@nowhere.invalid` still returns the whole organization. |
| The reference tables carry `updated_at` | None of them do — `marital_statuses`, `name_prefixes`, `name_suffixes`, `inactive_reasons` return a `value` and nothing else, so declaring them timestamped left the monotonic guard comparing against NULL. |
| `grade` sorts like a number | It was projected as text, so `order=-grade` opened on the ninth graders rather than the twelfth. |
| A household's membership had to be derived | PCO puts it on the Household itself, no `include` needed. Scanning every person for it only ever found the ones already fetched. |
| A to-one nested read returns a collection | It returns a single resource, and **404**s when the relationship is unset. |

## What is in each file

```json
{
  "name": "people_child_filter_page1",
  "request":  { "method": "GET", "path": "...", "query": "..." },
  "response": { "status": 200, "body": { "...": "verbatim JSON:API, sanitized" } }
}
```

`manifest.json` lists every recording with its row count and the API version the
capture ran against. Only reads are recorded — the capture harness refuses any
non-GET at the transport layer.

**Queries are sanitized too.** A recording is only a test if the sanitized data
still answers it, so literal values inside the query travel through the same maps:
`where[search_name]=<surname>` becomes the pseudonym, `where[birthdate][gte]`
shifts with the birthdates, and `where[id]` is remapped. Timestamps and controlled
vocabularies (`status`, `gender`, `membership`) are left alone, because they were
never sanitized. Email addresses keep one synthetic sub-domain per real provider,
so `where[search_name_or_email]=@provider` still selects a provider's users rather
than the whole organization.

## Sanitization

Every identifier, name, address, phone number, email address, date of birth and
free-text note is replaced through a deterministic map whose salt is generated
per capture and discarded, so the mapping cannot be reversed even by re-running
the capture. A final pass walks the output and fails the capture if any original
value survives in a position that can carry personal data.

Two properties of the real data are preserved **on purpose**, because they are
what the corpus exists to test:

* **Id ordering.** Synthetic ids keep each real id's digit-length and numeric
  rank, so `/emails` still mixes 8- and 9-digit ids and "PCO sorts numerically"
  stays checkable.
* **Name ordering.** Pseudonyms are assigned by case-folded rank from an
  alphabetical pool with a zero-padded suffix, so a pseudonym sorts where the
  original sorted — including the lowercase-initial surnames that exposed the
  collation bug. Names are mapped whole rather than word by word, so a compound
  surname does not land in an arbitrary part of the alphabet.

Organization configuration — field definition names, marital statuses, campuses —
is left real. It is not personal data, and keeping it makes the corpus legible.

## Declared divergences

Twelve recordings carry a `divergence` block naming a place where the mirror
answers differently from PCO on purpose, with the reason. They are data rather
than hidden exceptions: `test_declared_divergences_are_still_real` asserts each
one still happens, so a divergence that is fixed — or that drifts — fails the
suite and has to be reconciled with the code rather than quietly describing
something that no longer occurs.

Both kinds come from one rule: **the mirror never silently ignores a query
parameter.** It applies it or refuses the request.

* `filters` — PCO documents roughly a hundred `where[relationship][attribute]`
  filters and applies none of them; the same request with a value that cannot
  match anything still returns the whole collection. The mirror applies them.
* `refuses` — PCO answers 200 for a parameter it cannot apply: an unknown filter
  or `order` key, a value it cannot coerce, an `include` naming a type it offers
  and the mirror does not hold. An answer that silently ignores what was asked is
  indistinguishable from a correct one, so the mirror returns 400 instead.

## Refreshing

The capture harness is not in this repository: it needs live credentials, and a
corpus that anyone can regenerate on demand invites regenerating it until it
agrees with the code, which is the one thing it must never do. Re-record only
when Planning Center changes its API, and read the diff.
