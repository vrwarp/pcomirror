"""Catching the mirror disagreeing with Planning Center, continuously.

The case this has to catch, and the reason it exists: PCO demotes a previous
primary email **without moving `updated_at`** (measured — `docs/mutation-testing.md`).
The sweep filters on that timestamp so the record never comes back; the monotonic
writer would refuse it as not-newer if it did; drift counts rows and the count
does not change. Nothing in the design converges on it. Asking PCO is the only
way to find out.
"""
from __future__ import annotations

import json
import unittest

from base import build, wsgi_get
from fakepco import FakePCO, res
from pcomirror import divergence
from pcomirror.divergence import rules as cmp_mod
from pcomirror.ratelimit import RateLimiter, RateLimitBusy


def collection(*people, total=None):
    return {"data": list(people),
            "meta": {"total_count": total if total is not None else len(people),
                     "count": len(people)}}


def person(pid, first="Ada", last="Lovelace", updated="2026-01-01T00:00:00Z", **attrs):
    return {"id": pid, "type": "Person",
            "attributes": {"first_name": first, "last_name": last,
                           "updated_at": updated, **attrs}}


class TestWhatCountsAsADifference(unittest.TestCase):
    """A naive `a != b` reports every response as wrong. These are the decisions."""

    def test_identical_documents_agree(self):
        doc = collection(person("1"))
        self.assertEqual(cmp_mod.compare(doc, json.loads(json.dumps(doc))), [])

    def test_the_mirrors_own_meta_is_not_a_difference(self):
        mine = collection(person("1"))
        mine["meta"].update({"mirror": {"source": "mirror"}, "can_search_by": ["search_name"],
                             "can_filter": []})
        theirs = collection(person("1"))
        theirs["meta"]["can_filter"] = ["created_since"]
        self.assertEqual(cmp_mod.compare(mine, theirs), [])

    def test_links_are_not_a_difference(self):
        """Generated from the registry and rewritten relative, on purpose."""
        mine, theirs = collection(person("1")), collection(person("1"))
        mine["data"][0]["links"] = {"self": "/people/v2/people/1"}
        theirs["data"][0]["links"] = {"self": "https://api.planningcenteronline.com/people/v2/people/1"}
        mine["links"] = {"self": "/people/v2/people"}
        self.assertEqual(cmp_mod.compare(mine, theirs), [])

    def test_a_resources_own_meta_is_not_a_difference(self):
        mine, theirs = collection(person("1")), collection(person("1"))
        mine["data"][0]["meta"] = {"mirror": {"last_synced_at": "2026-01-01T00:00:00Z"}}
        self.assertEqual(cmp_mod.compare(mine, theirs), [])

    def test_a_count_that_differs_is_a_difference(self):
        """Stricter than the golden corpus on purpose: live, the mirror holds
        the whole organization, so a count that differs is a plain statement."""
        found = cmp_mod.compare(collection(person("1"), total=1),
                                collection(person("1"), total=2))
        self.assertTrue(any(d.pointer == "$.meta.total_count" for d in found))


class TestTheDifferencesItFinds(unittest.TestCase):
    def test_an_attribute_that_differs(self):
        found = cmp_mod.compare(collection(person("1", first="Ada")),
                                collection(person("1", first="Grace")))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pointer, "$.[Person/1].attributes.first_name")
        self.assertEqual((found[0].mirror, found[0].pco), ("Ada", "Grace"))

    def test_a_record_pco_has_and_the_mirror_does_not(self):
        found = cmp_mod.compare(collection(person("1")),
                                collection(person("1"), person("2")))
        self.assertTrue(any("Person/2" in d.pointer for d in found))

    def test_a_record_the_mirror_has_and_pco_does_not(self):
        found = cmp_mod.compare(collection(person("1"), person("2")),
                                collection(person("1")))
        self.assertTrue(any("Person/2" in d.pointer for d in found))

    def test_the_same_records_in_a_different_order(self):
        """Ordering is a real bug class here — PCO sorts ids numerically."""
        found = cmp_mod.compare(collection(person("2"), person("1")),
                                collection(person("1"), person("2")))
        self.assertEqual([d.note for d in found], ["same records, different order"])

    def test_a_relationship_pco_sent_and_the_mirror_lost(self):
        mine, theirs = collection(person("1")), collection(person("1"))
        theirs["data"][0]["relationships"] = {
            "households": {"data": [{"type": "Household", "id": "900"}]}}
        found = cmp_mod.compare(mine, theirs)
        self.assertTrue(any(d.pointer.endswith("relationships.households") for d in found))

    def test_a_relationship_with_different_members(self):
        mine, theirs = collection(person("1")), collection(person("1"))
        mine["data"][0]["relationships"] = {
            "households": {"data": [{"type": "Household", "id": "900"}]}}
        theirs["data"][0]["relationships"] = {
            "households": {"data": [{"type": "Household", "id": "900"},
                                    {"type": "Household", "id": "901"}]}}
        found = cmp_mod.compare(mine, theirs)
        self.assertEqual(found[0].note, "related ids differ")

    def test_a_wall_of_differences_is_capped(self):
        many = collection(*[person(str(i), first=f"A{i}") for i in range(200)])
        other = collection(*[person(str(i), first=f"B{i}") for i in range(200)])
        self.assertLessEqual(len(cmp_mod.compare(many, other)), cmp_mod.MAX_DIFFERENCES)


