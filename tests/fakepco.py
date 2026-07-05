"""An in-process fake Planning Center People API — a Transport for PcoClient.

Implements just enough of the JSON:API surface the mirror uses: collection GET
with order / per_page / offset / where[updated_at|created_at][op] / include /
fields[Person], single GET, /person_mergers, and POST/PATCH/DELETE for
write-through. Lets the whole system be tested end-to-end with zero network.
"""
from __future__ import annotations

import itertools
import json
import urllib.parse

from pcomirror import registry
from pcomirror.pcoclient import Response

_SEG_TO_TYPE = {r.endpoint.strip("/"): r.type for r in registry.RESOURCES.values()}


def res(rtype, rid, attributes, relationships=None, created="2020-01-01T00:00:00Z", updated=None):
    a = dict(attributes)
    a.setdefault("created_at", created)
    if "updated_at" not in a:
        a["updated_at"] = updated or created
    return {"id": str(rid), "type": rtype, "attributes": a, "relationships": relationships or {}, "links": {}}


class FakePCO:
    def __init__(self):
        self.data: dict[str, dict[str, dict]] = {}   # type -> {id -> resource}
        self.mergers: list[dict] = []
        self._ids = itertools.count(1000)
        self.fail_next = None                         # (status, detail) to force a write failure
        self.request_log: list[tuple[str, str]] = []

    # -- population --------------------------------------------------------
    def add(self, resource: dict):
        self.data.setdefault(resource["type"], {})[resource["id"]] = resource
        return resource

    def add_person(self, pid, first, last, updated, status="active", **rels):
        return self.add(res("Person", pid, {"first_name": first, "last_name": last, "status": status,
                                             "name": f"{first} {last}"},
                            relationships=rels, updated=updated))

    def add_child(self, rtype, cid, person_id, attrs, updated):
        return self.add(res(rtype, cid, attrs,
                            relationships={"person": {"data": {"type": "Person", "id": str(person_id)}}},
                            updated=updated))

    def merge(self, keep, remove, created):
        self.mergers.append(res("PersonMerger", f"m{keep}{remove}",
                                {"person_to_keep_id": str(keep), "person_to_remove_id": str(remove)},
                                created=created))
        self.data.get("Person", {}).pop(str(remove), None)  # merged-away id disappears from listings

    def destroy(self, rtype, rid):
        self.data.get(rtype, {}).pop(str(rid), None)

    # -- transport ---------------------------------------------------------
    def send(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        i = path.find("/people/v2")
        sub = path[i + len("/people/v2"):] if i >= 0 else path
        qs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        self.request_log.append((method, sub))
        segs = [s for s in sub.split("/") if s]
        if method == "GET":
            if segs and segs[0] == "person_mergers":
                return self._mergers(qs)
            if len(segs) == 1:
                return self._collection(segs[0], qs)
            if len(segs) == 2:
                return self._single(segs[0], segs[1], qs)
        if method in ("POST", "PATCH", "DELETE"):
            return self._write(method, segs, body)
        return Response(404, {}, json.dumps({"errors": [{"code": "404"}]}).encode())

    # -- handlers ----------------------------------------------------------
    def _type_of(self, seg):
        return _SEG_TO_TYPE.get(seg)

    def _collection(self, seg, qs):
        rtype = self._type_of(seg)
        if rtype is None:
            return Response(404, {}, b'{"errors":[{"code":"404"}]}')
        items = list(self.data.get(rtype, {}).values())
        items = self._filter(items, qs)
        order = qs.get("order", [None])[0]
        if order:
            desc = order.startswith("-")
            key = order.lstrip("-")
            items.sort(key=lambda r: r["attributes"].get(key, ""), reverse=desc)
        total = len(items)
        per_page = int(qs.get("per_page", ["25"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        page = items[offset:offset + per_page]
        sparse = "fields[Person]" in qs
        data = [self._sparse(r, qs["fields[Person]"][0]) if sparse else r for r in page]
        included = self._includes(page, qs.get("include", [None])[0]) if not sparse else []
        meta = {"total_count": total, "count": len(data)}
        if offset + per_page < total:
            meta["next"] = {"offset": offset + per_page}
        return self._ok({"data": data, "included": included, "meta": meta})

    def _single(self, seg, rid, qs):
        rtype = self._type_of(seg)
        item = self.data.get(rtype, {}).get(rid)
        if item is None:
            return Response(404, {}, b'{"errors":[{"code":"404","detail":"not found"}]}')
        included = self._includes([item], qs.get("include", [None])[0])
        return self._ok({"data": item, "included": included})

    def _mergers(self, qs):
        items = sorted(self.mergers, key=lambda r: r["attributes"]["created_at"])
        gte = qs.get("where[created_at][gte]", [None])[0]
        if gte:
            items = [m for m in items if m["attributes"]["created_at"] >= gte]
        per_page = int(qs.get("per_page", ["25"])[0])
        return self._ok({"data": items[:per_page], "meta": {"total_count": len(items)}})

    def _write(self, method, segs, body):
        if self.fail_next is not None:
            status, detail = self.fail_next
            self.fail_next = None
            return Response(status, {}, json.dumps({"errors": [{"code": str(status), "detail": detail}]}).encode())
        rtype = self._type_of(segs[0]) if segs else None
        payload = json.loads(body) if body else {}
        if method == "POST":
            rid = str(next(self._ids))
            attrs = payload.get("data", {}).get("attributes", {})
            item = res(rtype, rid, attrs, updated="2026-06-01T00:00:00Z", created="2026-06-01T00:00:00Z")
            self.add(item)
            return Response(201, {}, json.dumps({"data": item}).encode())
        if method == "PATCH":
            item = self.data.get(rtype, {}).get(segs[1])
            if item is None:
                return Response(404, {}, b'{"errors":[{"code":"404"}]}')
            item["attributes"].update(payload.get("data", {}).get("attributes", {}))
            item["attributes"]["updated_at"] = "2026-06-02T00:00:00Z"
            return Response(200, {}, json.dumps({"data": item}).encode())
        if method == "DELETE":
            self.destroy(rtype, segs[1])
            return Response(204, {}, b"")
        return Response(405, {}, b'{"errors":[{"code":"405"}]}')

    # -- helpers -----------------------------------------------------------
    def _filter(self, items, qs):
        out = []
        for r in items:
            ok = True
            for key, vals in qs.items():
                if not key.startswith("where["):
                    continue
                inner = key[len("where["):].rstrip("]")
                p = inner.split("][")
                attr, op = p[0], (p[1] if len(p) > 1 else "eq")
                v = vals[0]
                cur = r["attributes"].get(attr, "")
                if op == "gte" and not (cur >= v):
                    ok = False
                elif op == "gt" and not (cur > v):
                    ok = False
                elif op == "lt" and not (cur < v):
                    ok = False
                elif op == "lte" and not (cur <= v):
                    ok = False
                elif op == "eq" and str(cur).lower() != v.lower():
                    ok = False
            if ok:
                out.append(r)
        return out

    def _sparse(self, r, fields):
        keys = [k for k in fields.split(",") if k]
        attrs = {k: r["attributes"].get(k) for k in keys} if keys else {}
        return {"id": r["id"], "type": r["type"], "attributes": attrs}

    def _includes(self, page, include):
        if not include:
            return []
        out, seen = [], set()

        def add(r):
            k = (r["type"], r["id"])
            if k not in seen:
                seen.add(k)
                out.append(r)

        for token in include.split(","):
            first, _, second = token.partition(".")
            for parent in page:
                pr = registry.by_type(parent["type"])
                rel = pr.relationships.get(first) if pr else None
                if rel is None:
                    continue
                level1 = self._related(parent, rel)
                for x in level1:
                    add(x)
                if second:
                    tr = registry.by_name(rel.target)
                    for x in level1:
                        rel2 = tr.relationships.get(second)
                        if rel2:
                            for y in self._related(x, rel2):
                                add(y)
        return out

    def _related(self, parent, rel):
        tr = registry.by_name(rel.target)
        pid = parent["id"]
        if rel.kind == "many" and rel.via is None:
            return [c for c in self.data.get(tr.type, {}).values()
                    if (c.get("relationships", {}).get("person", {}).get("data") or {}).get("id") == pid]
        if rel.kind == "one":
            fk = (parent.get("relationships", {}).get(rel_key(rel), {}).get("data") or {}).get("id")
            t = self.data.get(tr.type, {}).get(fk)
            return [t] if t else []
        if rel.via:
            join = registry.by_name(rel.via)
            jt = self.data.get(join.type, {})
            tids = [(m.get("relationships", {}).get("household", {}).get("data") or {}).get("id")
                    for m in jt.values()
                    if (m.get("relationships", {}).get("person", {}).get("data") or {}).get("id") == pid]
            return [self.data[tr.type][t] for t in tids if t in self.data.get(tr.type, {})]
        return []

    def _ok(self, body):
        return Response(200, {"Content-Type": "application/vnd.api+json"}, json.dumps(body).encode())


def rel_key(rel):
    # map a Rel back to its person-side relationship key for 'one' includes
    return {"campus": "primary_campus", "marital_status": "marital_status"}.get(rel.target, rel.target)
