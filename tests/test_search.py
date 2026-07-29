"""The query surface a PCO client actually uses: `where[search_*]`, typed filters,
nested collections, and page links that survive being followed.

Every case here is a request some real client sends verbatim. They are grouped by
the thing that used to be wrong, because each of these was a silent failure rather
than an error: a search that 400'd, a boolean that matched nothing, a page link
that quietly changed the query.
"""
from __future__ import annotations

import unittest

from base import build, wsgi_get
from fakepco import FakePCO, res

PEOPLE = [
    # id, first, last, nickname, child, grade
    ("1", "Ada", "Byron", "Addie", True, 8),
    ("2", "Grace", "Hopper", None, True, 9),
    ("3", "Alan", "Turing", None, False, None),
    ("4", "Ann", "Byron", None, True, 10),
]


def _fixture():
    fake = FakePCO()
    fake.add(res("Household", "h1", {"name": "Byron", "member_count": 2},
                 relationships={"people": {"data": [{"type": "Person", "id": "1"},
                                                    {"type": "Person", "id": "4"}]}}))
    for pid, first, last, nick, child, grade in PEOPLE:
        person = fake.add_person(pid, first, last, "2026-01-01T00:00:00Z")
        person["attributes"].update(child=child, grade=grade, nickname=nick)
        # PCO returns a person's households inline, as an identifier array — there
        # is no `/household_memberships` collection to mirror the edge from.
        if pid in ("1", "4"):
            person["relationships"]["households"] = {"data": [{"type": "Household", "id": "h1"}]}
        # …and it names the sideloaded children on the person too, which is what a
        # backfill stores, since a backfill always asks for them.
        person["relationships"]["emails"] = {"data": [{"type": "Email", "id": "e" + pid}]}
        person["relationships"]["phone_numbers"] = {
            "data": [{"type": "PhoneNumber", "id": "p" + pid}]}
        fake.add_child("Email", "e" + pid, pid,
                       {"address": f"{first.lower()}@example.org", "primary": True},
                       "2026-01-01T00:00:00Z")
        fake.add_child("PhoneNumber", "p" + pid, pid,
                       {"number": f"(555) 010-{pid}", "e164": f"+1555010{pid}", "primary": True},
                       "2026-01-01T00:00:00Z")
    # PCO's shape: the household id lives only in `links.self`, and the roles are
    # the ones a real organization uses.
    fake.add_membership("hm1", "h1", "1", role="child_or_dependent")
    fake.add_membership("hm4", "h1", "4", role="parent_guardian")
    mirror, _ = build(fake)
    for name in ("person", "email", "phone_number", "household", "household_membership"):
        mirror.ingestor.backfill(name)
    return mirror, fake


def _fixture_with_auth():
    """The same fixture, with the api_key plane switched on."""
    mirror, fake = _fixture()
    mirror.settings.allow_anonymous = False
    return mirror, fake


