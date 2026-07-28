"""A durable record of what the mirror asked Planning Center and what came back.

This exists because of a question that could not be answered after the fact. A
write reached PCO, PCO applied it, the response never made it back, and the
caller — reasonably — sent it again, five times, leaving five copies of one
parent on a real family's record. Both halves of that are fixed. But when the
question became *why did the response go missing*, the only answer available was
a line on stderr that nobody had captured, in a container that had since been
replaced.

So the events worth keeping are the ones that were missing that day:

  * **Every write.** Mutations are rare, irreversible, and the only requests that
    cannot be safely repeated, so all of them are recorded — the ones that worked
    as much as the ones that did not. "It succeeded at 07:16:16" is evidence too,
    and on the day in question it was the evidence that mattered.
  * **Every upstream failure**, read or write, including the ones a retry went on
    to recover from. A read that needed three attempts is not a problem, but it
    is the reason the write beside it timed out, and by the time anybody asks,
    the successful retry has erased the only trace of it.
  * **`x-request-id`**, which is the one field in the whole exchange Planning
    Center's own support can look up. Without it a report is a description; with
    it, it is a lookup.

What is deliberately *not* kept: request and response bodies, any header
(`Authorization` most of all), and the values of query parameters. A mirror of a
church's people database has somebody's child's phone number in almost every
payload, and a diagnostic log is exactly the kind of thing that gets pasted into
an issue. Filter *names* survive because "the search filter was in play" is the
diagnostic fact; the name being searched for is not.
"""
from __future__ import annotations

import re
import urllib.parse

from .config import now_iso

#: Event kinds. Dotted so a prefix filter is meaningful, and stable because the
#: admin page and anyone grepping a support bundle both key off them.
WRITE_APPLIED = "write.applied"
WRITE_REFUSED = "write.refused"
WRITE_LOST = "write.lost"
WRITE_MIRROR_FAILED = "write.mirror_failed"
UPSTREAM_RETRY = "upstream.retry"
UPSTREAM_ERROR = "upstream.error"

INFO, WARNING, ERROR = "info", "warning", "error"

#: Long enough to carry a real message, short enough that no payload fits.
MAX_DETAIL = 400

#: Headers worth keeping off a response. Everything else is dropped; this is an
#: allowlist, so a header PCO adds tomorrow is not silently recorded.
_KEPT_HEADERS = ("x-request-id", "x-pco-api-request-rate-count",
                 "x-pco-api-request-rate-limit", "retry-after")


def redact_target(path: str, params: dict | None = None) -> str:
    """A path worth logging: every query *value* replaced with a placeholder.

    `where[search_name]=Nathaniel` is a personal detail about a child; that the
    request filtered on `search_name` at all is the diagnostic fact. Keeping the
    keys and dropping the values keeps the second without ever storing the first.
    """
    base, _, query = path.partition("?")
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True) if query else []
    pairs += [(k, "") for k in sorted(params or {})]
    if not pairs:
        return base
    shown = "&".join(f"{k}=•" for k, _ in pairs)
    return f"{base}?{shown}"


def _clip(value, limit: int = MAX_DETAIL) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pick_headers(headers: dict | None) -> dict:
    """The handful of response headers that help and cannot leak anything."""
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    return {k: lowered[k] for k in _KEPT_HEADERS if k in lowered}


def describe_error(error: BaseException) -> tuple[str, str | None]:
    """`(type, message)` for an exception, with anything URL-shaped defanged.

    A socket error's message can carry the URL it failed on, and a URL carries
    the query string this module has just gone to the trouble of redacting.
    """
    message = _clip(str(error))
    if message:
        message = re.sub(r"(https?://[^\s?]+)\?\S*", r"\1?•", message)
    return type(error).__name__, message