class TestStalenessIsNotDivergence(unittest.TestCase):
    """One heals itself; the other never will. Burying the second under the
    first is how this feature would fail quietly."""

    def test_pco_newer_is_only_lag(self):
        mine = collection(person("1", first="Ada", updated="2026-01-01T00:00:00Z"))
        theirs = collection(person("1", first="Grace", updated="2026-02-01T00:00:00Z"))
        found = cmp_mod.compare(mine, theirs)
        self.assertEqual(cmp_mod.classify(found, mine, theirs), "staleness")

    def test_the_same_timestamp_is_a_divergence(self):
        """The primary-demotion class: the sweep filters this record out and the
        monotonic writer would refuse it, so nothing converges on it."""
        stamp = "2026-01-01T00:00:00Z"
        mine = collection(person("1", updated=stamp, primary=True))
        theirs = collection(person("1", updated=stamp, primary=False))
        found = cmp_mod.compare(mine, theirs)
        self.assertEqual(cmp_mod.classify(found, mine, theirs), "divergence")

    def test_a_record_not_swept_yet_is_lag(self):
        mine, theirs = collection(person("1")), collection(person("1"), person("2"))
        self.assertEqual(cmp_mod.classify(cmp_mod.compare(mine, theirs), mine, theirs),
                         "staleness")

    def test_a_record_the_mirror_invented_is_not_lag(self):
        mine, theirs = collection(person("1"), person("2")), collection(person("1"))
        self.assertEqual(cmp_mod.classify(cmp_mod.compare(mine, theirs), mine, theirs),
                         "divergence")

    def test_agreement_is_neither(self):
        doc = collection(person("1"))
        self.assertEqual(cmp_mod.classify([], doc, doc), "match")


class TestSamplingByShape(unittest.TestCase):
    """Uniform sampling spends the budget on a thousand copies of one query."""

    def test_ids_and_paging_collapse_into_one_shape(self):
        first = divergence.shape_of("/people/v2/people/1/emails",
                                    ["include", "offset", "per_page"])
        second = divergence.shape_of("/people/v2/people/99999/emails",
                                     ["include", "per_page"])
        self.assertEqual(first, second)
        self.assertEqual(first, "/people/v2/people/{id}/emails?include")

    def test_different_filters_are_different_shapes(self):
        self.assertNotEqual(divergence.shape_of("/people/v2/people", ["where[grade]"]),
                            divergence.shape_of("/people/v2/people", ["where[child]"]))

    def test_query_key_order_does_not_matter(self):
        self.assertEqual(divergence.shape_of("/people/v2/people", ["order", "include"]),
                         divergence.shape_of("/people/v2/people", ["include", "order"]))


