"""Continuously check that the mirror still agrees with Planning Center.

Every bug this feature was built after had the same shape: the mirror served a
confident wrong answer and nothing noticed. Duplicate parents, an orphaned
household membership, a stale `people` array, two primary email addresses on one
person — none of them raised an error anywhere.

One of them proves nothing else could have caught it. PCO demotes a previous
primary email **without moving `updated_at`** — measured, `docs/mutation-testing.md`.
The incremental sweep filters on that timestamp so the record never comes back;
the canonical writer's monotonic guard would refuse it as not-newer if something
did fetch it; the drift probe counts rows and the count did not change. Every
freshness mechanism in the design rests on `updated_at` being truthful, and there
is now a measured case where it is not. Asking PCO directly is the only way to
see it.

**How it works.** Reads served from the mirror record their *shape* — the path
with ids replaced, plus the query keys — into a small queue. The scheduler drains
that queue under a rate cap, and for each shape replays the query against the
mirror *and* against PCO, back to back, then compares. Replaying both sides at
comparison time rather than storing the response from the original request is
what keeps them near-simultaneous: an edit landing between a stored response and
a later upstream read looks exactly like a bug, and this way that window is
milliseconds instead of hours.

**A live golden corpus.** Every row kept is a request a caller really made. The
checker never invents one: a synthesised query tests something nobody does, and
spends the PCO budget doing it. `tests/golden/` is the same idea recorded by hand
once; this is the same idea kept current by the traffic itself.

**A diagnostic, and only a diagnostic.** The checker never writes to the mirror
— not a tombstone, not a hydration, not a queued task. The moment it repaired
what it found, it would be perturbing the thing it measures: the second check
of a shape would agree because the first one patched it, a rule-level bug (a
search arm matching wrongly) would hide behind record-level fixes for ever, and
turning a diagnostic on would change production data. Repair belongs to the
reconciliation machinery — the sweeps, the id-set audits, and the drift probe
that requests an audit when the counts disagree (DESIGN §7.4) — which runs
whether or not anybody is watching this log.

**Shape is a fairness unit, not the sample.** Requests are grouped by shape — the
path with ids and paging removed — and checking takes the least-recently-checked
shape, then the least-recently-checked request *within* it. Grouping is what stops
the busiest query in the building taking every check; the several requests inside
a group are what cover the records callers actually touch, so a shape does not
mean re-verifying one person for ever.

**What is stored.** Both bodies, pseudonymised (`pcomirror.pseudonym`), plus the
computed differences. Pseudonyms are what make the log safe to hand to somebody
while still being worth reading — the structure survives, the people do not.

**What that costs, bounded in bytes.** A report holds two entire responses, so
counting reports never bounded the disk: two hundred single-person GETs is a few
hundred kilobytes and two hundred include-heavy pages of a hundred records is
hundreds of megabytes of somebody else's volume — the same setting, three orders
of magnitude apart. `MAX_BYTES` is the bound that does hold, at 25 MB, and
`shadow_keep` still counts reports; whichever bites first applies, oldest first
either way. `MAX_REPORT_BYTES` is what makes the total reachable by dropping
whole reports: without a per-report ceiling one enormous pair of bodies either
blows the total on its own or evicts the entire log to make room for itself, so
past it the bodies are dropped and the differences — the finding — are kept.

Off unless `PCOMIRROR_SHADOW_PER_MINUTE` is set above zero. It spends real PCO
budget, so it is a thing an operator turns on while chasing something.
"""
from __future__ import annotations

import json
import re
import time

from .. import registry
from ..config import now_iso
from .rules import MAX_STORE_NOTES, Difference, classify, compare, one_sided

__all__ = ["Difference", "classify", "compare", "shape_of", "ShadowChecker",
           "recent", "summary", "clear", "export", "effective", "configure",
           "trim_to_size", "MAX_PER_MINUTE", "MAX_BYTES", "MAX_REPORT_BYTES",
           "OVERRIDE_KEY", "SAMPLES_PER_SHAPE"]

