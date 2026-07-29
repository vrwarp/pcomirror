"""The relationship Planning Center synthesizes for a nested `include`.

`include=households.people` does not only sideload the households' members: PCO
also adds a `people` relationship to the **Person** the request was about,
listing what it sideloaded on that person's behalf. The mirror has to echo it,
and the shape of the echo turned out to have three separate rules, none of which
the mirror had right.

Each was measured against the live API — two of them in a divergence report taken
minutes after a family was added to a real organization:

  * **An empty second level is still a relationship.** Reading a household's
    memberships with `include=person,person.emails,person.phone_numbers` gives
    *every* membership an `emails` relationship, `"data": []` for the members who
    have no address on file. The mirror emitted the key only when it had ids to
    put in it — so a household of four with one contactable adult came back with
    the relationship on one membership and silently absent on the other three.
    Absent and empty are not the same answer.

  * **An empty first level is not.** A person with no field data gets no
    `field_definition` key at all from `include=field_data.field_definition`
    (`tests/golden/inc_two_nested_paths.json`). PCO synthesizes the key per
    first-level record it resolved, so no first-level records means no key.

  * **A duplicate is real.** PCO concatenates each first-level record's array
    rather than merging them, so a person in two households that share a member
    gets that member's id twice. The mirror resolved the whole level in one
    query, which is a set — and answered one id short of PCO for every family
    where a parent and a child are in a household together and one of them is
    also in another.
"""
from __future__ import annotations

import unittest

from base import build, wsgi_get
from fakepco import res


def _rel_ids(resource, name):
    node = ((resource.get("relationships") or {}).get(name) or {}).get("data")
    if node is None:
        return None
    return [x["id"] for x in node]


class TestAnEmptySecondLevelIsStillARelationship(unittest.TestCase):
    """The shape a household of four came back in, three memberships short."""

    def _household_of_two(self):
        m, fake = build()
        ts = "2026-01-01T00:00:00Z"
        fake.add_person("1", "Quillon", "West", ts)
        fake.add_person("2", "Trinity", "Mills", ts)
        fake.add_child("Email", "e1", "1", {"address": "quillon@x.org"}, ts)
        fake.add(res("Household", "h1", {"name": "West Household", "member_count": 2},
                     relationships={"people": {"data": [{"type": "Person", "id": "1"},
                                                        {"type": "Person", "id": "2"}]}},
                     updated=ts))
        fake.add_membership("m1", "h1", "1", role="head")
        fake.add_membership("m2", "h1", "2")
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        m.ingestor.nested_walk("household_membership")
        return m, fake

    def test_every_membership_carries_the_relationship(self):
        m, _ = self._household_of_two()
        _, _, body = wsgi_get(m.wsgi, "/people/v2/households/h1/household_memberships",
                              "include=person,person.emails")
        by_id = {r["id"]: r for r in body["data"]}
        self.assertEqual(_rel_ids(by_id["m1"], "emails"), ["e1"])
        self.assertEqual(_rel_ids(by_id["m2"], "emails"), [],
                         "the member with no email lost the relationship entirely")

    def test_the_empty_one_is_a_relationship_not_a_missing_key(self):
        m, _ = self._household_of_two()
        _, _, body = wsgi_get(m.wsgi, "/people/v2/households/h1/household_memberships",
                              "include=person,person.emails")
        m2 = next(r for r in body["data"] if r["id"] == "m2")
        self.assertIn("emails", m2["relationships"])
        self.assertEqual(m2["relationships"]["emails"]["data"], [])


class TestAnEmptyFirstLevelSynthesizesNothing(unittest.TestCase):
    """`tests/golden/inc_two_nested_paths.json`: a person with no field data has
    no `field_definition` relationship, where a person with two has one."""

    def test_no_first_level_rows_means_no_key(self):
        m, fake = build()
        ts = "2026-01-01T00:00:00Z"
        fake.add_person("1", "Ada", "L", ts)
        m.ingestor.backfill("person")
        _, _, body = wsgi_get(m.wsgi, "/people/v2/people/1",
                              "include=field_data,field_data.field_definition")
        self.assertNotIn("field_definition", body["data"].get("relationships") or {})


class TestTheEchoConcatenatesRatherThanMerges(unittest.TestCase):
    """Two households sharing a member. PCO lists the shared member once per
    household; the mirror listed them once in total."""

    def _two_households(self):
        m, fake = build()
        ts = "2026-01-01T00:00:00Z"
        for pid, first in (("1", "Trinity"), ("2", "Joanna"), ("3", "Quillon")):
            fake.add_person(pid, first, "Mills", ts)
        # Person 1 is in both, so PCO names them twice.
        fake.data["Person"]["1"]["relationships"]["households"] = {
            "data": [{"type": "Household", "id": "h1"}, {"type": "Household", "id": "h2"}]}
        fake.add(res("Household", "h1", {"name": "First"},
                     relationships={"people": {"data": [{"type": "Person", "id": "2"},
                                                        {"type": "Person", "id": "1"}]}},
                     updated=ts))
        fake.add(res("Household", "h2", {"name": "Second"},
                     relationships={"people": {"data": [{"type": "Person", "id": "3"},
                                                        {"type": "Person", "id": "1"}]}},
                     updated=ts))
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        return m, fake

    def test_a_member_of_two_households_is_named_twice(self):
        m, _ = self._two_households()
        _, _, body = wsgi_get(m.wsgi, "/people/v2/people/1",
                              "include=households,households.people")
        self.assertEqual(_rel_ids(body["data"], "people"), ["2", "1", "3", "1"])

    def test_included_still_carries_each_resource_once(self):
        """Concatenating the relationship does not duplicate the sideload —
        JSON:API requires `included[]` to hold each resource once."""
        m, _ = self._two_households()
        _, _, body = wsgi_get(m.wsgi, "/people/v2/people/1",
                              "include=households,households.people")
        people = [r["id"] for r in body["included"] if r["type"] == "Person"]
        self.assertEqual(sorted(people), ["1", "2", "3"])

    def test_the_order_is_pcos_own(self):
        """Household order comes from the person's array, member order from each
        household's — which is why the households edge is resolved in the order
        `raw` holds it rather than sorted."""
        m, fake = build()
        ts = "2026-01-01T00:00:00Z"
        for pid in ("1", "2", "3"):
            fake.add_person(pid, f"P{pid}", "X", ts)
        fake.data["Person"]["1"]["relationships"]["households"] = {
            "data": [{"type": "Household", "id": "h2"}, {"type": "Household", "id": "h1"}]}
        fake.add(res("Household", "h1", {"name": "First"},
                     relationships={"people": {"data": [{"type": "Person", "id": "1"}]}},
                     updated=ts))
        fake.add(res("Household", "h2", {"name": "Second"},
                     relationships={"people": {"data": [{"type": "Person", "id": "3"},
                                                        {"type": "Person", "id": "2"}]}},
                     updated=ts))
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        _, _, body = wsgi_get(m.wsgi, "/people/v2/people/1",
                              "include=households,households.people")
        self.assertEqual(_rel_ids(body["data"], "people"), ["3", "2", "1"])
        self.assertEqual(_rel_ids(body["data"], "households"), ["h2", "h1"])


if __name__ == "__main__":
    unittest.main()