class ShadowCase(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build()
        self.m.settings.shadow_per_minute = 10
        self.fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        self.fake.add_child("Email", "e1", "1", {"address": "ada@x.org", "primary": True},
                            "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")
        self.m.ingestor.backfill("email")


class TestObservingAndDraining(ShadowCase):
    def test_a_read_enrols_its_shape(self):
        wsgi_get(self.m.wsgi, "/people/v2/people", "where[status]=active")
        shapes = [r["shape"] for r in self.m.db.query("SELECT shape FROM shadow_sample")]
        self.assertEqual(shapes, ["/people/v2/people?where[status]"])

    def test_repeat_reads_are_counted_not_duplicated(self):
        for _ in range(5):
            wsgi_get(self.m.wsgi, "/people/v2/people")
        row = self.m.db.query_one("SELECT shape, sum(seen) seen FROM shadow_sample")
        self.assertEqual(row["seen"], 5)

    def test_nothing_is_enrolled_while_it_is_off(self):
        self.m.settings.shadow_per_minute = 0
        wsgi_get(self.m.wsgi, "/people/v2/people")
        self.assertEqual(self.m.db.query("SELECT * FROM shadow_sample"), [])

    def test_draining_checks_and_records_nothing_when_they_agree(self):
        wsgi_get(self.m.wsgi, "/people/v2/people/1")
        self.assertEqual(self.m.divergence.run_once(), 1)
        self.assertEqual(divergence.recent(self.m.db), [])
        self.assertIsNotNone(
            self.m.db.query_one("SELECT last_agreed_at FROM shadow_sample")["last_agreed_at"])

    def test_the_least_recently_checked_shape_goes_first(self):
        wsgi_get(self.m.wsgi, "/people/v2/people")
        wsgi_get(self.m.wsgi, "/people/v2/people", "where[status]=active")
        self.m.divergence.run_once(limit=1)
        checked = self.m.db.query(
            "SELECT shape FROM shadow_sample WHERE last_checked_at IS NOT NULL")
        self.assertEqual(len(checked), 1)
        self.m.divergence.run_once(limit=1)
        still = self.m.db.query(
            "SELECT shape FROM shadow_sample WHERE last_checked_at IS NOT NULL")
        self.assertEqual(len(still), 2, "the second pass should pick the other shape")

    def test_the_checkers_own_reads_do_not_enrol_themselves(self):
        wsgi_get(self.m.wsgi, "/people/v2/people/1")
        before = self.m.db.query_one("SELECT count(*) c FROM shadow_sample")["c"]
        self.m.divergence.run_once()
        self.assertEqual(self.m.db.query_one("SELECT count(*) c FROM shadow_sample")["c"],
                         before)

    def test_a_failed_check_does_not_stop_the_pass(self):
        wsgi_get(self.m.wsgi, "/people/v2/people/1")
        self.fake.unreachable = True
        self.assertEqual(self.m.divergence.run_once(), 1)
        self.assertIsNotNone(
            self.m.db.query_one("SELECT last_checked_at FROM shadow_sample")["last_checked_at"])


class TestTheCaseThisExistsFor(ShadowCase):
    def test_a_silent_demotion_is_found_and_called_a_divergence(self):
        """PCO flips `primary` without moving `updated_at`. Nothing else sees it."""
        self.fake.data["Email"]["e1"]["attributes"]["primary"] = False   # no timestamp change
        wsgi_get(self.m.wsgi, "/people/v2/emails")
        self.m.divergence.run_once()

        reports = divergence.recent(self.m.db)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["verdict"], "divergence")
        differences = json.loads(reports[0]["differences"])
        self.assertTrue(any(d["pointer"].endswith("attributes.primary") for d in differences))
        self.assertEqual(divergence.summary(self.m.db)["divergence"], 1)

    def test_the_monotonic_writer_would_refuse_the_repair(self):
        """The other half of why nothing converges: even handed the corrected
        record at an unchanged `updated_at`, the canonical writer keeps what it
        has unless the incoming copy is newer or strictly richer."""
        held = self.m.db.query_one("SELECT raw FROM email WHERE pco_id='e1'")["raw"]
        thinner = json.loads(held)
        thinner["attributes"]["primary"] = False
        thinner["relationships"] = {}                 # a sideloaded copy, as PCO sends one
        self.m.writer.upsert("email", "e1", thinner, "reconcile")
        self.assertEqual(
            self.m.db.query_one("SELECT is_primary FROM email WHERE pco_id='e1'")["is_primary"], 1)


