"""Every call to the webhook receiver, kept exactly as it arrived.

`webhook_delivery` already holds the bytes of a delivery the receiver
**accepted** — that is what re-verification and replay are made of. This is the
other half, and it exists because the deliveries worth diagnosing are almost
never the ones that got that far:

  * A **401** says a signature did not match. Which signature, over which bytes,
    sent to which token — none of that survived, so "Planning Center says it
    delivered and the mirror says nothing arrived" had no answer but a shrug.
  * A **404** says nobody here owns that token. Whether that is PCO delivering to
    a URL from a subscription somebody removed, or a scanner working through
    guesses, is the entire question — and the request that would settle it was
    dropped on the floor.
  * A **503** says the body would not parse or the capture failed. PCO will
    redeliver, forever, and until now the only record of what it kept sending was
    that it kept sending it.
  * **Headers were never kept at all.** `X-PCO-Webhooks-Authenticity` is the
    field the whole accept/reject decision is made of, and the mirror read it,
    compared it and threw it away.

**Verbatim, and that means verbatim.** No redaction, no pseudonyms, no dropped
headers, no reformatted body. This is the opposite policy to `diagnostics`, on
purpose: that log is a record of *what was asked of PCO* and is meant to be safe
to paste into an issue, so it keeps filter names and drops filter values. A
recording is a record of *what arrived here*, and a cleaned-up one cannot answer
the question it was kept for — whether the bytes the mirror hashed are the bytes
Planning Center signed is not a question you can ask of a summary, and a
signature over a re-serialized body is a signature over a different body.

So the cost is real, and it is stated on the page rather than engineered away:
these rows hold whole webhook payloads — names, addresses, phone numbers,
whatever the event carried — and the download hands them over as they are.
Treat the file like the database, because it is a copy of part of it.

Two bounds, neither of them a redaction:

  * **`MAX_BODY`** caps what is stored *per call*, because the receiver answers
    before it knows who is calling: anything that can reach the URL can post a
    gigabyte to it, and an unauthenticated endpoint that writes whatever it is
    handed straight to disk is a way to fill the disk. What is kept is the
    leading bytes, exactly; `body_bytes` records the true length and `truncated`
    says plainly that this is not all of it.
  * **`keep`** is a ring buffer over the whole table, for the same reason
    `diagnostic_event` has one. It has a consequence worth knowing before you
    need it: a flood of junk to an unknown token evicts real history. That is the
    price of recording the rejects — which is where the diagnostic value is — and
    the reason `keep` is a number the page can raise while something is being
    chased.
"""
from __future__ import annotations

import base64
import json

from .config import now_iso

#: The most of one body that is stored. Planning Center's deliveries are a few
#: kilobytes; this is large enough that a real one is never clipped and small
#: enough that filling `keep` rows with junk costs megabytes rather than the
#: disk.
MAX_BODY = 256 * 1024

#: The most an operator may ask to keep from the page. Every row holds a whole
#: request, so this is the setting that decides the log's size on disk: `keep ×
#: MAX_BODY` in the worst case somebody is deliberately causing, and `keep × a
#: few KB` for real Planning Center deliveries. The cap is where those two
#: readings stay tolerable — at 5,000 that is ~25 MB of real traffic and a bit
#: over a gigabyte of a determined flood, and the download has to fit in memory.
MAX_KEEP = 5_000

#: Where an operator's choice is kept. Absent means "whatever the environment
#: said", which is what a fresh install and a `docker run -e …` both expect.
OVERRIDE_KEY = "webhook_record_keep"


def effective(db, settings) -> dict:
    """How many calls are being kept, and who said so.

    The environment sets the default and the page overrides it; the override wins
    and persists, the same shape as the divergence rate and the CORS policy, and
    for the same reason. The person who wants the next delivery recorded is
    mid-investigation, and restarting the container to turn recording on loses
    the delivery they were waiting for.
    """
    default = max(0, getattr(settings, "webhook_record_keep", 0) or 0)
    held = db.get_meta(OVERRIDE_KEY)
    if held is None:
        return {"keep": default, "source": "environment", "default": default}
    try:
        chosen = max(0, min(MAX_KEEP, int(held)))
    except (TypeError, ValueError):
        return {"keep": default, "source": "environment", "default": default}
    return {"keep": chosen, "source": "admin", "default": default}


def configure(db, keep) -> dict:
    """Set the operator override, or clear it back to the environment default."""
    if keep is None:
        db.execute("DELETE FROM mirror_meta WHERE key=?", (OVERRIDE_KEY,))
    else:
        db.set_meta(OVERRIDE_KEY, str(max(0, min(MAX_KEEP, int(keep)))))
    return {"keep": keep}


