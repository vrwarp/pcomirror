"""A record may not be flattened, and a flattened record must be repairable.

Both halves come from one live incident. A pass-through of `/lists/{id}/people` —
which Planning Center answers with `primary_campus` and nothing else, no matter
what the person actually has — overwrote 82 people in a running mirror. Their
household edge went with it, so `include=households` sideloaded nothing and the
app reading the mirror told a room full of youth workers that nobody could reach
those families.

The equal-timestamp guard stops the overwrite. But stopping it is only half:
those 82 records stayed wrong for days afterwards, because nothing re-reads a
record whose `updated_at` will never move again. The incremental sweep is keyed
on `updated_at`, the audit only looks for deletions, and the drift probe only
counts rows. A degraded record was a one-way door.
"""
from __future__ import annotations

import json
import unittest

from base import build, wsgi_get
from fakepco import FakePCO, res

RICH_RELS = {
    "emails": {"data": [{"type": "Email", "id": "e1"}]},
    "phone_numbers": {"data": [{"type": "PhoneNumber", "id": "p1"}]},
    "households": {"data": [{"type": "Household", "id": "h1"}]},
}


def _fixture():
    fake = FakePCO()
    fake.add(res("Household", "h1", {"name": "Byron", "member_count": 2},
                 relationships={"people": {"data": [{"type": "Person", "id": "1"}]}}))
    fake.add(res("Person", "1", {"first_name": "Ada", "last_name": "Byron", "child": True,
                                 "name": "Ada Byron"},
                 relationships=dict(RICH_RELS), updated="2024-01-01T00:00:00Z"))
    fake.add_child("Email", "e1", "1", {"address": "ada@example.org", "primary": True},
                   "2024-01-01T00:00:00Z")
    fake.add_child("PhoneNumber", "p1", "1", {"number": "555-0100", "primary": True},
                   "2024-01-01T00:00:00Z")
    return fake


def _thin_copy(person_raw: dict) -> dict:
    """What PCO returns for a Person under `/lists/{id}/people`: every attribute,
    one relationship, and it is not the one anybody wanted."""
    return {"type": "Person", "id": person_raw["id"],
            "attributes": dict(person_raw["attributes"]),
            "relationships": {"primary_campus": {"data": None}},
            "links": {"self": "/people/v2/people/" + person_raw["id"]}}


