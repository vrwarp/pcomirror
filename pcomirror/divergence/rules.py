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

**A page is a window, and the window moves.** One thing page membership does
*not* say is which records a side matched. Two result sets of different sizes,
ordered the same way, put their hundredth record in different places: every
record the smaller side is missing from the shared prefix pushes one more record
across the larger side's page edge. So a record one side returned and the other
did not is evidence only when the other side's page was its *last* — otherwise
the record may simply sit past the edge, on a page nobody fetched.

Reading it as evidence regardless is not a harmless over-report. A live export
of 96 checks of `where[search_name_or_email]` — one bug, the mirror matching
names by word-prefix where PCO matched by substring, so every mirror result set
was a strict *subset* of PCO's — carried 1,888 rows accusing the mirror of
returning people PCO did not. Every one of them was the window: the mirror was
under-matching, in one direction only, and the report said both. `meta.next`
(and `links.next`) is what settles it, and both sides send it.
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

#: How many `$.store[…]` rows ride along behind those. They are testimony about
#: the differences rather than more of them, so they have their own budget — but
#: they had none at all, and a hundred-record page produced a report of 150 rows
#: against a documented ceiling of 40. The bound the log is trimmed by is bytes
#: (`MAX_BYTES`), which those rows spend without ever being read to the end of.
MAX_STORE_NOTES = MAX_DIFFERENCES


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


def _data_keys(body):
    """`{(type, id)}` for the records in `data`, as distinct from `included`."""
    data = (body or {}).get("data")
    items = data if isinstance(data, list) else [data]
    return {(i.get("type"), str(i["id"])) for i in items
            if isinstance(i, dict) and i.get("id")}


def has_more(body) -> bool:
    """Whether this side had pages left after the one being compared.

    PCO reports the cursor in `meta` and in `links`, and so does the mirror;
    either is enough. A response with neither is the whole answer, which is what
    makes a record's absence from it mean something.
    """
    meta = (body or {}).get("meta") or {}
    links = (body or {}).get("links") or {}
    return bool(meta.get("next") or links.get("next"))


