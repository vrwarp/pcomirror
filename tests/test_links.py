"""URL rewriting: served payloads must never hand back a URL needing a PCO credential."""
from __future__ import annotations

import json
import unittest

from base import build, wsgi_get
from pcomirror import links, registry
from pcomirror.config import Settings

S = Settings()
PCO = "https://api.planningcenteronline.com/people/v2"


class TestToMirrorPath(unittest.TestCase):
    def test_rewrites_pco_api_urls(self):
        self.assertEqual(links.to_mirror_path(f"{PCO}/people/1/emails", S),
                         "/people/v2/people/1/emails")

    def test_preserves_query_string(self):
        self.assertEqual(links.to_mirror_path(f"{PCO}/people?offset=25", S),
                         "/people/v2/people?offset=25")

    def test_leaves_the_web_ui_and_cdn_alone(self):
        for url in ("https://people.planningcenteronline.com/people/AC140201976",
                    "https://avatars.planningcenteronline.com/uploads/initials/KZ.png"):
            self.assertEqual(links.to_mirror_path(url, S), url)

    def test_leaves_other_hosts_and_relative_paths_alone(self):
        for url in ("https://example.org/people/v2/people/1", "/people/v2/people/1",
                    None, 7):
            self.assertEqual(links.to_mirror_path(url, S), url)

    def test_rewrites_the_other_products_too(self):
        """The mirror serves `/check-ins/v2/…` and friends by pass-through, so a
        page link into one is a link the caller can follow — but only relative.
        They hold a pcomirror key, not the PAT an absolute PCO URL would need."""
        self.assertEqual(
            links.to_mirror_path("https://api.planningcenteronline.com/services/v2/plans", S),
            "/services/v2/plans")
        self.assertEqual(
            links.to_mirror_path(
                "https://api.planningcenteronline.com/check-ins/v2/check_ins?offset=100", S),
            "/check-ins/v2/check_ins?offset=100")


class TestApiRoot(unittest.TestCase):
    """Where a foreign product is addressed from — `pco_base_url` minus People."""

    def test_derived_from_the_people_base(self):
        self.assertEqual(links.api_root(S), "https://api.planningcenteronline.com")

    def test_follows_a_configured_base_url(self):
        s = Settings(pco_base_url="https://pco-proxy.internal/people/v2")
        self.assertEqual(links.api_root(s), "https://pco-proxy.internal")

    def test_honours_a_configured_base_url(self):
        s = Settings(pco_base_url="https://pco-proxy.internal/people/v2")
        self.assertEqual(
            links.to_mirror_path("https://pco-proxy.internal/people/v2/people/1", s),
            "/people/v2/people/1")


class TestLinkMap(unittest.TestCase):
    def test_generated_from_the_registry(self):
        person = registry.by_name("person")
        m = links.link_map(person, "1")
        self.assertEqual(m["self"], "/people/v2/people/1")
        for rel in person.relationships:
            self.assertEqual(m[rel], f"/people/v2/people/1/{rel}")
        self.assertNotIn("html", m)

    def test_html_is_preserved_absolute(self):
        html = "https://people.planningcenteronline.com/people/AC1"
        m = links.link_map(registry.by_name("person"), "1", html)
        self.assertEqual(m["html"], html)


class TestServedPayload(unittest.TestCase):
    """The shape a client actually receives."""

    def setUp(self):
        self.m, self.fake = build()
        self.fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        self.fake.add_child("Email", "e1", "1", {"address": "a@x.org"}, "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")
        self.m.ingestor.backfill("email")

    def _person(self):
        return wsgi_get(self.m.wsgi, "/people/v2/people")[2]["data"][0]

    def test_no_pco_api_urls_survive(self):
        body = json.dumps(wsgi_get(self.m.wsgi, "/people/v2/people")[2])
        self.assertNotIn("api.planningcenteronline.com", body)

    def test_relationship_links_point_at_the_mirror(self):
        rels = self._person().get("relationships", {})
        for name, rel in rels.items():
            for url in (rel.get("links") or {}).values():
                self.assertTrue(url.startswith("/people/v2/"), f"{name}: {url}")

    def test_link_map_is_identical_regardless_of_sync_source(self):
        """The bug this fixes: a list page and a single fetch gave different maps.

        `html` is the one entry the mirror cannot generate — it needs PCO's
        account-prefixed id — so it is echoed when PCO supplies it. Everything
        else is generated, and PCO's extra entries are dropped.
        """
        generated = set(self._person()["links"])                 # from a list page
        # now simulate the same record written from a single-resource fetch, which
        # carries PCO's full link map
        raw = json.loads(self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"])
        raw["links"] = {"self": f"{PCO}/people/1", "notes": f"{PCO}/people/1/notes",
                        "workflow_cards": f"{PCO}/people/1/workflow_cards",
                        "html": "https://people.planningcenteronline.com/people/AC1"}
        self.m.db.execute("UPDATE person SET raw=?, source='reconcile' WHERE pco_id='1'",
                          (json.dumps(raw),))
        after = self._person()["links"]
        self.assertEqual(set(after) - {"html"}, generated)
        self.assertNotIn("notes", after)                         # PCO extras dropped
        self.assertNotIn("workflow_cards", after)
        self.assertEqual(after["self"], "/people/v2/people/1")

    def test_html_survives_but_stays_absolute(self):
        raw = json.loads(self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"])
        raw["links"] = {"html": "https://people.planningcenteronline.com/people/AC1"}
        self.m.db.execute("UPDATE person SET raw=? WHERE pco_id='1'", (json.dumps(raw),))
        self.assertEqual(self._person()["links"]["html"],
                         "https://people.planningcenteronline.com/people/AC1")

    def test_avatar_attributes_are_untouched(self):
        raw = json.loads(self.m.db.query_one("SELECT raw FROM person WHERE pco_id='1'")["raw"])
        avatar = "https://avatars.planningcenteronline.com/uploads/initials/KZ.png"
        raw.setdefault("attributes", {})["avatar"] = avatar
        self.m.db.execute("UPDATE person SET raw=? WHERE pco_id='1'", (json.dumps(raw),))
        self.assertEqual(self._person()["attributes"]["avatar"], avatar)

    def test_advertised_links_resolve(self):
        for url in self._person()["links"].values():
            if not url.startswith("/"):
                continue                                   # html
            status = wsgi_get(self.m.wsgi, url)[0]
            self.assertIn(status, (200, 404), f"{url} -> {status}")

    def test_included_resources_are_rewritten_too(self):
        body = json.dumps(wsgi_get(self.m.wsgi, "/people/v2/people", "include=emails")[2])
        self.assertNotIn("api.planningcenteronline.com", body)


class TestUnmirroredRelationship(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build()
        self.fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")

    def test_falls_back_to_passthrough_instead_of_400(self):
        status, headers, _ = wsgi_get(self.m.wsgi, "/people/v2/people/1/notes")
        self.assertNotEqual(status, 400)
        self.assertEqual(headers.get("X-Mirror-Source"), "passthrough")

    def test_requires_the_passthrough_scope(self):
        m, fake = build(allow_anonymous=False)
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        from pcomirror import apikeys
        key = apikeys.create(m.db, "app", "read:*")
        status, _, body = wsgi_get(m.wsgi, "/people/v2/people/1/notes",
                                   headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(status, 403)
        self.assertIn("passthrough", body["errors"][0]["detail"])

    def test_mirrored_relationships_still_served_locally(self):
        status, headers, _ = wsgi_get(self.m.wsgi, "/people/v2/people/1/emails")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Mirror-Source"), "mirror")


if __name__ == "__main__":
    unittest.main()