class TestSearchFilters(unittest.TestCase):
    """PCO's `where[search_*]` are fuzzy substring matches, not equality."""

    def setUp(self):
        self.m, self.fake = _fixture()

    def ids(self, query):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", query)
        self.assertEqual(status, 200, body)
        return sorted(d["id"] for d in body["data"])

    def test_search_name_matches_a_word_prefix(self):
        # Measured against the live API: the needle's words must be a run of
        # prefixes starting at a word boundary.
        self.assertEqual(self.ids("where[search_name]=byron"), ["1", "4"])
        self.assertEqual(self.ids("where[search_name]=byr"), ["1", "4"])
        self.assertEqual(self.ids("where[search_name]=ada byron"), ["1"])
        self.assertEqual(self.ids("where[search_name]=ada byr"), ["1"])

    def test_search_name_does_not_match_mid_word(self):
        # PCO returns nothing for an interior fragment. A substring match returned
        # a hundred people here where the live API returned nine.
        self.assertEqual(self.ids("where[search_name]=yron"), [])
        self.assertEqual(self.ids("where[search_name]=ron"), [])

    def test_search_name_word_order_matters(self):
        # "<surname> <given>" finds nobody at PCO, because the run has to be
        # contiguous and in order.
        self.assertEqual(self.ids("where[search_name]=byron ada"), [])

    def test_search_name_folds_case_and_whitespace(self):
        self.assertEqual(self.ids("where[search_name]=ADA   BYRON"), ["1"])

    def test_search_name_matches_a_nickname_on_its_own(self):
        # The legal name is Ada; everybody calls her Addie. PCO finds her by the
        # nickname alone — but NOT by "<nickname> <surname>", which is why the
        # nickname is its own haystack rather than being glued to the surname.
        self.assertEqual(self.ids("where[search_name]=addie"), ["1"])
        self.assertEqual(self.ids("where[search_name]=addie byron"), [])

    def test_search_name_does_not_reach_email(self):
        # That is what search_name_or_email is for; conflating them would make the
        # narrower filter silently useless.
        self.assertEqual(self.ids("where[search_name]=ada@example.org"), [])

    def test_search_name_or_email_matches_either(self):
        self.assertEqual(self.ids("where[search_name_or_email]=ada@example.org"), ["1"])
        self.assertEqual(self.ids("where[search_name_or_email]=byron"), ["1", "4"])
        self.assertEqual(self.ids("where[search_name_or_email]=example.org"), ["1", "2", "3", "4"])

    def test_phone_search_matches_a_digits_suffix(self):
        # How a person actually searches for a number: the last few digits.
        # Formatting is discounted on both sides.
        self.assertEqual(self.ids("where[search_phone_number]=5550101"), ["1"])
        self.assertEqual(self.ids("where[search_phone_number]=(555) 010-1"), ["1"])
        self.assertEqual(self.ids("where[search_phone_number]=0101"), ["1"])
        # ...but only as a suffix. A leading or interior run does not match, which
        # is why an E.164 value with its country code finds nothing here.
        self.assertEqual(self.ids("where[search_phone_number]=555"), [])

    def test_e164_search_is_exact_once_punctuation_is_discounted(self):
        self.assertEqual(self.ids("where[search_phone_number_e164]=%2B15550102"), ["2"])
        self.assertEqual(self.ids("where[search_phone_number_e164]=15550102"), ["2"])
        self.assertEqual(self.ids("where[search_phone_number_e164]=5550102"), [])

    def test_combined_filter_covers_every_arm(self):
        self.assertEqual(self.ids("where[search_name_or_email_or_phone_number]=hopper"), ["2"])
        self.assertEqual(self.ids("where[search_name_or_email_or_phone_number]=alan@example.org"), ["3"])
        self.assertEqual(self.ids("where[search_name_or_email_or_phone_number]=5550104"), ["4"])

    def test_each_arm_keeps_its_own_rule(self):
        # Names are word-prefix, emails are substring. The same needle therefore
        # behaves differently depending on which arm can match it.
        self.assertEqual(self.ids("where[search_name]=example.org"), [])
        self.assertEqual(self.ids("where[search_name_or_email]=xample.or"),
                         ["1", "2", "3", "4"])

    def test_a_name_needle_does_not_match_every_phone_number(self):
        # "hopper" has no digits; a naive digit-strip would leave the empty string,
        # which is a substring of every number in the church.
        self.assertEqual(self.ids("where[search_phone_number]=hopper"), [])

    def test_wildcards_are_literal(self):
        # No LIKE anywhere in the search path, so a needle containing % or _ has no
        # special meaning and there is no escaping to get wrong.
        self.assertEqual(self.ids("where[search_name]=%25"), [])
        self.assertEqual(self.ids("where[search_name]=_"), [])
        self.assertEqual(self.ids("where[search_name_or_email]=%25"), [])

    def test_empty_needle_filters_nothing(self):
        self.assertEqual(self.ids("where[search_name]="), ["1", "2", "3", "4"])

    def test_search_filter_rejects_a_range_operator(self):
        status, _, _ = wsgi_get(self.m.wsgi, "/people/v2/people", "where[search_name][gt]=a")
        self.assertEqual(status, 400)

    def test_search_surface_is_advertised(self):
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people")
        self.assertIn("search_name_or_email", body["meta"]["can_search_by"])


