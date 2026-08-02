"""What counts as the mirror disagreeing with Planning Center.

A naive `mirror_body != pco_body` reports every single response as wrong, because
the mirror differs from PCO on purpose in several places. Those decisions are
already written down and already tested — `tests/test_golden.py` replays 81
recorded PCO responses through the serving layer and asserts exactly which
differences are decisions and which are drift. This module is that judgement,
lifted out so the live comparison and the offline corpus cannot drift apart: if
the two ever disagree about what a difference means, one of them is lying.

**Ignored, because they are decisions:**

  * `links`, everywhere. The mirror generates a resource's link map from the
    registry rather than echoing PCO's, so a record has the same shape however it
    was synced — and rewrites them to mirror-relative paths, because a caller
    holds a pcomirror key and not a PCO token.
  * `meta.mirror`, `meta.can_search_by` — the mirror's own, which PCO has no
    opinion about.
  * `meta.can_filter`, which the mirror advertises as empty on purpose: `filter=`
    is not implemented, and claiming filters it does not honour would be worse
    than claiming none.
  * A resource's `meta`, which carries the mirror's sync bookkeeping.

**Compared, strictly:** the status, `data` ids *in order*, the `included` set,
every attribute of every resource, every relationship's ids, and
`meta.total_count`.

That last one is where this is deliberately *stricter* than the golden corpus.
There, page membership and `total_count` are not asserted, because the corpus is
a few hundred rows sampled out of a 1,915-person organization and a page boundary
naturally falls somewhere else. Live, the mirror holds the whole organization, so
a page that differs from PCO's is a real bug and a count that differs is the
plainest possible statement of one.
"""
from __future__ import annotations

#: Top-level `meta` keys the mirror owns. PCO never sends them and their absence
#: upstream is not a divergence.
MIRROR_ONLY_META = frozenset({"mirror", "can_search_by"})

#: `meta` keys where a difference is a decision rather than drift.
IGNORED_META = MIRROR_ONLY_META | {"can_filter", "parent", "next", "prev"}

#: How many differences one report will carry. A response that disagrees in
#: three hundred places has one cause, and three hundred rows of it in an admin
#: page is a wall nobody reads to the end of.
MAX_DIFFERENCES = 40


class Difference:
    """One place two documents disagree, named by where rather than by what."""

    __slots__ = ("pointer", "mirror", "pco", "note")

    def __init__(self, pointer: str, mirror=None, pco=None, note: str = ""):
        self.pointer, self.mirror, self.pco, self.note = pointer, mirror, pco, note

    def as_dict(self) -> dict:
        return {"pointer": self.pointer, "mirror": self.mirror,
                "pco": self.pco, "note": self.note}

    def __repr__(self) -> str:                                   # pragma: no cover
        return f"<Difference {self.pointer} mirror={self.mirror!r} pco={self.pco!r}>"


def _resources(body):
    """`{(type, id): resource}` for everything a document carries."""
    out = {}
    data = (body or {}).get("data")
    for item in (data if isinstance(data, list) else [data]):
        if isinstance(item, dict) and item.get("id"):
            out[(item.get("type"), str(item["id"]))] = item
    for item in (body or {}).get("included") or []:
        if isinstance(item, dict) and item.get("id"):
            out.setdefault((item.get("type"), str(item["id"])), item)
    return out


def _data_ids(body):
    data = (body or {}).get("data")
    if isinstance(data, dict):
        return [str(data.get("id"))] if data.get("id") else []
    return [str(i["id"]) for i in (data or []) if isinstance(i, dict) and i.get("id")]


def _relationship_ids(resource, name):
    """The ids on one side of a relationship, order-insensitively."""
    node = ((resource.get("relationships") or {}).get(name) or {}).get("data")
    if node is None:
        return None
    if isinstance(node, dict):
        node = [node]
    return sorted(str(x.get("id")) for x in node if isinstance(x, dict) and x.get("id"))


def _updated_at(resource):
    return ((resource or {}).get("attributes") or {}).get("updated_at")


