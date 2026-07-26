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
    fake.add(res("Household", "h1", {"name": "Byron", "member_count": 2}))
    for pid, first, last, nick, child, grade in PEOPLE:
        person = fake.add_person(pid, first, last, "2026-01-01T00:00:00Z")
        person["attributes"].update(child=child, grade=grade, nickname=nick,
                                    search_name=f"{first} {last}")
        fake.add_child("Email", "e" + pid, pid,
                       {"address": f"{first.lower()}@example.org", "primary": True},
                       "2026-01-01T00:00:00Z")
        fake.add_child("PhoneNumber", "p" + pid, pid,
                       {"number": f"(555) 010-{pid}", "e164": f"+1555010{pid}", "primary": True},
                       "2026-01-01T00:00:00Z")
    for pid in ("1", "4"):
        fake.add(res("HouseholdMembership", "hm" + pid,
                     {"household_role": "child", "pending": False},
                     relationships={"household": {"data": {"type": "Household", "id": "h1"}},
                                    "person": {"data": {"type": "Person", "id": pid}}}))
    mirror, _ = build(fake)
    for name in ("person", "email", "phone_number", "household", "household_membership"):
        mirror.ingestor.backfill(name)
    return mirror, fake


class TestSearchFilters(unittest.TestCase):
    """PCO's `where[search_*]` are fuzzy substring matches, not equality."""

    def setUp(self):
        self.m, self.fake = _fixture()

    def ids(self, query):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", query)
        self.assertEqual(status, 200, body)
        return sorted(d["id"] for d in body["data"])

    def test_search_name_matches_a_substring_not_the_whole_name(self):
        self.assertEqual(self.ids("where[search_name]=byron"), ["1", "4"])
        self.assertEqual(self.ids("where[search_name]=ada byron"), ["1"])

    def test_search_name_folds_case_and_whitespace(self):
        self.assertEqual(self.ids("where[search_name]=ADA   BYRON"), ["1"])

    def test_search_name_matches_the_nickname(self):
        # The legal name is Ada; everybody calls her Addie. Both must find her.
        self.assertEqual(self.ids("where[search_name]=addie byron"), ["1"])

    def test_search_name_does_not_reach_email(self):
        # That is what search_name_or_email is for; conflating them would make the
        # narrower filter silently useless.
        self.assertEqual(self.ids("where[search_name]=ada@example.org"), [])

    def test_search_name_or_email_matches_either(self):
        self.assertEqual(self.ids("where[search_name_or_email]=ada@example.org"), ["1"])
        self.assertEqual(self.ids("where[search_name_or_email]=byron"), ["1", "4"])
        self.assertEqual(self.ids("where[search_name_or_email]=example.org"), ["1", "2", "3", "4"])

    def test_phone_search_ignores_formatting(self):
        self.assertEqual(self.ids("where[search_phone_number]=5550101"), ["1"])
        self.assertEqual(self.ids("where[search_phone_number]=(555) 010-1"), ["1"])
        self.assertEqual(self.ids("where[search_phone_number_e164]=%2B15550102"), ["2"])

    def test_combined_filter_covers_every_arm(self):
        self.assertEqual(self.ids("where[search_name_or_email_or_phone_number]=hopper"), ["2"])
        self.assertEqual(self.ids("where[search_name_or_email_or_phone_number]=alan@example.org"), ["3"])
        self.assertEqual(self.ids("where[search_name_or_email_or_phone_number]=5550104"), ["4"])

    def test_a_name_needle_does_not_match_every_phone_number(self):
        # "hopper" has no digits; a naive digit-strip would leave the empty string,
        # which is a substring of every number in the church.
        self.assertEqual(self.ids("where[search_phone_number]=hopper"), [])

    def test_wildcards_are_literal(self):
        # `instr`, not LIKE — so a needle containing % or _ has no special meaning
        # and there is no escaping to get wrong.
        self.assertEqual(self.ids("where[search_name]=%25"), [])
        self.assertEqual(self.ids("where[search_name]=_"), [])

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


class TestNestedCollections(unittest.TestCase):
    """`/households/{id}/household_memberships` — mirrored all along, unreachable."""

    def setUp(self):
        self.m, self.fake = _fixture()

    def test_household_memberships_serve_from_the_mirror_with_includes(self):
        self.fake.request_log.clear()
        status, headers, body = wsgi_get(
            self.m.wsgi, "/people/v2/households/h1/household_memberships",
            "include=person,person.emails,person.phone_numbers&per_page=100")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["X-Mirror-Source"], "mirror")
        self.assertEqual(sorted(d["id"] for d in body["data"]), ["hm1", "hm4"])
        included = {(i["type"], i["id"]) for i in body["included"]}
        self.assertIn(("Person", "1"), included)
        self.assertIn(("Email", "e1"), included)
        self.assertIn(("PhoneNumber", "p4"), included)
        # and it cost nothing upstream — this used to be one PCO request per family
        self.assertEqual(self.fake.request_log, [])

    def test_people_side_of_the_same_relationship(self):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people/1/household_memberships",
                                   "include=household")
        self.assertEqual(status, 200, body)
        self.assertEqual([d["id"] for d in body["data"]], ["hm1"])
        self.assertEqual([(i["type"], i["id"]) for i in body["included"]], [("Household", "h1")])

    def test_nested_collections_page_and_filter_like_top_level_ones(self):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/households/h1/household_memberships",
                                   "per_page=1")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["meta"]["total_count"], 2)
        self.assertEqual(len(body["data"]), 1)
        self.assertIn("next", body["links"])

    def test_an_unset_one_relationship_is_an_empty_page_not_an_error(self):
        # Person 3 has no primary_campus. That is an empty collection, and it must
        # come back shaped like every other page.
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people/3/primary_campus")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"], [])
        self.assertEqual(body["meta"]["total_count"], 0)
        self.assertNotIn("next", body["links"])

    def test_a_relationship_the_mirror_does_not_cover_still_passes_through(self):
        self.fake.request_log.clear()
        wsgi_get(self.m.wsgi, "/people/v2/people/1/workflow_cards")
        self.assertEqual(self.fake.request_log, [("GET", "/people/1/workflow_cards")])


if __name__ == "__main__":
    unittest.main()
