"""Serving API (DESIGN §8): a JSON:API drop-in over the mirror.

Reads are served from SQLite (where/order/include/pagination on projected columns);
writes are synchronous write-through to PCO (PCO-first, fail-if-it-fails); anything
non-mirrorable or a caller demanding freshness is pass-through. Implemented as a
plain WSGI app so it runs on the stdlib server and is trivially unit-testable.
"""
from __future__ import annotations

import json
import urllib.parse

from . import apikeys, links, registry
from .admin import AdminApp, handles as admin_handles
from .config import now_iso
from .db import norm_digits, norm_text

JSONAPI = "application/vnd.api+json"
_SEG = {r.endpoint.strip("/"): r for r in registry.RESOURCES.values()}

# Query keys that are pagination, not filtering — stripped before a page link is
# rebuilt so `offset`/`per_page` are set once, by us.
_PAGING = ("offset", "per_page")

# Response headers that describe the hop they arrived on rather than the record,
# and so may never be relayed from PCO to our caller.
#
# `Content-Length` is the one that bites. Every document served is rewritten on
# the way out — absolute PCO URLs become mirror-relative paths, which makes the
# body *shorter* than the one PCO sent — so relaying PCO's length declared a
# response tens of bytes longer than the bytes that followed it. A client then
# blocks waiting for a remainder that never arrives, and eventually reports a
# dropped connection: a write that reached PCO looked, from the caller's side,
# exactly like one that never got there. `Transfer-Encoding` is worse in kind —
# framing this response does not use — and `Content-Encoding` describes a
# compression the transport has already undone.
_UNRELAYABLE = frozenset({
    "content-length", "transfer-encoding", "content-encoding", "connection",
    "keep-alive", "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization",
})

_BOOLS = {"true": 1, "t": 1, "1": 1, "yes": 1, "on": 1,
          "false": 0, "f": 0, "0": 0, "no": 0, "off": 0}

# PCO orders by id *numerically*. `pco_id` is TEXT, so a plain sort puts a
# nine-digit id before an eight-digit one and a whole page comes back in a
# different order than PCO would send — measured on `/emails`, where the two id
# lengths coexist and all 25 rows of page one differed. The trailing lexical sort
# only breaks ties between ids that are not numeric at all.
_ID_ORDER = "CAST(pco_id AS INTEGER), pco_id"

# How each `where[search_*]` arm matches. Verified against the live API, not
# assumed: the rules genuinely differ per arm.
_MATCHERS = {
    "words":         lambda expr: f"pcm_name_match({expr}, ?)",
    "contains":      lambda expr: f"instr(pcm_norm({expr}), ?) > 0",
    "digits_suffix": lambda expr: f"pcm_digits_suffix({expr}, ?)",
    "digits_exact":  lambda expr: f"pcm_digits_eq({expr}, ?)",
}


def _sparse_fields(qs) -> dict[str, set[str]]:
    """Parse `fields[Type]=a,b` into `{Type: {"a", "b"}}`.

    JSON:API sparse fieldsets, which PCO honours exactly: a named set limits both
    attributes *and* relationships for that type, applies to sideloaded resources
    by their own type, leaves `links` alone, and treats an unknown field name as
    simply selecting nothing rather than as an error.
    """
    out = {}
    for key, vals in qs.items():
        if not key.startswith("fields[") or not key.endswith("]"):
            continue
        rtype = key[len("fields["):-1]
        names = {n.strip() for v in vals for n in v.split(",") if n.strip()}
        out[rtype] = out.get(rtype, set()) | names
    return out


def _json_ids(raw: str, path: str) -> list[str]:
    """Resource ids out of a JSON:API relationship array stored on `raw`."""
    node = json.loads(raw)
    for key in path.lstrip("$.").split("."):
        if not isinstance(node, dict):
            return []
        node = node.get(key)
        if node is None:
            return []
    if isinstance(node, dict):
        node = [node]
    return [str(x["id"]) for x in node if isinstance(x, dict) and x.get("id")]


def _col_for(r, attr: str) -> str:
    if attr in r.col_aliases:
        return r.col_aliases[attr]
    return {"id": "pco_id", "created_at": "pco_created_at", "updated_at": "pco_updated_at"}.get(attr, attr)


def _coerce(r, col: str, val: str):
    """Match the value to the column's declared type.

    `child` is projected `INTEGER`, so SQLite stores JSON `true` as `1`. Comparing
    it as text against the literal string `true` — which is exactly what a PCO
    client sends — silently matched nothing, which is the worst way for a filter
    to fail.
    """
    sqltype = registry.col_type(r, col)
    if sqltype == "INTEGER":
        low = val.strip().lower()
        if low in _BOOLS:
            return _BOOLS[low]
        try:
            return int(low)
        except ValueError:
            raise _HttpError(400, f"{col} takes a boolean or integer, got {val!r}") from None
    if sqltype == "REAL":
        try:
            return float(val)
        except ValueError:
            raise _HttpError(400, f"{col} takes a number, got {val!r}") from None
    return val


