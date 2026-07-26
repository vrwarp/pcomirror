"""Replay real Planning Center responses against the mirror.

`tests/golden/` holds request/response pairs captured from the live Planning
Center People API and then sanitized (see `tests/golden/README.md`). This module
loads the resources out of those payloads, replays the same requests against the
serving layer, and asserts the mirror answers in the same shape and the same
order as PCO did.

Why this exists: every compatibility bug this suite was written for was invisible
to a hand-written fixture, because a hand-written fixture encodes what we *think*
the API does. `where[search_name]` was implemented as a substring match and looked
right for a year of unit tests; the live API returned nine people where the mirror
returned a hundred. The recordings are the only part of the suite that cannot be
wrong in the same direction as the code.

What is asserted, and what deliberately is not:

  * attribute keys, per resource type — exactly PCO's set;
  * relationship keys — PCO's are never lost, and the mirror never invents one
    PCO has not shown for that type anywhere in the corpus;
  * `meta` keys — PCO's are a subset of the mirror's;
  * ordering — the recorded ids come back in the recorded relative order;
  * single and nested reads — exact ids;
  * `included[]` — the same set, where the primary rows all survived the sample.

Three things are deliberately *not* asserted, because the corpus is a sample of a
1,915-person organization rather than the whole of it, and because two of the
mirror's differences from PCO are decisions rather than drift:

  * absolute page membership and `meta.total_count`/`meta.next` — the mirror holds
    a few hundred rows where PCO holds the organization, so a page boundary falls
    somewhere else. Relative order is asserted instead, over the whole population;
  * the per-resource `links` map, which the mirror generates from the registry so
    a record has the same shape however it was synced, where PCO sends a short map
    on a list page and a long one on a single read (README, "URLs in responses");
  * that those links are absolute — they are rewritten to mirror-relative paths on
    purpose, because a caller holds a pcomirror key and not a PCO token.
"""
from __future__ import annotations

import json
import os
import unittest

from base import build, wsgi_get

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")


def _records():
    for name in sorted(os.listdir(GOLDEN)):
        if name.endswith(".json") and name != "manifest.json":
            with open(os.path.join(GOLDEN, name)) as fh:
                yield json.load(fh)


def _load_order(record):
    """Include-bearing payloads last.

    Two recordings of the same person disagree about how much of the graph they
    carry: `?include=households` puts the household identifiers on the Person and
    a bare read does not. The writer is monotonic and same-timestamp writes
    overwrite, so loading the richer payload last leaves the mirror holding the
    edge — which is what a real backfill does, since it always passes `include=`.
    """
    return ("include=" in record["request"]["query"], record["name"])


def _by_type(resources):
    out = {}
    for item in resources:
        out.setdefault(item["type"], set()).update((item.get("attributes") or {}).keys())
    return out


def _rels_by_type(resources):
    out = {}
    for item in resources:
        out.setdefault(item["type"], set()).update((item.get("relationships") or {}).keys())
    return out


def _corpus_relationship_vocabulary():
    """Every relationship key PCO showed for each type, anywhere in the corpus.

    PCO varies a resource's relationship set by request — a Person fetched with
    `?include=emails` has an `emails` relationship and the same Person sideloaded
    into somebody else's household has none. The mirror deliberately does not
    vary: it serves the fullest representation it holds, so a record's shape does
    not depend on how it happened to be synced. So the check is not equality per
    call; it is that nothing PCO sent is lost, and nothing PCO never sent appears.
    """
    vocab = {}
    for record in _records():
        items, included = _flatten(record["response"]["body"])
        for item in items + included:
            vocab.setdefault(item["type"], set()).update((item.get("relationships") or {}).keys())
    return vocab


def _flatten(body):
    data = body.get("data")
    items = data if isinstance(data, list) else ([data] if data else [])
    return items, (body.get("included") or [])