def headers_of(environ) -> dict:
    """Every header the request carried, with nothing dropped and no value touched.

    Verbatim in the only sense still available at this layer. By the time a WSGI
    application can see a request the server has already upper-cased the header
    names, turned `-` into `_` and joined repeats of one name with commas, so the
    names come back in canonical case rather than the sender's. The *values* are
    exactly as they arrived — including `X-Pco-Webhooks-Authenticity`, which is
    the one field worth having, and `Authorization` if a sender ever puts one
    here, which is the one worth knowing is stored.
    """
    out = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:]
        elif key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            name = key
        else:
            continue
        out["-".join(part.capitalize() for part in name.split("_"))] = str(value)
    return dict(sorted(out.items()))


def _peek(raw: bytes) -> dict:
    """What the body says it is: delivery id, first event name, how many events.

    An index for whoever is reading the table, nothing more — the bytes beside it
    are the record. Best effort by construction: the calls most worth keeping are
    the ones whose bodies do not parse, and a row is worth keeping whether or not
    it can be labelled.
    """
    try:
        env = json.loads(raw)
        data = env.get("data") or []
        first = (data[0].get("attributes") or {}).get("name") if data else None
        return {"delivery_id": env.get("id"), "event_name": first, "event_count": len(data)}
    except Exception:  # noqa: BLE001 — any malformed body lands here, by design
        return {"delivery_id": None, "event_name": None, "event_count": 0}


class CallRecorder:
    """Writes recordings, and never lets writing one break a delivery.

    Every call is wrapped, for the reason `diagnostics.Recorder` is: a table that
    could fail a webhook would turn an observation into an outage, and PCO's
    answer to a 5xx is to send it again. A failure sets `last_failure` so the page
    can say the log is incomplete instead of quietly showing a short one.

    `keep` is read per call rather than held from startup, so turning recording on
    from the page takes effect on the next delivery — which is the delivery the
    operator is standing there waiting for.
    """

    def __init__(self, db, settings):
        self.db, self.s = db, settings
        self.last_failure: str | None = None

    def keep(self) -> int:
        return effective(self.db, self.s)["keep"]

    def record(self, environ, path: str, url_token: str, body: bytes,
               status: int | None, note: str, duration_ms: int | None = None) -> None:
        try:
            keep = self.keep()
            if not keep:
                return
            self._insert(environ, path, url_token, body or b"", status, note, duration_ms)
            self._trim(keep)
        except Exception as e:  # noqa: BLE001
            self.last_failure = f"{type(e).__name__}: {e}"

    def _insert(self, environ, path, url_token, body, status, note, duration_ms) -> None:
        stored = body[:MAX_BODY]
        # Labelled from the bytes that are *kept*, not from the whole body. A
        # clipped body is not valid JSON and simply goes unlabelled, which is the
        # right answer: an unknown token is answered before anything parses the
        # body, and parsing whatever an unauthenticated caller posted, at any size
        # it chooses, is work this must not be talked into doing.
        seen = _peek(stored)
        self.db.execute(
            """INSERT INTO webhook_call
                 (at, method, path, query, url_token, remote_addr, headers,
                  body, body_bytes, truncated, status, note, duration_ms,
                  delivery_id, event_name, event_count)
               VALUES (:at,:method,:path,:query,:token,:remote,:headers,
                       :body,:size,:truncated,:status,:note,:ms,
                       :delivery,:event,:count)""",
            {"at": now_iso(), "method": environ.get("REQUEST_METHOD", ""),
             "path": path, "query": environ.get("QUERY_STRING", "") or "",
             "token": url_token, "remote": environ.get("REMOTE_ADDR"),
             "headers": json.dumps(headers_of(environ)),
             "body": stored, "size": len(body),
             "truncated": 1 if len(body) > MAX_BODY else 0,
             "status": status, "note": note, "ms": duration_ms,
             "delivery": seen["delivery_id"], "event": seen["event_name"],
             "count": seen["event_count"]})

    def _trim(self, keep: int) -> None:
        self.db.execute(
            "DELETE FROM webhook_call WHERE call_id <= "
            "(SELECT max(call_id) FROM webhook_call) - ?", (keep,))


# -- reading, for the admin page and the download -------------------------
def _outcome_clause(outcome: str) -> str:
    """'rejected' is every call nothing was captured from — including the one
    whose status is NULL, which is a receiver that raised rather than answered."""
    if outcome == "rejected":
        return " WHERE status IS NULL OR status >= 400"
    if outcome == "accepted":
        return " WHERE status IS NOT NULL AND status < 400"
    return ""


