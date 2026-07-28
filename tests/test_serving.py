import json
import unittest

from base import build, wsgi_call, wsgi_get
from fakepco import FakePCO, res
from pcomirror import diagnostics
from pcomirror.pcoclient import Response


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



class TestWritesAreNeverReplayed(unittest.TestCase):
    """A mutation is sent to PCO exactly once, and a lost answer says so.

    All three failures here happened together on one "add a parent" in Tally: the
    write-through created five copies of one person, and the leader was told the
    mirror could not reach Planning Center at all. A write whose response went
    missing is not a write that did not happen, and nothing in this file may ever
    treat it as one again.
    """

    def _body(self, first="Dana", last="Reed"):
        return json.dumps({"data": {"type": "Person",
                                    "attributes": {"first_name": first, "last_name": last}}}).encode()

    def test_lost_response_is_reported_as_indeterminate_not_replayed(self):
        m, fake = build()
        fake.unreachable = True
        status, _, out = wsgi_call(m.wsgi, "POST", "/people/v2/people", body=self._body())
        # Exactly one attempt: the write may already have landed upstream.
        self.assertEqual(sum(1 for method, _ in fake.request_log if method == "POST"), 1)
        # The 504 DESIGN §8.4 specifies, not the bare 500 the blanket handler
        # used to produce — that status is an instruction to retry, which is the
        # one thing a caller must not do here.
        self.assertEqual(status, 504)
        error = out["errors"][0]
        self.assertEqual(error["meta"]["code"], "upstream_response_lost")
        self.assertTrue(error["meta"]["write_indeterminate"])
        self.assertFalse(error["meta"]["safe_to_retry"])

    def test_write_is_not_replayed_when_pco_answers_5xx(self):
        """The write lands and the gateway loses the answer — the classic duplicate."""

        class WritesLandThenFail(FakePCO):
            def _write(self, method, segs, body):
                super()._write(method, segs, body)          # the row IS created
                return Response(502, {}, b'{"errors":[{"code":"502"}]}')

        fake = WritesLandThenFail()
        m, _ = build(fake)
        status, _, _ = wsgi_call(m.wsgi, "POST", "/people/v2/people", body=self._body())
        self.assertEqual(status, 502)
        self.assertEqual(sum(1 for method, _ in fake.request_log if method == "POST"), 1)
        self.assertEqual(len(fake.data.get("Person", {})), 1)

    def test_a_read_is_still_retried_through_a_5xx(self):
        """The guard is about the verb, not about giving up on transient failures."""

        class FlakyReads(FakePCO):
            def __init__(self):
                super().__init__()
                self.failures = 2

            def send(self, method, url, headers, body):
                if method == "GET" and self.failures > 0:
                    self.failures -= 1
                    self.request_log.append((method, url))
                    return Response(503, {}, b'{"errors":[{"code":"503"}]}')
                return super().send(method, url, headers, body)

        fake = FlakyReads()
        fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        m, _ = build(fake)
        status, _, body = wsgi_get(m.wsgi, "/people/v2/people/1", "passthrough=on")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["id"], "1")

    def test_a_429_still_retries_a_write(self):
        """A limiter refuses before the request reaches anything that could apply it."""

        class RateLimitedOnce(FakePCO):
            def __init__(self):
                super().__init__()
                self.limited = True

            def _write(self, method, segs, body):
                if self.limited:
                    self.limited = False
                    return Response(429, {"Retry-After": "0"}, b'{"errors":[{"code":"429"}]}')
                return super()._write(method, segs, body)

        fake = RateLimitedOnce()
        m, _ = build(fake)
        status, _, _ = wsgi_call(m.wsgi, "POST", "/people/v2/people", body=self._body())
        self.assertEqual(status, 201)
        self.assertEqual(sum(1 for method, _ in fake.request_log if method == "POST"), 2)
        self.assertEqual(len(fake.data.get("Person", {})), 1)