class TestTypedFilters(unittest.TestCase):
    """A filter whose value does not match its column's type returned zero rows."""

    def setUp(self):
        self.m, self.fake = _fixture()

    def ids(self, query):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", query)
        self.assertEqual(status, 200, body)
        return sorted(d["id"] for d in body["data"])

    def test_boolean_filter_accepts_what_a_pco_client_sends(self):
        # `child` is projected INTEGER, so SQLite holds JSON `true` as 1. Comparing
        # it against the string "true" matched nobody and reported success.
        self.assertEqual(self.ids("where[child]=true"), ["1", "2", "4"])
        self.assertEqual(self.ids("where[child]=false"), ["3"])
        self.assertEqual(self.ids("where[child]=1"), ["1", "2", "4"])

    def test_grade_filter(self):
        self.assertEqual(self.ids("where[grade]=8"), ["1"])

    def test_nonsense_boolean_is_a_400_not_an_empty_page(self):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "where[child]=banana")
        self.assertEqual(status, 400)
        self.assertIn("boolean", body["errors"][0]["detail"])

    def test_primary_filter_uses_the_aliased_column(self):
        # PCO calls it `primary`; the projection is `is_primary`, because `primary`
        # is a reserved word. Without the alias this was a 500.
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/emails", "where[primary]=true")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["data"]), 4)

    def test_bad_pagination_value_is_a_400_not_a_500(self):
        status, _, _ = wsgi_get(self.m.wsgi, "/people/v2/people", "per_page=lots")
        self.assertEqual(status, 400)


class TestPageLinks(unittest.TestCase):
    """A page link is the same query, further along — or it is a data-loss bug."""

    def setUp(self):
        self.m, self.fake = _fixture()

    def test_next_link_carries_filter_order_and_include(self):
        _, _, body = wsgi_get(
            self.m.wsgi, "/people/v2/people",
            "where[child]=true&order=last_name&include=emails&per_page=1")
        nxt = body["links"]["next"]
        for expected in ("where[child]=true", "order=last_name", "include=emails"):
            self.assertIn(expected, nxt)
        self.assertIn("offset=1", nxt)

    def test_following_next_visits_every_row_exactly_once(self):
        seen, url, hops = [], "/people/v2/people?where[child]=true&order=last_name&include=emails&per_page=1", 0
        while url is not None:
            self.assertLess(hops, 10, "pagination did not terminate")
            path, _, query = url.partition("?")
            status, _, body = wsgi_get(self.m.wsgi, path, query)
            self.assertEqual(status, 200, body)
            # The include travels too: dropping it silently stripped every child
            # record from page two onwards.
            self.assertEqual(len(body.get("included", [])), len(body["data"]))
            seen += [d["id"] for d in body["data"]]
            url, hops = body["links"].get("next"), hops + 1
        self.assertEqual(seen, ["1", "4", "2"])          # ordered, complete, no repeats

    def test_meta_carries_the_cursor_too(self):
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "per_page=2&offset=2")
        self.assertEqual(body["meta"]["prev"], {"offset": 0})
        self.assertNotIn("next", body["meta"])
        self.assertIn("prev", body["links"])
        self.assertNotIn("next", body["links"])

    def test_self_link_is_the_query_that_produced_the_page(self):
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "where[child]=true&per_page=2")
        self.assertIn("where[child]=true", body["links"]["self"])


