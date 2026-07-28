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

**Sampling by shape, not by request.** A uniform sampler spends the whole budget
on a thousand copies of the roster read. Keying on shape and taking the
least-recently-checked covers the API *surface* for the same cost — and the
surface is where the bugs were: search filters, includes, ordering, nested reads.

**What is stored.** Both bodies, pseudonymised (`pcomirror.pseudonym`), plus the
computed differences. Pseudonyms are what make the log safe to hand to somebody
while still being worth reading — the structure survives, the people do not.

Off unless `PCOMIRROR_SHADOW_PER_MINUTE` is set above zero. It spends real PCO
budget, so it is a thing an operator turns on while chasing something.
"""
from __future__ import annotations

import json
import re

from ..config import now_iso
from .rules import Difference, classify, compare

__all__ = ["Difference", "classify", "compare", "shape_of", "ShadowChecker",
           "recent", "summary", "clear", "export"]

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

    def __init__(self, db, client, settings, pseudonymiser, serve, recorder=None):
        self.db, self.client, self.s = db, client, settings
        self.pseudonymiser = pseudonymiser
        self._serve = serve
        self._recorder = recorder

    # -- enrolling ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return getattr(self.s, "shadow_per_minute", 0) > 0

    def observe(self, path: str, qs: dict) -> None:
        """Note that a shape was asked for. Cheap, and never fails a read."""
        if not self.enabled:
            return
        try:
            shape = shape_of(path, qs.keys())
            self.db.execute(
                "INSERT INTO shadow_probe(shape, path, query, first_seen_at, seen) "
                "VALUES(?,?,?,?,1) ON CONFLICT(shape) DO UPDATE SET "
                "seen = seen + 1, path = excluded.path, query = excluded.query",
                (shape, path, json.dumps({k: v[0] for k, v in qs.items()}, sort_keys=True),
                 now_iso()))
        except Exception:  # noqa: BLE001
            pass

    # -- draining ----------------------------------------------------------
    def due(self, limit: int):
        """Least-recently-checked shapes first, so coverage spreads."""
        return self.db.query(
            "SELECT * FROM shadow_probe ORDER BY coalesce(last_checked_at,'') ASC, "
            "shape ASC LIMIT ?", (max(0, limit),))

    def run_once(self, limit: int | None = None) -> int:
        """One pass. Returns how many shapes were checked."""
        if not self.enabled:
            return 0
        budget = self.s.shadow_per_minute if limit is None else limit
        checked = 0
        for probe in self.due(budget):
            try:
                self.check(probe["shape"], probe["path"], json.loads(probe["query"] or "{}"))
            except Exception as e:  # noqa: BLE001
                self._note_failure(probe["shape"], e)
            self.db.execute("UPDATE shadow_probe SET last_checked_at=? WHERE shape=?",
                            (now_iso(), probe["shape"]))
            checked += 1
        return checked

    def check(self, shape: str, path: str, params: dict) -> str:
        """Serve one query both ways and record what differs.

        Both sides are asked *now*, back to back, rather than one of them being
        the response some earlier caller received. An edit landing between a
        stored response and a later upstream read is indistinguishable from a
        bug, and this way that window is milliseconds rather than hours.
        """
        mirror_status, mirror_body = self._serve(path, params)
        # The mirror serves `/people/v2/people/1`; the client's base URL already
        # ends in `/people/v2`, so it wants the tail on its own.
        upstream = self.client.get(_upstream_path(path), params or None, priority="reconcile")
        pco_body = upstream.json() if upstream.body else {}

        differences = compare(mirror_body, pco_body, mirror_status, upstream.status)
        verdict = classify(differences, mirror_body, pco_body)
        if verdict == "match":
            self.db.execute(
                "UPDATE shadow_probe SET last_agreed_at=? WHERE shape=?", (now_iso(), shape))
            return verdict

        p = self.pseudonymiser
        self.db.execute(
            """INSERT INTO shadow_report
                 (at, shape, path, verdict, difference_count, differences,
                  mirror_status, pco_status, mirror_body, pco_body, pco_request_id)
               VALUES (:at,:shape,:path,:verdict,:n,:diffs,:ms,:ps,:mb,:pb,:rid)""",
            {"at": now_iso(), "shape": shape, "path": path, "verdict": verdict,
             "n": len(differences),
             "diffs": json.dumps([_safe_difference(p, d) for d in differences]),
             "ms": mirror_status, "ps": upstream.status,
             "mb": json.dumps(p.document(mirror_body)),
             "pb": json.dumps(p.document(pco_body)),
             "rid": getattr(upstream, "request_id", None)})
        self._trim()
        return verdict

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
        f"SELECT report_id, at, shape, path, verdict, difference_count, differences, "
        f"mirror_status, pco_status, pco_request_id FROM shadow_report{where} "
        f"ORDER BY report_id DESC LIMIT ?", (*params, max(1, min(1000, limit))))


def summary(db) -> dict:
    by_verdict = {r["verdict"]: r["n"] for r in db.query(
        "SELECT verdict, count(*) n FROM shadow_report GROUP BY verdict")}
    probes = db.query_one(
        "SELECT count(*) shapes, sum(seen) seen, max(last_checked_at) last_checked, "
        "count(last_checked_at) checked FROM shadow_probe") or {}
    return {
        "divergence": by_verdict.get("divergence", 0),
        "staleness": by_verdict.get("staleness", 0),
        "total": sum(by_verdict.values()),
        "shapes": probes["shapes"] or 0,
        "checked": probes["checked"] or 0,
        "requests_seen": probes["seen"] or 0,
        "last_checked": probes["last_checked"],
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
            "at": r["at"], "shape": r["shape"], "path": r["path"], "verdict": r["verdict"],
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