class TestRelayedHeaders(unittest.TestCase):
    """What PCO said about *its* hop must not be repeated about ours.

    Every served document is rewritten on the way out — absolute PCO URLs become
    mirror-relative paths — so relaying PCO's `Content-Length` promised a body
    longer than the one that followed. A caller then waits for a remainder that
    never comes and reports a dropped connection, which is how a write that
    reached PCO got replayed as one that never left.
    """

    @staticmethod
    def _raw_call(app, method, path, body=b""):
        """Like `wsgi_call`, but keeps the bytes on the wire.

        The whole point of this class is the relationship between the declared
        length and the sent length, and a parsed-then-reserialized body is a
        different number of bytes than the one the client would actually read.
        """
        import io
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        captured = {}

        def start_response(status, hdrs):
            captured["status"] = int(status.split()[0])
            captured["headers"] = dict(hdrs)

        raw = b"".join(app(env, start_response))
        return captured["status"], captured["headers"], raw

    def assertLengthMatchesBody(self, headers, raw):
        self.assertEqual(int(headers["Content-Length"]), len(raw),
                         "a caller would block waiting for bytes that never come")

    class ChattyPCO(FakePCO):
        """PCO as it really answers a create: absolute links, and its own framing."""

        declared_length = None

        def _write(self, method, segs, body):
            resp = super()._write(method, segs, body)
            doc = json.loads(resp.body)
            pid = doc["data"]["id"]
            doc["data"]["links"] = {
                "self": f"https://api.planningcenteronline.com/people/v2/people/{pid}",
                "html": f"https://people.planningcenteronline.com/people/AC{pid}",
            }
            payload = json.dumps(doc).encode()
            self.declared_length = len(payload)
            return Response(resp.status, {
                "Content-Type": "application/vnd.api+json",
                "Content-Length": str(len(payload)),
                "Transfer-Encoding": "chunked",
                "Connection": "keep-alive",
                "Location": f"https://api.planningcenteronline.com/people/v2/people/{pid}",
                "X-PCO-API-Request-Rate-Count": "3",
            }, payload)

    _NEW_PERSON = json.dumps({"data": {"type": "Person",
                                       "attributes": {"first_name": "Dana",
                                                      "last_name": "Reed"}}}).encode()

    def test_content_length_counts_the_bytes_actually_sent(self):
        fake = self.ChattyPCO()
        m, _ = build(fake)
        status, headers, raw = self._raw_call(m.wsgi, "POST", "/people/v2/people", self._NEW_PERSON)
        self.assertEqual(status, 201)
        self.assertLengthMatchesBody(headers, raw)
        # And the relay really would have been wrong, rather than this passing by
        # luck on a payload that happened to survive rewriting at the same size:
        # the absolute self link became a mirror path, so PCO's count is larger.
        self.assertNotIn("api.planningcenteronline.com/people/v2", raw.decode())
        self.assertGreater(fake.declared_length, len(raw))

    def test_framing_headers_are_dropped_and_location_is_rewritten(self):
        m, _ = build(self.ChattyPCO())
        _, headers, raw = self._raw_call(m.wsgi, "POST", "/people/v2/people", self._NEW_PERSON)
        lowered = {k.lower() for k in headers}
        self.assertNotIn("transfer-encoding", lowered)
        self.assertNotIn("connection", lowered)
        # A caller holds a mirror key, not a PAT, so a PCO URL is a dead end.
        new_id = json.loads(raw)["data"]["id"]
        self.assertEqual(headers["Location"], f"/people/v2/people/{new_id}")
        # Headers that genuinely describe the exchange still come through.
        self.assertEqual(headers["X-PCO-API-Request-Rate-Count"], "3")

    def test_content_length_is_right_on_a_relayed_failure_too(self):
        class RefusingPCO(FakePCO):
            def _write(self, method, segs, body):
                payload = b'{"errors":[{"code":"422","detail":"Grade must be a number"}]}'
                return Response(422, {"Content-Length": "99999",
                                      "Connection": "close"}, payload)

        m, _ = build(RefusingPCO())
        status, headers, raw = self._raw_call(m.wsgi, "POST", "/people/v2/people", self._NEW_PERSON)
        self.assertEqual(status, 422)
        self.assertLengthMatchesBody(headers, raw)
        self.assertNotIn("connection", {k.lower() for k in headers})


class TestMirrorFailureAfterAWriteLands(unittest.TestCase):
    """A write PCO accepted must not be reported as a failure the caller can retry.

    Everything after the upstream call runs with the record already created. The
    blanket handler turned any exception there into a bare 500 — the same
    "please send it again" a lost response used to produce, and repeatable,
    because the same payload raises the same way every time.
    """

    def _post(self, m):
        body = json.dumps({"data": {"type": "Person",
                                    "attributes": {"first_name": "Dana", "last_name": "Reed"}}}).encode()
        return wsgi_call(m.wsgi, "POST", "/people/v2/people", body=body)

    def test_a_broken_mirror_write_does_not_fail_the_request(self):
        m, fake = build()
        boom = RuntimeError("projection blew up")

        def explode(*a, **k):
            raise boom

        m.writer.route_page = explode
        noted = []
        m.wsgi.log_mirror_failure = lambda *a: noted.append(a)

        status, _, out = self._post(m)
        # PCO created the person, so the caller is told so — exactly once.
        self.assertEqual(status, 201)
        self.assertEqual(sum(1 for method, _ in fake.request_log if method == "POST"), 1)
        self.assertEqual(len(fake.data.get("Person", {})), 1)
        self.assertEqual(out["data"]["attributes"]["first_name"], "Dana")
        # And it is not silent: somebody has to be able to find out.
        self.assertEqual(len(noted), 1)
        self.assertIs(noted[0][2], boom)

    def test_a_broken_tombstone_does_not_fail_a_delete(self):
        m, fake = build()
        fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        m.writer.tombstone = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no"))
        m.wsgi.log_mirror_failure = lambda *a: None
        status, _, _ = wsgi_call(m.wsgi, "DELETE", "/people/v2/people/1")
        self.assertIn(status, (200, 204))
        self.assertEqual(sum(1 for method, _ in fake.request_log if method == "DELETE"), 1)


