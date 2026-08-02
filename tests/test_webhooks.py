import hashlib
import hmac
import json
import unittest

from base import build, wsgi_get
from pcomirror.config import Settings, parse_subscriptions
from pcomirror.webhooks import TOKEN_RE, upsert_subscription


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


def _document(resource, parent=None):
    """A payload exactly as Planning Center sends one: the resource one level
    down in a JSON:API document, not the resource itself. Taken verbatim from a
    production receiver's recorded calls (ids and names replaced)."""
    doc = {"data": resource, "meta": {"parent": parent or {"id": "461178", "type": "Organization"}}}
    return doc


def _real_delivery(event_name, document, event_id):
    """A delivery envelope with the real shape: only `data`, no top-level id."""
    return {"data": [{"id": event_id, "type": "EventDelivery",
                      "attributes": {"name": event_name,
                                     "payload": json.dumps(document)}}]}


class TestTheRealPayloadShape(unittest.TestCase):
    """Deliveries as Planning Center actually sends them.

    A recorded production log (116 calls) showed every payload as a full
    document — `{"data": {...}, "meta": {...}}` — and `person_merger` ids as
    JSON numbers. The suite's own fixture used the bare resource, so all of
    this passed while every real destroy dead-lettered and every real merge
    silently applied nothing.
    """

    def setUp(self):
        self.m, self.fake = build()
        self.secret = "whsec_test"
        self.token = "tok123"
        self.m.db.execute(
            "INSERT INTO webhook_subscription(subscription_pco_id,event_name,resource,action,"
            "url_token,authenticity_secret) VALUES(?,?,?,?,?,?)",
            ("sub1", "people.v2.events.person.updated", "person", "updated", self.token, self.secret))
        self._eid = 0

    def _post(self, event_name, document):
        self._eid += 1
        raw = json.dumps(_real_delivery(event_name, document, f"rev{self._eid}")).encode()
        code, _ = self.m.webhooks.receive(self.token, raw, _sign(self.secret, raw))
        self.assertEqual(code, 204)

    def _person(self, pid, first, uat="2026-01-01T00:00:00Z"):
        return {"id": pid, "type": "Person",
                "attributes": {"first_name": first, "last_name": "Real", "status": "active",
                               "child": False, "created_at": "2026-01-01T00:00:00Z",
                               "updated_at": uat}}

    def _drained_clean(self):
        self.m.webhooks.drain()
        self.assertEqual(self.m.db.query("SELECT * FROM webhook_dead_letter"), [])
        self.assertEqual(self.m.db.query(
            "SELECT * FROM webhook_event WHERE status NOT IN ('done','ignored')"), [])

    def test_a_created_documents_person_lands(self):
        self._post("people.v2.events.person.created", _document(self._person("9001", "Ada")))
        self._drained_clean()
        self.assertEqual(self.m.db.query_one(
            "SELECT first_name FROM person WHERE pco_id='9001'")["first_name"], "Ada")
        # thin payload -> the survivor of the old bug: hydration must still enqueue
        self.assertIsNotNone(self.m.db.query_one(
            "SELECT 1 FROM hydration_task WHERE resource_type='person' AND pco_id='9001'"))

    def test_a_destroy_with_no_attributes_tombstones_and_cascades(self):
        # Real destroy payloads carry relationships and links but no attributes.
        self.m.writer.upsert("person", "9002", self._person("9002", "Gone"), "backfill")
        self.m.writer.upsert("email", "e9002", {
            "id": "e9002", "type": "Email",
            "attributes": {"address": "gone@x.org", "updated_at": "2026-01-01T00:00:00Z"},
            "relationships": {"person": {"data": {"type": "Person", "id": "9002"}}}}, "backfill")
        self._post("people.v2.events.person.destroyed", _document(
            {"id": "9002", "type": "Person",
             "relationships": {"primary_campus": {"data": None}, "gender": {"data": None}},
             "links": {"self": "https://api.planningcenteronline.com/people/v2/people/9002"}}))
        self._drained_clean()
        person = self.m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='9002'")
        self.assertIsNotNone(person["deleted_at"])
        email = self.m.db.query_one(
            "SELECT deleted_at, tombstone_reason FROM email WHERE pco_id='e9002'")
        self.assertIsNotNone(email["deleted_at"])
        self.assertEqual(email["tombstone_reason"], "owner_deleted")

    def test_a_merger_with_numeric_ids_buries_and_points(self):
        self.m.writer.upsert("person", "9003", self._person("9003", "Dup"), "backfill")
        self.fake.add_person("9004", "Kept", "Real", "2026-01-02T00:00:00Z")
        self._post("people.v2.events.person_merger.created", _document(
            {"id": "777001", "type": "PersonMerger",
             "attributes": {"created_at": "2026-08-01T23:49:13Z",
                            "person_to_keep_id": 9004, "person_to_remove_id": 9003},
             "relationships": {
                 "person_to_keep": {"data": {"type": "Person", "id": "9004"}},
                 "person_to_remove": {"data": {"type": "Person", "id": "9003"}}}}))
        self._drained_clean()
        row = self.m.db.query_one(
            "SELECT deleted_at, tombstone_reason, merged_into_pco_id FROM person WHERE pco_id='9003'")
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual((row["tombstone_reason"], row["merged_into_pco_id"]), ("merged", "9004"))
        self.assertEqual(self.m.db.query_one(
            "SELECT pco_id FROM hydration_task WHERE resource_type='person'")["pco_id"], "9004")
        self.assertIsNotNone(self.m.db.query_one(
            "SELECT 1 FROM person_merger WHERE pco_id='777001'"))
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people/9003")
        self.assertEqual(status, 410)
        self.assertEqual(body["errors"][0]["meta"]["merged_into"], "9004")

    def test_the_destroy_may_arrive_before_its_merger(self):
        # The production log's ordering at 23:49:17Z: person.destroyed for the
        # removed record, then person_merger.created, one second apart.
        self.m.writer.upsert("person", "9005", self._person("9005", "Dup"), "backfill")
        self._post("people.v2.events.person.destroyed", _document(
            {"id": "9005", "type": "Person", "relationships": {}}))
        self._drained_clean()
        self._post("people.v2.events.person_merger.created", _document(
            {"id": "777002", "type": "PersonMerger",
             "attributes": {"created_at": "2026-08-01T23:49:13Z",
                            "person_to_keep_id": 9006, "person_to_remove_id": 9005}}))
        self._drained_clean()
        row = self.m.db.query_one(
            "SELECT merged_into_pco_id FROM person WHERE pco_id='9005'")
        self.assertEqual(row["merged_into_pco_id"], "9006")

    def test_the_merger_may_arrive_before_its_destroy(self):
        self.m.writer.upsert("person", "9007", self._person("9007", "Dup"), "backfill")
        self._post("people.v2.events.person_merger.created", _document(
            {"id": "777003", "type": "PersonMerger",
             "attributes": {"created_at": "2026-08-01T23:49:13Z",
                            "person_to_keep_id": 9008, "person_to_remove_id": 9007}}))
        self._drained_clean()
        self._post("people.v2.events.person.destroyed", _document(
            {"id": "9007", "type": "Person", "relationships": {}}))
        self._drained_clean()
        row = self.m.db.query_one(
            "SELECT merged_into_pco_id, deleted_at FROM person WHERE pco_id='9007'")
        self.assertIsNotNone(row["deleted_at"])
        # the destroy must not erase where the merge went
        self.assertEqual(row["merged_into_pco_id"], "9008")

    def test_a_merge_fan_whose_keeper_is_then_destroyed(self):
        # The log's second fan: seven people merged into one keeper inside a
        # minute, then the keeper itself destroyed. Every id must answer 410,
        # the removed ones still naming the keeper they went into.
        for pid in ("9010", "9011"):
            self.m.writer.upsert("person", pid, self._person(pid, f"Dup{pid}"), "backfill")
        self.m.writer.upsert("person", "9012", self._person("9012", "Keeper"), "backfill")
        for n, pid in enumerate(("9010", "9011")):
            self._post("people.v2.events.person_merger.created", _document(
                {"id": f"77701{n}", "type": "PersonMerger",
                 "attributes": {"created_at": "2026-08-01T23:50:31Z",
                                "person_to_keep_id": 9012, "person_to_remove_id": int(pid)}}))
        self._drained_clean()
        self._post("people.v2.events.person.destroyed", _document(
            {"id": "9012", "type": "Person", "relationships": {}}))
        self._drained_clean()
        # keeper hydrations (merge_survivor) drain against a PCO that has
        # forgotten the keeper too - they must settle, not spin
        self.m.ingestor.drain_hydration()
        for pid in ("9010", "9011"):
            status, _, body = wsgi_get(self.m.wsgi, f"/people/v2/people/{pid}")
            self.assertEqual(status, 410)
            self.assertEqual(body["errors"][0]["meta"]["merged_into"], "9012")
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people/9012")
        self.assertEqual(status, 410)
        self.assertNotIn("merged_into", body["errors"][0].get("meta") or {})

    def test_a_document_for_an_unmirrored_resource_is_still_ignored_not_dead(self):
        self._post("people.v2.events.workflow_card.created", _document(
            {"id": "555", "type": "WorkflowCard", "attributes": {"stage": "new"}}))
        self.m.webhooks.drain()
        row = self.m.db.query_one("SELECT status, last_error FROM webhook_event")
        self.assertEqual(row["status"], "ignored")
        self.assertEqual(self.m.db.query("SELECT * FROM webhook_dead_letter"), [])

    def test_the_delivery_row_carries_the_headers_identity(self):
        # A real envelope has no top-level id; the X-Pco-Webhooks-Event header
        # is what names the delivery. Without it every same-second delivery
        # collapsed into one nodelivery-<second> audit row.
        doc = _document(self._person("9020", "Head"))
        raw = json.dumps(_real_delivery("people.v2.events.person.created", doc, "rev-h1")).encode()
        code, _ = self.m.webhooks.receive(self.token, raw, _sign(self.secret, raw),
                                          delivery_id="0ae1c5fe-4d6d-4b18", attempt="2")
        self.assertEqual(code, 204)
        row = self.m.db.query_one(
            "SELECT delivery_id, attempt FROM webhook_delivery")
        self.assertEqual((row["delivery_id"], row["attempt"]), ("0ae1c5fe-4d6d-4b18", 2))

    def test_dead_letters_from_the_envelope_era_can_be_retried(self):
        # The production shape this exists for: destroys dead-lettered by the
        # payload misread, retried after the fix, landing their tombstones.
        from pcomirror import webhooks as webhooks_mod
        self.m.writer.upsert("person", "9021", self._person("9021", "Buried"), "backfill")
        self.m.db.execute(
            "INSERT INTO webhook_delivery(delivery_id,subscription_pco_id,signature,raw_body,attempt) "
            "VALUES('d-old','sub1','',x'',NULL)")
        self.m.db.execute(
            "INSERT INTO webhook_event(event_id,delivery_id,event_name,resource_type,action,pco_id,payload,"
            "status,process_attempts,last_error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("ev-old", "d-old", "people.v2.events.person.destroyed", "person", "destroyed",
             None, json.dumps(_document({"id": "9021", "type": "Person", "relationships": {}})),
             "dead", 8, "'id'"))
        self.m.db.execute(
            "INSERT INTO webhook_dead_letter(event_id,event_name,payload,last_error,attempts) "
            "VALUES('ev-old','people.v2.events.person.destroyed','{}',\"'id'\",8)")

        self.assertEqual(webhooks_mod.retry_dead_letters(self.m.db), 1)
        self.assertEqual(self.m.db.query("SELECT * FROM webhook_dead_letter"), [])
        self.m.webhooks.drain()
        self.assertIsNotNone(self.m.db.query_one(
            "SELECT deleted_at FROM person WHERE pco_id='9021'")["deleted_at"])
        self.assertEqual(self.m.db.query_one(
            "SELECT status FROM webhook_event WHERE event_id='ev-old'")["status"], "done")

    def test_a_list_results_destroy_document_tombstones_it(self):
        # `list_result` is mirrored (a walked child of `list`), so its destroy
        # is applied, not ignored - through the same document unwrapping.
        self.m.writer.upsert("list_result", "lr1", {
            "id": "lr1", "type": "ListResult",
            "attributes": {"updated_at": "2026-01-01T00:00:00Z"},
            "relationships": {"list": {"data": {"type": "List", "id": "L1"}},
                              "person": {"data": {"type": "Person", "id": "9001"}}}}, "backfill")
        self._post("people.v2.events.list_result.destroyed", _document(
            {"id": "lr1", "type": "ListResult", "relationships": {}}))
        self._drained_clean()
        self.assertIsNotNone(self.m.db.query_one(
            "SELECT deleted_at FROM list_result WHERE pco_id='lr1'")["deleted_at"])