#: How many distinct requests to keep per shape. Enough that a shape covers a
#: spread of records rather than one, bounded so a caller iterating a thousand
#: ids cannot turn the corpus into a log of its own traffic. The busiest are
#: kept: a request made once may never be made again, and one made constantly is
#: the one whose breaking would be noticed.
SAMPLES_PER_SHAPE = 25

#: How long one check will wait for room in the shared budget before giving up
#: and leaving the slot to whoever is actually using it.
MAX_LIMITER_WAIT = 5.0

#: Where an operator's choice is kept. Absent means "whatever the environment
#: said", which is what a fresh install and a `docker run -e …` both expect.
OVERRIDE_KEY = "shadow_per_minute"

#: The most an operator may dial in from the page. The shared limiter stops this
#: from hurting PCO, but a large enough number would still crowd out the reads
#: real callers are waiting on — and nothing this feature learns is worth that.
MAX_PER_MINUTE = 60

#: The most disk the log may take, whatever `shadow_keep` says. `shadow_keep`
#: counts reports, and a report is two whole responses: the same 200 is a few
#: hundred kilobytes of single-record GETs or hundreds of megabytes of paged
#: include-heavy collections, and the operator who typed it cannot tell which
#: they asked for. This is the bound that means the same thing every time. The
#: newest reports are the ones kept, as under the count bound.
MAX_BYTES = 25 * 1024 * 1024

#: The most one report may take, so that the total above is always reachable by
#: dropping whole reports. A tenth: large enough that a real response pair —
#: a hundred records with their includes, twice — is kept intact, small enough
#: that no single report can evict most of the log to make room for itself.
MAX_REPORT_BYTES = MAX_BYTES // 10

#: What one report occupies, measured as stored. `length()` over TEXT counts
#: characters, so it is cast to BLOB first: a pseudonym is ASCII but an
#: attribute value need not be, and a bound that under-counts the bytes of the
#: rows it is bounding is not a bound.
_ROW_BYTES = " + ".join(
    f"length(cast(coalesce({c},'') AS BLOB))"
    for c in ("mirror_body", "pco_body", "differences", "query", "path", "shape"))


def effective(db, settings) -> dict:
    """The rate in force, and where it came from.

    The environment sets the default and an operator may override it from the
    admin page; the override wins and persists, because the person turning this
    on mid-investigation is not the person who can edit the container's
    environment and restart it.
    """
    default = max(0, getattr(settings, "shadow_per_minute", 0) or 0)
    held = db.get_meta(OVERRIDE_KEY)
    if held is None:
        return {"per_minute": default, "source": "environment", "default": default}
    try:
        chosen = max(0, min(MAX_PER_MINUTE, int(held)))
    except (TypeError, ValueError):
        return {"per_minute": default, "source": "environment", "default": default}
    return {"per_minute": chosen, "source": "admin", "default": default}


def configure(db, per_minute) -> dict:
    """Set the operator override, or clear it back to the environment default."""
    if per_minute is None:
        db.execute("DELETE FROM mirror_meta WHERE key=?", (OVERRIDE_KEY,))
    else:
        db.set_meta(OVERRIDE_KEY, str(max(0, min(MAX_PER_MINUTE, int(per_minute)))))
    return {"per_minute": per_minute}

#: A path segment that is a record id, replaced so `/people/1` and `/people/2`
#: are one shape rather than two thousand.
_ID = re.compile(r"^\d+$")

#: Query keys that name *which page*, not *which question*. Two reads of the same
#: collection at different offsets are the same shape; keeping them apart would
#: fill the queue with pagination and never get to the next filter.
_PAGING_KEYS = frozenset({"offset", "per_page"})


def _upstream_path(path: str) -> str:
    """The mirror's path as PCO wants it: without the prefix the base URL carries."""
    prefix = "/people/v2"
    return path[len(prefix):] or "/" if path.startswith(prefix) else path


def shape_of(path: str, query_keys) -> str:
    """`/people/1234/emails?include=…&offset=50` -> `/people/{id}/emails?include`."""
    segments = ["{id}" if _ID.match(s) else s for s in path.split("/")]
    keys = sorted(k for k in query_keys if k not in _PAGING_KEYS)
    return "/".join(segments) + (f"?{'&'.join(keys)}" if keys else "")