class Recorder:
    """Writes events, and never lets writing one break the thing it is watching.

    Every call is wrapped. A diagnostics table that could fail a request would be
    a strictly worse version of the bug it was built to explain — the whole point
    is that the write path is the dangerous one, and observing it must not add a
    way for it to go wrong.
    """

    def __init__(self, db, keep: int = 1000):
        self.db = db
        self.keep = max(1, int(keep))
        #: Set when recording itself has failed, so the admin page can say the
        #: log is incomplete instead of quietly showing a short one.
        self.last_failure: str | None = None

    def record(self, kind: str, severity: str = INFO, **fields) -> None:
        try:
            self._insert(kind, severity, fields)
        except Exception as e:  # noqa: BLE001
            self.last_failure = f"{type(e).__name__}: {e}"

    def _insert(self, kind: str, severity: str, f: dict) -> None:
        self.db.execute(
            """INSERT INTO diagnostic_event
                 (at, kind, severity, method, target, status, duration_ms, attempts,
                  pco_id, pco_request_id, detail, error_type, error_detail)
               VALUES (:at,:kind,:sev,:method,:target,:status,:ms,:attempts,
                       :pco_id,:req_id,:detail,:etype,:edetail)""",
            {"at": f.get("at") or now_iso(), "kind": kind, "sev": severity,
             "method": f.get("method"), "target": _clip(f.get("target")),
             "status": f.get("status"), "ms": f.get("duration_ms"),
             "attempts": f.get("attempts"), "pco_id": _clip(f.get("pco_id"), 64),
             "req_id": _clip(f.get("pco_request_id"), 64), "detail": _clip(f.get("detail")),
             "etype": _clip(f.get("error_type"), 80), "edetail": _clip(f.get("error_detail"))})
        self._trim()

    def _trim(self) -> None:
        """Keep the newest `keep` rows.

        A cap rather than an age cutoff: the operator's question is always "what
        happened around the time it broke", and a mirror that has been quiet for
        a month should still be able to answer it about last month.
        """
        self.db.execute(
            "DELETE FROM diagnostic_event WHERE event_id <= "
            "(SELECT max(event_id) FROM diagnostic_event) - ?", (self.keep,))

    # -- the calls the rest of the code makes ------------------------------
    def upstream_attempt(self, method, target, *, status=None, attempt=0,
                         duration_ms=None, headers=None, error=None, will_retry=False) -> None:
        """One send that did not succeed — whether or not another one follows."""
        etype, edetail = describe_error(error) if error is not None else (None, None)
        picked = pick_headers(headers)
        if error is not None:
            detail = "the connection failed before an answer arrived"
        elif status == 429:
            detail = f"rate limited (count {picked.get('x-pco-api-request-rate-count', '?')})"
        else:
            detail = f"Planning Center answered {status}"
        self.record(
            UPSTREAM_RETRY if will_retry else UPSTREAM_ERROR,
            WARNING if will_retry else ERROR,
            method=method, target=target, status=status, attempts=attempt + 1,
            duration_ms=duration_ms, pco_request_id=picked.get("x-request-id"),
            detail=detail + (", retrying" if will_retry else ""),
            error_type=etype, error_detail=edetail)

    def write_outcome(self, kind, severity, method, target, **fields) -> None:
        self.record(kind, severity, method=method, target=target, **fields)


class NullRecorder:
    """Records nothing. Keeps every call site unconditional."""

    last_failure = None

    def record(self, *a, **k) -> None:
        pass

    def upstream_attempt(self, *a, **k) -> None:
        pass

    def write_outcome(self, *a, **k) -> None:
        pass


# -- reading, for the admin page ------------------------------------------
def recent(db, limit: int = 200, kind_prefix: str = "", severity: str = "") -> list:
    where, params = [], []
    if kind_prefix:
        where.append("kind LIKE ?")
        params.append(kind_prefix + "%")
    if severity:
        where.append("severity = ?")
        params.append(severity)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return db.query(
        f"SELECT * FROM diagnostic_event{clause} ORDER BY event_id DESC LIMIT ?",
        (*params, max(1, min(1000, limit))))


def summary(db) -> dict:
    """Counts the dashboard leads with, so a quiet log needs no reading at all."""
    by_kind = {r["kind"]: r["n"] for r in db.query(
        "SELECT kind, count(*) n FROM diagnostic_event GROUP BY kind")}
    by_severity = {r["severity"]: r["n"] for r in db.query(
        "SELECT severity, count(*) n FROM diagnostic_event GROUP BY severity")}
    return {
        "by_kind": by_kind,
        "by_severity": by_severity,
        "total": sum(by_kind.values()),
        "errors": by_severity.get(ERROR, 0),
        "warnings": by_severity.get(WARNING, 0),
        "writes": sum(n for k, n in by_kind.items() if k.startswith("write.")),
        "indeterminate": by_kind.get(WRITE_LOST, 0) + by_kind.get(WRITE_MIRROR_FAILED, 0),
        "oldest": (db.query_one("SELECT min(at) a FROM diagnostic_event") or {"a": None})["a"],
        "newest": (db.query_one("SELECT max(at) a FROM diagnostic_event") or {"a": None})["a"],
    }