class GoldenReplay(unittest.TestCase):
    """One mirror, loaded from the recordings, replayed against every recording."""

    @classmethod
    def setUpClass(cls):
        cls.records = sorted(_records(), key=_load_order)
        cls.mirror, _ = build()
        import urllib.parse

        def load(record):
            include = urllib.parse.parse_qs(record["request"]["query"]).get("include", [None])[0]
            cls.mirror.writer.route_page(
                record["response"]["body"], "backfill",
                synthesized=cls.mirror.writer.synthesized_rels(include))

        # Definitions first: a field datum's typed projection reads its
        # definition's data_type.
        for record in cls.records:
            if "field_definition" in record["name"]:
                load(record)
        for record in cls.records:
            load(record)

    @classmethod
    def tearDownClass(cls):
        cls.mirror.close()

    def replay(self, record):
        request = record["request"]
        status, headers, body = wsgi_get(
            self.mirror.wsgi, "/people/v2" + request["path"], request["query"])
        return status, headers, body

    # -- the assertions ---------------------------------------------------
    def test_status_matches(self):
        for record in self.records:
            with self.subTest(record["name"]):
                status, _, _ = self.replay(record)
                self.assertEqual(status, record["response"]["status"])

    def test_attribute_keys_match_pco_exactly(self):
        for record in self.records:
            with self.subTest(record["name"]):
                _, _, mine = self.replay(record)
                theirs, their_inc = _flatten(record["response"]["body"])
                ours, our_inc = _flatten(mine)
                expected = _by_type(theirs + their_inc)
                actual = _by_type(ours + our_inc)
                for rtype, keys in expected.items():
                    if rtype not in actual:
                        continue          # nothing of that type survived the sample
                    self.assertEqual(
                        keys, actual[rtype],
                        f"{record['name']}: {rtype} attribute keys drifted from PCO")

    def test_no_relationship_pco_sent_is_lost(self):
        for record in self.records:
            with self.subTest(record["name"]):
                _, _, mine = self.replay(record)
                theirs, their_inc = _flatten(record["response"]["body"])
                ours, our_inc = _flatten(mine)
                expected = _rels_by_type(theirs + their_inc)
                actual = _rels_by_type(ours + our_inc)
                for rtype, keys in expected.items():
                    if rtype not in actual:
                        continue
                    self.assertTrue(
                        keys <= actual[rtype],
                        f"{record['name']}: {rtype} lost relationships PCO sent: "
                        f"{sorted(keys - actual[rtype])}")

    def test_no_relationship_is_invented(self):
        vocab = _corpus_relationship_vocabulary()
        for record in self.records:
            with self.subTest(record["name"]):
                _, _, mine = self.replay(record)
                ours, our_inc = _flatten(mine)
                for rtype, keys in _rels_by_type(ours + our_inc).items():
                    invented = keys - vocab.get(rtype, set())
                    self.assertEqual(
                        invented, set(),
                        f"{record['name']}: {rtype} claims relationships PCO never sent: "
                        f"{sorted(invented)}")

    # `next`/`prev` say whether another page exists, which depends on how many
    # rows are held — and the corpus holds a sample.
    PAGINATION_META = {"next", "prev", "total_count", "count"}

    def test_meta_keys_cover_pcos(self):
        for record in self.records:
            with self.subTest(record["name"]):
                _, _, mine = self.replay(record)
                expected = set((record["response"]["body"].get("meta") or {}).keys())
                actual = set((mine.get("meta") or {}).keys())
                missing = expected - actual - self.PAGINATION_META
                self.assertEqual(
                    missing, set(),
                    f"{record['name']}: meta keys PCO sends and the mirror does not: "
                    f"{sorted(missing)}")

    def test_ordering_matches_pco(self):
        """The recorded rows come back in the recorded order.

        Page *membership* cannot be compared: the mirror holds a sample, so its
        page one of `/people` is a different twenty-five and PCO's page-one rows
        are scattered across the mirror's listing. So the query is replayed
        unpaged and the answer is filtered down to the rows PCO's page contained —
        their relative order is the part that has to hold, and it is exactly what
        the numeric-id and case-folding rules were wrong about.
        """
        import urllib.parse
        for record in self.records:
            theirs, _ = _flatten(record["response"]["body"])
            if len(theirs) < 2:
                continue
            with self.subTest(record["name"]):
                query = urllib.parse.parse_qs(record["request"]["query"])
                query.pop("offset", None)
                query["per_page"] = ["100"]
                flat = urllib.parse.urlencode(
                    [(k, v) for k, vals in query.items() for v in vals], safe="[]")
                _, _, mine = wsgi_get(self.mirror.wsgi,
                                      "/people/v2" + record["request"]["path"], flat)
                recorded = [i["id"] for i in theirs]
                actual = [i["id"] for i in _flatten(mine)[0]]
                missing = [i for i in recorded if i not in set(actual)]
                self.assertEqual(missing, [],
                                 f"{record['name']}: rows PCO returned that the mirror "
                                 f"does not hold at all")
                self.assertEqual([i for i in actual if i in set(recorded)], recorded,
                                 f"{record['name']}: same rows, different order than PCO")

    def test_single_and_nested_reads_match_exactly(self):
        for record in self.records:
            if "collection" in record["name"] or "order" in record["name"] \
                    or "where" in record["name"] or "filter" in record["name"]:
                continue
            with self.subTest(record["name"]):
                _, _, mine = self.replay(record)
                theirs, _ = _flatten(record["response"]["body"])
                ours, _ = _flatten(mine)
                self.assertEqual([i["id"] for i in ours], [i["id"] for i in theirs])

    def test_included_sets_match(self):
        for record in self.records:
            with self.subTest(record["name"]):
                _, _, mine = self.replay(record)
                theirs, their_inc = _flatten(record["response"]["body"])
                # Only meaningful where the primary rows all survived the sample.
                ours, our_inc = _flatten(mine)
                if [i["id"] for i in ours] != [i["id"] for i in theirs]:
                    continue
                self.assertEqual(sorted((i["type"], i["id"]) for i in our_inc),
                                 sorted((i["type"], i["id"]) for i in their_inc),
                                 f"{record['name']}: included[] differs from PCO")

    def test_links_point_at_the_mirror_and_name_only_real_relationships(self):
        """Deliberately not PCO's map, and deliberately mirror-relative.

        The mirror generates the link map from the registry so a record looks the
        same however it was synced — PCO sends `self`/`html` on a list page and a
        dozen more on a single read. What must hold is that every link it offers
        is one PCO really has, and that none of them sends a caller to PCO, where
        their pcomirror key would not work.
        """
        pco_links = {}
        for record in _records():
            items, included = _flatten(record["response"]["body"])
            for item in items + included:
                pco_links.setdefault(item["type"], set()).update((item.get("links") or {}).keys())
        for record in self.records:
            with self.subTest(record["name"]):
                _, _, mine = self.replay(record)
                ours, our_inc = _flatten(mine)
                for item in ours + our_inc:
                    offered = set(item.get("links") or {})
                    self.assertLessEqual(
                        offered, pco_links.get(item["type"], set()),
                        f"{record['name']}: {item['type']} offers a link PCO does not")
                    for key, url in (item.get("links") or {}).items():
                        if key == "html":
                            continue      # PCO's web UI for a human; not an API URL
                        self.assertTrue(
                            url.startswith("/people/v2/"),
                            f"{record['name']}: {key} still points at PCO")


