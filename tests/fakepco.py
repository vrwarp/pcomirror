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
        self.unreachable = False                      # every request fails, as in an outage
        self.request_log: list[tuple[str, str]] = []
        self.echo_self_link = True   # see _create_membership
        self.echo_owner = True       # see _create_child
        # A read replica that has not caught up with a write PCO itself just
        # confirmed — measured on the live API minutes after a family was
        # built, and the same lag class as `echo_owner`. Keyed `(type, id)` to
        # `(stale_copy, reads_remaining)`: `_single` serves the staged copy
        # that many times, then the truth again.
        self.stale_single_reads: dict[tuple[str, str], tuple[dict, int]] = {}
        # Types whose `where[created_at]` the fake ignores — silently returning
        # the full collection, exactly as `/addresses` was measured doing on
        # the live API. A fake that always honoured the filter could never
        # have caught the audit oscillating on it.
        self.ignore_created_filters: set[str] = set()

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

    def add_membership(self, mid, household_id, person_id, role="child_or_dependent",
                       pending=False):
        """PCO's shape, faithfully: the household id appears *only* in `links.self`.

        There is no `household` relationship on a real membership payload, which is
        why the mirror parses the owning id back out of the link — so the fake must
        not offer an easier route than the API does.
        """
        resource = res("HouseholdMembership", mid,
                       {"household_role": role, "pending": pending,
                        "person_name": f"member-{person_id}"},
                       relationships={"person": {"data": {"type": "Person", "id": str(person_id)}}})
        resource["links"] = {
            "self": f"https://api.planningcenteronline.com/people/v2/households/"
                    f"{household_id}/household_memberships/{mid}"}
        return self.add(resource)

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
        if self.unreachable:
            raise ConnectionResetError("PCO is unreachable")
        segs = [s for s in sub.split("/") if s]
        if method == "GET":
            if segs and segs[0] == "person_mergers":
                return self._mergers(qs)
            if len(segs) == 1:
                return self._collection(segs[0], qs)
            if len(segs) == 2:
                return self._single(segs[0], segs[1], qs)
            if len(segs) == 3:
                return self._nested(segs[0], segs[1], segs[2], qs)
        if method in ("POST", "PATCH", "DELETE"):
            return self._write(method, segs, body)
        return Response(404, {}, json.dumps({"errors": [{"code": "404"}]}).encode())

    def _nested(self, seg, pco_id, rel, qs):
        """Only what PCO actually serves this way.

        `/households/{id}/household_memberships` is the one collection PCO will
        not list wholesale, so it is the one the fake serves nested. Everything
        else 404s here, exactly as PCO does — the mirror's pass-through for
        relationships it does not hold depends on that.
        """
        if (seg, rel) != ("households", "household_memberships"):
            return Response(404, {}, b'{"errors":[{"code":"404"}]}')
        if pco_id not in self.data.get("Household", {}):
            # A collection served only under its parent 404s when the parent is
            # gone, and that 404 is the *only* announcement PCO makes of a
            # deleted household — `where[updated_at]` cannot return one. A fake
            # that answered "no memberships" instead would hide the one signal
            # the walk has to act on.
            return Response(404, {}, b'{"errors":[{"code":"404"}]}')
        items = [r for r in self.data.get("HouseholdMembership", {}).values()
                 if f"/households/{pco_id}/household_memberships/" in (r.get("links") or {}).get("self", "")]
        per_page = int(qs.get("per_page", ["25"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        page = items[offset:offset + per_page]
        body = {"data": page, "meta": {"total_count": len(items), "count": len(page),
                                       "parent": {"type": "Household", "id": pco_id}},
                "links": {"self": f"/households/{pco_id}/household_memberships"}}
        included = self._includes(page, qs.get("include", [None])[0])
        if included:
            body["included"] = included
        return Response(200, {}, json.dumps(body).encode())

    # -- handlers ----------------------------------------------------------
    def _type_of(self, seg):
        return _SEG_TO_TYPE.get(seg)

    def _collection(self, seg, qs):
        rtype = self._type_of(seg)
        if rtype is None:
            return Response(404, {}, b'{"errors":[{"code":"404"}]}')
        items = list(self.data.get(rtype, {}).values())
        if rtype in self.ignore_created_filters:
            qs = {k: v for k, v in qs.items() if not k.startswith("where[created_at]")}
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
        # `fields[Type]` for whatever type this collection serves, not `Person`
        # alone: the delete audit asks for `fields[Household]=created_at` too, and
        # a fake that quietly ignored it would test a request PCO never gets.
        sparse = f"fields[{rtype}]" in qs
        data = [self._sparse(r, qs[f"fields[{rtype}]"][0]) if sparse else r for r in page]
        included = self._includes(page, qs.get("include", [None])[0]) if not sparse else []
        meta = {"total_count": total, "count": len(data)}
        if offset + per_page < total:
            meta["next"] = {"offset": offset + per_page}
        return self._ok({"data": data, "included": included, "meta": meta})

    def _single(self, seg, rid, qs):
        rtype = self._type_of(seg)
        staged = self.stale_single_reads.get((rtype, rid))
        if staged is not None:
            copy, remaining = staged
            if remaining <= 1:
                self.stale_single_reads.pop((rtype, rid))
            else:
                self.stale_single_reads[(rtype, rid)] = (copy, remaining - 1)
            included = self._includes([copy], qs.get("include", [None])[0])
            return self._ok({"data": copy, "included": included})
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
        if method == "POST" and segs == ["households"]:
            return self._create_household(body)
        if method == "POST" and len(segs) == 3 and segs[2] == "household_memberships":
            return self._create_membership(segs[1], body)
        if method == "POST" and len(segs) == 3:
            return self._create_child(segs[1], segs[2], body)
        if method == "DELETE" and len(segs) == 4:
            self.destroy(self._type_of(segs[2]), segs[3])
            return Response(204, {}, b"")
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


    def _create_child(self, person_id, seg, body):
        """`POST /people/{id}/emails` and friends, with PCO's side effects.

        Two things a create reply does not describe, and both bit the mirror:
        whether it repeats the owning `person` relationship (`echo_owner` — the
        owner is in the URL, so a mirror must not need the echo), and that
        setting `primary` demotes whatever held it before.

        The demotion deliberately leaves `updated_at` alone, because that is what
        the live API does — measured, 2026-07-28, in `docs/mutation-testing.md`.
        It is the whole difficulty: a change PCO makes without moving the
        timestamp is one the sweep will never re-fetch and the monotonic writer
        would refuse anyway, so nothing converges on it unless the write path
        goes and looks.
        """
        rtype = self._type_of(seg)
        if rtype is None:
            return Response(404, {}, b'{"errors":[{"code":"404"}]}')
        attrs = (json.loads(body).get("data") or {}).get("attributes") or {}
        cid = str(next(self._ids))
        item = self.add_child(rtype, cid, person_id, attrs, "2026-06-01T00:00:00Z")
        if attrs.get("primary"):
            for other in self.data.get(rtype, {}).values():
                owner = ((other.get("relationships") or {}).get("person") or {}).get("data") or {}
                if other["id"] != cid and owner.get("id") == str(person_id):
                    other["attributes"]["primary"] = False
        returned = dict(item)
        if not self.echo_owner:
            returned.pop("relationships", None)
        return Response(201, {}, json.dumps({"data": returned}).encode())

    def _create_household(self, body):
        """`POST /households`, which builds a whole family in one call.

        The members are named in `relationships.people` on the way in, and the
        edge lands on both sides exactly as it does for a membership created
        under an existing household — a fake that stored it only on the
        household would let a mirror that refreshes only the household look
        correct, which is the bug this models.

        Nobody's `updated_at` moves. That is the awkward part and it is faithful:
        joining a household is a change the watermark sweep cannot see, so a
        mirror that does not go and look on the write does not converge on the
        next sweep either.
        """
        data = (json.loads(body) if body else {}).get("data") or {}
        rid = str(next(self._ids))
        item = res("Household", rid, data.get("attributes") or {},
                   created="2026-06-01T00:00:00Z", updated="2026-06-01T00:00:00Z")
        members = ((data.get("relationships") or {}).get("people") or {}).get("data") or []
        item["relationships"] = {k: v for k, v in (data.get("relationships") or {}).items()}
        item["relationships"]["people"] = {"data": [dict(m) for m in members]}
        self.add(item)
        for member in members:
            person = self.data.get("Person", {}).get(str(member.get("id")))
            if person is None:
                continue
            households = person.setdefault("relationships", {}).setdefault(
                "households", {"data": []})["data"]
            if not any(h["id"] == rid for h in households):
                households.append({"type": "Household", "id": rid})
        return Response(201, {}, json.dumps({"data": item}).encode())

    def _create_membership(self, household_id, body):
        """`POST /households/{id}/household_memberships`, shaped as PCO shapes it.

        The owning household appears only in `links.self` — there is no
        `household` relationship on a membership payload — and `echo_self_link`
        exists because whether a *create* response repeats that link is exactly
        what a mirror must not depend on. PCO knows the association either way,
        so the stored copy always carries it and only the reply may omit it.
        """
        attrs = (json.loads(body).get("data") or {}).get("attributes") or {}
        person_id = str(attrs.get("person_id"))
        mid = str(next(self._ids))
        item = self.add_membership(mid, household_id, person_id,
                                   role=attrs.get("household_role", "child_or_dependent"))
        # The edge is stored on both sides at PCO, and both move together. A fake
        # that updated only the household would have let a mirror that refreshes
        # only the household look correct.
        household = self.data.get("Household", {}).get(str(household_id))
        if household is not None:
            members = household.setdefault("relationships", {}).setdefault(
                "people", {"data": []})["data"]
            if not any(m["id"] == person_id for m in members):
                members.append({"type": "Person", "id": person_id})
        person = self.data.get("Person", {}).get(person_id)
        if person is not None:
            households = person.setdefault("relationships", {}).setdefault(
                "households", {"data": []})["data"]
            if not any(h["id"] == str(household_id) for h in households):
                households.append({"type": "Household", "id": str(household_id)})
        returned = dict(item)
        if not self.echo_self_link:
            returned.pop("links", None)
        return Response(201, {}, json.dumps({"data": returned}).encode())

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
                if attr in ("search_name", "search_name_or_email"):
                    if not self._search_matches(r, v, emails=attr.endswith("email")):
                        ok = False
                    continue
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

    def _search_matches(self, person, needle, emails=False):
        """PCO's `where[search_name*]`, as measured: names match by word-prefix
        over each name field, and the `_or_email` arm also matches an address by
        substring. The email arm reads the *fake's* Email rows — which is the
        point: a mirror whose own email table has drifted answers this search
        differently than PCO does, and a fake without the arm could never model
        that divergence.

        One more measured wrinkle, or-email family only: a single-word needle
        matches the name fields by substring anywhere (`onzale` finds
        Gonzalez), while two or more words keep the word-prefix rule."""
        probe = (needle or "").lower().split()
        a = person.get("attributes", {})
        for hay in (a.get("name"), f'{a.get("first_name", "")} {a.get("last_name", "")}',
                    a.get("first_name"), a.get("last_name"), a.get("nickname"),
                    a.get("given_name")):
            words = (hay or "").lower().split()
            if emails and len(probe) == 1 and probe[0] in (hay or "").lower():
                return True
            if probe and len(probe) <= len(words) and \
                    all(words[i].startswith(probe[i]) for i in range(len(probe))):
                return True
        if emails:
            flat = (needle or "").lower().strip()
            for email in self.data.get("Email", {}).values():
                owner = ((email.get("relationships") or {}).get("person") or {}).get("data") or {}
                if owner.get("id") == person["id"] and flat and \
                        flat in (email["attributes"].get("address") or "").lower():
                    return True
        return False

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