class ShadowChecker:
    """Records shapes, drains them, and writes what it finds.

    Deliberately holds no opinion about *how* the mirror answers a query: it is
    handed a callable that serves one, so the thing under test is the real
    serving path and not a reimplementation of it.
    """

    def __init__(self, db, client, settings, pseudonymiser, serve, recorder=None,
                 monotonic=time.monotonic):
        self.db, self.client, self.s = db, client, settings
        self.pseudonymiser = pseudonymiser
        self._serve = serve
        self._recorder = recorder
        self._now = monotonic
        # A token bucket, because the setting says *per minute* and the scheduler
        # ticks every few seconds. Spending the whole allowance on every pass —
        # which is what a plain per-pass limit did — made the number mean twelve
        # times what it claimed, and the operator who typed it had no way to tell.
        # Started with one token, not a full bucket. Full means an operator who
        # switches this on gets a whole minute's allowance fired at once, into a
        # budget shared with the reads people are waiting on. One is enough for
        # the first pass to do something immediately, and everything after that
        # arrives at the rate on the label.
        self._tokens = 0.0
        self._last_refill = self._now() - (60.0 / max(1, self.per_minute or 1))

    # -- enrolling ---------------------------------------------------------
    @property
    def per_minute(self) -> int:
        return effective(self.db, self.s)["per_minute"]

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0

    def _allowance(self) -> int:
        """How many checks this pass may make, from time actually elapsed."""
        rate = self.per_minute
        if rate <= 0:
            return 0
        now = self._now()
        self._tokens = min(float(rate),
                           self._tokens + (now - self._last_refill) * rate / 60.0)
        self._last_refill = now
        return int(self._tokens)

    def _spend(self, n: int) -> None:
        self._tokens = max(0.0, self._tokens - n)

    def observe(self, path: str, qs: dict) -> None:
        """Keep this request in the corpus. Cheap, and never fails a read."""
        if not self.enabled:
            return
        try:
            shape = shape_of(path, qs.keys())
            query = json.dumps({k: v[0] for k, v in qs.items()}, sort_keys=True)
            self.db.execute(
                "INSERT INTO shadow_sample(shape, path, query, first_seen_at, seen) "
                "VALUES(?,?,?,?,1) ON CONFLICT(shape, path, query) DO UPDATE SET "
                "seen = seen + 1",
                (shape, path, query, now_iso()))
            self._trim_samples(shape)
        except Exception:  # noqa: BLE001
            pass

    def _trim_samples(self, shape: str) -> None:
        """Hold the busiest `SAMPLES_PER_SHAPE` requests for this shape.

        Busiest rather than newest: a request made once may never be made again,
        and the one made constantly is the one whose breaking gets noticed. A
        caller walking a thousand ids would otherwise turn the corpus into a
        transcript of its own traffic and push everything else out.
        """
        self.db.execute(
            "DELETE FROM shadow_sample WHERE shape = ? AND sample_id NOT IN ("
            "  SELECT sample_id FROM shadow_sample WHERE shape = ? "
            "  ORDER BY seen DESC, sample_id ASC LIMIT ?)",
            (shape, shape, SAMPLES_PER_SHAPE))

    # -- draining ----------------------------------------------------------
    def due(self, limit: int):
        """One request per shape, least-recently-checked shape first.

        Two levels, because they answer different questions. Across shapes it
        keeps the busiest query in the building from taking every check. Within a
        shape it moves through the requests callers actually made, so a shape
        covers a spread of records instead of re-verifying one for ever.
        """
        shapes = self.db.query(
            "SELECT shape, max(coalesce(last_checked_at,'')) AS touched "
            "FROM shadow_sample GROUP BY shape "
            "ORDER BY touched ASC, shape ASC LIMIT ?", (max(0, limit),))
        picked = []
        for row in shapes:
            sample = self.db.query_one(
                "SELECT * FROM shadow_sample WHERE shape = ? "
                "ORDER BY coalesce(last_checked_at,'') ASC, sample_id ASC LIMIT 1",
                (row["shape"],))
            if sample is not None:
                picked.append(sample)
        return picked

    def run_once(self, limit: int | None = None) -> int:
        """One pass. Returns how many shapes were checked."""
        if not self.enabled:
            return 0
        budget = self._allowance() if limit is None else limit
        if budget <= 0:
            return 0
        checked = 0
        for sample in self.due(budget):
            try:
                self.check(sample["sample_id"], sample["path"],
                           json.loads(sample["query"] or "{}"))
            except Exception as e:  # noqa: BLE001
                self._note_failure(sample["shape"], e)
            # Marked checked whether or not it succeeded, so one request PCO
            # keeps failing on cannot stall every other request behind it.
            self.db.execute("UPDATE shadow_sample SET last_checked_at=? WHERE sample_id=?",
                            (now_iso(), sample["sample_id"]))
            checked += 1
        if limit is None:
            self._spend(checked)
        return checked

    def check(self, sample_id, path: str, params: dict) -> str:
        """Serve one query both ways and record what differs.

        Both sides are asked *now*, back to back, rather than one of them being
        the response some earlier caller received. An edit landing between a
        stored response and a later upstream read is indistinguishable from a
        bug, and this way that window is milliseconds rather than hours.
        """
        mirror_status, mirror_body = self._serve(path, params)
        # The mirror serves `/people/v2/people/1`; the client's base URL already
        # ends in `/people/v2`, so it wants the tail on its own.
        # `divergence` is deferrable and bounded: nobody is waiting on this, and
        # holding the scheduler thread while a busy foreground keeps the bucket
        # would stall the webhook drain and every sweep behind it.
        # `record_outcome=False`: whatever status PCO answers is the thing being
        # compared, not a failed exchange — a deleted record's shape re-checks as
        # a 404 on cadence for ever, and each one logged at error severity buried
        # the feed. Disagreements get a report; an incomplete check gets a
        # failure note below; both are this caller logging its own result.
        upstream = self.client.get(_upstream_path(path), params or None,
                                   priority="divergence", max_wait=MAX_LIMITER_WAIT,
                                   record_outcome=False)
        pco_body = upstream.json() if upstream.body else {}

        differences = compare(mirror_body, pco_body, mirror_status, upstream.status)
        facts, past_the_edge = self._store_facts(mirror_body, pco_body)
        verdict = classify(differences, mirror_body, pco_body, store=facts)
        if verdict == "match":
            self.db.execute("UPDATE shadow_sample SET last_agreed_at=? WHERE sample_id=?",
                            (now_iso(), sample_id))
            return verdict
        # The store's testimony rides along in the report, after the verdict is
        # decided: the reader of a one-sided record needs to know *which* of the
        # three stories it is — store gap, tombstone, or a search the sync state
        # cannot explain — and only these rows can say.
        p = self.pseudonymiser
        # Pseudonymised before they are measured: the bound is on what is stored,
        # and there is no unpseudonymised copy for a size check to reason about.
        mirror_json, pco_json, oversize = _fit_bodies(
            json.dumps(p.document(mirror_body)), json.dumps(p.document(pco_body)))
        stored_differences = [*differences,
                              *self._store_notes(facts, past_the_edge), *oversize]

        self.db.execute(
            """INSERT INTO shadow_report
                 (at, shape, path, query, verdict, difference_count, differences,
                  mirror_status, pco_status, mirror_body, pco_body, pco_request_id)
               VALUES (:at,:shape,:path,:query,:verdict,:n,:diffs,:ms,:ps,:mb,:pb,:rid)""",
            {"at": now_iso(), "shape": self._shape_of_sample(sample_id, path),
             # The shape says a query was ordered; only this says by what. An
             # ordering difference is otherwise unreproducible from the export —
             # the reader can see one record eight places out of position and has
             # no way to learn which field put it there.
             "path": path, "query": json.dumps(p.query(params or {})), "verdict": verdict,
             # The comparison's own count — the store rows below are testimony
             # about it, not more places the documents disagree.
             "n": len(differences),
             "diffs": json.dumps([_safe_difference(p, d) for d in stored_differences]),
             "ms": mirror_status, "ps": upstream.status,
             "mb": mirror_json, "pb": pco_json,
             "rid": getattr(upstream, "request_id", None)})
        self._trim()
        return verdict

    def _shape_of_sample(self, sample_id, path: str) -> str:
        row = self.db.query_one("SELECT shape FROM shadow_sample WHERE sample_id=?",
                                (sample_id,))
        return row["shape"] if row else shape_of(path.split("?")[0], [])

    def _store_facts(self, mirror_body, pco_body):
        """What the mirror's own tables hold for each record only one side returned.

        The response can only say *that* the sides disagree about a record's
        presence; whether that is a store gap, a tombstone, or a search that
        will not match a row the mirror holds is knowable only here, next to
        the database. `classify` uses these facts to keep its `staleness`
        promise honest, and `_store_notes` writes them into the report.

        Returns `(facts, past_the_edge)`, the second a `{side: count}` of the
        records the other side still had pages to reach. Those get no fact and
        no query: the store would answer "held live" for every one of them,
        which is what a page boundary already predicts, and it answered it two
        hundred times a check to say so.
        """
        facts, past_the_edge = {}, {"mirror": 0, "pco": 0}
        for key, side, resource, is_windowed in one_sided(mirror_body, pco_body):
            if is_windowed:
                past_the_edge[side] += 1
                continue
            rtype, rid = key
            r = registry.by_type(rtype)
            if r is None:
                continue    # an unmirrored type in an include set — no store to ask
            row = self.db.query_one(
                f"SELECT deleted_at, tombstone_reason, tombstone_uat, pco_updated_at, "
                f"last_synced_at, source FROM {r.table} WHERE pco_id=?", (rid,))
            state = self.db.query_one(
                "SELECT reconcile_watermark FROM mirror_sync_state WHERE resource_type=?",
                (r.name,))
            facts[key] = {
                "side": side,
                "held": ("absent" if row is None
                         else "tombstoned" if row["deleted_at"] is not None else "live"),
                "stored_uat": row["pco_updated_at"] if row else None,
                "tombstone_uat": row["tombstone_uat"] if row else None,
                "tombstone_reason": row["tombstone_reason"] if row else None,
                "last_synced_at": row["last_synced_at"] if row else None,
                "source": row["source"] if row else None,
                "watermark": state["reconcile_watermark"] if state else None,
                "upstream_uat": (resource.get("attributes") or {}).get("updated_at"),
            }
        return facts, past_the_edge

    @staticmethod
    def _store_notes(facts: dict, past_the_edge=None) -> list:
        """The store's testimony, one `$.store[...]` row per one-sided record.

        Timestamps, sources and reasons only — nothing here is anybody's name,
        so these pass the pseudonymiser untouched and the export stays safe to
        hand to somebody.

        Bounded, like the differences they follow. These rows had no ceiling at
        all, so a hundred-record page whose membership differed filed a report
        of 150 rows against a documented 40 — and the log's real bound is bytes,
        which they spent. Past `MAX_STORE_NOTES` the count is kept and the rows
        are not: a hundred more of the same sentence names no new cause.
        """
        notes = []
        for side, n in sorted((past_the_edge or {}).items()):
            if not n:
                continue
            notes.append(Difference(
                "$.store", f"{n} records" if side == "mirror" else None,
                f"{n} records" if side == "pco" else None,
                "past the far side's page edge — the store was not asked, because "
                "a record beyond a page boundary is not a record either side is "
                "missing"))
        listed = sorted(facts.items())
        for position, ((rtype, rid), f) in enumerate(listed):
            if position >= MAX_STORE_NOTES:
                notes.append(Difference(
                    "$.store", None, None,
                    f"{len(listed) - position} more records only one side returned, "
                    f"not listed — they have the cause above, not another one"))
                break
            if f["held"] == "live":
                held = (f"live, updated {f['stored_uat']}, "
                        f"synced {f['last_synced_at']} via {f['source']}")
                why = ("the mirror holds this record and did not return it — a serving or "
                       "search difference here, or a match that rides on a child row (an "
                       "email, a number) the mirror lacks"
                       if f["side"] == "pco" else
                       "the mirror returned a record PCO did not — an over-broad search or "
                       "filter match here, or an upstream deletion nothing has delivered yet")
            elif f["held"] == "tombstoned":
                held = f"tombstoned ({f['tombstone_reason']}) at {f['tombstone_uat']}"
                why = ("PCO still returns a record the mirror has buried; only a payload "
                       "newer than the tombstone resurrects it"
                       if f["side"] == "pco" else
                       "the mirror returned a record its own store holds tombstoned")
            else:
                held = "absent"
                behind = (f["upstream_uat"] and f["watermark"]
                          and f["upstream_uat"] < f["watermark"])
                if f["side"] == "pco" and behind:
                    why = (f"no such row, and the sweep watermark ({f['watermark']}) is "
                           f"already past its updated_at ({f['upstream_uat']}) — no sweep "
                           f"collects it again; the audit's restore pass or a backfill is "
                           f"what repairs this")
                elif f["side"] == "pco":
                    why = "no such row yet — the next incremental sweep collects it"
                else:
                    why = "the mirror returned a record its own store does not hold"
            notes.append(Difference(
                f"$.store[{rtype}/{rid}]", held,
                "returned" if f["side"] == "pco" else "not returned", why))
        return notes

    def _note_failure(self, shape: str, error: Exception) -> None:
        if self._recorder is None:
            return
        from .. import diagnostics
        etype, edetail = diagnostics.describe_error(error)
        self._recorder.record(
            diagnostics.UPSTREAM_ERROR, diagnostics.WARNING, method="GET", target=shape,
            detail="the divergence check could not complete", error_type=etype,
            error_detail=edetail)

    def _trim(self) -> None:
        """Both bounds, oldest first: the count of reports and the bytes of them.

        The count runs first because it is a single statement and usually the
        one that bites; the byte pass then reads only what the count left.
        """
        keep = max(1, getattr(self.s, "shadow_keep", 200))
        self.db.execute(
            "DELETE FROM shadow_report WHERE report_id <= "
            "(SELECT max(report_id) FROM shadow_report) - ?", (keep,))
        trim_to_size(self.db)