class TestWhatGetsStored(ShadowCase):
    def _make_a_divergence(self):
        self.fake.data["Person"]["1"]["attributes"]["first_name"] = "Grace"
        wsgi_get(self.m.wsgi, "/people/v2/people/1")
        self.m.divergence.run_once()

    def test_both_bodies_are_stored_pseudonymised(self):
        self._make_a_divergence()
        row = self.m.db.query_one("SELECT mirror_body, pco_body FROM shadow_report")
        stored = row["mirror_body"] + row["pco_body"]
        self.assertNotIn("Lovelace", stored)
        self.assertNotIn("Grace", stored)
        self.assertIn('"id": "1"', stored.replace('"id":"1"', '"id": "1"'))

    def test_the_difference_values_are_pseudonymised_too(self):
        self._make_a_divergence()
        differences = json.loads(
            self.m.db.query_one("SELECT differences FROM shadow_report")["differences"])
        rendered = json.dumps(differences)
        self.assertNotIn("Grace", rendered)
        self.assertIn("attributes.first_name", rendered)

    def test_a_difference_still_reads_as_a_difference_after_pseudonymisation(self):
        self._make_a_divergence()
        d = json.loads(
            self.m.db.query_one("SELECT differences FROM shadow_report")["differences"])[0]
        self.assertNotEqual(d["mirror"], d["pco"])

    def test_the_concrete_query_is_stored_not_only_the_shape(self):
        """A real export showed one record eight places out of position and gave
        the reader no way to learn which field had sorted it: the shape says a
        query was ordered, and only this says by what."""
        self.fake.data["Person"]["1"]["attributes"]["first_name"] = "Grace"
        wsgi_get(self.m.wsgi, "/people/v2/people", "order=last_name&per_page=25")
        self.m.divergence.run_once()
        report = divergence.recent(self.m.db)[0]
        self.assertEqual(json.loads(report["query"])["order"], "last_name")
        self.assertEqual(json.loads(report["query"])["per_page"], "25")

    def test_a_filter_value_in_the_query_is_pseudonymised(self):
        """`where[last_name]=Lovelace` is exactly as identifying as the attribute
        of the same name, and the export is meant to be safe to hand over."""
        self.fake.data["Person"]["1"]["attributes"]["first_name"] = "Grace"
        wsgi_get(self.m.wsgi, "/people/v2/people",
                 "where%5Blast_name%5D=Lovelace&order=last_name")
        self.m.divergence.run_once()
        payload = divergence.export(self.m.db).decode()
        self.assertNotIn("Lovelace", payload)
        self.assertIn('"order": "last_name"', payload)

    def test_the_log_is_capped(self):
        self.m.settings.shadow_keep = 3
        for i in range(6):
            self.fake.data["Person"]["1"]["attributes"]["first_name"] = f"Name{i}"
            self.m.divergence.check(1, "/people/v2/people/1", {})
        self.assertEqual(
            self.m.db.query_one("SELECT count(*) c FROM shadow_report")["c"], 3)

    def test_export_carries_no_real_value_and_clear_empties_it(self):
        self._make_a_divergence()
        payload = divergence.export(self.m.db).decode()
        self.assertNotIn("Lovelace", payload)
        self.assertNotIn("Grace", payload)
        self.assertIn("pseudonymised", payload)
        self.assertEqual(divergence.clear(self.m.db), 1)
        self.assertEqual(divergence.recent(self.m.db), [])


