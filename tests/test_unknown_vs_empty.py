""""I don't know yet" must never be served as "there is nothing".

Household memberships are the one collection PCO will not list wholesale, so the
mirror collects them by walking one household at a time on a periodic schedule.
That leaves a window — between a household existing and the walk reaching it —
where the mirror holds no rows for it. An empty page is the wrong answer in that
window, and not a little wrong: it is the answer that says a student has no
parent, so Tally's detail screen reads "nobody can reach this family in an
emergency" above a household that has a reachable parent in Planning Center.

The window was not a narrow one either. Every sweep in the scheduler is gated on
the resource having been backfilled, and nothing backfills a resource that was
added to the registry after the mirror was first built — so on an existing
deployment the walk never ran at all, and every household read empty forever.
"""
from __future__ import annotations

import unittest

from base import build, wsgi_get
from fakepco import FakePCO, res


def _fixture():
    """One household, one child, one parent — the shape the UI screen needs."""
    fake = FakePCO()
    fake.add(res("Household", "h1", {"name": "Byron", "member_count": 2},
                 relationships={"people": {"data": [{"type": "Person", "id": "1"},
                                                    {"type": "Person", "id": "2"}]}}))
    for pid, first, child in (("1", "Ada", True), ("2", "Ann", False)):
        fake.add(res("Person", pid, {"first_name": first, "last_name": "Byron", "child": child,
                                     "name": f"{first} Byron"},
                     relationships={"households": {"data": [{"type": "Household", "id": "h1"}]}},
                     updated="2024-01-01T00:00:00Z"))
    fake.add_membership("hm1", "h1", "1", role="child_or_dependent")
    fake.add_membership("hm2", "h1", "2", role="parent_guardian")
    return fake


class TestUnwalkedParentIsNotEmpty(unittest.TestCase):
    """A mirror that holds the households but has never walked their memberships."""

    def setUp(self):
        self.m, self.fake = build(_fixture())
        # Everything *except* the walk — exactly an upgrade that added the
        # resource to the registry and then served traffic.
        for name in ("person", "household"):
            self.m.ingestor.backfill(name)
        self.assertEqual(self.m.db.query_one(
            "SELECT count(*) c FROM household_membership")["c"], 0)
        self.fake.request_log.clear()

    def test_a_never_walked_household_is_filled_on_read(self):
        status, headers, body = wsgi_get(
            self.m.wsgi, "/people/v2/households/h1/household_memberships")
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(d["id"] for d in body["data"]), ["hm1", "hm2"])
        self.assertEqual(self.fake.request_log,
                         [("GET", "/households/h1/household_memberships")])

    def test_the_role_the_parent_is_chosen_by_survives_the_fill(self):
        """The point of the read is the role, so the fill has to land it."""
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/household_memberships")
        roles = {d["id"]: d["attributes"]["household_role"] for d in body["data"]}
        self.assertEqual(roles, {"hm1": "child_or_dependent", "hm2": "parent_guardian"})

    def test_it_costs_one_request_ever_not_one_per_read(self):
        for _ in range(4):
            wsgi_get(self.m.wsgi, "/people/v2/households/h1/household_memberships")
        self.assertEqual(self.fake.request_log,
                         [("GET", "/households/h1/household_memberships")])

    def test_an_include_fills_it_too(self):
        """`?include=household_memberships` reads the same rows by another route."""
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1",
                                   "include=household_memberships")
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(i["id"] for i in body["included"]), ["hm1", "hm2"])

    def test_reached_from_the_person_side_it_walks_that_persons_households(self):
        """`/people/{id}/household_memberships` is a real PCO endpoint, and the
        parent it needs walked is the person's household, not the person."""
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people/1/household_memberships")
        self.assertEqual(status, 200, body)
        self.assertEqual([d["id"] for d in body["data"]], ["hm1"])
        self.assertEqual(self.fake.request_log,
                         [("GET", "/households/h1/household_memberships")])

    def test_a_page_wide_include_is_bounded_rather_than_a_timeout(self):
        """Filling one parent per row of a page is a hundred serial requests. Say
        the collection is not ready — still not "these households are empty"."""
        from pcomirror.serving import Application
        for i in range(3, 8):
            self.fake.add(res("Household", f"h{i}", {"name": f"H{i}", "member_count": 0},
                              relationships={"people": {"data": []}}))
            self.fake.add_membership(f"hm{i}0", f"h{i}", "1", role="parent_guardian")
        self.m.ingestor.backfill("household")
        original = Application.WALK_FILL_BUDGET
        Application.WALK_FILL_BUDGET = 2
        try:
            status, _, body = wsgi_get(self.m.wsgi, "/people/v2/households",
                                       "include=household_memberships&per_page=25")
        finally:
            Application.WALK_FILL_BUDGET = original
        self.assertEqual(status, 503, body)
        self.assertIn("has not been walked", body["errors"][0]["detail"])

    def test_an_outage_is_an_error_not_an_empty_household(self):
        """The failure that matters. Answering 200 with no rows here is a claim
        about the family; a 503 is a claim about the mirror."""
        self.fake.unreachable = True
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/household_memberships")
        self.assertEqual(status, 503, body)
        self.assertIn("not mirrored yet", body["errors"][0]["detail"])

    def test_a_genuinely_empty_household_is_served_as_empty(self):
        """Once walked, empty means empty — the fill must not make every read
        upstream, and a childless household is a legitimate 200 with no rows."""
        self.fake.add(res("Household", "h2", {"name": "Lonely", "member_count": 0},
                          relationships={"people": {"data": []}}))
        self.m.ingestor.backfill("household")
        self.fake.request_log.clear()
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h2/household_memberships")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"], [])
        self.assertEqual(len(self.fake.request_log), 1)
        wsgi_get(self.m.wsgi, "/people/v2/households/h2/household_memberships")
        self.assertEqual(len(self.fake.request_log), 1, "a walked-and-empty parent re-fetched")


