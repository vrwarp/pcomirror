"""The other Planning Center products, reached through the mirror.

The mirror holds People and only People. But a caller doing the base-URL swap
the mirror promises points *all* of its PCO traffic at it, not just the People
half — a directory app that reads `/check-ins/v2/…` for attendance and
`/groups/v2/…` for membership sends those here too. Those paths used to 404,
which made the swap a code change for anybody reading a second product.

They now resolve against PCO. Three things about that are easy to get wrong and
are pinned here: the URL the mirror builds, the version header it does *not*
send, and — the one that would corrupt data rather than merely fail — that
nothing coming back from a foreign product is written to the mirror.
"""
import json
import unittest

from base import build, wsgi_call, wsgi_get
from fakepco import FakePCO
from pcomirror.pcoclient import Response


class ProductAwareFake(FakePCO):
    """A FakePCO that also answers Check-Ins and Groups, and records the exchange."""

    def __init__(self):
        super().__init__()
        self.foreign: list[tuple[str, str, str | None]] = []

    def send(self, method, url, headers, body):
        if "/check-ins/v2/" in url or "/groups/v2/" in url:
            self.foreign.append((method, url, headers.get("X-PCO-API-Version")))
            if "/check-ins/v2/people/" in url:
                # A Check-Ins Person: same JSON:API type as a People Person, a
                # different record, and deliberately newer than the mirrored one.
                doc = {"data": {"id": "1", "type": "Person",
                                "attributes": {"first_name": "NotAda",
                                               "check_in_count": 5,
                                               "updated_at": "2030-01-01T00:00:00Z"}}}
            else:
                doc = {"data": [], "meta": {"total_count": 3, "count": 0},
                       "links": {"next": "https://api.planningcenteronline.com"
                                         "/check-ins/v2/check_ins?offset=100"}}
            return Response(200, {"Content-Type": "application/json"},
                            json.dumps(doc).encode())
        return super().send(method, url, headers, body)


class TestForeignProductsPassThrough(unittest.TestCase):
    def setUp(self):
        self.fake = ProductAwareFake()
        self.fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        self.m, _ = build(self.fake)
        self.m.ingestor.backfill("person")

    def test_a_foreign_get_is_served_rather_than_404ed(self):
        for path in ("/check-ins/v2/people/1", "/check-ins/v2/events",
                     "/groups/v2/people/1/memberships"):
            status, headers, _ = wsgi_get(self.m.wsgi, path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["X-Mirror-Source"], "passthrough", path)

    def test_it_is_addressed_from_the_api_root_not_under_people_v2(self):
        """`pco_base_url` ends in `/people/v2`, so relaying the path against it
        would have asked PCO for `/people/v2/check-ins/v2/events`."""
        wsgi_get(self.m.wsgi, "/check-ins/v2/events")
        _, url, _ = self.fake.foreign[-1]
        self.assertIn("/check-ins/v2/events", url)
        self.assertNotIn("/people/v2/check-ins", url)

    def test_the_people_version_pin_is_not_sent_to_another_product(self):
        """A PCO version string names a dated revision of one product. People's is
        not a valid Check-Ins version, so sending it is an error rather than a
        default — the header comes off and PCO answers at the org's own version."""
        wsgi_get(self.m.wsgi, "/check-ins/v2/events")
        _, _, version = self.fake.foreign[-1]
        self.assertIsNone(version)

    def test_a_foreign_payload_never_reaches_the_mirror(self):
        """The corruption guard. The registry routes a payload by its JSON:API
        `type`, and `type` is not unique across products: a Check-Ins `Person` is
        a different record in a different id space. Warming from one would
        overwrite a mirrored person with a stranger sharing an id — and this
        one's `updated_at` is newer, so the monotonic guard would have let it."""
        wsgi_get(self.m.wsgi, "/check-ins/v2/people/1")
        row = self.m.db.query_one("SELECT first_name FROM person WHERE pco_id='1'")
        self.assertEqual(row["first_name"], "Ada")

    def test_foreign_links_are_rewritten_back_onto_the_mirror(self):
        """The caller holds a pcomirror key, not a PCO PAT, so an absolute PCO
        URL handed back is one they cannot follow."""
        _, _, body = wsgi_get(self.m.wsgi, "/check-ins/v2/check_ins", "per_page=100")
        self.assertEqual(body["links"]["next"], "/check-ins/v2/check_ins?offset=100")

    def test_a_foreign_write_is_still_refused(self):
        """Read-only: a write to an unmirrored product would be the mirror lending
        out its credential with no record of what was done with it."""
        status, _, _ = wsgi_call(self.m.wsgi, "POST", "/check-ins/v2/check_ins",
                                 "", b"{}")
        self.assertEqual(status, 404)
        self.assertEqual(self.fake.foreign, [])

    def test_people_reads_are_unaffected(self):
        status, headers, body = wsgi_get(self.m.wsgi, "/people/v2/people")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Mirror-Source"], "mirror")
        self.assertEqual(body["data"][0]["attributes"]["first_name"], "Ada")


class TestForeignProductsNeedThePassthroughScope(unittest.TestCase):
    """Spending the server's PCO credential is a distinct privilege from reading
    the mirror, and reaching a second product does not escape that."""

    def setUp(self):
        from pcomirror import apikeys
        self.fake = ProductAwareFake()
        self.m, _ = build(self.fake, allow_anonymous=False)
        self.read_only = apikeys.create(self.m.db, "reader", scopes="read:*")
        self.full = apikeys.create(self.m.db, "proxy",
                                   scopes=f"read:*,{apikeys.SCOPE_PASSTHROUGH}")

    def _get(self, path, key):
        return wsgi_get(self.m.wsgi, path, "", {"Authorization": f"Bearer {key}"})

    def test_a_read_only_key_cannot_reach_another_product(self):
        status, _, _ = self._get("/check-ins/v2/events", self.read_only)
        self.assertEqual(status, 403)
        self.assertEqual(self.fake.foreign, [])

    def test_a_passthrough_key_can(self):
        status, _, _ = self._get("/check-ins/v2/events", self.full)
        self.assertEqual(status, 200)

    def test_an_unauthenticated_caller_still_gets_401(self):
        status, _, _ = wsgi_get(self.m.wsgi, "/check-ins/v2/events")
        self.assertEqual(status, 401)
        self.assertEqual(self.fake.foreign, [])


if __name__ == "__main__":
    unittest.main()
