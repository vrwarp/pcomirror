"""The record of what was asked of Planning Center and what came back.

The failure that motivated this log: a write reached PCO, PCO applied it, the
response never came back, and afterwards there was no way to find out why. Every
test here is a question somebody had to ask that day and could not answer.
"""
from __future__ import annotations

import json
import unittest

from base import build, wsgi_call, wsgi_get
from fakepco import FakePCO
from pcomirror import diagnostics
from pcomirror.pcoclient import Response

NEW_PERSON = json.dumps({"data": {"type": "Person",
                                  "attributes": {"first_name": "Dana",
                                                 "last_name": "Reed"}}}).encode()


def events(m, kind=None):
    rows = diagnostics.recent(m.db, limit=100)
    return [r for r in rows if kind is None or r["kind"] == kind]


class TestNothingPersonalIsKept(unittest.TestCase):
    """A diagnostic log is the thing that gets pasted into an issue.

    A mirror of a church's people database has somebody's child's name in almost
    every request, so what survives into the log is deliberately thin: which
    filter was used, never what was searched for.
    """

    def test_query_values_are_replaced_and_keys_survive(self):
        target = diagnostics.redact_target("/people?where[search_name]=Nathaniel&per_page=100")
        self.assertNotIn("Nathaniel", target)
        self.assertIn("where[search_name]", target)   # which filter is the diagnostic fact
        self.assertIn("per_page", target)
        self.assertTrue(target.startswith("/people?"))

    def test_params_passed_separately_are_redacted_too(self):
        target = diagnostics.redact_target(
            "/people", {"where[search_name]": "Nathaniel", "offset": 100})
        self.assertNotIn("Nathaniel", target)
        self.assertIn("where[search_name]", target)

    def test_a_path_with_no_query_is_left_alone(self):
        self.assertEqual(diagnostics.redact_target("/people/123"), "/people/123")

    def test_an_error_message_cannot_smuggle_a_query_string_back_in(self):
        """Socket errors quote the URL they failed on, query string included."""
        err = OSError("timed out reading "
                      "https://api.planningcenteronline.com/people/v2/people?where[search_name]=Ada")
        _, message = diagnostics.describe_error(err)
        self.assertNotIn("Ada", message)
        self.assertIn("api.planningcenteronline.com", message)

    def test_only_chosen_headers_are_kept(self):
        kept = diagnostics.pick_headers({
            "X-Request-Id": "abc", "Authorization": "Basic hunter2",
            "Set-Cookie": "session=secret", "X-PCO-API-Request-Rate-Count": "3"})
        self.assertEqual(kept["x-request-id"], "abc")
        self.assertEqual(kept["x-pco-api-request-rate-count"], "3")
        self.assertNotIn("authorization", kept)
        self.assertNotIn("set-cookie", kept)

    def test_no_credential_reaches_the_table_on_a_real_exchange(self):
        m, _ = build()
        wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        dumped = json.dumps([dict(r) for r in events(m)])
        for secret in ("sec", "Basic", "Authorization"):
            self.assertNotIn(secret, dumped)


class TestWritesAreAlwaysRecorded(unittest.TestCase):
    """Every mutation, not only the failures.

    "It succeeded at 07:16:16" is evidence too — on the day this log was built
    for, five successful creates were the whole story.
    """

    def test_a_successful_create_is_recorded_with_its_id(self):
        m, _ = build()
        _, _, out = wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        applied = events(m, diagnostics.WRITE_APPLIED)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["method"], "POST")
        self.assertEqual(applied[0]["target"], "/people")
        self.assertEqual(applied[0]["status"], 201)
        self.assertEqual(applied[0]["pco_id"], out["data"]["id"])
        self.assertEqual(applied[0]["severity"], diagnostics.INFO)

    def test_a_delete_is_recorded_against_the_record_it_removed(self):
        m, fake = build()
        fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        wsgi_call(m.wsgi, "DELETE", "/people/v2/people/1")
        applied = events(m, diagnostics.WRITE_APPLIED)
        self.assertEqual(applied[0]["method"], "DELETE")
        self.assertEqual(applied[0]["pco_id"], "1")

    def test_a_refused_write_says_so_and_is_not_an_error(self):
        """PCO declining is a fact about the request, not a fault in the mirror."""
        m, fake = build()
        fake.fail_next = (422, "invalid")
        wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        refused = events(m, diagnostics.WRITE_REFUSED)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["status"], 422)
        self.assertEqual(refused[0]["severity"], diagnostics.WARNING)
        self.assertIn("declined", refused[0]["detail"])