def _safe_difference(pseudonymiser, difference: Difference) -> dict:
    """A difference with its *values* pseudonymised, its location left alone.

    The pointer names an attribute, which is a fact about the schema and not
    about anybody. The values are somebody's, and go through the same mapping as
    the bodies so that a difference and the documents it came from still agree
    with each other.
    """
    out = difference.as_dict()
    attribute = out["pointer"].rsplit(".", 1)[-1] if ".attributes." in out["pointer"] else None
    if attribute:
        out["mirror"] = pseudonymiser.value(attribute, out["mirror"])
        out["pco"] = pseudonymiser.value(attribute, out["pco"])
    return out


def _placeholder(size: int) -> str:
    """What stands in for a body too big to keep — still a JSON document.

    A clipped body would be neither: `export` and every reader of the log parse
    these columns, and half a document is a parse error rather than a shorter
    answer. This says what was dropped and how large it was, which is the part a
    reader can act on.
    """
    return json.dumps({"pcomirror_elided": True, "bytes": size,
                       "why": f"over the {MAX_REPORT_BYTES}-byte limit on one report"})


def _fit_bodies(mirror_json: str, pco_json: str):
    """The two bodies as they will be stored, plus a note if they did not fit.

    Both go or neither does. A report is a comparison, and one side of one is
    not a smaller version of it — the reader who opens a body opens it to hold
    it against the other. What survives is the differences, which is where the
    finding actually is; the bodies were only ever the corroboration.
    """
    sizes = (len(mirror_json.encode()), len(pco_json.encode()))
    if sizes[0] + sizes[1] <= MAX_REPORT_BYTES:
        return mirror_json, pco_json, []
    note = Difference(
        "$.report.bodies", f"{sizes[0]} bytes", f"{sizes[1]} bytes",
        f"the two bodies together exceed the {MAX_REPORT_BYTES} bytes one report "
        f"may hold, so neither was kept — the differences are the finding; re-run "
        f"the request against both sides to see the documents")
    return _placeholder(sizes[0]), _placeholder(sizes[1]), [note]


