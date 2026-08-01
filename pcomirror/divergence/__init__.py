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

**Shape is a fairness unit, not the sample.** Requests are grouped by shape — the
path with ids and paging removed — and checking takes the least-recently-checked
shape, then the least-recently-checked request *within* it. Grouping is what stops
the busiest query in the building taking every check; the several requests inside
a group are what cover the records callers actually touch, so a shape does not
mean re-verifying one person for ever.

**What is stored.** Both bodies, pseudonymised (`pcomirror.pseudonym`), plus the
computed differences. Pseudonyms are what make the log safe to hand to somebody
while still being worth reading — the structure survives, the people do not.

Off unless `PCOMIRROR_SHADOW_PER_MINUTE` is set above zero. It spends real PCO
budget, so it is a thing an operator turns on while chasing something.
"""
from __future__ import annotations

import json
import re
import time

from .. import registry
from ..config import now_iso
from .rules import Difference, classify, compare, one_sided

__all__ = ["Difference", "classify", "compare", "shape_of", "ShadowChecker",
           "recent", "summary", "clear", "export", "effective", "configure",
           "MAX_PER_MINUTE", "OVERRIDE_KEY", "SAMPLES_PER_SHAPE"]

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
        upstream = self.client.get(_upstream_path(path), params or None,
                                   priority="divergence", max_wait=MAX_LIMITER_WAIT)
        pco_body = upstream.json() if upstream.body else {}

        differences = compare(mirror_body, pco_body, mirror_status, upstream.status)
        facts = self._store_facts(mirror_body, pco_body)
        verdict = classify(differences, mirror_body, pco_body, store=facts)
        if verdict == "match":
            self.db.execute("UPDATE shadow_sample SET last_agreed_at=? WHERE sample_id=?",
                            (now_iso(), sample_id))
            return verdict
        # The store's testimony rides along in the report, after the verdict is
        # decided: the reader of a one-sided record needs to know *which* of the
        # three stories it is — store gap, tombstone, or a search the sync state
        # cannot explain — and only these rows can say.
        stored_differences = [*differences, *self._store_notes(facts)]

        p = self.pseudonymiser
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
             "mb": json.dumps(p.document(mirror_body)),
             "pb": json.dumps(p.document(pco_body)),
             "rid": getattr(upstream, "request_id", None)})
        self._trim()
        return verdict

    def _shape_of_sample(self, sample_id, path: str) -> str:
        row = self.db.query_one("SELECT shape FROM shadow_sample WHERE sample_id=?",
                                (sample_id,))
        return row["shape"] if row else shape_of(path.split("?")[0], [])

    def _store_facts(self, mirror_body, pco_body) -> dict:
        """What the mirror's own tables hold for each record only one side returned.

        The response can only say *that* the sides disagree about a record's
        presence; whether that is a store gap, a tombstone, or a search that
        will not match a row the mirror holds is knowable only here, next to
        the database. `classify` uses these facts to keep its `staleness`
        promise honest, and `_store_notes` writes them into the report.
        """
        facts = {}
        for key, side, resource in one_sided(mirror_body, pco_body):
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
        return facts

    @staticmethod
    def _store_notes(facts: dict) -> list:
        """The store's testimony, one `$.store[...]` row per one-sided record.

        Timestamps, sources and reasons only — nothing here is anybody's name,
        so these pass the pseudonymiser untouched and the export stays safe to
        hand to somebody.
        """
        notes = []
        for (rtype, rid), f in sorted(facts.items()):
            if f["held"] == "live":
                held = (f"live, updated {f['stored_uat']}, "
                        f"synced {f['last_synced_at']} via {f['source']}")
                why = ("the mirror holds this record and did not return it — a serving or "
                       "search difference, which no amount of re-syncing changes"
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
        keep = max(1, getattr(self.s, "shadow_keep", 200))
        self.db.execute(
            "DELETE FROM shadow_report WHERE report_id <= "
            "(SELECT max(report_id) FROM shadow_report) - ?", (keep,))


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
    return {
        "divergence": by_verdict.get("divergence", 0),
        "staleness": by_verdict.get("staleness", 0),
        "total": sum(by_verdict.values()),
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