class TestAWriteRefreshesWhatItChanged(unittest.TestCase):
    """Read-your-writes has to cover the records a write *affected*.

    `route_page` applies the resource PCO returned. For a nested write that is
    not the only record that moved, and the two that did not move are exactly
    the ones a caller reads back: adding a parent to an existing household left
    the app that added them reading the family and finding only the child.
    """

    def _family(self, echo_self_link=True):
        fake = FakePCO()
        fake.echo_self_link = echo_self_link
        fake.add_person("100", "Kid", "Reed", "2026-01-01T00:00:00Z",
                        households={"data": [{"type": "Household", "id": "900"}]})
        fake.add(res("Household", "900", {"name": "Reed Household"},
                     relationships={"people": {"data": [{"type": "Person", "id": "100"}]}},
                     updated="2026-01-01T00:00:00Z"))
        fake.add_membership("500", "900", "100", role="child_or_dependent")
        m, _ = build(fake)
        m.ingestor.backfill("person")
        m.ingestor.backfill("household")
        # The screen that offers "add a parent" has just read the family, which
        # is what marks the household walked — the ordinary case, not a corner.
        wsgi_get(m.wsgi, "/people/v2/households/900/household_memberships")
        return m, fake

    def _add_parent(self, m):
        _, _, created = wsgi_call(m.wsgi, "POST", "/people/v2/people", body=json.dumps(
            {"data": {"type": "Person",
                      "attributes": {"first_name": "Dana", "last_name": "Reed",
                                     "child": False}}}).encode())
        parent_id = created["data"]["id"]
        status, _, _ = wsgi_call(
            m.wsgi, "POST", "/people/v2/households/900/household_memberships",
            body=json.dumps({"data": {"type": "HouseholdMembership", "attributes": {
                "person_id": parent_id, "pending": False,
                "household_role": "parent_guardian"}}}).encode())
        self.assertEqual(status, 201)
        return parent_id

    def _members(self, m):
        _, _, page = wsgi_get(m.wsgi, "/people/v2/households/900/household_memberships",
                              "include=person")
        return [d["relationships"]["person"]["data"]["id"] for d in page.get("data", [])]

    def test_the_new_member_is_visible_on_the_very_next_read(self):
        m, _ = self._family()
        parent_id = self._add_parent(m)
        self.assertIn(parent_id, self._members(m))

    def test_it_does_not_depend_on_the_create_response_echoing_the_link(self):
        """A membership's household is only ever in `links.self`, and whether a
        *create* reply repeats it is not something a mirror may rely on."""
        m, _ = self._family(echo_self_link=False)
        parent_id = self._add_parent(m)
        self.assertIn(parent_id, self._members(m))

    def test_the_household_payload_catches_up_from_the_queue(self):
        """`include=households.people` reads the household's own array, which only
        a household fetch rewrites — off the critical path, so it is queued."""
        m, _ = self._family()
        parent_id = self._add_parent(m)
        m.ingestor.drain_hydration()
        _, _, doc = wsgi_get(m.wsgi, "/people/v2/people/100", "include=households.people")
        self.assertIn(("Person", parent_id),
                      {(i["type"], i["id"]) for i in doc.get("included", [])})

    def test_a_failed_re_read_leaves_the_rows_that_are_there(self):
        """Stale beats absent, and it must never turn a write into a failure."""
        m, fake = self._family()
        before = self._members(m)
        original = m.ingestor.walk_parent
        m.ingestor.walk_parent = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PCO down"))
        try:
            parent_id = self._add_parent(m)
        finally:
            m.ingestor.walk_parent = original
        self.assertTrue(set(before).issubset(self._members(m)))
        noted = [r for r in diagnostics.recent(m.db, limit=50)
                 if r["kind"] == diagnostics.WRITE_MIRROR_FAILED]
        self.assertEqual(len(noted), 1)
        self.assertIn("may be stale", noted[0]["detail"])
        self.assertEqual(noted[0]["pco_id"], "900")
        self.assertIsNotNone(parent_id)

    def test_an_ordinary_write_does_not_trigger_a_walk(self):
        """One extra request on a nested write; none on every other write."""
        m, fake = self._family()
        before = sum(1 for method, path in fake.request_log
                     if method == "GET" and "household_memberships" in path)
        wsgi_call(m.wsgi, "PATCH", "/people/v2/people/100", body=json.dumps(
            {"data": {"type": "Person", "id": "100",
                      "attributes": {"last_name": "Byron"}}}).encode())
        after = sum(1 for method, path in fake.request_log
                    if method == "GET" and "household_memberships" in path)
        self.assertEqual(before, after)

if __name__ == "__main__":
    unittest.main()