def trim_to_size(db, cap: int | None = None) -> int:
    """Drop the oldest reports until the log fits `cap` bytes. Returns how many.

    Counted newest-first and cut at the first report that does not fit, so what
    survives is the most recent history the budget affords — the same eviction
    order as the count bound, for the same reason: the check somebody is reading
    the page for is the one that just ran.

    A single report larger than the whole budget is dropped like any other. It
    cannot be one this version wrote — `_fit_bodies` sees to that — but a log
    carried over from before this bound existed can hold one, and that is the
    case the bound is for.
    """
    cap = MAX_BYTES if cap is None else cap
    total = 0
    for row in db.query(f"SELECT report_id, {_ROW_BYTES} AS bytes "
                        f"FROM shadow_report ORDER BY report_id DESC"):
        total += row["bytes"]
        if total > cap:
            n = (db.query_one("SELECT count(*) c FROM shadow_report WHERE report_id <= ?",
                              (row["report_id"],)) or {"c": 0})["c"]
            db.execute("DELETE FROM shadow_report WHERE report_id <= ?", (row["report_id"],))
            return n
    return 0


# -- reading, for the admin page ------------------------------------------
def recent(db, limit: int = 100, verdict: str = ""):
    where, params = ("", [])
    if verdict:
        where, params = " WHERE verdict = ?", [verdict]
    return db.query(
        f"SELECT report_id, at, shape, path, query, verdict, difference_count, differences, "
        f"mirror_status, pco_status, pco_request_id FROM shadow_report{where} "
        f"ORDER BY report_id DESC LIMIT ?", (*params, max(1, min(1000, limit))))