class TestTheRateMeansPerMinute(ShadowCase):
    """The scheduler ticks every few seconds; the setting says *per minute*.

    Spending the whole allowance on every pass — which a plain per-pass limit
    does — makes the number mean twelve times what it claims, and the operator
    who typed it has no way to find that out.
    """

    def setUp(self):
        super().setUp()
        self.clock = [0.0]
        self.m.divergence._now = lambda: self.clock[0]
        self.m.divergence._last_refill = -60.0
        # Genuinely *distinct* shapes. Twenty different values of one filter
        # collapse to one shape, which is the point of shape sampling and was
        # briefly the reason this test measured nothing.
        for key in ("where[grade]", "where[child]", "where[status]", "order",
                    "include", "where[first_name]", "where[last_name]",
                    "where[created_at]", "where[updated_at]", "where[gender]",
                    "where[search_name]", "where[site_administrator]"):
            wsgi_get(self.m.wsgi, "/people/v2/people", f"{key}=x")
        for endpoint in ("emails", "phone_numbers", "addresses", "households"):
            wsgi_get(self.m.wsgi, f"/people/v2/{endpoint}")
        self.assertGreaterEqual(
            self.m.db.query_one("SELECT count(*) c FROM shadow_sample")["c"], 12)
        divergence.configure(self.m.db, 6)

    def test_the_first_pass_gets_a_full_allowance(self):
        """Turning it on should do something now, not in sixty seconds."""
        self.assertEqual(self.m.divergence.run_once(), 6)

    def test_a_second_pass_straight_afterwards_gets_nothing(self):
        self.m.divergence.run_once()
        self.assertEqual(self.m.divergence.run_once(), 0)

    def test_twelve_fast_passes_do_not_spend_twelve_allowances(self):
        """The exact shape of the bug: 5-second ticks, one per scheduler tick."""
        self.m.divergence.run_once()                  # spend the initial fill
        checked = 0
        for _ in range(12):                           # one minute of ticks
            self.clock[0] += 5.0
            checked += self.m.divergence.run_once()
        self.assertLessEqual(checked, 6, "a minute of ticks bought more than a minute")

    def test_it_keeps_going_over_time(self):
        self.m.divergence.run_once()
        for _ in range(3):
            self.clock[0] += 60.0
            self.m.divergence.run_once()
        self.assertGreaterEqual(
            self.m.db.query_one(
                "SELECT count(*) c FROM shadow_sample WHERE last_checked_at IS NOT NULL")["c"],
            12)

    def test_the_allowance_does_not_bank_forever(self):
        """An hour idle must not buy an hour's worth in one pass."""
        self.m.divergence.run_once()
        self.clock[0] = 3600.0
        self.assertLessEqual(self.m.divergence.run_once(), 6)


class TestTurningItOnAndOff(unittest.TestCase):
    def setUp(self):
        self.m, _ = build()

    def test_the_environment_is_the_default(self):
        self.m.settings.shadow_per_minute = 4
        rate = divergence.effective(self.m.db, self.m.settings)
        self.assertEqual((rate["per_minute"], rate["source"]), (4, "environment"))

    def test_an_override_wins_and_persists(self):
        self.m.settings.shadow_per_minute = 4
        divergence.configure(self.m.db, 9)
        rate = divergence.effective(self.m.db, self.m.settings)
        self.assertEqual((rate["per_minute"], rate["source"], rate["default"]),
                         (9, "admin", 4))

    def test_zero_is_off_and_is_a_real_choice(self):
        """Off has to override an environment that says on, or the page's switch
        is a lie on exactly the deployment that needs it."""
        self.m.settings.shadow_per_minute = 10
        divergence.configure(self.m.db, 0)
        self.assertFalse(self.m.divergence.enabled)
        self.assertEqual(divergence.effective(self.m.db, self.m.settings)["source"], "admin")

    def test_resetting_falls_back_to_the_environment(self):
        self.m.settings.shadow_per_minute = 4
        divergence.configure(self.m.db, 9)
        divergence.configure(self.m.db, None)
        self.assertEqual(divergence.effective(self.m.db, self.m.settings),
                         {"per_minute": 4, "source": "environment", "default": 4})

    def test_it_is_clamped(self):
        divergence.configure(self.m.db, 10_000)
        self.assertEqual(divergence.effective(self.m.db, self.m.settings)["per_minute"],
                         divergence.MAX_PER_MINUTE)

    def test_a_corrupt_override_falls_back_rather_than_failing(self):
        self.m.settings.shadow_per_minute = 4
        self.m.db.set_meta(divergence.OVERRIDE_KEY, "not-a-number")
        self.assertEqual(divergence.effective(self.m.db, self.m.settings)["per_minute"], 4)

    def test_turning_it_on_makes_reads_start_enrolling(self):
        divergence.configure(self.m.db, 0)
        wsgi_get(self.m.wsgi, "/people/v2/people")
        self.assertEqual(self.m.db.query("SELECT * FROM shadow_sample"), [])
        divergence.configure(self.m.db, 5)
        wsgi_get(self.m.wsgi, "/people/v2/people")
        self.assertEqual(len(self.m.db.query("SELECT * FROM shadow_sample")), 1)