class TestAnEqualTimestampMayNotFlatten(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build(_fixture())
        for name in ("person", "email", "phone_number", "household"):
            self.m.ingestor.backfill(name)

    def _rels(self, pid="1"):
        row = self.m.db.query_one("SELECT raw FROM person WHERE pco_id=?", (pid,))
        return set(json.loads(row["raw"]).get("relationships", {}))

    def test_a_bare_list_page_does_not_take_the_households_edge(self):
        before = self._rels()
        self.assertIn("households", before)
        raw = json.loads(self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"])
        self.m.writer.route_page({"data": [_thin_copy(raw)]}, "passthrough")
        self.assertEqual(self._rels(), before)

    def test_an_equal_sized_but_different_set_does_not_take_it_either(self):
        """The reason counting is not enough. A payload can carry exactly as many
        relationships as the mirror holds and still be missing the one that
        matters — a narrower `include=` returns a different set, not a smaller."""
        raw = json.loads(self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"])
        swapped = dict(raw)
        swapped["relationships"] = {"emails": {"data": []}, "phone_numbers": {"data": []},
                                    "addresses": {"data": []}}          # same count, no households
        self.assertEqual(len(swapped["relationships"]), len(raw["relationships"]))
        self.m.writer.route_page({"data": [swapped]}, "passthrough")
        self.assertIn("households", self._rels())

    def test_a_genuinely_newer_payload_still_wins(self):
        """The guard must not freeze a record. A relationship that really was
        removed moves `updated_at`, and that write has to land."""
        raw = json.loads(self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"])
        newer = dict(raw)
        newer["attributes"] = dict(raw["attributes"], updated_at="2026-01-01T00:00:00Z")
        newer["relationships"] = {"emails": {"data": []}}
        self.m.writer.route_page({"data": [newer]}, "passthrough")
        self.assertEqual(self._rels(), {"emails"})

    def test_a_richer_payload_still_lands_at_an_equal_timestamp(self):
        """Repair depends on this: the re-fetch carries the same `updated_at` as
        the flattened copy, so if a superset could not land, nothing could heal."""
        self.m.db.execute(
            "UPDATE person SET raw=? WHERE pco_id='1'",
            (json.dumps(_thin_copy(json.loads(
                self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"]))),))
        self.assertNotIn("households", self._rels())
        self.m.ingestor.hydrate("person", "1")
        self.assertIn("households", self._rels())


class TestAFlattenedRecordIsRepaired(unittest.TestCase):
    """The half that was missing: noticing, without being told."""

    def setUp(self):
        self.m, self.fake = build(_fixture())
        for name in ("person", "email", "phone_number", "household"):
            self.m.ingestor.backfill(name)
        raw = json.loads(self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"])
        # what the mirror was left holding after the live incident
        self.m.db.execute("UPDATE person SET raw=?, source='passthrough' WHERE pco_id='1'",
                          (json.dumps(_thin_copy(raw)),))

    def _rels(self):
        return set(json.loads(
            self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"]
        ).get("relationships", {}))

    def test_counting_rows_cannot_see_a_hollow_one(self):
        """Why this check has to exist. Drift is the only standing check that
        looks at the whole table, and a flattened record still counts as one."""
        probe = self.m.ingestor.drift_probe("person")
        self.assertEqual(probe["delta"], 0)
        self.assertNotIn("households", self._rels())

    def test_the_repair_pass_finds_and_refetches_it(self):
        queued = self.m.ingestor.repair_incomplete("person", min_age_seconds=0)
        self.assertEqual(queued, 1)
        self.m.ingestor.drain_hydration()
        self.assertIn("households", self._rels())

    def test_a_record_just_refetched_is_not_queued_again(self):
        """What stops a record PCO will not answer any more fully than this from
        being re-read on every pass forever."""
        self.m.ingestor.repair_incomplete("person", min_age_seconds=0)
        self.m.ingestor.drain_hydration()
        self.assertEqual(self.m.ingestor.repair_incomplete("person"), 0)

    def test_a_whole_table_of_flattened_rows_is_still_seen(self):
        """The case a peer comparison would miss: a pass-through of a whole
        collection flattens every row at once, leaving no richer row to compare
        against. The expectation has to come from the registry, not the data."""
        self.assertEqual(
            self.m.db.query_one("SELECT count(*) c FROM person WHERE deleted_at IS NULL")["c"], 1)
        self.assertEqual(self.m.ingestor.repair_incomplete("person", min_age_seconds=0), 1)

    def test_the_household_edge_is_readable_again(self):
        """The end the user sees: `include=households` sideloads the household."""
        _, _, before = wsgi_get(self.m.wsgi, "/people/v2/people/1", "include=households")
        self.assertIsNone(before.get("included"))
        self.m.ingestor.repair_incomplete("person", min_age_seconds=0)
        self.m.ingestor.drain_hydration()
        _, _, after = wsgi_get(self.m.wsgi, "/people/v2/people/1", "include=households")
        self.assertEqual([i["id"] for i in after["included"]], ["h1"])

    def test_the_scheduler_runs_it(self):
        from pcomirror.scheduler import Scheduler
        sched = Scheduler(self.m)
        sched._last_drift = -10_000            # due now
        self.m.db.execute("UPDATE person SET last_synced_at='2020-01-01T00:00:00Z' WHERE pco_id='1'")
        sched.run_once()
        self.m.ingestor.drain_hydration()
        self.assertIn("households", self._rels())


if __name__ == "__main__":
    unittest.main()