def summary(db) -> dict:
    by_verdict = {r["verdict"]: r["n"] for r in db.query(
        "SELECT verdict, count(*) n FROM shadow_report GROUP BY verdict")}
    corpus = db.query_one(
        "SELECT count(*) samples, count(DISTINCT shape) shapes, sum(seen) seen, "
        "max(last_checked_at) last_checked, count(last_checked_at) checked "
        "FROM shadow_sample") or {}
    held = db.query_one(f"SELECT coalesce(sum({_ROW_BYTES}),0) bytes FROM shadow_report")
    return {
        "divergence": by_verdict.get("divergence", 0),
        "staleness": by_verdict.get("staleness", 0),
        "total": sum(by_verdict.values()),
        # What the log is costing, against what it may cost. Reports vary by
        # three orders of magnitude in size, so the count on its own tells an
        # operator nothing about the disk they have given this.
        "bytes": held["bytes"] or 0,
        "bytes_cap": MAX_BYTES,
        "shapes": corpus["shapes"] or 0,
        "samples": corpus["samples"] or 0,
        "checked": corpus["checked"] or 0,
        "requests_seen": corpus["seen"] or 0,
        "last_checked": corpus["last_checked"],
    }


def export(db) -> bytes:
    """The whole log as one JSON document, for handing to somebody.

    Both bodies come out of the database already pseudonymised — there is no
    unpseudonymised copy anywhere to forget to strip.
    """
    rows = db.query("SELECT * FROM shadow_report ORDER BY report_id ASC")
    return json.dumps({
        "exported_at": now_iso(),
        "note": ("Values are pseudonymised: consistent per organization, "
                 "reversible by nobody. Record ids and structure are real."),
        "reports": [{
            "at": r["at"], "shape": r["shape"], "path": r["path"],
            "query": json.loads(r["query"] or "{}"), "verdict": r["verdict"],
            "mirror_status": r["mirror_status"], "pco_status": r["pco_status"],
            "pco_request_id": r["pco_request_id"],
            "differences": json.loads(r["differences"] or "[]"),
            "mirror_body": json.loads(r["mirror_body"] or "null"),
            "pco_body": json.loads(r["pco_body"] or "null"),
        } for r in rows],
    }, indent=2).encode()


def clear(db) -> int:
    n = (db.query_one("SELECT count(*) c FROM shadow_report") or {"c": 0})["c"]
    db.execute("DELETE FROM shadow_report")
    return n