class TestTheCorpusIsRealTraffic(unittest.TestCase):
    """Every request checked is one a caller really made.

    The alternative — synthesising requests to walk the data — tests queries
    nobody makes and spends the PCO budget doing it. This is `tests/golden/`
    kept current by the traffic itself rather than recorded by hand once.
    """

    def setUp(self):
        self.m, self.fake = build()
        self.m.settings.shadow_per_minute = 60
        for i in range(1, 9):
            self.fake.add_person(str(i), f"P{i}", "Reed", "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")

    def _paths_checked(self, passes):
        seen = []
        for _ in range(passes):
            for sample in self.m.divergence.due(1):
                seen.append(sample["path"])
                self.m.db.execute(
                    "UPDATE shadow_sample SET last_checked_at=? WHERE sample_id=?",
                    (f"2026-01-01T00:00:{len(seen):02d}Z", sample["sample_id"]))
        return seen

    def test_several_records_under_one_shape_are_all_kept(self):
        for pid in (3, 5, 7):
            wsgi_get(self.m.wsgi, f"/people/v2/people/{pid}")
        rows = self.m.db.query("SELECT shape, path FROM shadow_sample ORDER BY path")
        self.assertEqual({r["shape"] for r in rows}, {"/people/v2/people/{id}"})
        self.assertEqual([r["path"] for r in rows],
                         ["/people/v2/people/3", "/people/v2/people/5",
                          "/people/v2/people/7"])

    def test_checking_moves_through_them(self):
        for pid in (3, 5, 7):
            wsgi_get(self.m.wsgi, f"/people/v2/people/{pid}")
        self.assertEqual(sorted(self._paths_checked(3)),
                         ["/people/v2/people/3", "/people/v2/people/5",
                          "/people/v2/people/7"])

    def test_nothing_is_ever_synthesised(self):
        """A path never asked for must never be asked of PCO."""
        wsgi_get(self.m.wsgi, "/people/v2/people/2")
        for _ in range(6):
            self.m.divergence.run_once(limit=1)
        asked = [path for method, path in self.fake.request_log if method == "GET"]
        self.assertTrue(all(p in ("/people/2", "/people") or p.startswith("/people?")
                            for p in asked), asked)
        self.assertNotIn("/people/3", asked)

    def test_a_busy_shape_does_not_starve_a_quiet_one(self):
        """Grouping exists for exactly this: one hot query must not take every
        check while a filter nobody runs twice is never verified at all."""
        for pid in range(1, 9):
            wsgi_get(self.m.wsgi, f"/people/v2/people/{pid}")
        wsgi_get(self.m.wsgi, "/people/v2/people", "where[child]=true")
        shapes = {self.m.db.query_one(
            "SELECT shape FROM shadow_sample WHERE path=? OR query LIKE ?",
            (p, "%child%"))["shape"] for p in ("/people/v2/people/1",)}
        checked = self._paths_checked(2)
        self.assertIn("/people/v2/people", checked,
                      "the quiet shape should be reached on the second check")
        self.assertEqual(len(shapes), 1)

    def test_the_corpus_is_capped_per_shape(self):
        original = divergence.SAMPLES_PER_SHAPE
        divergence.SAMPLES_PER_SHAPE = 3
        try:
            for pid in range(1, 9):
                wsgi_get(self.m.wsgi, f"/people/v2/people/{pid}")
        finally:
            divergence.SAMPLES_PER_SHAPE = original
        self.assertEqual(
            self.m.db.query_one("SELECT count(*) c FROM shadow_sample")["c"], 3)

    def test_the_busiest_requests_are_the_ones_kept(self):
        """A request made once may never be made again; the one made constantly
        is the one whose breaking gets noticed."""
        original = divergence.SAMPLES_PER_SHAPE
        divergence.SAMPLES_PER_SHAPE = 2
        try:
            for _ in range(5):
                wsgi_get(self.m.wsgi, "/people/v2/people/1")
            for _ in range(4):
                wsgi_get(self.m.wsgi, "/people/v2/people/2")
            wsgi_get(self.m.wsgi, "/people/v2/people/3")       # a one-off
        finally:
            divergence.SAMPLES_PER_SHAPE = original
        kept = {r["path"] for r in self.m.db.query("SELECT path FROM shadow_sample")}
        self.assertEqual(kept, {"/people/v2/people/1", "/people/v2/people/2"})

    def test_a_divergence_in_a_record_callers_do_read_is_found(self):
        wsgi_get(self.m.wsgi, "/people/v2/people/6")
        self.fake.data["Person"]["6"]["attributes"]["first_name"] = "Changed"
        self.m.divergence.run_once(limit=1)
        found = divergence.recent(self.m.db)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["path"], "/people/v2/people/6")
        self.assertEqual(found[0]["verdict"], "divergence")

    def test_a_record_nobody_reads_is_not_checked(self):
        """Stated so the boundary is deliberate rather than discovered. This
        verifies the mirror against the traffic it serves; a record no caller
        has ever asked for is outside that, and the reconcile sweep owns it."""
        wsgi_get(self.m.wsgi, "/people/v2/people/2")
        self.fake.data["Person"]["6"]["attributes"]["first_name"] = "Changed"
        for _ in range(6):
            self.m.divergence.run_once(limit=1)
        self.assertEqual(divergence.recent(self.m.db), [])

    def test_a_failing_request_does_not_stall_the_ones_behind_it(self):
        for pid in (1, 2):
            wsgi_get(self.m.wsgi, f"/people/v2/people/{pid}")
        self.fake.unreachable = True
        self.m.divergence.run_once(limit=1)
        self.fake.unreachable = False
        checked = self.m.db.query_one(
            "SELECT count(last_checked_at) c FROM shadow_sample")["c"]
        self.assertEqual(checked, 1, "it should have been marked checked regardless")