class TestSubscriptionRegistration(unittest.TestCase):
    """`upsert_subscription` backs both `add-subscription` and PCOMIRROR_SUBSCRIPTIONS,
    so it has to be safe to re-run on every container start."""

    def setUp(self):
        self.m, _ = build()

    def _row(self, sub_id="sub1"):
        return self.m.db.query_one(
            "SELECT * FROM webhook_subscription WHERE subscription_pco_id=?", (sub_id,))

    def test_explicit_token_is_used_and_parses_event(self):
        token, created = upsert_subscription(
            self.m.db, "sub1", "people.v2.events.person.updated", "s3cret", "my-token-01")
        self.assertEqual((token, created), ("my-token-01", True))
        row = self._row()
        self.assertEqual((row["resource"], row["action"]), ("person", "updated"))
        self.assertEqual(row["authenticity_secret"], "s3cret")

    def test_generated_token_is_url_safe(self):
        token, _ = upsert_subscription(self.m.db, "sub1", "people.v2.events.person.created", "s")
        self.assertRegex(token, TOKEN_RE)

    def test_reapply_keeps_token_but_refreshes_secret(self):
        token, _ = upsert_subscription(self.m.db, "sub1", "people.v2.events.person.updated", "old")
        again, created = upsert_subscription(
            self.m.db, "sub1", "people.v2.events.person.updated", "new")
        # the URL registered at PCO must survive a restart; only the secret moves
        self.assertEqual(again, token)
        self.assertFalse(created)
        self.assertEqual(self._row()["authenticity_secret"], "new")
        self.assertEqual(self.m.db.query_one("SELECT count(*) c FROM webhook_subscription")["c"], 1)

    def test_reapply_with_explicit_token_rotates_it(self):
        upsert_subscription(self.m.db, "sub1", "people.v2.events.person.updated", "s", "token-aaa")
        token, _ = upsert_subscription(
            self.m.db, "sub1", "people.v2.events.person.updated", "s", "token-bbb")
        self.assertEqual(token, "token-bbb")

    def test_rejects_unusable_tokens(self):
        for bad in ("short", "has/slash/in/it", "has space", "x" * 65, "tok?en=1"):
            with self.assertRaises(ValueError):
                upsert_subscription(self.m.db, "sub1", "people.v2.events.person.updated", "s", bad)

    def test_registered_subscription_receives(self):
        token, _ = upsert_subscription(
            self.m.db, "sub1", "people.v2.events.person.created", "whsec", "live-token-1")
        env = _delivery("people.v2.events.person.created", {"id": "1"})
        raw = json.dumps(env).encode()
        self.assertEqual(self.m.webhooks.receive(token, raw, _sign("whsec", raw))[0], 204)