class TestRelationshipReads(unittest.TestCase):
    """`/:type/:id/:rel` — the same query surface as a collection, from the mirror."""

    def setUp(self):
        self.m, self.fake = _fixture()

    def test_households_edge_comes_off_the_person_payload(self):
        # PCO has no `/household_memberships` collection to mirror the edge from,
        # but it does return the household identifiers inline on the Person. That
        # array is the edge, and it costs nothing upstream to follow.
        self.fake.request_log.clear()
        status, headers, body = wsgi_get(self.m.wsgi, "/people/v2/people/1/households")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["X-Mirror-Source"], "mirror")
        self.assertEqual([d["id"] for d in body["data"]], ["h1"])
        self.assertEqual(self.fake.request_log, [])

    def test_include_households_resolves_locally(self):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people",
                                   "include=households&where[child]=true")
        self.assertEqual(status, 200, body)
        self.assertEqual([(i["type"], i["id"]) for i in body["included"]], [("Household", "h1")])

    def test_the_other_side_of_the_edge_reads_the_household_payload(self):
        # PCO puts the membership on the Household too, so each side is read from
        # its own record rather than by scanning the other side's.
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/people",
                                   "order=last_name")
        self.assertEqual(status, 200, body)
        self.assertEqual([d["id"] for d in body["data"]], ["1", "4"])

    def test_a_person_in_no_household_is_an_empty_page_not_an_error(self):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people/3/households")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"], [])
        self.assertEqual(body["meta"]["total_count"], 0)

    def test_nested_collections_page_and_filter_like_top_level_ones(self):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/people", "per_page=1")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["meta"]["total_count"], 2)
        self.assertEqual(len(body["data"]), 1)
        self.assertIn("next", body["links"])

    def test_child_collections_still_serve_from_the_mirror(self):
        self.fake.request_log.clear()
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people/1/emails")
        self.assertEqual(status, 200, body)
        self.assertEqual([d["id"] for d in body["data"]], ["e1"])
        self.assertEqual(self.fake.request_log, [])

    def test_household_memberships_serve_from_the_mirror(self):
        """Walked, not proxied.

        PCO serves these one household at a time and omits the household from the
        payload, so they are collected by a periodic walk and the owning id is
        parsed out of `links.self`. Once held, the read costs nothing upstream.
        """
        self.fake.request_log.clear()
        status, headers, body = wsgi_get(
            self.m.wsgi, "/people/v2/households/h1/household_memberships", "include=person")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["X-Mirror-Source"], "mirror")
        self.assertEqual(sorted(d["id"] for d in body["data"]), ["hm1", "hm4"])
        self.assertEqual(self.fake.request_log, [])

    def test_the_role_that_decides_the_parent_is_held(self):
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/household_memberships", "")
        roles = {d["id"]: d["attributes"]["household_role"] for d in body["data"]}
        self.assertEqual(roles, {"hm1": "child_or_dependent", "hm4": "parent_guardian"})

    def test_a_membership_pco_stops_returning_is_tombstoned(self):
        """A walk that could only ever add would never notice a parent leaving."""
        self.fake.destroy("HouseholdMembership", "hm4")
        self.m.ingestor.incremental_sweep("household_membership")
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/household_memberships", "")
        self.assertEqual([d["id"] for d in body["data"]], ["hm1"])
        row = self.m.db.query_one(
            "SELECT tombstone_reason FROM household_membership WHERE pco_id='hm4'")
        self.assertEqual(row["tombstone_reason"], "destroyed")

    def test_a_relationship_the_mirror_does_not_cover_still_passes_through(self):
        self.fake.request_log.clear()
        wsgi_get(self.m.wsgi, "/people/v2/people/1/workflow_cards")
        self.assertEqual(self.fake.request_log, [("GET", "/people/1/workflow_cards")])


class TestSparseFieldsets(unittest.TestCase):
    """`fields[Type]=a,b` — JSON:API sparse fieldsets, as PCO honours them."""

    def setUp(self):
        self.m, self.fake = _fixture()

    def get(self, query, path="/people/v2/people"):
        status, _, body = wsgi_get(self.m.wsgi, path, query)
        self.assertEqual(status, 200, body)
        return body

    def test_attributes_are_limited_to_the_named_set(self):
        body = self.get("fields[Person]=first_name,last_name")
        for item in body["data"]:
            self.assertEqual(sorted(item["attributes"]), ["first_name", "last_name"])

    def test_relationships_are_fields_too(self):
        # A relationship survives only if it is named, even though `include=`
        # still sideloads it.
        named = self.get("fields[Person]=first_name,emails&include=emails")
        self.assertEqual(sorted(named["data"][0].get("relationships") or {}), ["emails"])
        self.assertTrue(named["included"])

        unnamed = self.get("fields[Person]=first_name&include=emails")
        self.assertEqual(unnamed["data"][0].get("relationships"), {})
        self.assertTrue(unnamed["included"], "include= still sideloads")

    def test_a_sideloaded_type_gets_its_own_fieldset(self):
        body = self.get("fields[Person]=first_name&fields[Email]=address&include=emails")
        for item in body["included"]:
            self.assertEqual(sorted(item["attributes"]), ["address"])

    def test_an_unknown_field_name_selects_nothing_rather_than_erroring(self):
        body = self.get("fields[Person]=no_such_field")
        self.assertEqual(body["data"][0]["attributes"], {})

    def test_links_survive_a_fieldset(self):
        body = self.get("fields[Person]=first_name")
        self.assertIn("self", body["data"][0]["links"])

    def test_fieldsets_apply_to_a_single_read(self):
        body = self.get("fields[Person]=first_name", "/people/v2/people/1")
        self.assertEqual(sorted(body["data"]["attributes"]), ["first_name"])