class TestTheEventThisExistsFor(unittest.TestCase):
    def test_a_lost_write_is_recorded_before_the_error_is_raised(self):
        """The raise is what loses the context, so the row goes in first."""
        m, fake = build()
        fake.unreachable = True
        status, _, _ = wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        self.assertEqual(status, 504)
        lost = events(m, diagnostics.WRITE_LOST)
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["severity"], diagnostics.ERROR)
        self.assertEqual(lost[0]["method"], "POST")
        self.assertIn("may or may not have been applied", lost[0]["detail"])
        # The type of failure is the question the log was built to answer.
        self.assertEqual(lost[0]["error_type"], "ConnectionResetError")

    def test_a_mirror_failure_after_a_successful_write_is_recorded(self):
        m, _ = build()
        m.writer.route_page = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        m.wsgi.log_mirror_failure = lambda *a: None
        status, _, _ = wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        self.assertEqual(status, 201)                       # the write did happen
        failed = events(m, diagnostics.WRITE_MIRROR_FAILED)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["severity"], diagnostics.ERROR)
        self.assertEqual(failed[0]["error_type"], "RuntimeError")
        self.assertIn("applied at Planning Center", failed[0]["detail"])

    def test_the_pco_request_id_is_kept(self):
        """The one field PCO's own support can look up."""

        class WithRequestId(FakePCO):
            def _write(self, method, segs, body):
                resp = super()._write(method, segs, body)
                resp.headers = {"X-Request-Id": "97575aba-0a5f-416d-8bf4-99ce2530b17b"}
                return resp

        m, _ = build(WithRequestId())
        wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        applied = events(m, diagnostics.WRITE_APPLIED)
        self.assertEqual(applied[0]["pco_request_id"], "97575aba-0a5f-416d-8bf4-99ce2530b17b")


class TestUpstreamFailures(unittest.TestCase):
    """A retry that worked still erased the reason the write beside it did not."""

    def test_a_recovered_read_still_leaves_a_trace(self):
        class FlakyThenFine(FakePCO):
            def __init__(self):
                super().__init__()
                self.failures = 2

            def send(self, method, url, headers, body):
                if method == "GET" and self.failures > 0:
                    self.failures -= 1
                    return Response(503, {}, b'{"errors":[{"code":"503"}]}')
                return super().send(method, url, headers, body)

        fake = FlakyThenFine()
        fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        m, _ = build(fake)
        m.ingestor.backfill("person")
        retries = events(m, diagnostics.UPSTREAM_RETRY)
        self.assertGreaterEqual(len(retries), 2)
        self.assertEqual(retries[0]["status"], 503)
        self.assertIn("retrying", retries[0]["detail"])
        self.assertEqual(retries[0]["severity"], diagnostics.WARNING)

    def test_one_failure_is_one_row(self):
        """The layer with the most context writes the line; the other stays quiet.

        Both the client and the write path can see a failed write, and both used
        to record it — two rows for one event, the thinner one on top. A log that
        double-counts is a log an operator has to decode before reading.
        """
        m, fake = build()
        fake.unreachable = True
        wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        rows = diagnostics.recent(m.db, limit=50)
        self.assertEqual([r["kind"] for r in rows], [diagnostics.WRITE_LOST])
        self.assertEqual(rows[0]["error_type"], "ConnectionResetError")

    def test_a_refused_write_is_also_one_row(self):
        m, fake = build()
        fake.fail_next = (422, "invalid")
        wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        self.assertEqual([r["kind"] for r in diagnostics.recent(m.db, limit=50)],
                         [diagnostics.WRITE_REFUSED])

    def test_a_failed_read_is_still_recorded_by_the_client(self):
        """Nothing else logs a read, so the client must not go quiet for those."""
        m, _ = build()
        wsgi_get(m.wsgi, "/people/v2/people/404", "passthrough=on")
        errors = events(m, diagnostics.UPSTREAM_ERROR)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["status"], 404)
        self.assertEqual(errors[0]["attempts"], 1)


class TestTheLogCannotBreakTheMirror(unittest.TestCase):
    """A diagnostics table that could fail a request would be worse than none."""

    def test_a_broken_recorder_does_not_fail_a_write(self):
        m, fake = build()
        m.diagnostics.db = None                     # every insert will now raise
        status, _, _ = wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        self.assertEqual(status, 201)
        self.assertEqual(len(fake.data.get("Person", {})), 1)
        # ...and it admits it, so a short log is never mistaken for a quiet one.
        self.assertIsNotNone(m.diagnostics.last_failure)

    def test_recording_is_capped(self):
        m, _ = build()
        m.diagnostics.keep = 5
        for i in range(12):
            m.diagnostics.record(diagnostics.UPSTREAM_ERROR, diagnostics.ERROR,
                                 method="GET", target=f"/people/{i}")
        rows = diagnostics.recent(m.db, limit=100)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["target"], "/people/11")       # newest kept

    def test_a_long_message_is_clipped_rather_than_stored_whole(self):
        m, _ = build()
        m.diagnostics.record(diagnostics.UPSTREAM_ERROR, diagnostics.ERROR,
                             method="GET", target="/people", detail="x" * 5000)
        stored = diagnostics.recent(m.db, limit=1)[0]["detail"]
        self.assertLessEqual(len(stored), diagnostics.MAX_DETAIL)

    def test_recording_can_be_switched_off_entirely(self):
        m, _ = build(diagnostic_keep=0)
        wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        self.assertEqual(diagnostics.recent(m.db, limit=10), [])


class TestReading(unittest.TestCase):
    def test_filters(self):
        m, fake = build()
        wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)
        fake.unreachable = True
        wsgi_call(m.wsgi, "POST", "/people/v2/people", body=NEW_PERSON)

        self.assertTrue(all(r["kind"].startswith("write.")
                            for r in diagnostics.recent(m.db, kind_prefix="write.")))
        self.assertTrue(all(r["severity"] == "error"
                            for r in diagnostics.recent(m.db, severity="error")))
        s = diagnostics.summary(m.db)
        self.assertEqual(s["writes"], 2)                # one applied, one lost
        self.assertEqual(s["indeterminate"], 1)
        self.assertGreaterEqual(s["errors"], 1)
        self.assertIsNotNone(s["newest"])


if __name__ == "__main__":
    unittest.main()
