import unittest

from base import build
from fakepco import res
from pcomirror.config import now_iso


class TestBackfill(unittest.TestCase):
    def test_backfill_with_sideloaded_children(self):
        m, fake = build()
        fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z",
                        primary_campus={"data": {"type": "Campus", "id": "10"}})
        fake.add_person("2", "Grace", "Hopper", "2026-01-02T00:00:00Z")
        fake.add_child("Email", "e1", "1", {"address": "ada@x.org", "primary": True}, "2026-01-01T00:00:00Z")
        fake.add_child("PhoneNumber", "p1", "1", {"number": "555", "primary": True}, "2026-01-01T00:00:00Z")
        fake.add(res("Campus", "10", {"name": "Downtown"}, updated="2025-01-01T00:00:00Z"))

        n = m.ingestor.backfill("person")
        self.assertEqual(n, 2)
        # people projected
        self.assertEqual(m.db.query_one("SELECT count(*) c FROM person")["c"], 2)
        # children routed from included[] into their own tables, linked to the person
        self.assertEqual(m.db.query_one(
            "SELECT count(*) c FROM email WHERE person_pco_id='1' AND deleted_at IS NULL")["c"], 1)
        self.assertEqual(m.db.query_one(
            "SELECT number FROM phone_number WHERE pco_id='p1'")["number"], "555")
        # 'one' relationship target (campus) also sideloaded
        self.assertEqual(m.db.query_one("SELECT name FROM campus WHERE pco_id='10'")["name"], "Downtown")
        # watermark handed to reconcile = max updated_at
        st = m.ingestor.state("person")
        self.assertEqual(st["reconcile_watermark"], "2026-01-02T00:00:00Z")
        self.assertEqual(st["phase"], "streaming")

    def test_backfill_paginates_and_ties(self):
        m, fake = build()
        # 150 people, 120 sharing one updated_at second (forces pagination + tie handling)
        for i in range(120):
            fake.add_person(str(i), f"P{i}", "Same", "2026-03-01T12:00:00Z")
        for i in range(120, 150):
            fake.add_person(str(i), f"P{i}", "Later", f"2026-03-02T00:{i%60:02d}:00Z")
        n = m.ingestor.backfill("person")
        self.assertEqual(m.db.query_one("SELECT count(*) c FROM person")["c"], 150)