class Application:
    # How many un-walked parents one read may fill inline. A single nested read
    # needs one; only a page-wide `include` of a walked collection can want more,
    # and a hundred serial upstream requests is not a response, it is a timeout.
    WALK_FILL_BUDGET = 25

    def __init__(self, db, writer, ingestor, client, webhooks, settings):
        self.db, self.writer, self.ingestor = db, writer, ingestor
        self.client, self.webhooks, self.s = client, webhooks, settings
        self.admin = AdminApp(db, settings)

    # -- WSGI --------------------------------------------------------------
    def __call__(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "")
        qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        body = self._read_body(environ)
        try:
            status, headers, payload = self.route(method, path, qs, body, environ)
        except _HttpError as e:
            # Both schemes are accepted (apikeys.bearer_token), so both are offered.
            headers = ({"WWW-Authenticate": 'Bearer realm="pcomirror", Basic realm="pcomirror"'}
                       if e.status == 401 else {})
            error = {"code": str(e.status), "detail": e.detail}
            if e.meta:
                error["meta"] = e.meta
            status, payload = e.status, {"errors": [error]}
        except Exception as e:  # noqa: BLE001
            status, headers, payload = 500, {}, {"errors": [{"code": "500", "detail": str(e)}]}
        raw = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
        base = {"Content-Type": JSONAPI}
        base.update(headers)
        # Set last and unconditionally, so no relayed header can win: it counts
        # the bytes this response is actually about to send, which is the one
        # number a caller cannot recover for itself if we get it wrong.
        base["Content-Length"] = str(len(raw))
        start_response(f"{status} ", [(k, v) for k, v in base.items()])
        return [raw]

    def _read_body(self, environ) -> bytes:
        try:
            n = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            n = 0
        return environ["wsgi.input"].read(n) if n else b""

    # -- auth (DESIGN §8.4: the local api_key plane) -----------------------
    def _authenticate(self, environ) -> set[str]:
        """Return the caller's scopes, or raise 401. `/healthz`, `/readyz` and the
        webhook receiver are exempt (the receiver carries its own HMAC auth)."""
        if self.s.allow_anonymous:
            return {"read:*", apikeys.SCOPE_WRITE, apikeys.SCOPE_PASSTHROUGH}
        row = apikeys.authenticate(self.db, environ.get("HTTP_AUTHORIZATION"))
        if row is None:
            if not apikeys.any_enabled(self.db):
                # Fail closed, but say why: an empty key table is a fresh install,
                # not an attack.
                raise _HttpError(401, "no API keys configured — run "
                                      "`pcomirror create-api-key --name <app>`")
            raise _HttpError(401, "missing or invalid API key")
        return apikeys.parse_scopes(row["scopes"])

    def _require(self, scopes: set[str], needed: str) -> None:
        if needed not in scopes:
            raise _HttpError(403, f"key lacks the {needed!r} scope")

    # -- routing -----------------------------------------------------------
    def route(self, method, path, qs, body, environ):
        if path == "/healthz":
            return 200, {}, {"status": "ok"}
        if path == "/readyz":
            self.db.query_one("SELECT 1")
            return 200, {}, {"status": "ready"}
        if path.startswith(self.s.webhook_path_prefix + "/"):
            token = path[len(self.s.webhook_path_prefix) + 1:]
            sig = environ.get("HTTP_X_PCO_WEBHOOKS_AUTHENTICITY")
            code, note = self.webhooks.receive(token, body, sig)
            return code, {}, {"status": note}
        # The operator page carries its own session auth, not an API key.
        if admin_handles(path):
            return self.admin.handle(method, path, qs, body, environ)

        scopes = self._authenticate(environ)

        prefix = "/people/v2/"
        if not path.startswith(prefix):
            raise _HttpError(404, "not found")
        segs = [s for s in path[len(prefix):].split("/") if s]
        if not segs:
            raise _HttpError(404, "no resource")
        r = _SEG.get(segs[0])
        if r is None:
            # unmirrored type -> pass-through
            if method == "GET":
                return self._passthrough(method, path, qs, body, scopes)
            raise _HttpError(404, f"unknown type {segs[0]}")

        if method == "GET":
            if not apikeys.allows_read(scopes, segs[0]):
                raise _HttpError(403, f"key lacks the 'read:{segs[0]}' scope")
            if len(segs) == 1:
                return self._collection(r, qs, environ)
            if len(segs) == 2:
                if r.parent:
                    # `/household_memberships/{id}` is a 404 upstream too: the row
                    # is only addressable through its household.
                    raise _HttpError(
                        404, f"a {r.type} is addressed under its "
                             f"{registry.by_name(r.parent).type}")
                return self._single(r, segs[1], qs, environ, scopes)
            if len(segs) == 3:
                return self._nested(r, segs[1], segs[2], qs, environ, scopes)
            if len(segs) in (4, 5):
                return self._nested_single(r, segs[1], segs[2], segs[3],
                                           segs[4] if len(segs) == 5 else None,
                                           qs, environ, scopes)
            raise _HttpError(404, "bad path")
        if method in ("POST", "PATCH", "DELETE"):
            self._require(scopes, apikeys.SCOPE_WRITE)
            return self._write_through(method, path, r, segs, body)
        raise _HttpError(405, "method not allowed")

    # -- reads -------------------------------------------------------------
    def _collection(self, r, qs, environ):
        if r.parent:
            # PCO does not expose this type at the top level — the rows only exist
            # under their parent, and `GET /household_memberships` is a 404 there.
            # Answering 200 would invent a collection the API being mirrored does
            # not have, which is the one thing a drop-in must never do.
            raise _HttpError(404, f"{r.type} is only served under a {registry.by_name(r.parent).type}")
        return self._list(r, qs, environ)

    # -- "unknown" is not "empty" ------------------------------------------
    def _ensure_walked(self, r, rel, rows):
        """Make a `nested_walk` child collection knowable before serving it.

        A child PCO only exposes one parent at a time is mirrored by a periodic
        walk, so between a parent appearing and the walk reaching it the mirror
        holds no rows for that parent. Those rows are not absent — they are
        unknown, and an empty page says the opposite. Fill the gap for exactly
        the parents this read touches (one upstream request each, once ever),
        and refuse the read if that cannot be done.
        """
        tr = registry.by_name(rel.target)
        if tr.method != "nested_walk" or rel.kind != "many":
            return
        walk_parent = registry.by_name(tr.parent)
        budget = self.WALK_FILL_BUDGET
        for row in rows:
            if r.name == tr.parent:
                parent_ids = [row["pco_id"]]
            else:
                # Reached from the other side — a person's memberships hang off
                # that person's households. Walk each of them.
                hop = next((v for v in r.relationships.values()
                            if v.target == tr.parent), None)
                if hop is None:
                    return
                parent_ids = self._rel_target_ids(hop, row)
            for parent_id in parent_ids:
                if self.ingestor.parent_walked(tr.name, parent_id):
                    continue
                if not self.db.query_one(
                        f"SELECT 1 FROM {walk_parent.table} WHERE pco_id=? AND deleted_at IS NULL",
                        (parent_id,)):
                    continue  # the parent itself is not mirrored; nothing to walk
                if budget <= 0:
                    # A page-wide include of an un-walked collection would spend a
                    # request per row and hold the response open for all of them.
                    # Say the collection is not ready instead — which is true, and
                    # is not the same sentence as "these households are empty".
                    raise _HttpError(
                        503, f"{tr.type} has not been walked for every "
                             f"{walk_parent.type} on this page yet — retry once the "
                             f"walk has run, or read one {walk_parent.type} at a time")
                budget -= 1
                try:
                    self.ingestor.ensure_parent_walked(tr.name, parent_id)
                except Exception as e:  # noqa: BLE001
                    raise _HttpError(
                        503, f"{tr.type} for {walk_parent.type} {parent_id} is not mirrored yet "
                             f"and could not be fetched: {e}") from e

    def _rel_target_ids(self, rel, row) -> list[str]:
        """The ids on the far side of `rel` for one row, without serializing it."""
        if rel.kind == "json":
            return _json_ids(row["raw"], rel.json_path)
        if rel.kind == "one":
            return [row[rel.local_fk]] if row[rel.local_fk] else []
        tr = registry.by_name(rel.target)
        return [x["pco_id"] for x in self.db.query(
            f"SELECT pco_id FROM {tr.table} WHERE {rel.child_fk}=? AND deleted_at IS NULL",
            (row["pco_id"],))]

    def _org_parent(self):
        """`meta.parent` for a top-level collection: the organization, as PCO
        reports it. Learned from PCO's own responses during ingest."""
        org = self.db.get_meta("organization_id")
        return {"type": "Organization", "id": org} if org else None

    def _list(self, r, qs, environ, extra_sql: str = "", extra_params=(), parent=None):
        """One paged, filtered, ordered, included collection read.

        Shared by `/people` and by the nested `/households/{id}/household_memberships`
        form, which PCO serves with the identical query surface — `extra_sql` is
        just the relationship restriction.
        """
        where_sql, params = self._build_where(r, qs)
        where_sql = extra_sql + where_sql
        params = [*extra_params, *params]
        order_sql = self._build_order(r, qs)
        # PCO honours `per_page=0` as "no rows, but tell me the total", so the
        # floor is zero rather than one.
        per_page = max(0, min(100, self._int(qs, "per_page", 25)))
        offset = max(0, self._int(qs, "offset", 0))
        total = self.db.query_one(
            f"SELECT count(*) c FROM {r.table} WHERE deleted_at IS NULL{where_sql}", params)["c"]
        rows = self.db.query(
            f"SELECT * FROM {r.table} WHERE deleted_at IS NULL{where_sql} {order_sql} "
            f"LIMIT ? OFFSET ?", (*params, per_page, offset))
        fields = _sparse_fields(qs)
        included, echo = self._build_includes(r, rows, qs)
        data = [self._serialize(r, row, echo.get(row["pco_id"]), fields) for row in rows]
        oldest = min((row["last_synced_at"] for row in rows), default=None)
        meta = {"total_count": total, "count": len(data),
                "can_query_by": list(r.can_query_by), "can_order_by": list(r.can_order_by),
                "can_include": list(r.relationships.keys()),
                "can_search_by": list(r.search_filters.keys()),
                # `filter=` is not implemented, so the honest advertisement is an
                # empty list — PCO always sends the key, and claiming filters we do
                # not honour would be worse than claiming none.
                "can_filter": [],
                "parent": parent or self._org_parent(),
                "mirror": {"source": "mirror", "oldest_last_synced_at": oldest}}
        # PCO reports the cursor in `meta` as well as in `links`; clients pick
        # whichever they find, so serve both rather than half of the contract.
        if offset + per_page < total:
            meta["next"] = {"offset": offset + per_page}
        if offset > 0:
            meta["prev"] = {"offset": max(0, offset - per_page)}
        body = {"data": data, "meta": meta,
                "links": self._page_links(environ, qs, offset, per_page, total)}
        if included:
            body["included"] = included
        return 200, {"X-Mirror-Source": "mirror"}, body

    def _int(self, qs, key, default):
        raw = qs.get(key, [""])[0]
        try:
            return int(raw) if raw != "" else default
        except ValueError:
            raise _HttpError(400, f"{key} must be an integer, got {raw!r}") from None

    def _page_links(self, environ, qs, offset, per_page, total):
        """Page links that carry the caller's whole query.

        The previous form emitted a bare `?offset=…&per_page=…`, which dropped
        `where[…]`, `order` and `include` — so following `links.next` silently
        switched to an unfiltered, unordered, un-included page. Records were
        duplicated and skipped with nothing to signal it. A page link is only
        meaningful as *the same query, further along*.
        """
        path = environ.get("PATH_INFO", "") or ""
        keep = [(k, v) for k, vals in qs.items() if k not in _PAGING for v in vals]

        def url(off):
            pairs = [*keep, ("offset", str(off)), ("per_page", str(per_page))]
            # Brackets left literal: PCO accepts either form and `where[grade]` is
            # far easier to read in a log than `where%5Bgrade%5D`.
            return f"{path}?{urllib.parse.urlencode(pairs, safe='[]')}"

        out = {"self": url(offset)}
        if offset + per_page < total:
            out["next"] = url(offset + per_page)
        if offset > 0:
            out["prev"] = url(max(0, offset - per_page))
        return out

    def _single(self, r, pco_id, qs, environ, scopes=None):
        row = self.db.query_one(f"SELECT * FROM {r.table} WHERE pco_id=?", (pco_id,))
        if row is None:
            if qs.get("passthrough", [""])[0] in ("1", "on", "auto") or qs.get("fallback", [""])[0] == "1":
                return self._passthrough("GET", environ.get("PATH_INFO"), qs, b"", scopes)
            raise _HttpError(404, "not found")
        if row["deleted_at"] is not None:
            headers = {}
            body = {"errors": [{"code": "410", "detail": "resource deleted",
                                "meta": {"deleted_at": row["deleted_at"],
                                         "tombstone_reason": row["tombstone_reason"]}}]}
            if row["merged_into_pco_id"]:
                body["errors"][0]["meta"]["merged_into"] = row["merged_into_pco_id"]
                headers["Location"] = f"/people/v2/{r.endpoint.strip('/')}/{row['merged_into_pco_id']}"
            return 410, headers, body
        included, echo = self._build_includes(r, [row], qs)
        data = self._serialize(r, row, echo.get(row["pco_id"]), _sparse_fields(qs))
        body = {"data": data,
                "meta": {"can_include": list(r.relationships.keys()),
                         "parent": self._org_parent()}}
        if included:
            body["included"] = included
        return 200, {"X-Mirror-Source": "mirror"}, body

    def _nested(self, r, pco_id, rel_name, qs, environ, scopes=None):
        rel = r.relationships.get(rel_name)
        if rel is None:
            # PCO exposes relationships the mirror does not cover (notes,
            # workflow_cards, …). The generated link map does not advertise them,
            # but a caller porting PCO URLs will still ask — resolve it against PCO
            # rather than 400, so no mirror path is a dead end.
            return self._passthrough("GET", environ.get("PATH_INFO"), qs, b"", scopes)
        row = self.db.query_one(f"SELECT * FROM {r.table} WHERE pco_id=? AND deleted_at IS NULL", (pco_id,))
        if row is None:
            raise _HttpError(404, "not found")
        tr = registry.by_name(rel.target)
        if rel.kind == "one":
            # A to-one relationship is a resource, not a collection: PCO answers
            # `/field_data/{id}/field_definition` with an object, and 404s when the
            # foreign key is unset rather than handing back an empty page.
            target_id = row[rel.local_fk]
            target = self.db.query_one(
                f"SELECT * FROM {tr.table} WHERE pco_id=? AND deleted_at IS NULL",
                (target_id,)) if target_id else None
            if target is None:
                raise _HttpError(404, "not found")
            return self._single(tr, target["pco_id"], qs, environ, scopes)
        self._ensure_walked(r, rel, [row])
        restriction, params = self._rel_restriction(rel, row)
        # PCO gives a nested collection the same query surface as the top-level
        # one, so serve it the same way: where/order/include/pagination all work.
        # Its `meta.parent` is the record it hangs off, not the organization.
        return self._list(tr, qs, environ, restriction, params,
                          parent={"type": r.type, "id": pco_id})

    def _nested_single(self, r, pco_id, rel_name, child_id, tail, qs, environ, scopes=None):
        """One record addressed through its parent — `/households/{h}/household_memberships/{m}`,
        and its own relationships one segment further, which PCO also serves.

        The only way to read a membership at PCO, and therefore the link the mirror
        publishes for one; publishing a link the router then refuses would be its
        own bug. The child must genuinely belong to this parent, or the path is a
        404 rather than a redirect to the same row under another parent.
        """
        rel = r.relationships.get(rel_name)
        if rel is None or rel.kind != "many":
            return self._passthrough("GET", environ.get("PATH_INFO"), qs, b"", scopes)
        parent_row = self.db.query_one(
            f"SELECT * FROM {r.table} WHERE pco_id=? AND deleted_at IS NULL", (pco_id,))
        if parent_row is None:
            raise _HttpError(404, "not found")
        tr = registry.by_name(rel.target)
        self._ensure_walked(r, rel, [parent_row])
        restriction, params = self._rel_restriction(rel, parent_row)
        row = self.db.query_one(
            f"SELECT * FROM {tr.table} WHERE pco_id=? {restriction}", (child_id, *params))
        if row is None:
            raise _HttpError(404, "not found")
        if tail:
            return self._nested(tr, child_id, tail, qs, environ, scopes)
        return self._single(tr, child_id, qs, environ, scopes)

    def _rel_restriction(self, rel, row):
        """The SQL restricting a relationship's target table to `row`'s side of it."""
        if rel.kind == "json":
            ids = _json_ids(row["raw"], rel.json_path)
            if not ids:
                return " AND 0", ()
            return f" AND pco_id IN ({','.join('?' * len(ids))})", tuple(ids)
        if rel.kind == "one":
            target_id = row[rel.local_fk]
            # An unset foreign key is an empty collection, not an error — and it
            # still goes through the normal read so the page shape stays identical.
            return (" AND pco_id=?", (target_id,)) if target_id else (" AND 0", ())
        if rel.via:
            join = registry.by_name(rel.via)
            return (f" AND pco_id IN (SELECT {rel.via_target_fk} FROM {join.table} "
                    f"WHERE {rel.via_local_fk}=? AND deleted_at IS NULL)", (row["pco_id"],))
        return f" AND {rel.child_fk}=?", (row["pco_id"],)

    # -- writes: PCO-first write-through (DESIGN §8.4) ---------------------
    def _relay_headers(self, headers) -> dict:
        """PCO's response headers, minus the ones that describe its hop not ours.

        `Location` is rewritten rather than dropped: a 201 that points at
        `api.planningcenteronline.com` hands the caller a URL their mirror key
        cannot open, which is the one thing a base-URL swap must never do.
        """
        out = {}
        for k, v in (headers or {}).items():
            if k.lower() in _UNRELAYABLE:
                continue
            out[k] = links.to_mirror_path(v, self.s) if k.lower() == "location" else v
        return out

    def _write_through(self, method, path, r, segs, body):
        pco_path = path[len("/people/v2"):]  # same path, host-swapped
        json_body = json.loads(body) if body else None
        try:
            resp = self.client.request(method, pco_path, json_body=json_body, priority="passthrough")
        except Exception as e:  # noqa: BLE001
            # The write never came back. It is *not* known to have failed — the
            # client deliberately refuses to replay a mutation whose response was
            # lost, because that is indistinguishable from one that never left,
            # and replaying it creates a second record on somebody's real file.
            #
            # Reported here rather than left to the blanket handler above, which
            # answered a bare 500: to a caller that retries 5xx — which is every
            # sensible HTTP client — that status is an instruction to do exactly
            # the thing this client just refused to do, and each attempt landed
            # another copy. 504 is the status §8.4 specifies for this, and the
            # marker carries the part no status code can: nobody knows whether it
            # applied, so the resolution is to go and look, not to send it again.
            raise _HttpError(
                504, f"the {method} reached Planning Center but its response was lost, so it "
                     f"may or may not have been applied — check upstream before retrying: {e}",
                meta={"code": "upstream_response_lost",
                      "write_indeterminate": True, "safe_to_retry": False},
            ) from e
        if not resp.ok:  # FAIL IF IT FAILS — mirror untouched, relay PCO's status
            return (resp.status, self._relay_headers(resp.headers),
                    resp.body or {"errors": [{"code": str(resp.status)}]})
        if method == "DELETE":
            if len(segs) >= 2:
                self.writer.tombstone(r.table, segs[1], None, "destroyed")
            return resp.status or 204, {}, b""
        out = resp.json() or {}
        self.writer.route_page(out, "passthrough")
        # Store PCO's payload verbatim, but hand the caller mirror-relative links.
        return resp.status, self._relay_headers(resp.headers), links.rewrite_document(out, self.s)

    # -- pass-through (non-mirrorable / miss / freshness) -----------------
    def _passthrough(self, method, path, qs, body, scopes=None):
        # Spending the server's PCO credential is a distinct privilege from
        # reading the mirror, so it needs its own scope. `scopes=None` means an
        # internal caller that has already been authorised.
        if scopes is not None:
            self._require(scopes, apikeys.SCOPE_PASSTHROUGH)
        pco_path = path[len("/people/v2"):] if path.startswith("/people/v2") else path
        params = {k: v[0] for k, v in qs.items() if k not in ("passthrough", "fallback")}
        resp = self.client.request(method, pco_path, params=params or None,
                                   json_body=(json.loads(body) if body else None), priority="passthrough")
        out = resp.json() if resp.body else {}
        # read-through: warm the mirror for mirrorable results
        if resp.ok and isinstance(out, dict):
            self.writer.route_page(
                out, "passthrough",
                synthesized=self.writer.synthesized_rels(qs.get("include", [None])[0]))
        # Warm the mirror from PCO's payload above, then rewrite for the caller —
        # a proxied response must not hand back URLs needing a PCO credential.
        return (resp.status, {"X-Mirror-Source": "passthrough"},
                links.rewrite_document(out, self.s) or {"status": resp.status})

    # -- query building ----------------------------------------------------
    def _build_where(self, r, qs):
        clauses, params = [], []
        for key, vals in qs.items():
            if not key.startswith("where["):
                continue
            inner = key[len("where["):].rstrip("]")
            parts = inner.split("][")
            val = vals[0]

            search = r.search_filters.get(parts[0])
            if search is not None:
                if len(parts) > 1:
                    raise _HttpError(400, f"{parts[0]} is a search filter and takes no "
                                          f"{parts[1]!r} operator")
                clause, ps = self._search_clause(r, search, val)
                if clause:                      # an empty needle filters nothing, as at PCO
                    clauses.append(clause)
                    params.extend(ps)
                continue

            # `where[emails][address]`, `where[field_data][field_definition][name]`:
            # PCO lets a filter reach through a relationship, to any depth its
            # documentation lists. Everything up to the attribute is a chain of
            # relationship names.
            chain = []
            target = r
            while parts and parts[0] in target.relationships:
                chain.append(parts[0])
                target = registry.by_name(target.relationships[parts[0]].target)
                parts = parts[1:]
            if chain:
                if not parts:
                    raise _HttpError(400, f"{'.'.join(chain)} needs an attribute to filter on")
                clause, ps = self._rel_exists(r, r.table, chain, parts, val)
                clauses.append(clause)
                params.extend(ps)
                continue

            clause, ps = self._leaf_where(r, r.table, parts, val)
            clauses.append(clause)
            params.extend(ps)
        sql = (" AND " + " AND ".join(clauses)) if clauses else ""
        return sql, params

    def _leaf_where(self, r, ref, parts, val):
        """One `attr` / `attr][op` comparison against `ref`'s table."""
        attr = parts[0]
        op = parts[1] if len(parts) > 1 else "eq"
        if attr not in r.can_query_by:
            raise _HttpError(400, f"unsupported filter: {attr}")
        col = f"{ref}.{_col_for(r, attr)}"
        bare = _col_for(r, attr)
        if op == "eq":
            if isinstance(val, str) and "%" in val:
                return f"{col} LIKE ?", [val]
            coerced = _coerce(r, bare, val)
            if isinstance(coerced, str):
                return f"lower({col})=lower(?)", [coerced]
            # Numeric columns are compared through a CAST so the answer does not
            # depend on whether this database was created before the column was
            # declared numeric — SQLite would otherwise compare 8 against '8' and
            # find them unequal.
            return f"CAST({col} AS INTEGER)=?", [coerced]
        if op in ("gt", "gte", "lt", "lte"):
            sym = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            coerced = _coerce(r, bare, val)
            if isinstance(coerced, str):
                return f"{col}{sym}?", [coerced]
            return f"CAST({col} AS INTEGER){sym}?", [coerced]
        raise _HttpError(400, f"unsupported operator: {op}")

    def _rel_exists(self, r, ref, chain, parts, val):
        """`EXISTS` chain for a filter that reaches through relationships.

        Built outside-in, one correlated subquery per hop, so
        `where[field_data][field_definition][name]` becomes an EXISTS over this
        person's field data containing an EXISTS over that datum's definition.
        """
        rel = r.relationships[chain[0]]
        tr = registry.by_name(rel.target)
        alias = f"n{len(chain)}_{abs(hash(chain[0])) % 1000}"
        frm, cond, params = self._rel_hop(rel, ref, tr, alias)
        if len(chain) > 1:
            inner, ps = self._rel_exists(tr, alias, chain[1:], parts, val)
        else:
            inner, ps = self._leaf_where(tr, alias, parts, val)
        return f"EXISTS (SELECT 1 FROM {frm} WHERE {cond} AND {inner})", [*params, *ps]

    def _rel_hop(self, rel, ref, tr, alias):
        """One hop: how the target table is joined back to `ref`."""
        live = f"{alias}.deleted_at IS NULL"
        if rel.kind == "one":
            return f"{tr.table} {alias}", f"{alias}.pco_id={ref}.{rel.local_fk} AND {live}", []
        if rel.kind == "json":
            return (f"json_each({ref}.raw, ?) j_{alias} JOIN {tr.table} {alias} "
                    f"ON {alias}.pco_id = j_{alias}.value ->> '$.id'", live, [rel.json_path])
        if rel.via:
            join = registry.by_name(rel.via)
            return (f"{tr.table} {alias}",
                    f"{live} AND {alias}.pco_id IN (SELECT {rel.via_target_fk} FROM {join.table} "
                    f"WHERE {rel.via_local_fk}={ref}.pco_id AND deleted_at IS NULL)", [])
        return f"{tr.table} {alias}", f"{alias}.{rel.child_fk}={ref}.pco_id AND {live}", []

    def _search_clause(self, r, search, needle: str):
        """PCO's `where[search_*]`, one arm at a time.

        The arms do not share a rule, and the differences are not cosmetic. Names
        match by word-prefix — `byron` and `ada by` find Ada Byron, `yron` does
        not. Email addresses match by substring. Phone numbers match on a digits
        *suffix*, which is how a person searches for one. Treating all three as
        "contains" returned a hundred people where PCO returned nine.
        """
        terms, params = [], []
        text = norm_text(needle)
        if text:
            for expr in search.names:
                terms.append(_MATCHERS["words"](expr))
                params.append(text)
        for target, child_fk, col, mode in search.children:
            probe = norm_digits(needle) if mode.startswith("digits") else text
            if not probe:       # a name typed into a phone search matches no digits
                continue
            tr = registry.by_name(target)
            inner = _MATCHERS[mode](f"c.{col}")
            terms.append(
                f"EXISTS (SELECT 1 FROM {tr.table} c WHERE c.{child_fk}={r.table}.pco_id "
                f"AND c.deleted_at IS NULL AND {inner})")
            params.append(needle if mode.startswith("digits") else probe)
        if terms:
            return "(" + " OR ".join(terms) + ")", params
        # Two very different empty cases. A blank value is no filter at all, which
        # is how PCO reads it. A value that *is* something but normalises away for
        # every haystack the filter covers — a name typed into a phone-number
        # search — matches nobody; falling back to "no filter" would answer that
        # query with the entire church.
        return ("", []) if not text else ("(0)", [])

    def _build_order(self, r, qs):
        order = qs.get("order", [None])[0]
        if not order:
            return f"ORDER BY {_ID_ORDER}"
        cols = []
        for tok in order.split(","):
            desc = tok.startswith("-")
            attr = tok.lstrip("-")
            if attr not in r.can_order_by:
                raise _HttpError(400, f"unsupported order: {attr}")
            col = _col_for(r, attr)
            if registry.col_type(r, col) in ("INTEGER", "REAL"):
                # `grade` holds numbers. Sorted as text, 9 comes after 12 — so
                # `order=-grade` opened on the ninth graders instead of the twelfth.
                cols.append(f"CAST({col} AS INTEGER) {'DESC' if desc else 'ASC'}")
            else:
                # PCO sorts names case-insensitively. SQLite's default BINARY
                # collation puts every capital ahead of every lowercase letter, so a
                # surname entered in lower case jumped to the end of the roster.
                cols.append(f"{col} COLLATE NOCASE {'DESC' if desc else 'ASC'}")
        cols.append(_ID_ORDER)
        return "ORDER BY " + ", ".join(cols)

    # -- includes ----------------------------------------------------------
    def _build_includes(self, r, rows, qs):
        """Resolve `include=` into `included[]`, plus the relationship echo PCO adds.

        Returns `(included, echo)`. `echo` maps a primary row's id to the extra
        relationship PCO synthesizes for a *nested* include: asking for
        `include=households.people` makes PCO add a `people` relationship to the
        Person itself, listing the ids it sideloaded. It is derived from the rows
        already resolved here, so echoing it costs nothing.
        """
        inc_param = qs.get("include", [None])[0]
        if not inc_param:
            return [], {}
        out, seen, echo = [], set(), {}
        fields = _sparse_fields(qs)

        def add(res, row):
            key = (res.type, row["pco_id"])
            if key not in seen:
                seen.add(key)
                out.append(self._serialize(res, row, None, fields))

        for token in inc_param.split(","):
            first, _, second = token.partition(".")
            rel = r.relationships.get(first)
            if rel is None:
                # PCO offers includes for types the mirror does not hold (`school`,
                # `social_profiles`, …). Answering 200 with no `included` would look
                # exactly like "this person has none", so say so instead.
                raise _HttpError(400, f"cannot include {first!r}: not mirrored")
            self._ensure_walked(r, rel, rows)
            level1 = self._related_rows(rel, rows)
            tr = registry.by_name(rel.target)
            for x in level1:
                add(tr, x)
            if not second:
                continue
            rel2 = tr.relationships.get(second)
            if rel2 is None:
                continue
            self._ensure_walked(tr, rel2, level1)
            tr2 = registry.by_name(rel2.target)
            for y in self._related_rows(rel2, level1):
                add(tr2, y)
            # Per row, so the echo says what *this* record is related to rather
            # than what the whole page is.
            for row in rows:
                own1 = self._related_rows(rel, [row])
                ids = [{"type": tr2.type, "id": y["pco_id"]}
                       for y in self._related_rows(rel2, own1)]
                if ids:
                    echo.setdefault(row["pco_id"], {})[second] = ids
        return out, echo

    def _related_rows(self, rel, rows):
        tr = registry.by_name(rel.target)
        ids = [row["pco_id"] for row in rows]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        if rel.kind == "json":
            # PCO puts a person's households inline on the Person payload and has
            # no bulk endpoint for the join rows, so the array on `raw` is the edge.
            targets = sorted({t for row in rows for t in _json_ids(row["raw"], rel.json_path)})
            if not targets:
                return []
            ph2 = ",".join("?" * len(targets))
            return self.db.query(
                f"SELECT * FROM {tr.table} WHERE pco_id IN ({ph2}) AND deleted_at IS NULL", targets)
        if rel.kind == "many" and rel.via is None:
            return self.db.query(
                f"SELECT * FROM {tr.table} WHERE {rel.child_fk} IN ({ph}) AND deleted_at IS NULL", ids)
        if rel.kind == "one":
            fkvals = [row[rel.local_fk] for row in rows if row[rel.local_fk]]
            if not fkvals:
                return []
            ph2 = ",".join("?" * len(fkvals))
            return self.db.query(
                f"SELECT * FROM {tr.table} WHERE pco_id IN ({ph2}) AND deleted_at IS NULL", fkvals)
        if rel.via:  # many-to-many via a join table
            join = registry.by_name(rel.via)
            links = self.db.query(
                f"SELECT {rel.via_target_fk} tid FROM {join.table} "
                f"WHERE {rel.via_local_fk} IN ({ph}) AND deleted_at IS NULL", ids)
            tids = [x["tid"] for x in links if x["tid"]]
            if not tids:
                return []
            ph3 = ",".join("?" * len(tids))
            return self.db.query(
                f"SELECT * FROM {tr.table} WHERE pco_id IN ({ph3}) AND deleted_at IS NULL", tids)
        return []

    # -- serialization -----------------------------------------------------
    def _serialize(self, r, row, echo=None, fields=None):
        obj = json.loads(row["raw"])
        obj.setdefault("id", row["pco_id"])
        obj.setdefault("type", r.type)
        # Generated from the registry rather than echoed from `raw`: PCO returns a
        # different link map for a list page than for a single fetch, which made a
        # record's shape depend on how it was synced.
        obj["links"] = links.link_map(r, row["pco_id"], (obj.get("links") or {}).get("html"),
                                      row[r.parent_fk] if r.parent_fk else None)
        for name, ids in (echo or {}).items():
            # PCO's own shape for a nested include; `related` is null there too.
            obj.setdefault("relationships", {})[name] = {"links": {"related": None}, "data": ids}
        links.rewrite_relationships(obj, self.s)
        keep = (fields or {}).get(r.type)
        if keep is not None:
            # Relationships are fields too, so one that was not named goes as well —
            # even one PCO synthesized for a nested include.
            obj["attributes"] = {k: v for k, v in (obj.get("attributes") or {}).items()
                                 if k in keep}
            rels = {k: v for k, v in (obj.get("relationships") or {}).items() if k in keep}
            if rels or "relationships" in obj:
                obj["relationships"] = rels
        obj["meta"] = {"mirror": {"last_synced_at": row["last_synced_at"],
                                  "pco_updated_at": row["pco_updated_at"],
                                  "source": row["source"], "deleted_at": row["deleted_at"]}}
        return obj


class _HttpError(Exception):
    def __init__(self, status, detail, meta: dict | None = None):
        self.status, self.detail, self.meta = status, detail, meta