class TestPartialPayloadsAreNotStored(unittest.TestCase):
    """A representation without `updated_at` is not a record."""

    def setUp(self):
        self.m, self.fake = _fixture()

    def test_a_sparse_payload_cannot_replace_a_mirrored_record(self):
        import json
        before = json.loads(
            self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"])
        # Exactly what a pass-through with `fields[Person]=first_name` returns.
        self.m.writer.route_page(
            {"data": [{"type": "Person", "id": "1", "attributes": {"first_name": "Ada"}}]},
            "passthrough")
        after = json.loads(
            self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"])
        self.assertEqual(before, after)
        # …and the monotonic guard is still intact, which a NULL timestamp would
        # have broken permanently.
        self.assertIsNotNone(
            self.m.db.query_one("SELECT pco_updated_at u FROM person WHERE pco_id='1'")["u"])

    def test_a_complete_payload_still_applies(self):
        self.m.writer.route_page({"data": [{"type": "Person", "id": "1", "attributes": {
            "first_name": "Ada", "last_name": "Lovelace",
            "updated_at": "2026-09-01T00:00:00Z"}}]}, "passthrough")
        self.assertEqual(
            self.m.db.query_one("SELECT last_name l FROM person WHERE pco_id='1'")["l"],
            "Lovelace")

    def test_an_untimed_resource_is_unaffected(self):
        # `field_definition` carries no `updated_at` by design, so the guard must
        # not apply to it.
        self.m.writer.route_page({"data": [{"type": "FieldDefinition", "id": "fd9",
                                            "attributes": {"name": "Allergies",
                                                           "data_type": "text"}}]},
                                 "backfill")
        self.assertIsNotNone(
            self.m.db.query_one("SELECT 1 FROM field_definition WHERE pco_id='fd9'"))


class TestPersonDetailReadShape(unittest.TestCase):
    """The read behind a check-in detail view, exactly as Tally issues it.

    `getPersonDetails` is two requests. The first — the person with their
    contacts, households and household members — must come from the mirror, since
    it is on the path a counselor waits on at a door. The second asks for the
    `household_role` that decides which adult is the parent, and that one has to
    reach PCO: membership is not mirrorable (`DESIGN.md` §8.1), so what matters is
    that it passes through rather than answering an empty page.
    """

    def setUp(self):
        self.m, self.fake = _fixture()

    DETAIL_INCLUDE = "include=emails,phone_numbers,households,households.people"

    def test_the_person_half_is_served_locally(self):
        self.fake.request_log.clear()
        status, headers, body = wsgi_get(self.m.wsgi, "/people/v2/people/1", self.DETAIL_INCLUDE)
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["X-Mirror-Source"], "mirror")
        self.assertEqual(self.fake.request_log, [], "the detail read must not wait on PCO")

    def test_it_carries_the_household_and_its_other_members(self):
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people/1", self.DETAIL_INCLUDE)
        included = {(i["type"], i["id"]) for i in body["included"]}
        self.assertIn(("Household", "h1"), included)
        # The other member of the household — the candidate parent. Without them
        # there is nobody to rank, and the detail view has no contact to show.
        self.assertIn(("Person", "4"), included)
        self.assertIn(("Email", "e1"), included)
        self.assertIn(("PhoneNumber", "p1"), included)

    def test_the_household_edge_survives_a_bare_read_of_the_same_person(self):
        # A person fetched without includes is a thinner representation. If it
        # displaced the stored one, the next detail read would find no household
        # and the parent contact would silently disappear.
        self.m.writer.route_page({"data": [{"type": "Person", "id": "1", "attributes": {
            "first_name": "Ada", "last_name": "Byron",
            "updated_at": "2026-01-01T00:00:00Z"}}]}, "passthrough")
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people/1", self.DETAIL_INCLUDE)
        self.assertIn(("Household", "h1"),
                      {(i["type"], i["id"]) for i in body.get("included", [])})

    def test_the_membership_half_is_served_locally_too(self):
        """The request that decides which adult is the parent, and carries their
        contact details. Both halves of the detail read are now local."""
        self.fake.request_log.clear()
        status, headers, body = wsgi_get(
            self.m.wsgi, "/people/v2/households/h1/household_memberships",
            "include=person,person.emails,person.phone_numbers")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["X-Mirror-Source"], "mirror")
        included = {(i["type"], i["id"]) for i in body["included"]}
        self.assertIn(("Person", "4"), included)          # the candidate parent
        self.assertIn(("Email", "e4"), included)          # and how to reach them
        self.assertIn(("PhoneNumber", "p4"), included)
        self.assertEqual(self.fake.request_log, [],
                         "the parent-contact request must not wait on PCO either")

    def test_a_read_only_key_can_complete_the_whole_detail_view(self):
        """No `passthrough` scope required any more, which is the point.

        While membership was proxied, a key without that scope turned the
        parent-contact request into a 403 — and because the client does not catch
        it, the entire detail view was lost, including the allergies the mirror
        already held. Both halves are local now, so a read-only key is enough.
        """
        from pcomirror import apikeys
        m, fake = _fixture_with_auth()
        key = apikeys.create(m.db, "reader", "read:*")
        auth = {"Authorization": f"Bearer {key}"}
        fake.request_log.clear()
        for path, query in (("/people/v2/people/1", self.DETAIL_INCLUDE),
                            ("/people/v2/households/h1/household_memberships",
                             "include=person,person.emails,person.phone_numbers")):
            status, _, body = wsgi_get(m.wsgi, path, query, headers=auth)
            self.assertEqual(status, 200, f"{path} -> {body}")
        self.assertEqual(fake.request_log, [])