def windowed_sides(mirror_body, pco_body) -> dict:
    """Whose exclusive records are explained by the *other* side's page ending.

    Keyed by the side that returned the record, because that is how `one_sided`
    names it: `windowed["mirror"]` is true when PCO had more pages, so a record
    only the mirror returned proves nothing about what PCO matched.
    """
    return {"mirror": has_more(pco_body), "pco": has_more(mirror_body)}


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
    """`(key, side, resource, windowed)` for every record only one document carries.

    `side` names who returned it — `"pco"` or `"mirror"` — and `resource` is
    that side's copy. This is the set the checker looks up in the mirror's own
    store: a record one side returned and the other did not is either a store
    gap, a tombstone disagreement, or a serving/search difference, and only the
    store can say which.

    Unless it is none of those. `windowed` is true when this is a `data` member
    and the *other* side still had pages, and then its absence there is not a
    fact about the other side at all — it is where the page ended. The store has
    nothing to say about those, and asking it a hundred times a check to print a
    hundred rows of "held live", which is exactly what the window predicts, is
    how one bug filled a report.

    `data` membership is the whole of it. A page boundary explains a record the
    far side's page did not reach; it does not explain a *sideload* the far side
    left out of the page it did return, and suppressing one of those would lose
    the include-set difference that child-delete detection rests on.
    """
    mine, theirs = _resources(mirror_body), _resources(pco_body)
    windowed = windowed_sides(mirror_body, pco_body)
    mine_data, their_data = _data_keys(mirror_body), _data_keys(pco_body)
    out = [(key, "pco", theirs[key], windowed["pco"] and key in their_data)
           for key in sorted(set(theirs) - set(mine))]
    out += [(key, "mirror", mine[key], windowed["mirror"] and key in mine_data)
            for key in sorted(set(mine) - set(theirs))]
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
    windowed = windowed_sides(mirror_body, pco_body)
    if mine_ids != their_ids:
        only_mine = [i for i in mine_ids if i not in set(their_ids)]
        only_theirs = [i for i in their_ids if i not in set(mine_ids)]
        if not (only_mine or only_theirs):
            note("$.data", mine_ids, their_ids, "same records, different order")
        else:
            said_mine = [] if windowed["mirror"] else only_mine
            said_theirs = [] if windowed["pco"] else only_theirs
            if said_mine or said_theirs:
                note("$.data", said_mine or None, said_theirs or None,
                     "records on one side only")
            past_mine = len(only_mine) - len(said_mine)
            past_theirs = len(only_theirs) - len(said_theirs)
            if past_mine or past_theirs:
                note("$.data", past_mine or None, past_theirs or None,
                     "records the other side still had pages to reach — a page "
                     "boundary, not a statement about what the other side matched")

    mine_meta = (mirror_body or {}).get("meta") or {}
    their_meta = (pco_body or {}).get("meta") or {}
    for key in sorted(set(their_meta) - IGNORED_META):
        if key not in mine_meta:
            note(f"$.meta.{key}", None, their_meta[key], "meta key the mirror omits")
        elif key in ("total_count", "count") and mine_meta[key] != their_meta[key]:
            note(f"$.meta.{key}", mine_meta[key], their_meta[key], "count differs")

    # -- resource by resource ----------------------------------------------
    # Before the one-sided roll-call below, not after. A record both sides
    # returned whose *attributes* differ is the class this feature exists to
    # find — the silent primary demotion — and it is one row. A page whose
    # membership differs is one cause and up to two hundred rows restating the
    # `$.data` difference above. Enumerated first, the roll-call spent the whole
    # budget and the attribute difference was never reached.
    mine_res, their_res = _resources(mirror_body), _resources(pco_body)
    for key in sorted(set(mine_res) & set(their_res)):
        found.extend(_compare_resource(key, mine_res[key], their_res[key],
                                       MAX_DIFFERENCES - len(found)))

    # -- records only one side carries -------------------------------------
    # Named where they were found: a `data` member is page membership, an
    # `included` one is a sideload the other side did not send. The ones past
    # the far side's page edge are already summarised on `$.data` and are not
    # worth a row each.
    mine_data, their_data = _data_keys(mirror_body), _data_keys(pco_body)
    for key, side, _resource, is_windowed in one_sided(mirror_body, pco_body):
        if is_windowed:
            continue
        in_data = key in (mine_data if side == "mirror" else their_data)
        where = "$.data" if in_data else "$.included"
        if side == "pco":
            note(f"{where}[{key[0]}/{key[1]}]", None, "present",
                 "resource PCO returned and the mirror did not")
        else:
            note(f"{where}[{key[0]}/{key[1]}]", "present", None,
                 "resource the mirror returned and PCO did not")
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
    # Presence is read from the same window as everything else: a record only
    # one side returned, where the other side had pages left, says nothing in
    # either direction and must not be weighed as though it did — least of all
    # the mirror's, where a single tail record beyond PCO's page edge used to
    # read as "the mirror invented one" and rule out lag outright.
    unexplained = [(key, side) for key, side, _res, is_windowed
                   in one_sided(mirror_body, pco_body) if not is_windowed]
    only_theirs = {key for key, side in unexplained if side == "pco"}
    only_mine = {key for key, side in unexplained if side == "mirror"}
    # A record PCO has and the mirror does not may be lag — it has not been
    # swept yet — provided nothing else disagrees, and provided the sweep is in
    # fact still going to collect it.
    if only_theirs and not only_mine:
        if all(d.pointer.startswith("$.included[") or d.pointer.startswith("$.data")
               or d.pointer.startswith("$.meta") for d in differences):
            if all(_sweep_converges(store.get(key) if store else None,
                                    _updated_at(theirs[key]))
                   for key in only_theirs):
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
