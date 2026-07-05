import json
import unittest

from base import build, wsgi_call, wsgi_get


class TestServingReads(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build()
        self.fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        self.fake.add_person("2", "Grace", "Hopper", "2026-02-01T00:00:00Z", status="inactive")
        self.fake.add_child("Email", "e1", "1", {"address": "ada@x.org", "primary": True}, "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")

    def test_collection_and_meta(self):
        status, headers, body = wsgi_get(self.m.wsgi, "/people/v2/people")
        self.assertEqual(status, 200)
        self.assertEqual(body["meta"]["total_count"], 2)
        self.assertEqual(headers["X-Mirror-Source"], "mirror")
        self.assertIn("first_name", body["data"][0]["attributes"])
        self.assertIn("mirror", body["data"][0]["meta"])

    def test_where_and_order(self):
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "where[status]=active")
        self.assertEqual([d["id"] for d in body["data"]], ["1"])
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "order=-updated_at")
        self.assertEqual([d["id"] for d in body["data"]], ["2", "1"])
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "where[updated_at][gt]=2026-01-15T00:00:00Z")
        self.assertEqual([d["id"] for d in body["data"]], ["2"])

    def test_unsupported_filter_400(self):
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "where[medical_notes]=x")
        self.assertEqual(status, 400)

    def test_include(self):
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "include=emails")
        inc = body.get("included", [])
        self.assertTrue(any(i["type"] == "Email" and i["id"] == "e1" for i in inc))

    def test_pagination(self):
        for i in range(3, 30):
            self.fake.add_person(str(i), f"P{i}", "X", f"2026-03-{i:02d}T00:00:00Z")
        self.m.ingestor.backfill("person")
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", "per_page=10&offset=0")
        self.assertEqual(len(body["data"]), 10)
        self.assertIn("next", body["links"])

    def test_single_and_410_on_merge(self):
        # a duplicate person appears after the initial backfill -> picked up by a sweep
        self.fake.add_person("2b", "Dup", "Person", "2026-03-01T00:00:00Z")
        self.m.ingestor.incremental_sweep("person")
        self.fake.merge(keep="1", remove="2b", created="2026-04-01T00:00:00Z")
        self.m.ingestor.merger_poll()
        status, headers, body = wsgi_get(self.m.wsgi, "/people/v2/people/2b")
        self.assertEqual(status, 410)
        self.assertEqual(body["errors"][0]["meta"]["merged_into"], "1")
        self.assertIn("Location", headers)


class TestWriteThrough(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build()
        self.fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")

    def test_patch_hits_pco_and_updates_mirror(self):
        body = json.dumps({"data": {"type": "Person", "id": "1",
                                    "attributes": {"last_name": "Byron"}}}).encode()
        status, _, out = wsgi_call(self.m.wsgi, "PATCH", "/people/v2/people/1", body=body)
        self.assertEqual(status, 200)
        self.assertIn(("PATCH", "/people/1"), self.fake.request_log)
        self.assertEqual(self.m.db.query_one("SELECT last_name FROM person WHERE pco_id='1'")["last_name"], "Byron")

    def test_post_creates_and_inserts(self):
        body = json.dumps({"data": {"type": "Person",
                                    "attributes": {"first_name": "New", "last_name": "Person",
                                                   "status": "active"}}}).encode()
        status, _, out = wsgi_call(self.m.wsgi, "POST", "/people/v2/people", body=body)
        self.assertEqual(status, 201)
        new_id = out["data"]["id"]
        self.assertIsNotNone(self.m.db.query_one("SELECT 1 FROM person WHERE pco_id=?", (new_id,)))

    def test_delete_tombstones(self):
        status, _, _ = wsgi_call(self.m.wsgi, "DELETE", "/people/v2/people/1")
        self.assertIn(status, (200, 204))
        self.assertIsNotNone(self.m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='1'")["deleted_at"])

    def test_fail_if_pco_fails_leaves_mirror_untouched(self):
        before = self.m.db.query_one("SELECT count(*) c FROM person")["c"]
        self.fake.fail_next = (422, "invalid")
        body = json.dumps({"data": {"type": "Person", "attributes": {"first_name": "Bad"}}}).encode()
        status, _, out = wsgi_call(self.m.wsgi, "POST", "/people/v2/people", body=body)
        self.assertEqual(status, 422)
        self.assertEqual(self.m.db.query_one("SELECT count(*) c FROM person")["c"], before)

    def test_patch_conflict_relayed_no_write(self):
        self.fake.fail_next = (409, "stale")
        body = json.dumps({"data": {"type": "Person", "id": "1",
                                    "attributes": {"last_name": "ShouldNotApply"}}}).encode()
        status, _, _ = wsgi_call(self.m.wsgi, "PATCH", "/people/v2/people/1", body=body)
        self.assertEqual(status, 409)
        self.assertEqual(self.m.db.query_one("SELECT last_name FROM person WHERE pco_id='1'")["last_name"], "Lovelace")


class TestPassThrough(unittest.TestCase):
    def test_single_miss_passthrough_reads_and_warms_mirror(self):
        m, fake = build()
        fake.add_person("77", "Live", "Only", "2026-01-01T00:00:00Z")  # at PCO, not mirrored
        status, headers, body = wsgi_get(m.wsgi, "/people/v2/people/77", "passthrough=on")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Mirror-Source"), "passthrough")
        # read-through warmed the mirror
        self.assertIsNotNone(m.db.query_one("SELECT 1 FROM person WHERE pco_id='77'"))


if __name__ == "__main__":
    unittest.main()