class GoldenCorpusHygiene(unittest.TestCase):
    """The corpus is real data's shape without real data in it. Keep it that way."""

    def test_every_recording_is_well_formed(self):
        names = set()
        for record in _records():
            self.assertIn("request", record)
            self.assertIn("response", record)
            self.assertEqual(record["request"]["method"], "GET",
                             "the corpus records reads only")
            names.add(record["name"])
        self.assertGreaterEqual(len(names), 15)

    # An independent backstop for the capture-time leak check, written against the
    # shape of the values rather than against the sanitizer's own map — so it
    # still fails if the sanitizer is the thing that is broken.
    FORBIDDEN = {
        "an email outside example.org": r"[\w.+-]+@(?!example\.org)[\w.-]+\.\w+",
        "a phone number outside the 555 range": r"\+1(?!555)\d{10}",
        "an un-redacted avatar URL": r"avatars\.planningcenteronline\.com/(?!uploads/redacted)",
        "a real account code in a web link": r"/people/(?!XX)[A-Za-z]{1,4}\d+",
        "a populated medical note": r'"medical_notes": "[^"]',
        "an un-synthetic record id": r"\b(?!1000|10000)\d{8,9}\b",
    }

    def test_no_recording_carries_real_data(self):
        import re
        for record in _records():
            blob = json.dumps(record)
            for label, pattern in self.FORBIDDEN.items():
                hits = set(re.findall(pattern, blob))
                self.assertEqual(hits, set(),
                                 f"{record['name']} contains {label}")

    def test_manifest_describes_the_corpus(self):
        with open(os.path.join(GOLDEN, "manifest.json")) as fh:
            manifest = json.load(fh)
        recorded = {r["name"] for r in _records()}
        self.assertEqual({c["name"] for c in manifest["calls"]}, recorded)


if __name__ == "__main__":
    unittest.main()