class TestParseSubscriptions(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(parse_subscriptions(None), [])
        self.assertEqual(parse_subscriptions("  "), [])

    def test_compact_form(self):
        specs = parse_subscriptions(
            "sub1:people.v2.events.person.updated:tok-one-01:sec1,"
            " sub2:people.v2.events.person.created::sec2 ")
        self.assertEqual([s.subscription_id for s in specs], ["sub1", "sub2"])
        self.assertEqual(specs[0].url_token, "tok-one-01")
        self.assertEqual(specs[1].url_token, "")          # empty = keep/mint
        self.assertEqual(specs[1].secret, "sec2")

    def test_secret_may_contain_colons(self):
        specs = parse_subscriptions("sub1:people.v2.events.person.updated:tok-one-01:a:b:c")
        self.assertEqual(specs[0].secret, "a:b:c")

    def test_json_form(self):
        specs = parse_subscriptions(
            '[{"id":"sub1","event":"people.v2.events.person.updated",'
            '"token":"tok-one-01","secret":"a,b:c"}]')
        self.assertEqual(specs[0].secret, "a,b:c")        # JSON form for awkward secrets
        self.assertEqual(specs[0].url_token, "tok-one-01")

    def test_malformed_raises(self):
        for bad in ("sub1:event:token", "::tok:sec", "sub1::tok:sec",
                    "[{\"id\":\"s\"}]", "[1]", "{\"id\":\"s\"}", "[not json"):
            with self.assertRaises(ValueError, msg=bad):
                parse_subscriptions(bad)

    def test_settings_reads_env(self):
        s = Settings.from_env({"PCOMIRROR_SUBSCRIPTIONS":
                               "sub1:people.v2.events.person.updated:tok-one-01:sec"})
        self.assertEqual(len(s.subscriptions), 1)
        self.assertEqual(Settings.from_env({}).subscriptions, [])


if __name__ == "__main__":
    unittest.main()
