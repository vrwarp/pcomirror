"""Serving API (DESIGN §8): a JSON:API drop-in over the mirror.

Reads are served from SQLite (where/order/include/pagination on projected columns);
writes are synchronous write-through to PCO (PCO-first, fail-if-it-fails); anything
non-mirrorable or a caller demanding freshness is pass-through. Implemented as a
plain WSGI app so it runs on the stdlib server and is trivially unit-testable.
"""
from __future__ import annotations

import json
import urllib.parse

from . import registry
from .config import now_iso

JSONAPI = "application/vnd.api+json"
_SEG = {r.endpoint.strip("/"): r for r in registry.RESOURCES.values()}


def _col_for(attr: str) -> str:
    return {"id": "pco_id", "created_at": "pco_created_at", "updated_at": "pco_updated_at"}.get(attr, attr)


class Application:
    def __init__(self, db, writer, ingestor, client, webhooks, settings):
        self.db, self.writer, self.ingestor = db, writer, ingestor
        self.client, self.webhooks, self.s = client, webhooks, settings

    # -- WSGI --------------------------------------------------------------
    def __call__(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "")
        qs = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        body = self._read_body(environ)
        try:
            status, headers, payload = self.route(method, path, qs, body, environ)
        except _HttpError as e:
            status, headers, payload = e.status, {}, {"errors": [{"code": str(e.status), "detail": e.detail}]}
        except Exception as e:  # noqa: BLE001
            status, headers, payload = 500, {}, {"errors": [{"code": "500", "detail": str(e)}]}
        raw = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
        base = {"Content-Type": JSONAPI, "Content-Length": str(len(raw))}
        base.update(headers)
        start_response(f"{status} ", [(k, v) for k, v in base.items()])
        return [raw]

    def _read_body(self, environ) -> bytes:
        try:
            n = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            n = 0
        return environ["wsgi.input"].read(n) if n else b""

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
                return self._passthrough(method, path, qs, body)
            raise _HttpError(404, f"unknown type {segs[0]}")

        if method == "GET":
            if len(segs) == 1:
                return self._collection(r, qs, environ)
            if len(segs) == 2:
                return self._single(r, segs[1], qs, environ)
            if len(segs) == 3:
                return self._nested(r, segs[1], segs[2], qs, environ)
            raise _HttpError(404, "bad path")
        if method in ("POST", "PATCH", "DELETE"):
            return self._write_through(method, path, r, segs, body)
        raise _HttpError(405, "method not allowed")

    # -- reads -------------------------------------------------------------
    def _collection(self, r, qs, environ):
        where_sql, params = self._build_where(r, qs)
        order_sql = self._build_order(r, qs)
        per_page = max(1, min(100, int(qs.get("per_page", ["25"])[0])))
        offset = max(0, int(qs.get("offset", ["0"])[0]))
        total = self.db.query_one(
            f"SELECT count(*) c FROM {r.table} WHERE deleted_at IS NULL{where_sql}", params)["c"]
        rows = self.db.query(
            f"SELECT * FROM {r.table} WHERE deleted_at IS NULL{where_sql} {order_sql} "
            f"LIMIT ? OFFSET ?", (*params, per_page, offset))
        data = [self._serialize(r, row) for row in rows]
        included = self._build_includes(r, rows, qs)
        oldest = min((row["last_synced_at"] for row in rows), default=None)
        meta = {"total_count": total, "count": len(data),
                "can_query_by": list(r.can_query_by), "can_order_by": list(r.can_order_by),
                "can_include": list(r.relationships.keys()),
                "mirror": {"source": "mirror", "oldest_last_synced_at": oldest}}
        links = {"self": environ.get("PATH_INFO", "")}
        if offset + per_page < total:
            links["next"] = f"{links['self']}?offset={offset + per_page}&per_page={per_page}"
        body = {"data": data, "meta": meta, "links": links}
        if included:
            body["included"] = included
        return 200, {"X-Mirror-Source": "mirror"}, body

    def _single(self, r, pco_id, qs, environ):
        row = self.db.query_one(f"SELECT * FROM {r.table} WHERE pco_id=?", (pco_id,))
        if row is None:
            if qs.get("passthrough", [""])[0] in ("1", "on", "auto") or qs.get("fallback", [""])[0] == "1":
                return self._passthrough("GET", environ.get("PATH_INFO"), qs, b"")
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
        data = self._serialize(r, row)
        included = self._build_includes(r, [row], qs)
        body = {"data": data}
        if included:
            body["included"] = included
        return 200, {"X-Mirror-Source": "mirror"}, body

    def _nested(self, r, pco_id, rel_name, qs, environ):
        rel = r.relationships.get(rel_name)
        if rel is None:
            raise _HttpError(400, f"unknown relationship {rel_name}")
        row = self.db.query_one(f"SELECT * FROM {r.table} WHERE pco_id=? AND deleted_at IS NULL", (pco_id,))
        if row is None:
            raise _HttpError(404, "not found")
        targets = self._related_rows(rel, [row])
        tr = registry.by_name(rel.target)
        data = [self._serialize(tr, x) for x in targets]
        return 200, {"X-Mirror-Source": "mirror"}, {"data": data, "meta": {"count": len(data)}}

    # -- writes: PCO-first write-through (DESIGN §8.4) ---------------------
    def _write_through(self, method, path, r, segs, body):
        pco_path = path[len("/people/v2"):]  # same path, host-swapped
        json_body = json.loads(body) if body else None
        resp = self.client.request(method, pco_path, json_body=json_body, priority="passthrough")
        if not resp.ok:  # FAIL IF IT FAILS — mirror untouched, relay PCO's status
            return resp.status, dict(resp.headers), resp.body or {"errors": [{"code": str(resp.status)}]}
        if method == "DELETE":
            if len(segs) >= 2:
                self.writer.tombstone(r.table, segs[1], None, "destroyed")
            return resp.status or 204, {}, b""
        out = resp.json() or {}
        for item in ([out["data"]] if isinstance(out.get("data"), dict) else out.get("data", [])):
            if item:
                self.writer.route(item, "passthrough")
        for inc in out.get("included", []) or []:
            self.writer.route(inc, "passthrough")
        return resp.status, dict(resp.headers), out

    # -- pass-through (non-mirrorable / miss / freshness) -----------------
    def _passthrough(self, method, path, qs, body):
        pco_path = path[len("/people/v2"):] if path.startswith("/people/v2") else path
        params = {k: v[0] for k, v in qs.items() if k not in ("passthrough", "fallback")}
        resp = self.client.request(method, pco_path, params=params or None,
                                   json_body=(json.loads(body) if body else None), priority="passthrough")
        out = resp.json() if resp.body else {}
        # read-through: warm the mirror for mirrorable results
        if resp.ok and isinstance(out, dict):
            for item in ([out.get("data")] if isinstance(out.get("data"), dict) else out.get("data", []) or []):
                if item and registry.by_type(item.get("type", "")):
                    self.writer.route(item, "passthrough")
        return resp.status, {"X-Mirror-Source": "passthrough"}, out or {"status": resp.status}

    # -- query building ----------------------------------------------------
    def _build_where(self, r, qs):
        clauses, params = [], []
        for key, vals in qs.items():
            if not key.startswith("where["):
                continue
            inner = key[len("where["):].rstrip("]")
            parts = inner.split("][")
            attr = parts[0]
            op = parts[1] if len(parts) > 1 else "eq"
            if attr not in r.can_query_by:
                raise _HttpError(400, f"unsupported filter: {attr}")
            col = _col_for(attr)
            val = vals[0]
            if op == "eq":
                if "%" in val:
                    clauses.append(f"{col} LIKE ?")
                    params.append(val)
                else:
                    clauses.append(f"lower({col})=lower(?)")
                    params.append(val)
            elif op in ("gt", "gte", "lt", "lte"):
                sym = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
                clauses.append(f"{col}{sym}?")
                params.append(val)
            else:
                raise _HttpError(400, f"unsupported operator: {op}")
        sql = (" AND " + " AND ".join(clauses)) if clauses else ""
        return sql, params

    def _build_order(self, r, qs):
        order = qs.get("order", [None])[0]
        if not order:
            return "ORDER BY pco_id"
        cols = []
        for tok in order.split(","):
            desc = tok.startswith("-")
            attr = tok.lstrip("-")
            if attr not in r.can_order_by:
                raise _HttpError(400, f"unsupported order: {attr}")
            cols.append(f"{_col_for(attr)} {'DESC' if desc else 'ASC'}")
        cols.append("pco_id")
        return "ORDER BY " + ", ".join(cols)

    # -- includes ----------------------------------------------------------
    def _build_includes(self, r, rows, qs):
        inc_param = qs.get("include", [None])[0]
        if not inc_param:
            return []
        out, seen = [], set()

        def add(res, row):
            key = (res.type, row["pco_id"])
            if key not in seen:
                seen.add(key)
                out.append(self._serialize(res, row))

        for token in inc_param.split(","):
            first, _, second = token.partition(".")
            rel = r.relationships.get(first)
            if rel is None:
                continue
            level1 = self._related_rows(rel, rows)
            tr = registry.by_name(rel.target)
            for x in level1:
                add(tr, x)
            if second:  # one nested level
                rel2 = tr.relationships.get(second)
                if rel2:
                    for y in self._related_rows(rel2, level1):
                        add(registry.by_name(rel2.target), y)
        return out

    def _related_rows(self, rel, rows):
        tr = registry.by_name(rel.target)
        ids = [row["pco_id"] for row in rows]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
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
    def _serialize(self, r, row):
        obj = json.loads(row["raw"])
        obj.setdefault("id", row["pco_id"])
        obj.setdefault("type", r.type)
        obj.setdefault("links", {})
        obj["links"]["self"] = f"/people/v2/{r.endpoint.strip('/')}/{row['pco_id']}"
        obj["meta"] = {"mirror": {"last_synced_at": row["last_synced_at"],
                                  "pco_updated_at": row["pco_updated_at"],
                                  "source": row["source"], "deleted_at": row["deleted_at"]}}
        return obj


class _HttpError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