class TestItYieldsToTheForeground(unittest.TestCase):
    """Nobody is waiting on a divergence check; somebody is waiting on a read.

    The `priority` argument every caller passes was accepted and then ignored by
    the limiter, so "background work runs at low priority" described nothing.
    """

    def setUp(self):
        self.limiter = RateLimiter(target_rps=4.0)

    def test_foreground_spends_the_last_token_and_background_does_not(self):
        self.limiter.tokens = 1.0
        self.limiter.acquire("passthrough")                   # allowed
        self.limiter.tokens = 1.0
        with self.assertRaises(RateLimitBusy):
            self.limiter.acquire("divergence", max_wait=0.05)

    def test_background_runs_when_there_is_headroom(self):
        self.limiter.tokens = self.limiter.capacity
        self.limiter.acquire("divergence", max_wait=0.05)     # no exception

    def test_every_background_caller_is_covered_not_just_this_one(self):
        for priority in ("reconcile", "backfill", "webhook_hydrate", "divergence"):
            self.limiter.tokens = 1.0
            with self.assertRaises(RateLimitBusy, msg=priority):
                self.limiter.acquire(priority, max_wait=0.05)

    def test_a_write_is_never_deferrable(self):
        """A caller is blocked on it, and it is the one request that cannot
        simply be tried again later."""
        self.limiter.tokens = 1.0
        self.limiter.acquire("passthrough")

    def test_an_unbounded_wait_is_still_the_default(self):
        """The sweeps have always waited; this must not quietly change them."""
        self.limiter.tokens = self.limiter.capacity
        self.limiter.acquire("reconcile")                     # returns, no timeout


class TestItDoesNotBurstOnStartup(ShadowCase):
    """A full bucket means switching this on fires a whole minute's allowance at
    once, into a budget shared with the reads people are waiting on."""

    def setUp(self):
        super().setUp()
        for key in ("where[grade]", "where[child]", "where[status]", "order",
                    "include", "where[gender]", "where[first_name]", "where[last_name]"):
            wsgi_get(self.m.wsgi, "/people/v2/people", f"{key}=x")
        divergence.configure(self.m.db, 6)

    def test_the_first_pass_checks_one_thing_not_a_minutes_worth(self):
        fresh = self.m.divergence
        fresh._tokens = 0.0
        fresh._last_refill = fresh._now() - (60.0 / 6)
        self.assertEqual(fresh.run_once(), 1)

    def test_the_rate_still_averages_out_over_a_minute(self):
        clock = [0.0]
        checker = self.m.divergence
        checker._now = lambda: clock[0]
        checker._tokens = 0.0
        checker._last_refill = -(60.0 / 6)
        checked = checker.run_once()
        for _ in range(12):
            clock[0] += 5.0
            checked += checker.run_once()
        self.assertLessEqual(checked, 7)
        self.assertGreaterEqual(checked, 5)


if __name__ == "__main__":
    unittest.main()