class TestReconcile(unittest.TestCase):
    def _seed(self):
        m, fake = build()
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        return m, fake

    def test_incremental_sweep_catches_update(self):
        m, fake = self._seed()
        # a change a webhook "missed": bump the person at PCO
        fake.data["Person"]["1"]["attributes"].update(last_name="Byron", updated_at="2026-05-01T00:00:00Z")
        applied = m.ingestor.incremental_sweep("person")
        self.assertGreaterEqual(applied, 1)
        self.assertEqual(m.db.query_one("SELECT last_name FROM person WHERE pco_id='1'")["last_name"], "Byron")

    def test_merger_poll_tombstones_and_redirects(self):
        m, fake = self._seed()
        fake.add_person("2", "Ada", "Dup", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        fake.merge(keep="1", remove="2", created="2026-04-01T00:00:00Z")
        applied = m.ingestor.merger_poll()
        self.assertEqual(applied, 1)
        row = m.db.query_one("SELECT deleted_at, merged_into_pco_id, tombstone_reason FROM person WHERE pco_id='2'")
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(row["merged_into_pco_id"], "1")
        self.assertEqual(row["tombstone_reason"], "merged")

    def test_merger_poll_applies_each_merge_exactly_once(self):
        """The watermark filter is `gte`, so the newest merge comes back for ever.

        Applying it every time is what put a permanent `GET /people/{survivor}`
        on a 120-second cadence in a real mirror — answered `404`, because that
        survivor had since been deleted, for ever.
        """
        m, fake = self._seed()
        fake.add_person("2", "Ada", "Dup", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        fake.merge(keep="1", remove="2", created="2026-04-01T00:00:00Z")
        self.assertEqual(m.ingestor.merger_poll(), 1)
        m.ingestor.drain_hydration()

        # The same merge, still the newest, still returned by `gte`.
        self.assertEqual(m.ingestor.merger_poll(), 0)
        self.assertEqual(m.db.query_one("SELECT count(*) c FROM hydration_task")["c"], 0)
        # ...and nothing was re-fetched on its behalf.
        fake.request_log.clear()
        m.ingestor.merger_poll()
        m.ingestor.drain_hydration()
        self.assertEqual([p for meth, p in fake.request_log if p.startswith("/people/")], [])

    def test_merger_poll_terminates_when_a_page_shares_one_second(self):
        """`per_page` merges at one `created_at` cannot advance the cursor.

        `gte` then returns the identical full page every time, so paging on asks
        for it again for ever.
        """
        m, fake = self._seed()
        for i in range(2, 152):
            fake.add_person(str(i), f"Dup{i}", "Person", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        for i in range(2, 152):
            fake.merge(keep="1", remove=str(i), created="2026-04-01T00:00:00Z")
        self.assertEqual(m.ingestor.merger_poll(), 100)   # one page, then stuck
        self.assertEqual(m.ingestor.merger_poll(), 0)     # and it stops, rather than spinning

    def test_delete_audit_tombstones_hard_deleted(self):
        m, fake = self._seed()
        fake.add_person("2", "Gone", "Person", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        # hard-delete at PCO (disappears from listings, GET returns 404)
        fake.destroy("Person", "2")
        tombstoned = m.ingestor.delete_audit("person")
        self.assertEqual(tombstoned, 1)
        self.assertIsNotNone(m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='2'")["deleted_at"])
        self.assertIsNone(m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='1'")["deleted_at"])

    def test_include_diff_tombstones_removed_child(self):
        m, fake = build()
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        fake.add_child("Email", "e1", "1", {"address": "a@x.org"}, "2026-01-01T00:00:00Z")
        fake.add_child("Email", "e2", "1", {"address": "b@x.org"}, "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        self.assertEqual(m.db.query_one("SELECT count(*) c FROM email WHERE deleted_at IS NULL")["c"], 2)
        # one email removed at PCO (person unchanged) -> include-diff on hydrate catches it
        fake.destroy("Email", "e2")
        m.ingestor.hydrate("person", "1")
        self.assertIsNotNone(m.db.query_one("SELECT deleted_at FROM email WHERE pco_id='e2'")["deleted_at"])
        self.assertIsNone(m.db.query_one("SELECT deleted_at FROM email WHERE pco_id='e1'")["deleted_at"])

    def test_drift_probe_records_counts(self):
        m, fake = self._seed()
        d = m.ingestor.drift_probe("person")
        self.assertEqual(d["total_count"], 1)
        self.assertEqual(d["mirror_live"], 1)
        self.assertEqual(d["delta"], 0)

    def test_drift_probe_does_not_ask_for_a_collection_pco_will_not_serve(self):
        """`GET /household_memberships` is a 404 by design — there is no such
        collection, only `/households/{id}/household_memberships`. Probing it
        spent a request every 15 minutes to log a permanent error."""
        m, fake = self._seed()
        fake.request_log.clear()
        d = m.ingestor.drift_probe("household_membership")
        self.assertEqual(fake.request_log, [])
        self.assertIsNone(d["total_count"])
        self.assertIsNone(d["delta"])
        self.assertEqual(d["mirror_live"], 0)


class TestScheduledAudit(unittest.TestCase):
    """The delete audit is the only mechanism that finds a hard delete with no
    webhook and no merge. It was written, tested, documented and exposed on the
    CLI — and never scheduled."""

    def _seeded(self):
        from pcomirror.scheduler import Scheduler
        m, fake = build()
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        fake.add_person("2", "Gone", "Person", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        return m, fake, Scheduler(m)

    def test_scheduler_runs_the_delete_audit(self):
        m, fake, sched = self._seeded()
        fake.destroy("Person", "2")
        sched.run_once()
        self.assertIsNotNone(
            m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='2'")["deleted_at"])

    def test_audit_is_not_repeated_within_its_interval(self):
        m, fake, sched = self._seeded()
        sched.run_once()
        first = m.ingestor.state("person")["last_audit_completed_at"]
        self.assertIsNotNone(first)
        fake.request_log.clear()
        sched.run_once()
        self.assertEqual([p for meth, p in fake.request_log if "fields[Person]" in p], [])

    def test_audit_interval_of_zero_switches_it_off(self):
        m, fake, sched = self._seeded()
        m.settings.audit_interval_hours = 0
        fake.destroy("Person", "2")
        sched.run_once()
        self.assertIsNone(m.ingestor.state("person")["last_audit_completed_at"])

    def test_a_failed_audit_waits_rather_than_retrying_every_tick(self):
        """Due-ness reads the later of started/completed, so an audit that dies
        partway does not re-enumerate the organization on the next 5s tick."""
        m, fake, sched = self._seeded()

        def explode(*a, **k):
            raise ConnectionResetError("PCO is unreachable")
        m.ingestor.client.get = explode
        sched.run_once()
        st = m.ingestor.state("person")
        self.assertIsNone(st["last_audit_completed_at"])
        self.assertIsNotNone(st["last_audit_started_at"])
        self.assertFalse(sched._audit_due("person", now_iso()))


class TestReferenceRefresh(unittest.TestCase):
    def test_list_and_replace_tombstones_absent(self):
        m, fake = build()
        fake.add(res("Campus", "10", {"name": "A"}, updated="2026-01-01T00:00:00Z"))
        fake.add(res("Campus", "11", {"name": "B"}, updated="2026-01-01T00:00:00Z"))
        m.ingestor.reference_refresh("campus")
        self.assertEqual(m.db.query_one("SELECT count(*) c FROM campus WHERE deleted_at IS NULL")["c"], 2)
        fake.destroy("Campus", "11")   # removed at PCO
        m.ingestor.reference_refresh("campus")
        self.assertIsNotNone(m.db.query_one("SELECT deleted_at FROM campus WHERE pco_id='11'")["deleted_at"])
        self.assertIsNone(m.db.query_one("SELECT deleted_at FROM campus WHERE pco_id='10'")["deleted_at"])


if __name__ == "__main__":
    unittest.main()