def recent(db, limit: int = 100, outcome: str = "", body_limit: int | None = None) -> list:
    """Newest first, bounded — what a page can render. `export` is the whole log.

    `body_limit` clips the *selected* bytes, for a caller that is going to show a
    preview: a page of a hundred maximal bodies is twenty-five megabytes read out
    of the database to render two thousand characters of each. `body_bytes` still
    reports the true length, so a clipped read cannot make a call look smaller
    than it was.
    """
    body = "body" if body_limit is None else f"substr(body,1,{int(body_limit)}) AS body"
    return db.query(
        f"SELECT call_id, at, method, path, query, url_token, remote_addr, headers, "
        f"       {body}, body_bytes, truncated, status, note, duration_ms, "
        f"       delivery_id, event_name, event_count "
        f"FROM webhook_call{_outcome_clause(outcome)} ORDER BY call_id DESC LIMIT ?",
        (max(1, min(MAX_KEEP, limit)),))


def get(db, call_id):
    try:
        call_id = int(call_id)
    except (TypeError, ValueError):
        return None
    return db.query_one("SELECT * FROM webhook_call WHERE call_id=?", (call_id,))


def summary(db) -> dict:
    row = db.query_one(
        "SELECT count(*) total, "
        "       coalesce(sum(CASE WHEN status IS NULL OR status >= 400 THEN 1 ELSE 0 END),0) "
        "         rejected, "
        "       coalesce(sum(body_bytes),0) bytes, min(at) oldest, max(at) newest "
        "FROM webhook_call")
    by_status = {(r["status"] if r["status"] is not None else 0): r["n"] for r in db.query(
        "SELECT status, count(*) n FROM webhook_call GROUP BY status ORDER BY status")}
    return {"total": row["total"], "rejected": row["rejected"],
            "accepted": row["total"] - row["rejected"], "by_status": by_status,
            "bytes": row["bytes"], "oldest": row["oldest"], "newest": row["newest"]}


def body_of(row) -> bytes:
    """The stored bytes, as bytes — SQLite hands a BLOB back as `bytes` already,
    but a row read from a database written by an older sqlite3 may hand back a
    `memoryview`, and a caller writing this to a socket needs neither surprise."""
    return bytes(row["body"] or b"")


def as_document(row) -> dict:
    """One recording as something JSON can hold, losing nothing.

    A webhook body is JSON and therefore UTF-8, so it is carried as text and is
    readable in the file — that is the point of the download. A body that is
    *not* valid UTF-8 is by that fact interesting, so rather than replacing the
    bytes that broke it (which is exactly the sanitising this module refuses),
    the whole body goes into `body_base64` and `body` is left out.
    """
    body = body_of(row)
    out = {
        "call_id": row["call_id"], "at": row["at"], "method": row["method"],
        "path": row["path"], "query": row["query"], "url_token": row["url_token"],
        "remote_addr": row["remote_addr"],
        "headers": json.loads(row["headers"] or "{}"),
        "status": row["status"], "note": row["note"], "duration_ms": row["duration_ms"],
        "delivery_id": row["delivery_id"], "event_name": row["event_name"],
        "event_count": row["event_count"],
        "body_bytes": row["body_bytes"], "truncated": bool(row["truncated"]),
    }
    try:
        out["body"] = body.decode()
    except UnicodeDecodeError:
        out["body_base64"] = base64.b64encode(body).decode()
    return out


#: Said in the file itself, not only on the page that offered it. A support
#: bundle outlives the context it was downloaded in, and whoever opens it next
#: needs to know what they are holding before they forward it.
EXPORT_NOTE = (
    "Verbatim and unredacted: every header and the exact request body as "
    "received, including personal data. Treat this file like the database. "
    "Bodies are carried as text in `body`; a body that is not valid UTF-8 is "
    "carried whole in `body_base64` instead. `truncated` means only the first "
    f"{MAX_BODY} bytes were stored.")


def export(db, outcome: str = "") -> bytes:
    """The whole log as one JSON document, oldest first, for handing to somebody.

    Unbounded on purpose, unlike `recent`: a bundle that quietly stopped at the
    row a page happened to render is a bundle missing the delivery it was
    downloaded for. What bounds it is `keep`.
    """
    rows = db.query(
        f"SELECT * FROM webhook_call{_outcome_clause(outcome)} ORDER BY call_id ASC")
    return json.dumps({
        "exported_at": now_iso(),
        "note": EXPORT_NOTE,
        "calls": [as_document(r) for r in rows],
    }, indent=2).encode()


def clear(db) -> int:
    n = (db.query_one("SELECT count(*) c FROM webhook_call") or {"c": 0})["c"]
    db.execute("DELETE FROM webhook_call")
    return n
