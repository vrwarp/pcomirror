import unittest

from base import build, wsgi_get
from fakepco import res
from pcomirror import registry
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
        tombstoned, restored = m.ingestor.delete_audit("person")
        self.assertEqual(tombstoned, 1)
        self.assertEqual(restored, 0)
        self.assertIsNotNone(m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='2'")["deleted_at"])
        self.assertIsNone(m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='1'")["deleted_at"])

    def test_delete_audit_restores_a_record_the_mirror_lost(self):
        """The reverse gap: PCO lists an id the mirror has no row for.

        Nothing else ever repairs it — the sweep filters on `updated_at`, which
        for a record this old is far behind the watermark, so a live divergence
        report once promised `staleness` for a person no sweep would ever
        collect again. The audit already enumerates every id, so it is the one
        pass that can notice, and now the one that fixes."""
        m, fake = self._seed()
        fake.add_person("2", "Lost", "Person", "2024-01-31T06:37:30Z")
        m.ingestor.backfill("person")
        m.db.execute("DELETE FROM person WHERE pco_id='2'")

        tombstoned, restored = m.ingestor.delete_audit("person")

        self.assertEqual((tombstoned, restored), (0, 1))
        row = m.db.query_one("SELECT deleted_at, source FROM person WHERE pco_id='2'")
        self.assertIsNotNone(row)
        self.assertIsNone(row["deleted_at"])

    def test_delete_audit_does_not_dig_up_a_tombstone(self):
        """`confirm_live` stays reserved for rows the mirror thinks live — the
        writer's documented invariant. A burial PCO disagrees with is surfaced
        by the divergence checker's store notes, never silently reversed on the
        strength of an id listing alone."""
        m, fake = self._seed()
        fake.add_person("2", "Buried", "Person", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        m.writer.tombstone("person", "2", None, "destroyed")

        tombstoned, restored = m.ingestor.delete_audit("person")

        self.assertEqual(restored, 0)
        self.assertIsNotNone(
            m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='2'")["deleted_at"])

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


class TestAChildCannotOutliveItsOwner(unittest.TestCase):
    """Every other way a child gets tombstoned needs the owner still to be there
    to ask about. When the owner itself is gone — a 404 on hydration, an absence
    the audit confirmed, a `destroyed` webhook — nothing looked, and the emails
    and phone numbers of a hard-deleted person stayed live for ever."""

    def _seed(self):
        m, fake = build()
        fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        fake.add_person("2", "Gone", "Person", "2026-01-01T00:00:00Z")
        fake.add_child("Email", "e1", "1", {"address": "ada@x.org"}, "2026-01-01T00:00:00Z")
        fake.add_child("Email", "e2", "2", {"address": "gone@x.org"}, "2026-01-01T00:00:00Z")
        fake.add_child("PhoneNumber", "p2", "2", {"number": "5550100"}, "2026-01-01T00:00:00Z")
        fake.add(res("Household", "h1", {"name": "The Persons"}, updated="2026-01-01T00:00:00Z"))
        fake.add_membership("hm2", "h1", "2")
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        m.ingestor.nested_walk("household_membership")
        return m, fake

    def _live(self, m, table):
        return [r["pco_id"] for r in m.db.query(
            f"SELECT pco_id FROM {table} WHERE deleted_at IS NULL")]

    def test_the_audit_takes_the_children_with_the_person(self):
        m, fake = self._seed()
        fake.destroy("Person", "2")
        self.assertEqual(m.ingestor.delete_audit("person")[0], 1)
        self.assertEqual(self._live(m, "email"), ["e1"])
        self.assertEqual(self._live(m, "phone_number"), [])

    def test_a_deleted_person_stops_being_served_from_the_child_collection(self):
        m, fake = self._seed()
        fake.destroy("Person", "2")
        m.ingestor.delete_audit("person")
        _, _, body = wsgi_get(m.wsgi, "/people/v2/emails", "per_page=100")
        self.assertEqual([d["attributes"]["address"] for d in body["data"]], ["ada@x.org"])
        self.assertEqual(body["meta"]["total_count"], 1)

    def test_a_404_on_hydration_takes_them_too(self):
        m, fake = self._seed()
        fake.destroy("Person", "2")
        m.ingestor.hydrate("person", "2")
        self.assertEqual(self._live(m, "email"), ["e1"])

    def test_a_deleted_household_takes_its_memberships(self):
        """The walk only ever visits *live* households, so a membership under a
        deleted one is never revisited and would never be tombstoned."""
        m, fake = self._seed()
        m.writer.tombstone("household", "h1", None, "destroyed")
        self.assertEqual(self._live(m, "household_membership"), [])

    def test_a_merge_does_not_take_them(self):
        """PCO moves a merged person's children to the survivor rather than
        deleting them, and the survivor's hydration re-routes them at their
        existing `updated_at` — which the monotonic guard would refuse to
        resurrect. Cascading here would bury them permanently."""
        m, fake = self._seed()
        fake.merge(keep="1", remove="2", created="2026-04-01T00:00:00Z")
        m.ingestor.merger_poll()
        self.assertEqual(self._live(m, "email"), ["e1", "e2"])

    def test_a_person_who_comes_back_brings_the_children_with_them(self):
        m, fake = self._seed()
        m.writer.tombstone("person", "2", None, "audit_absent")
        self.assertEqual(self._live(m, "email"), ["e1"])
        fake.data["Person"]["2"]["attributes"]["updated_at"] = "2026-06-01T00:00:00Z"
        m.ingestor.hydrate("person", "2")
        self.assertEqual(self._live(m, "person"), ["1", "2"])
        self.assertEqual(self._live(m, "email"), ["e1", "e2"])

    def test_a_re_read_too_old_to_revive_the_owner_leaves_the_children_buried(self):
        """The `EXISTS` guard: an ordinary sweep re-reading a still-tombstoned
        person at a timestamp too old to revive them must not dig up their
        children on its own."""
        m, fake = self._seed()
        m.writer.tombstone("person", "2", None, "audit_absent")
        m.ingestor.incremental_sweep("person")
        self.assertEqual(self._live(m, "person"), ["1"])
        self.assertEqual(self._live(m, "email"), ["e1"])

    def test_a_child_deleted_on_its_own_evidence_keeps_its_tombstone(self):
        m, fake = self._seed()
        fake.destroy("Email", "e2")
        m.ingestor.hydrate("person", "2")
        self.assertEqual(self._live(m, "email"), ["e1"])
        m.ingestor.hydrate("person", "2")       # a later re-read must not revive it
        self.assertEqual(self._live(m, "email"), ["e1"])

    def test_ownership_is_containment_not_reference(self):
        """`person.primary_campus` points at a campus. Deleting a campus must
        never tombstone the people in it."""
        self.assertEqual(registry.owned_children("campus"), ())
        self.assertEqual(
            [(c.table, fk) for c, fk in registry.owned_children("person")],
            [("email", "person_pco_id"), ("phone_number", "person_pco_id"),
             ("social_profile", "person_pco_id"), ("address", "person_pco_id"),
             ("field_datum", "person_pco_id"), ("note", "person_pco_id")])
        self.assertEqual(
            [(c.table, fk) for c, fk in registry.owned_children("household")],
            [("household_membership", "household_pco_id")])
        # The per-parent collections PCO serves under one owner and nowhere else
        # are owned by that owner, not by the person they also name. A list
        # result naming a person is a reference — deleting the person must not
        # decide what is in somebody's list.
        self.assertEqual(
            [(c.table, fk) for c, fk in registry.owned_children("list")],
            [("list_result", "list_pco_id")])
        self.assertEqual(
            [(c.table, fk) for c, fk in registry.owned_children("form")],
            [("form_submission", "form_pco_id")])


class TestTheLastChildIsStillADelete(unittest.TestCase):
    """PCO answers `"included": []` both for a person whose emails were not
    requested and for one who has no emails left. Reading which children to diff
    off the *response* meant a person's last email could be deleted at PCO and
    stay in the mirror for ever."""

    def test_deleting_the_only_email_is_noticed(self):
        m, fake = build()
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        fake.add_child("Email", "e1", "1", {"address": "ada@x.org"}, "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        fake.destroy("Email", "e1")
        m.ingestor.hydrate("person", "1")
        self.assertIsNotNone(
            m.db.query_one("SELECT deleted_at FROM email WHERE pco_id='e1'")["deleted_at"])

    def test_a_narrower_hydrate_does_not_touch_what_it_did_not_ask_about(self):
        m, fake = build()
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        fake.add_child("Email", "e1", "1", {"address": "a@x.org"}, "2026-01-01T00:00:00Z")
        fake.add_child("PhoneNumber", "p1", "1", {"number": "555"}, "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        m.ingestor.hydrate("person", "1", includes=["phone_numbers"])
        self.assertIsNone(
            m.db.query_one("SELECT deleted_at FROM email WHERE pco_id='e1'")["deleted_at"])


class TestBothHalvesHaveToAgreeBeforeAChildIsBuried(unittest.TestCase):
    """A compound document has two halves and they can disagree.

    Measured live, three minutes after a parent was added to a real
    organization: `/people?include=emails` came back with the new person's
    `relationships.emails` naming an address and `included[]` not carrying it.
    Reading `included[]` alone as the answer would have buried an email Planning
    Center had, in the same breath, said the person has.
    """

    TS = "2026-01-01T00:00:00Z"

    def _person_naming_their_email(self):
        m, fake = build()
        fake.add_person("1", "Jemima", "Allen", self.TS,
                        emails={"data": [{"type": "Email", "id": "e1"}]})
        fake.add_child("Email", "e1", "1", {"address": "jemima@x.org"}, self.TS)
        m.ingestor.backfill("person")
        return m, fake

    def test_a_relationship_pco_still_names_is_not_a_delete(self):
        m, fake = self._person_naming_their_email()
        # PCO stops sideloading the email but goes on naming it — the inconsistent
        # document the live report caught.
        fake.destroy("Email", "e1")
        m.ingestor.hydrate("person", "1")
        self.assertIsNone(
            m.db.query_one("SELECT deleted_at FROM email WHERE pco_id='e1'")["deleted_at"],
            "an email PCO still lists on the person was buried on the strength "
            "of a sideload that did not arrive")

    def test_dropping_it_from_both_halves_is_still_a_delete(self):
        m, fake = self._person_naming_their_email()
        fake.data["Person"]["1"]["relationships"]["emails"] = {"data": []}
        fake.destroy("Email", "e1")
        m.ingestor.hydrate("person", "1")
        self.assertIsNotNone(
            m.db.query_one("SELECT deleted_at FROM email WHERE pco_id='e1'")["deleted_at"])


class TestAVanishedCollectionBuriesItsParent(unittest.TestCase):
    """`GET /households/{id}/household_memberships` answering 404 is the only
    announcement PCO makes that a household is gone — no webhook is guaranteed,
    and `where[updated_at]` cannot return a record that no longer exists. The
    walk used to shrug it off, so three households abandoned while somebody added
    a family were still live in a mirror a day later."""

    TS = "2026-01-01T00:00:00Z"

    def _walked(self):
        m, fake = build()
        fake.add_person("1", "Debra", "Allen", self.TS)
        fake.add(res("Household", "h1", {"name": "Allen Household"}, updated=self.TS))
        fake.add_membership("hm1", "h1", "1")
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        m.ingestor.nested_walk("household_membership")
        return m, fake

    def test_the_household_and_its_memberships_are_tombstoned(self):
        m, fake = self._walked()
        fake.destroy("Household", "h1")
        m.ingestor.nested_walk("household_membership")
        self.assertIsNotNone(
            m.db.query_one("SELECT deleted_at FROM household WHERE pco_id='h1'")["deleted_at"])
        self.assertIsNotNone(
            m.db.query_one(
                "SELECT deleted_at FROM household_membership WHERE pco_id='hm1'")["deleted_at"])

    def test_it_stops_being_served(self):
        m, fake = self._walked()
        fake.destroy("Household", "h1")
        m.ingestor.nested_walk("household_membership")
        status, _, _ = wsgi_get(m.wsgi, "/people/v2/households/h1", "")
        self.assertEqual(status, 410)

    def test_the_404_is_confirmed_before_anything_is_buried(self):
        """The nested 404 is evidence, not proof. A household PCO still answers
        for keeps every member's family — the cascade is not something to run on
        a guess."""
        m, fake = self._walked()
        from pcomirror.pcoclient import Response
        fake._nested = lambda *a, **k: Response(404, {}, b'{"errors":[{"code":"404"}]}')
        m.ingestor.nested_walk("household_membership")
        self.assertIsNone(
            m.db.query_one("SELECT deleted_at FROM household WHERE pco_id='h1'")["deleted_at"])


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
        sched.drain_cold()
        self.assertIsNotNone(
            m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='2'")["deleted_at"])

    def test_audit_is_not_repeated_within_its_interval(self):
        m, fake, sched = self._seeded()
        sched.drain_cold()
        first = m.ingestor.state("person")["last_audit_completed_at"]
        self.assertIsNotNone(first)
        fake.request_log.clear()
        sched.run_once()
        sched.drain_cold()
        self.assertEqual([p for meth, p in fake.request_log if "fields[Person]" in p], [])

    def test_audit_interval_of_zero_switches_it_off(self):
        m, fake, sched = self._seeded()
        m.settings.audit_interval_hours = 0
        fake.destroy("Person", "2")
        sched.run_once()
        sched.drain_cold()
        self.assertIsNone(m.ingestor.state("person")["last_audit_completed_at"])

    def test_the_audit_covers_every_resource_that_declares_one(self):
        """A household is hard-deleted by the same click a person is, and was
        just as invisible: the audit ran for `person` and nothing else."""
        from pcomirror.scheduler import Scheduler
        m, fake = build()
        fake.add_person("1", "Debra", "Allen", "2026-01-01T00:00:00Z")
        fake.add(res("Household", "h1", {"name": "Allen Household"},
                     updated="2026-01-01T00:00:00Z"))
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        fake.destroy("Household", "h1")
        Scheduler(m).drain_cold()
        self.assertIsNotNone(
            m.db.query_one("SELECT deleted_at FROM household WHERE pco_id='h1'")["deleted_at"])

    def test_a_failed_audit_waits_rather_than_retrying_every_tick(self):
        """Due-ness reads the later of started/completed, so an audit that dies
        partway does not re-enumerate the organization on the next 5s tick."""
        m, fake, sched = self._seeded()

        def explode(*a, **k):
            raise ConnectionResetError("PCO is unreachable")
        m.ingestor.client.get = explode
        sched.run_once()
        sched.drain_cold()
        st = m.ingestor.state("person")
        self.assertIsNone(st["last_audit_completed_at"])
        self.assertIsNotNone(st["last_audit_started_at"])
        self.assertFalse(sched._audit_due("person", now_iso()))


class TestTheColdLaneInterleaves(unittest.TestCase):
    """Bulk work runs as bounded resumable units so the hot lane never queues
    behind it. Measured live before this existed: a first-day audit plus a
    burst of hang-and-retry held the single loop for eight minutes while
    hydration tasks with `not_before` in the past sat undrained."""

    def _org(self, people=12):
        m, fake = build()
        for i in range(1, people + 1):
            fake.add_person(str(i), f"P{i}", "Reed", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        return m, fake

    def test_an_audit_step_is_bounded_and_resumes(self):
        m, fake = self._org()
        for pid in ("3", "7"):
            fake.destroy("Person", pid)
        steps, out = 0, None
        while True:
            out = m.ingestor.delete_audit_step("person", budget=1)
            steps += 1
            self.assertLess(steps, 50)
            if out["done"]:
                break
        self.assertGreater(steps, 2, "one budgeted request per step cannot finish in two")
        self.assertEqual(out["tombstoned"], 2)
        for pid in ("3", "7"):
            self.assertIsNotNone(m.db.query_one(
                "SELECT deleted_at FROM person WHERE pco_id=?", (pid,))["deleted_at"])

    def test_a_round_survives_between_steps(self):
        m, fake = self._org()
        fake.destroy("Person", "5")
        m.ingestor.delete_audit_step("person", budget=1)
        # State and scratch are on disk; the next step resumes this round
        # rather than starting over — which is also what makes a crash free.
        self.assertIsNotNone(m.ingestor.audit_round("person"))
        started = m.ingestor.state("person")["last_audit_started_at"]
        while not m.ingestor.delete_audit_step("person", budget=2)["done"]:
            pass
        self.assertEqual(m.ingestor.state("person")["last_audit_started_at"], started,
                         "resuming is not restarting")
        self.assertIsNotNone(m.db.query_one(
            "SELECT deleted_at FROM person WHERE pco_id='5'")["deleted_at"])

    def test_the_one_shot_audit_still_answers_at_once(self):
        m, fake = self._org()
        fake.destroy("Person", "2")
        self.assertEqual(m.ingestor.delete_audit("person"), (1, 0))

    def test_restores_resume_too(self):
        m, fake = self._org()
        m.db.execute("DELETE FROM person WHERE pco_id='9'")     # the mirror lost one
        while not m.ingestor.delete_audit_step("person", budget=1)["done"]:
            pass
        self.assertIsNotNone(m.db.query_one(
            "SELECT 1 FROM person WHERE pco_id='9' AND deleted_at IS NULL"))

    def test_a_walk_round_is_paced_and_completes(self):
        m, fake = build()
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        for h in ("71", "72", "73"):
            fake.add(res("Household", h, {"name": f"H{h}"}, updated="2026-01-01T00:00:00Z"))
            fake.add_membership(f"m{h}", h, "1")
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        m.ingestor.backfill("household_membership")
        m.db.execute("UPDATE nested_walk_state SET walked_at='2026-01-01T00:00:00Z'")
        fake.destroy("HouseholdMembership", "m72")              # dropped upstream since
        steps, out = 0, None
        while True:
            out = m.ingestor.walk_round_step("household_membership", budget=1)
            steps += 1
            self.assertLess(steps, 10)
            if out["done"]:
                break
        self.assertGreaterEqual(steps, 4,
                                "three parents at one per step, plus the completing call")
        self.assertIsNotNone(m.db.query_one(
            "SELECT deleted_at FROM household_membership WHERE pco_id='m72'")["deleted_at"])

    def test_a_bounded_backfill_resumes_to_the_same_place(self):
        m, fake = build()
        for i in range(1, 251):
            fake.add_person(str(i), f"P{i}", "Reed", f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z")
        calls = 0
        while m.ingestor.state("person")["backfill_completed_at"] is None:
            m.ingestor.backfill("person", max_pages=1)
            calls += 1
            self.assertLess(calls, 20)
        self.assertGreater(calls, 1, "one page per call cannot finish in one")
        self.assertEqual(m.db.query_one("SELECT count(*) c FROM person")["c"], 250)

    def test_a_saturated_second_does_not_stall_the_enumeration(self):
        """The signature of a bulk import: more records created in one second
        than a page holds. Measured live — the address audit sat at one 2024
        cursor re-reading the same page every tick, for ever. The enumeration
        pages through the saturated second by offset, like the backfill's
        `_drain_second`, and completes."""
        m, fake = build()
        for i in range(1, 251):
            fake.add_person(str(i), f"P{i}", "Imported", "2026-01-01T00:00:00Z",)
        for i, p in enumerate(fake.data["Person"].values()):
            p["attributes"]["created_at"] = "2024-01-30T06:37:57Z"    # one second, all of them
        m.ingestor.backfill("person")
        fake.destroy("Person", "17")
        steps, out = 0, None
        while True:
            out = m.ingestor.delete_audit_step("person", budget=2)
            steps += 1
            self.assertLess(steps, 40, "the enumeration must advance past the cluster")
            if out["done"]:
                break
        self.assertEqual(out["tombstoned"], 1)
        self.assertIsNotNone(m.db.query_one(
            "SELECT deleted_at FROM person WHERE pco_id='17'")["deleted_at"])

    def test_one_long_round_does_not_own_the_lane(self):
        """Rotation: with one audit round in progress and another due, the lane
        alternates instead of feeding every tick to the first in registry
        order — which was measured starving the second audit and the walks."""
        from pcomirror import ingest as ingest_mod
        from pcomirror.scheduler import Scheduler
        m, fake = build()
        for i in range(1, 251):
            fake.add_person(str(i), f"P{i}", "Reed", "2026-01-01T00:00:00Z")
        fake.add_child("Email", "e1", "1", {"address": "a@x.org"}, "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        m.ingestor.backfill("email")
        sched = Scheduler(m)
        sched.drain_cold()                         # settle late-backfills + day-one audits
        sched.COLD_UNIT_BUDGET = 1                 # person needs several units
        sched._cold_ring_last = None               # deterministic ring start
        ingest_mod.request_audit(m.db, "person", "operator")
        ingest_mod.request_audit(m.db, "email", "operator")
        sched.run_cold_once()                      # person's round opens
        self.assertIsNotNone(m.ingestor.audit_round("person"))
        sched.run_cold_once()                      # rotation hands email its unit
        for _ in range(30):
            if m.ingestor.state("email")["last_audit_completed_at"]:
                break
            sched.run_cold_once()
        self.assertIsNotNone(m.ingestor.state("email")["last_audit_completed_at"],
                             "the second audit must not wait for the first round")
        self.assertIsNotNone(m.ingestor.audit_round("person") or
                             m.ingestor.state("person")["last_audit_completed_at"])
        sched.drain_cold()
        self.assertIsNotNone(m.ingestor.state("person")["last_audit_completed_at"])

    def test_the_hot_lane_never_runs_the_bulk(self):
        from pcomirror.scheduler import Scheduler
        m, fake = build()
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        sched = Scheduler(m)
        sched.run_once()
        self.assertIsNone(m.ingestor.state("person")["last_audit_started_at"],
                          "audits belong to the cold lane")
        self.assertIsNone(m.ingestor.state("email")["backfill_completed_at"],
                          "late backfills belong to the cold lane")
        sched.drain_cold()
        self.assertIsNotNone(m.ingestor.state("person")["last_audit_started_at"])
        self.assertIsNotNone(m.ingestor.state("email")["backfill_completed_at"])

    def test_a_hung_cold_unit_does_not_stall_the_hot_lane(self):
        """The measured failure, reproduced: an audit enumeration that never
        returns, and a hydration task the hot lane drains beside it anyway."""
        import threading as _t
        from pcomirror import ingest as ingest_mod
        from pcomirror.scheduler import Scheduler
        m, fake = build()
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        sched = Scheduler(m)
        sched.drain_cold()                     # settle the backfills and day-one audit
        ingest_mod.request_audit(m.db, "person", "operator")

        release, entered = _t.Event(), _t.Event()
        original = fake.send

        def hanging(method, url, headers, body):
            if "fields%5BPerson%5D" in url or "fields[Person]" in url:
                entered.set()
                release.wait(timeout=30)
            return original(method, url, headers, body)

        fake.send = hanging
        cold = _t.Thread(target=sched.run_cold_once, daemon=True)
        cold.start()
        self.assertTrue(entered.wait(timeout=10), "the audit should be mid-hang")
        m.ingestor.enqueue_hydration("person", "1", reason="write_verify")
        sched.run_once()                       # the hot lane, on this thread
        self.assertEqual(
            m.db.query_one("SELECT count(*) c FROM hydration_task")["c"], 0,
            "the hot lane drained while the cold lane hung")
        release.set()
        cold.join(timeout=30)
        self.assertFalse(cold.is_alive())


class TestDriftAsksForTheAudit(unittest.TestCase):
    """§7.4 always said what a delta means — `mirror_live > total_count` ⇒
    ghosts a missed delete left behind, `<` ⇒ rows nothing collected — and the
    probe wrote it down every 15 minutes while a ghost stayed served, and
    offered back by every duplicate-check search, until the nightly cadence.
    Now the probe *requests* the audit; when it runs is still the scheduler's
    decision, under a cooldown, so a count PCO and the mirror genuinely
    disagree about costs a bounded re-audit rather than one per probe."""

    def _seeded(self):
        m, fake = build()
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        fake.add_person("2", "Gone", "Person", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        return m, fake

    def test_a_ghost_makes_the_probe_request_an_audit(self):
        m, fake = self._seeded()
        fake.destroy("Person", "2")
        m.ingestor.drift_probe("person")
        self.assertIsNotNone(m.ingestor.audit_requested("person"))

    def test_a_row_the_mirror_lacks_requests_one_too(self):
        """The restore direction: the audit is also what re-collects a record
        every sweep's watermark has already passed."""
        m, fake = self._seeded()
        m.db.execute("DELETE FROM person WHERE pco_id='2'")
        m.ingestor.drift_probe("person")
        self.assertIsNotNone(m.ingestor.audit_requested("person"))

    def test_matching_counts_request_nothing(self):
        m, fake = self._seeded()
        m.ingestor.drift_probe("person")
        self.assertIsNone(m.ingestor.audit_requested("person"))

    def test_a_resource_with_no_audit_declared_is_never_asked_for_one(self):
        """The registry decides which resources are audited; a request the
        scheduler could never honour would sit in `mirror_meta` for ever."""
        m, fake = self._seeded()
        m.writer.upsert("note", "n1",
                        {"id": "n1", "type": "Note",
                         "attributes": {"updated_at": "2026-01-01T00:00:00Z"}}, "reconcile")
        m.ingestor.drift_probe("note")                     # mirror 1, PCO 0
        self.assertIsNone(m.ingestor.audit_requested("note"))

    def test_the_request_waits_out_the_cooldown_then_pulls_the_audit_forward(self):
        from pcomirror.scheduler import Scheduler
        m, fake = self._seeded()
        sched = Scheduler(m)
        sched.drain_cold()                                 # the first audit runs at once
        self.assertIsNotNone(m.ingestor.state("person")["last_audit_completed_at"])
        fake.destroy("Person", "2")
        m.ingestor.drift_probe("person")
        self.assertFalse(sched._audit_due("person", now_iso()),
                         "inside the cooldown the request must wait")
        two_hours_ago = sched._minus(2 * 3600, now_iso())
        m.ingestor._set("person", last_audit_started_at=two_hours_ago,
                        last_audit_completed_at=two_hours_ago)
        self.assertTrue(sched._audit_due("person", now_iso()),
                        "past the cooldown the request pulls the audit ahead of cadence")

    def test_without_a_request_the_cadence_alone_decides(self):
        from pcomirror.scheduler import Scheduler
        m, fake = self._seeded()
        sched = Scheduler(m)
        sched.drain_cold()
        two_hours_ago = sched._minus(2 * 3600, now_iso())
        m.ingestor._set("person", last_audit_started_at=two_hours_ago,
                        last_audit_completed_at=two_hours_ago)
        self.assertFalse(sched._audit_due("person", now_iso()))

    def test_the_probe_never_demotes_an_operators_request(self):
        """The probe re-measures every 15 minutes. Without the escalation
        guard, a person clicking the audit button on a drifted mirror had
        their request quietly downgraded to `drift` by the very next probe —
        back behind the cooldown, or refused outright where scheduled audits
        are switched off."""
        from pcomirror import ingest as ingest_mod
        m, fake = self._seeded()
        fake.destroy("Person", "2")
        ingest_mod.request_audit(m.db, "person", "operator")
        m.ingestor.drift_probe("person")                   # measures the ghost
        self.assertEqual(m.ingestor.audit_requested("person"), "operator")

    def test_the_audit_answers_the_request_and_clears_it(self):
        m, fake = self._seeded()
        fake.destroy("Person", "2")
        m.ingestor.drift_probe("person")
        tombstoned, _restored = m.ingestor.delete_audit("person")
        self.assertEqual(tombstoned, 1)
        self.assertIsNone(m.ingestor.audit_requested("person"),
                          "a completed audit has answered whatever drift asked")

    def test_a_scheduler_pass_carries_the_ghost_to_its_grave(self):
        """End to end on the scheduler's own machinery: probe measures, request
        stands, cooldown passes, audit runs, ghost buried, request cleared."""
        from pcomirror.scheduler import Scheduler
        m, fake = self._seeded()
        sched = Scheduler(m)
        sched.drain_cold()
        fake.destroy("Person", "2")
        sched._last_drift = -1e9                           # make the probe due again
        two_hours_ago = sched._minus(2 * 3600, now_iso())
        m.ingestor._set("person", last_audit_started_at=two_hours_ago,
                        last_audit_completed_at=two_hours_ago)
        sched.run_once()
        sched.drain_cold()
        self.assertIsNotNone(
            m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='2'")["deleted_at"])
        self.assertIsNone(m.ingestor.audit_requested("person"))


class TestSplitEdgesAreRejoined(unittest.TestCase):
    """The household edge is stored twice — the household's `people` array and
    each member's own `households` array — learned from different requests at
    different moments, with neither record's `updated_at` obliged to move
    again. When the halves disagree, the serving layer answers from whichever
    half the caller reads: the household page showed the family while the
    child's own read said they had none, for hours. This pass is what looks."""

    def _split_person(self):
        """Mirror holds: household names person 1; person 1 names no household."""
        m, fake = build()
        fake.add_person("1", "Janet", "Lee", "2026-01-01T00:00:00Z")
        fake.add(res("Household", "77", {"name": "Lee Household"},
                     updated="2026-01-01T00:00:00Z",
                     relationships={"people": {"data": [{"type": "Person", "id": "1"}]}}))
        m.ingestor.backfill("person")          # person raw carries no households edge
        m.ingestor.backfill("household")
        # PCO itself is consistent — the person's copy there has the edge — the
        # mirror simply collected the person at the wrong moment.
        fake.data["Person"]["1"]["relationships"] = {
            "households": {"data": [{"type": "Household", "id": "77"}]}}
        return m, fake

    def _age(self, m, table, pco_id):
        m.db.execute(f"UPDATE {table} SET last_synced_at='2026-01-01T00:00:00Z' "
                     f"WHERE pco_id=?", (pco_id,))

    def test_a_person_missing_their_households_edge_is_re_read(self):
        m, fake = self._split_person()
        self._age(m, "person", "1")
        self.assertEqual(m.ingestor.repair_split_edges(), 1)
        task = m.db.query_one("SELECT * FROM hydration_task WHERE pco_id='1'")
        self.assertEqual(task["reason"], "split_edge")
        m.ingestor.drain_hydration()
        held = m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"]
        self.assertIn('"77"', held, "the re-read should land the missing edge")
        self.assertEqual(m.ingestor.repair_split_edges(), 0, "and the pass goes quiet")

    def test_a_household_missing_a_member_is_re_read(self):
        m, fake = build()
        fake.add_person("1", "Janet", "Lee", "2026-01-01T00:00:00Z",
                        households={"data": [{"type": "Household", "id": "77"}]})
        fake.add(res("Household", "77", {"name": "Lee Household"},
                     updated="2026-01-01T00:00:00Z"))
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")       # household raw carries no people
        fake.data["Household"]["77"]["relationships"] = {
            "people": {"data": [{"type": "Person", "id": "1"}]}}
        self._age(m, "household", "77")
        self.assertEqual(m.ingestor.repair_split_edges(), 1)
        task = m.db.query_one("SELECT * FROM hydration_task WHERE pco_id='77'")
        self.assertEqual((task["resource_type"], task["reason"]), ("household", "split_edge"))
        m.ingestor.drain_hydration()
        held = m.db.query_one("SELECT raw FROM household WHERE pco_id='77'")["raw"]
        self.assertIn('"people"', held)
        self.assertEqual(m.ingestor.repair_split_edges(), 0)

    def test_a_record_synced_recently_is_left_alone(self):
        """The write path's own delayed verify owns the fresh window; this pass
        owns everything older, and the split between them is what stops a
        disagreement PCO itself serves from becoming a spin."""
        m, fake = self._split_person()
        self.assertEqual(m.ingestor.repair_split_edges(), 0)

    def test_an_agreeing_family_queues_nothing(self):
        m, fake = build()
        fake.add_person("1", "Janet", "Lee", "2026-01-01T00:00:00Z",
                        households={"data": [{"type": "Household", "id": "77"}]})
        fake.add(res("Household", "77", {"name": "Lee Household"},
                     updated="2026-01-01T00:00:00Z",
                     relationships={"people": {"data": [{"type": "Person", "id": "1"}]}}))
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        self._age(m, "person", "1")
        self._age(m, "household", "77")
        self.assertEqual(m.ingestor.repair_split_edges(), 0)

    def test_the_scheduler_runs_the_pass_and_the_family_converges(self):
        from pcomirror.scheduler import Scheduler
        m, fake = self._split_person()
        self._age(m, "person", "1")
        sched = Scheduler(m)
        sched._last_drift = -1e9
        sched.run_once()                       # queues the re-read…
        sched.run_once()                       # …and the next tick drains it
        held = m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"]
        self.assertIn('"77"', held)


class TestAnEdgeThatDoesNotResolve(unittest.TestCase):
    """A relationship naming a record the mirror cannot serve.

    Two of these were live at once in a divergence report taken minutes after a
    family was added, and neither was visible to anything else the mirror runs.
    The sweep filters on `updated_at` and neither record's would ever move again;
    the drift probe counts rows and both counts were fine; `repair_incomplete`
    asks whether the relationship *key* is there, and it was.

      * a new parent whose `emails` named an address the mirror held no row for,
        so `include=emails` answered with the id in the relationship and an empty
        `included[]`;
      * a person still listing three households that had been deleted at PCO.
    """

    TS = "2026-01-01T00:00:00Z"

    def test_a_child_pco_named_but_never_sent_is_fetched_by_id(self):
        m, fake = build()
        fake.add_person("1", "Jemima", "Allen", self.TS,
                        emails={"data": [{"type": "Email", "id": "e1"}]})
        m.ingestor.backfill("person")     # the person names e1; nothing sideloaded it
        self.assertIsNone(m.db.query_one("SELECT 1 FROM email WHERE pco_id='e1'"))
        fake.add_child("Email", "e1", "1", {"address": "jemima@x.org"}, self.TS)

        self.assertTrue(m.ingestor.repair_dangling("person", min_age_seconds=0))
        m.ingestor.drain_hydration()

        row = m.db.query_one("SELECT person_pco_id, deleted_at FROM email WHERE pco_id='e1'")
        self.assertIsNotNone(row)
        self.assertEqual(row["person_pco_id"], "1")
        self.assertIsNone(row["deleted_at"])
        _, _, body = wsgi_get(m.wsgi, "/people/v2/people/1", "include=emails")
        self.assertEqual([r["id"] for r in body["included"]], ["e1"])

    def test_an_edge_pco_has_dropped_is_re_read_from_the_holder(self):
        """The households were gone and the person's own record still listed
        them — with an `updated_at` PCO never moved, so no sweep would ever
        re-read it."""
        m, fake = build()
        fake.add_person("1", "Debra", "Allen", self.TS,
                        households={"data": [{"type": "Household", "id": "h1"}]})
        fake.add(res("Household", "h1", {"name": "Allen Household"}, updated=self.TS))
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        # Deleted at PCO and dropped from the person, without their record moving.
        fake.destroy("Household", "h1")
        fake.data["Person"]["1"]["relationships"]["households"] = {"data": []}
        m.ingestor.delete_audit("household")

        self.assertTrue(m.ingestor.repair_dangling("person", min_age_seconds=0))
        m.ingestor.drain_hydration()

        _, _, body = wsgi_get(m.wsgi, "/people/v2/people/1", "include=households")
        self.assertEqual(body["data"]["relationships"]["households"]["data"], [])
        self.assertNotIn("included", body)

    def test_a_record_whose_edges_all_resolve_is_left_alone(self):
        m, fake = build()
        fake.add_person("1", "Ada", "L", self.TS,
                        emails={"data": [{"type": "Email", "id": "e1"}]})
        fake.add_child("Email", "e1", "1", {"address": "ada@x.org"}, self.TS)
        m.ingestor.backfill("person")
        self.assertEqual(m.ingestor.repair_dangling("person", min_age_seconds=0), 0)

    def test_a_record_just_synced_waits_rather_than_spinning(self):
        """If PCO keeps naming an id it will not hand over, this has to cost one
        re-read per interval, not one per pass."""
        m, fake = build()
        fake.add_person("1", "Jemima", "Allen", self.TS,
                        emails={"data": [{"type": "Email", "id": "e1"}]})
        m.ingestor.backfill("person")
        self.assertEqual(m.ingestor.repair_dangling("person"), 0)


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