class TestPcoOrdering(unittest.TestCase):
    """Ordering rules copied from the live API, where both of these were wrong."""

    def setUp(self):
        self.m, self.fake = _fixture()

    def test_ids_sort_numerically_not_lexically(self):
        # Real PCO ids vary in length within one collection (/emails carries both
        # eight- and nine-digit ids). Sorting them as text put every short id
        # first, so an entire page came back in an order PCO would never send.
        for pid in ("100", "99", "1000", "9"):
            self.fake.add_person(pid, "Zed", "Zeta", "2026-05-01T00:00:00Z")
        self.m.ingestor.backfill("person")
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "per_page=100")
        ids = [d["id"] for d in body["data"]]
        self.assertEqual(ids, sorted(ids, key=int))

    def test_text_ordering_is_case_insensitive(self):
        # SQLite's BINARY collation sorts every capital before every lowercase
        # letter, so a surname entered in lower case fell to the end of the roster.
        self.fake.add_person("90", "Zoe", "apple", "2026-05-01T00:00:00Z")
        self.m.ingestor.backfill("person")
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "order=last_name&per_page=100")
        surnames = [(d["attributes"].get("last_name") or "") for d in body["data"]]
        self.assertEqual(surnames, sorted(surnames, key=str.lower))
        self.assertEqual(surnames[0].lower(), "apple")

    def test_an_accented_surname_sorts_where_pco_puts_it(self):
        """`NOCASE` folds ASCII A–Z and nothing else, so every accented surname
        sorted after `z` — `Márquez` landed past all the `Mar…` names instead of
        among them. Measured against a live 1925-person organization ordered by
        `last_name`: NOCASE disagreed with PCO in 34 positions; folding combining
        marks first agreed in all 1925."""
        for pid, last in (("91", "Manriquez"), ("92", "Márquez"),
                          ("93", "Martinez"), ("94", "Mee")):
            self.fake.add_person(pid, "N", last, "2026-05-01T00:00:00Z")
        self.m.ingestor.backfill("person")
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people",
                              "order=last_name&where[first_name]=N&per_page=100")
        self.assertEqual([d["attributes"]["last_name"] for d in body["data"]],
                         ["Manriquez", "Márquez", "Martinez", "Mee"])

    def test_folding_does_not_disturb_the_case_rule_it_replaced(self):
        for pid, last in (("95", "apple"), ("96", "Ápple"), ("97", "Banana")):
            self.fake.add_person(pid, "F", last, "2026-05-01T00:00:00Z")
        self.m.ingestor.backfill("person")
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people",
                              "order=last_name&where[first_name]=F&per_page=100")
        self.assertEqual([d["attributes"]["last_name"] for d in body["data"]],
                         ["apple", "Ápple", "Banana"])

    def test_ordering_ties_break_on_numeric_id(self):
        for pid in ("300", "40"):
            self.fake.add_person(pid, "Tie", "Byron", "2026-05-01T00:00:00Z")
        self.m.ingestor.backfill("person")
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people",
                              "order=last_name&where[last_name]=Byron&per_page=100")
        ids = [d["id"] for d in body["data"]]
        self.assertEqual(ids, sorted(ids, key=int))


if __name__ == "__main__":
    unittest.main()