def one_sided(mirror_body, pco_body):
    """`(key, side, resource)` for every record only one document carries.

    `side` names who returned it — `"pco"` or `"mirror"` — and `resource` is
    that side's copy. This is the set the checker looks up in the mirror's own
    store: a record one side returned and the other did not is either a store
    gap, a tombstone disagreement, or a serving/search difference, and only the
    store can say which.
    """
    mine, theirs = _resources(mirror_body), _resources(pco_body)
    out = [(key, "pco", theirs[key]) for key in sorted(set(theirs) - set(mine))]
    out += [(key, "mirror", mine[key]) for key in sorted(set(mine) - set(theirs))]
    return out


def compare(mirror_body, pco_body, mirror_status=200, pco_status=200) -> list:
    """Every way these two disagree that is not a documented decision."""
    found = []

    def note(pointer, mine=None, theirs=None, why=""):
        if len(found) < MAX_DIFFERENCES:
            found.append(Difference(pointer, mine, theirs, why))

    if mirror_status != pco_status and not (mirror_status == 410 and pco_status == 404):
        # 410-for-404 is a decision, not drift: PCO forgets a deleted record
        # entirely, while the mirror keeps the tombstone and answers with when
        # it died, why, and where a merge went (§4.4). A live log filled with
        # a report per deleted record's shape re-check taught nobody anything.
        note("$.status", mirror_status, pco_status, "different status")

    # -- the page itself ---------------------------------------------------
    mine_ids, their_ids = _data_ids(mirror_body), _data_ids(pco_body)
    if mine_ids != their_ids:
        only_mine = [i for i in mine_ids if i not in set(their_ids)]
        only_theirs = [i for i in their_ids if i not in set(mine_ids)]
        if only_mine or only_theirs:
            note("$.data", only_mine or None, only_theirs or None,
                 "records on one side only")
        else:
            note("$.data", mine_ids, their_ids, "same records, different order")

    mine_meta = (mirror_body or {}).get("meta") or {}
    their_meta = (pco_body or {}).get("meta") or {}
    for key in sorted(set(their_meta) - IGNORED_META):
        if key not in mine_meta:
            note(f"$.meta.{key}", None, their_meta[key], "meta key the mirror omits")
        elif key in ("total_count", "count") and mine_meta[key] != their_meta[key]:
            note(f"$.meta.{key}", mine_meta[key], their_meta[key], "count differs")

    # -- included ----------------------------------------------------------
    mine_res, their_res = _resources(mirror_body), _resources(pco_body)
    for key in sorted(set(their_res) - set(mine_res)):
        note(f"$.included[{key[0]}/{key[1]}]", None, "present",
             "resource PCO returned and the mirror did not")
    for key in sorted(set(mine_res) - set(their_res)):
        note(f"$.included[{key[0]}/{key[1]}]", "present", None,
             "resource the mirror returned and PCO did not")

    # -- resource by resource ----------------------------------------------
    for key in sorted(set(mine_res) & set(their_res)):
        found.extend(_compare_resource(key, mine_res[key], their_res[key],
                                       MAX_DIFFERENCES - len(found)))
    return found[:MAX_DIFFERENCES]


def _compare_resource(key, mine, theirs, budget) -> list:
    if budget <= 0:
        return []
    rtype, rid = key
    where = f"$.[{rtype}/{rid}]"
    out = []

    mine_attrs = mine.get("attributes") or {}
    their_attrs = theirs.get("attributes") or {}
    for name in sorted(set(mine_attrs) | set(their_attrs)):
        if len(out) >= budget:
            return out
        if name not in mine_attrs:
            out.append(Difference(f"{where}.attributes.{name}", None, their_attrs[name],
                                  "attribute the mirror does not carry"))
        elif name not in their_attrs:
            out.append(Difference(f"{where}.attributes.{name}", mine_attrs[name], None,
                                  "attribute PCO does not carry"))
        elif mine_attrs[name] != their_attrs[name]:
            out.append(Difference(f"{where}.attributes.{name}",
                                  mine_attrs[name], their_attrs[name], "value differs"))

    # PCO's relationships must never be lost. The mirror carrying one PCO did not
    # send is not symmetric — a nested `include` makes PCO synthesize
    # relationships it does not otherwise show, so an extra one here is ordinary.
    for name in sorted((theirs.get("relationships") or {})):
        if len(out) >= budget:
            return out
        theirs_ids = _relationship_ids(theirs, name)
        mine_ids = _relationship_ids(mine, name)
        if theirs_ids is None:
            continue
        if mine_ids is None:
            out.append(Difference(f"{where}.relationships.{name}", None, theirs_ids,
                                  "relationship the mirror does not carry"))
        elif mine_ids != theirs_ids:
            out.append(Difference(f"{where}.relationships.{name}", mine_ids, theirs_ids,
                                  "related ids differ"))
    return out


