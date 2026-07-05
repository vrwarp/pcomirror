import hashlib
import hmac
import json
import unittest

from base import build


def _sign(secret, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _delivery(event_name, payload, delivery_id="d1", event_id="ev1", attempt=1):
    return {"id": delivery_id, "attempt": attempt,
            "data": [{"id": event_id, "type": "Event",
                      "attributes": {"name": event_name, "payload": json.dumps(payload)}}]}


class TestWebhooks(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build()
        self.secret = "whsec_test"
        self.token = "tok123"
        self.m.db.execute(
            "INSERT INTO webhook_subscription(subscription_pco_id,event_name,resource,action,"
            "url_token,authenticity_secret) VALUES(?,?,?,?,?,?)",
            ("sub1", "people.v2.events.person.updated", "person", "updated", self.token, self.secret))

    def _post(self, event_name, payload, secret=None, **kw):
        env = _delivery(event_name, payload, **kw)
        raw = json.dumps(env).encode()
        sig = _sign(secret or self.secret, raw)
        return self.m.webhooks.receive(self.token, raw, sig)

    def test_bad_signature_401_unknown_token_404(self):
        env = _delivery("people.v2.events.person.created", {"id": "1"})
        raw = json.dumps(env).encode()
        self.assertEqual(self.m.webhooks.receive(self.token, raw, "deadbeef")[0], 401)
        self.assertEqual(self.m.webhooks.receive("nope", raw, _sign(self.secret, raw))[0], 404)

    def test_created_dispatched_and_deduped(self):
        person = {"id": "1", "type": "Person",
                  "attributes": {"first_name": "Ada", "last_name": "L", "status": "active",
                                 "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}}
        code, _ = self._post("people.v2.events.person.created", person)
        self.assertEqual(code, 204)
        # duplicate delivery of the same event id -> still one inbox row
        self._post("people.v2.events.person.created", person)
        self.assertEqual(self.m.db.query_one("SELECT count(*) c FROM webhook_event")["c"], 1)
        self.m.webhooks.drain()
        self.assertEqual(self.m.db.query_one("SELECT first_name FROM person WHERE pco_id='1'")["first_name"], "Ada")
        self.assertEqual(self.m.db.query_one("SELECT status FROM webhook_event WHERE event_id='ev1'")["status"], "done")

    def test_destroyed_tombstones(self):
        person = {"id": "1", "type": "Person",
                  "attributes": {"first_name": "Ada", "last_name": "L", "status": "active",
                                 "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}}
        self._post("people.v2.events.person.created", person, event_id="e1", delivery_id="da")
        self.m.webhooks.drain()
        self._post("people.v2.events.person.destroyed",
                   {"id": "1", "type": "Person", "attributes": {"updated_at": "2026-02-01T00:00:00Z"}},
                   event_id="e2", delivery_id="db")
        self.m.webhooks.drain()
        self.assertIsNotNone(self.m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='1'")["deleted_at"])

    def test_thin_person_enqueues_hydration(self):
        # person webhook has no children embedded -> should enqueue a hydrate
        self.fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        self.fake.add_child("Email", "e1", "1", {"address": "a@x.org"}, "2026-01-01T00:00:00Z")
        person = self.fake.data["Person"]["1"]
        self._post("people.v2.events.person.updated", person, event_id="e9", delivery_id="dc")
        self.m.webhooks.drain()
        self.assertEqual(self.m.db.query_one(
            "SELECT count(*) c FROM hydration_task WHERE resource_type='person' AND pco_id='1'")["c"], 1)
        # draining hydration pulls the child in
        self.m.ingestor.drain_hydration()
        self.assertEqual(self.m.db.query_one(
            "SELECT count(*) c FROM email WHERE person_pco_id='1' AND deleted_at IS NULL")["c"], 1)

    def test_merge_webhook(self):
        self.m.writer.upsert("person", "2", {"id": "2", "type": "Person",
                             "attributes": {"first_name": "Dup", "last_name": "X", "status": "active",
                                            "created_at": "2020-01-01T00:00:00Z",
                                            "updated_at": "2026-01-01T00:00:00Z"}}, "backfill")
        self._post("people.v2.events.person_merger.created",
                   {"id": "m1", "type": "PersonMerger",
                    "attributes": {"person_to_keep_id": "1", "person_to_remove_id": "2"}},
                   event_id="em", delivery_id="dm")
        self.m.webhooks.drain()
        row = self.m.db.query_one("SELECT deleted_at, merged_into_pco_id FROM person WHERE pco_id='2'")
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(row["merged_into_pco_id"], "1")


if __name__ == "__main__":
    unittest.main()