class TestWalkLedger(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build(_fixture())
        for name in ("person", "household", "household_membership"):
            self.m.ingestor.backfill(name)

    def test_the_periodic_walk_records_every_parent_it_visited(self):
        """Without the ledger there is nothing to distinguish unknown from empty,
        so the walk has to write it, not just the read-time fill."""
        self.assertEqual(self.m.ingestor.walked_parents("household_membership"), 1)
        self.assertTrue(self.m.ingestor.parent_walked("household_membership", "h1"))
        self.assertFalse(self.m.ingestor.parent_walked("household_membership", "h404"))

    def test_rows_already_held_count_as_walked(self):
        """The ledger arrived after the walk did. A mirror that has been walking
        for months has the rows and no ledger, and re-fetching every parent to
        learn what its own rows already prove is work for nothing."""
        self.m.db.execute("DELETE FROM nested_walk_state")
        self.m.db.init_schema()                       # what a restart does
        self.assertTrue(self.m.ingestor.parent_walked("household_membership", "h1"))
        self.fake.request_log.clear()
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/household_memberships")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(self.fake.request_log, [])

    def test_a_walked_household_costs_nothing_to_read(self):
        self.fake.request_log.clear()
        status, headers, body = wsgi_get(
            self.m.wsgi, "/people/v2/households/h1/household_memberships", "include=person")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["X-Mirror-Source"], "mirror")
        self.assertEqual(self.fake.request_log, [])


class TestTopLevelCollectionMatchesPco(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build(_fixture())
        for name in ("person", "household", "household_membership"):
            self.m.ingestor.backfill(name)

    def test_household_memberships_has_no_top_level_collection(self):
        """PCO answers 404 for `GET /household_memberships`. Answering 200 would
        invent a collection the API being mirrored does not have — and a client
        that found it would be relying on something no other backend serves."""
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/household_memberships")
        self.assertEqual(status, 404, body)
        self.assertIn("Household", body["errors"][0]["detail"])

    def test_the_rows_are_still_there_under_their_parent(self):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/household_memberships")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 2)

    def test_a_single_membership_is_addressed_under_its_household(self):
        """`/household_memberships/{id}` is a 404 upstream too — the row is only
        addressable through its household, and the mirror says the same."""
        status, _, _ = wsgi_get(self.m.wsgi, "/people/v2/household_memberships/hm1")
        self.assertEqual(status, 404)
        status, _, body = wsgi_get(
            self.m.wsgi, "/people/v2/households/h1/household_memberships/hm1")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["id"], "hm1")

    def test_the_self_link_is_the_one_that_works(self):
        """A generated link the router would refuse, or that 404s at PCO, is worse
        than no link: it works against the mirror and breaks against the API the
        mirror stands in for."""
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/household_memberships")
        row = body["data"][0]
        self.assertEqual(row["links"]["self"],
                         f"/people/v2/households/h1/household_memberships/{row['id']}")
        for name, link in row["links"].items():
            status, _, _ = wsgi_get(self.m.wsgi, link)
            self.assertEqual(status, 200, f"{name} link {link} is not served")

    def test_a_membership_read_under_the_wrong_household_is_not_found(self):
        self.fake.add(res("Household", "h9", {"name": "Other", "member_count": 0},
                          relationships={"people": {"data": []}}))
        self.m.ingestor.backfill("household")
        status, _, _ = wsgi_get(
            self.m.wsgi, "/people/v2/households/h9/household_memberships/hm1")
        self.assertEqual(status, 404)


class TestSchedulerAdoptsANewResource(unittest.TestCase):
    """The reason the walk had never run on a live deployment."""

    def setUp(self):
        self.m, self.fake = build(_fixture())
        for name in ("person", "household"):
            self.m.ingestor.backfill(name)

    def test_a_resource_added_after_the_first_backfill_gets_one(self):
        from pcomirror.scheduler import Scheduler
        self.assertIsNone(
            self.m.ingestor.state("household_membership")["backfill_completed_at"])
        Scheduler(self.m).drain_cold()
        self.assertIsNotNone(
            self.m.ingestor.state("household_membership")["backfill_completed_at"])
        self.assertEqual(self.m.db.query_one(
            "SELECT count(*) c FROM household_membership WHERE deleted_at IS NULL")["c"], 2)

    def test_it_adopts_the_resource_once_not_on_every_tick(self):
        """Adoption is a one-off. From the second tick on it is an ordinary
        resource on its own cadence — which for a full walk is daily."""
        from pcomirror.scheduler import Scheduler
        sched = Scheduler(self.m)
        sched.drain_cold()
        first = self.m.ingestor.state("household_membership")["backfill_completed_at"]
        sched.run_once()
        sched.drain_cold()
        self.assertEqual(self.m.ingestor.state("household_membership")["backfill_completed_at"],
                         first)


if __name__ == "__main__":
    unittest.main()