def classify(differences, mirror_body, pco_body, store=None) -> str:
    """`staleness` if the sweep will fix it, `divergence` if nothing will.

    The distinction is the whole point of separating these, and it is not
    cosmetic. If PCO's `updated_at` for a record has moved past the mirror's,
    the mirror is simply behind: the incremental sweep filters on exactly that
    timestamp, so it will collect the record and the difference will go away on
    its own.

    A difference where the timestamps *match* is the opposite. The sweep filters
    that record out, and the canonical writer's monotonic guard would refuse it
    as not-newer even if something did fetch it — so nothing converges on it,
    ever. That is the class this whole feature exists to surface: PCO demoting a
    primary email without moving `updated_at` was measured doing precisely this,
    and the mirror would have reported two primary numbers for one person
    indefinitely.

    `store`, when given, maps each one-sided record's `(type, id)` to what the
    mirror's own tables hold for it (see `ShadowChecker._store_facts`). It
    exists because "PCO has it and the mirror does not" is *not* always lag: a
    live report showed a record whose `updated_at` was two years behind the
    sweep watermark filed as `staleness`, promising a repair no sweep would
    ever make. With the store in hand the promise is checked instead of
    assumed — a record the mirror holds live but did not return is a serving
    or search difference, and a record it lacks is only "not swept yet" while
    the watermark has not already passed it.
    """
    if not differences:
        return "match"
    mine, theirs = _resources(mirror_body), _resources(pco_body)
    for key in set(mine) & set(theirs):
        ours, upstream = _updated_at(mine[key]), _updated_at(theirs[key])
        if ours and upstream and upstream > ours:
            return "staleness"
    # A record PCO has and the mirror does not may be lag — it has not been
    # swept yet — provided nothing else disagrees, and provided the sweep is in
    # fact still going to collect it.
    if set(theirs) - set(mine) and not (set(mine) - set(theirs)):
        if all(d.pointer.startswith("$.included[") or d.pointer.startswith("$.data")
               or d.pointer.startswith("$.meta") for d in differences):
            if all(_sweep_converges(store.get(key) if store else None,
                                    _updated_at(theirs[key]))
                   for key in set(theirs) - set(mine)):
                return "staleness"
    return "divergence"


def _sweep_converges(fact, upstream_uat) -> bool:
    """Whether the incremental sweep will still deliver this record.

    Without store facts there is no basis to overrule the optimistic reading,
    so absence of a fact keeps the old answer. With them, three ways the
    promise fails: the mirror already *has* the record and chose not to return
    it (a serving or search difference — syncing again changes nothing); the
    record carries no `updated_at` for the sweep to see; or the watermark is
    already past it, so the sweep's filter will never match it again. A
    tombstoned row additionally needs the upstream copy to be newer than the
    tombstone, or the canonical writer will refuse the resurrection.
    """
    if fact is None:
        return True
    if fact["held"] == "live":
        return False
    if not upstream_uat:
        return False
    watermark = fact.get("watermark")
    if watermark and upstream_uat < watermark:
        return False
    if fact["held"] == "tombstoned":
        buried_at = fact.get("tombstone_uat")
        return not buried_at or upstream_uat > buried_at
    return True
