"""Every call to the receiver, kept exactly as it arrived.

The failure behind this feature: Planning Center's console said a delivery went
out, the mirror had no record of one, and there was nothing in between to look
at. `webhook_delivery` holds the deliveries that were *accepted* — the ones that
were refused, and the headers that decided it, were read, compared and dropped.

So the tests here are the questions that could not be answered that day: did the
request arrive, what did it carry, what was it signed with, and what did we say
back. The other half of the suite is what a verbatim recording has to keep being:
byte-exact, unredacted, bounded on disk, and incapable of failing a delivery.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import re
import unittest
import urllib.parse

from base import build, wsgi_call, wsgi_get
from pcomirror import admin, adminauth, cli, webhooklog, webhooks
from pcomirror.config import Settings

SECRET = "sec"                      # base.build() sets pco_secret="sec"
GOOD_PASSWORD = "a-long-enough-password"
WHSEC = "whsec_test"
TOKEN = "person-events-01"


def _sign(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _delivery(payload, event_name="people.v2.events.person.updated",
              delivery_id="d1", event_id="ev1"):
    return json.dumps({"id": delivery_id, "attempt": 1,
                       "data": [{"id": event_id, "type": "Event",
                                 "attributes": {"name": event_name,
                                                "payload": json.dumps(payload)}}]}).encode()


def _form(**fields) -> bytes:
    return urllib.parse.urlencode(fields).encode()


class RecordingCase(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build(allow_anonymous=False)
        adminauth._clear_failures()
        webhooks.upsert_subscription(
            self.m.db, "sub1", "people.v2.events.person.updated", WHSEC, TOKEN)

    # -- posting to the receiver ------------------------------------------
    def post(self, raw: bytes, *, token: str = TOKEN, secret: str | None = WHSEC,
             signature: str | None = None, headers: dict | None = None):
        head = dict(headers or {})
        if signature is not None:
            head["X-PCO-Webhooks-Authenticity"] = signature
        elif secret is not None:
            head["X-PCO-Webhooks-Authenticity"] = _sign(secret, raw)
        return wsgi_call(self.m.wsgi, "POST", f"/pco/webhooks/{token}",
                         body=raw, headers=head)

    def calls(self, **kw):
        return webhooklog.recent(self.m.db, **kw)

    def only(self):
        rows = self.calls()
        self.assertEqual(len(rows), 1, f"expected one recording, got {len(rows)}")
        return rows[0]


class TestWhatArrivedIsWhatIsKept(RecordingCase):
    """Byte-exact, header for header. A signature is over the bytes that were
    sent, so a recording that reformats them cannot be checked against it."""

    def test_an_accepted_delivery_is_kept_byte_for_byte(self):
        raw = _delivery({"id": "1", "type": "Person",
                         "attributes": {"first_name": "Ada", "updated_at": "2026-01-01T00:00:00Z"}})
        status, _, _ = self.post(raw)
        self.assertEqual(status, 204)
        row = self.only()
        self.assertEqual(webhooklog.body_of(row), raw)
        self.assertEqual((row["method"], row["status"], row["note"]), ("POST", 204, "ok"))
        self.assertEqual(row["path"], f"/pco/webhooks/{TOKEN}")
        self.assertEqual(row["url_token"], TOKEN)
        self.assertEqual(row["body_bytes"], len(raw))
        self.assertFalse(row["truncated"])

    def test_the_signature_header_is_kept(self):
        """The one field the accept/reject decision is made of."""
        raw = _delivery({"id": "1"})
        signature = _sign(WHSEC, raw)
        self.post(raw, signature=signature)
        headers = json.loads(self.only()["headers"])
        self.assertEqual(headers["X-Pco-Webhooks-Authenticity"], signature)

    def test_every_header_is_kept_not_an_allowlist(self):
        """A header PCO adds next year is recorded without a release here."""
        self.post(_delivery({"id": "1"}),
                  headers={"X-Forwarded-For": "203.0.113.9", "User-Agent": "PCO/1.0",
                           "X-Something-New": "tomorrow"})
        headers = json.loads(self.only()["headers"])
        self.assertEqual(headers["X-Forwarded-For"], "203.0.113.9")
        self.assertEqual(headers["User-Agent"], "PCO/1.0")
        self.assertEqual(headers["X-Something-New"], "tomorrow")

    def test_nothing_personal_is_stripped(self):
        """The opposite policy to `diagnostics`, deliberately: a delivery that
        cannot be compared with what Planning Center signed is not evidence."""
        raw = _delivery({"id": "1", "type": "Person",
                         "attributes": {"first_name": "Nathaniel",
                                        "phone": "+1-555-0142"}})
        self.post(raw)
        stored = webhooklog.body_of(self.only())
        self.assertIn(b"Nathaniel", stored)
        self.assertIn(b"+1-555-0142", stored)
        self.assertEqual(stored, raw)

    def test_the_delivery_is_labelled_from_its_own_body(self):
        raw = _delivery({"id": "7"}, delivery_id="dlv-9", event_id="ev-9")
        self.post(raw)
        row = self.only()
        self.assertEqual(row["delivery_id"], "dlv-9")
        self.assertEqual(row["event_name"], "people.v2.events.person.updated")
        self.assertEqual(row["event_count"], 1)

    def test_query_string_and_caller_are_kept(self):
        raw = _delivery({"id": "1"})
        status, _, _ = wsgi_call(self.m.wsgi, "POST", f"/pco/webhooks/{TOKEN}",
                                 query="retry=3", body=raw,
                                 headers={"X-PCO-Webhooks-Authenticity": _sign(WHSEC, raw)})
        self.assertEqual(status, 204)
        self.assertEqual(self.only()["query"], "retry=3")


class TestTheOnesThatWereRefused(RecordingCase):
    """Where the diagnostic value is. `webhook_delivery` has the accepted ones."""

    def test_a_bad_signature_is_recorded_with_what_it_sent(self):
        raw = _delivery({"id": "1"})
        status, _, _ = self.post(raw, signature="deadbeef")
        self.assertEqual(status, 401)
        row = self.only()
        self.assertEqual((row["status"], row["note"]), (401, "bad signature"))
        self.assertEqual(webhooklog.body_of(row), raw)
        self.assertEqual(json.loads(row["headers"])["X-Pco-Webhooks-Authenticity"],
                         "deadbeef")
        # and nothing was captured, which is the point of recording it here
        self.assertEqual(self.m.db.query_one(
            "SELECT count(*) c FROM webhook_delivery")["c"], 0)

    def test_an_unknown_token_is_recorded(self):
        """PCO delivering to a subscription somebody removed, or a scanner
        working through guesses — the request is what tells them apart."""
        status, _, _ = self.post(_delivery({"id": "1"}), token="who-is-this")
        self.assertEqual(status, 404)
        row = self.only()
        self.assertEqual((row["status"], row["url_token"]), (404, "who-is-this"))

    def test_an_unparseable_body_is_recorded(self):
        raw = b"{not json at all"
        status, _, _ = self.post(raw)
        self.assertEqual(status, 503)
        row = self.only()
        self.assertEqual((row["status"], row["note"]), (503, "unparseable"))
        self.assertEqual(webhooklog.body_of(row), raw)
        self.assertIsNone(row["delivery_id"])       # nothing to label it with

    def test_a_missing_signature_header_is_recorded_as_missing(self):
        status, _, _ = self.post(_delivery({"id": "1"}), secret=None)
        self.assertEqual(status, 401)
        self.assertNotIn("X-Pco-Webhooks-Authenticity", json.loads(self.only()["headers"]))

    def test_a_receiver_that_raises_still_leaves_the_request(self):
        """The delivery nobody can otherwise reconstruct: the one that crashed
        something. Recorded with no status, because none was ever given."""
        def boom(*a, **k):
            raise RuntimeError("the inbox is on fire")
        self.m.wsgi.webhooks.receive = boom
        status, _, _ = self.post(_delivery({"id": "1"}))
        self.assertEqual(status, 500)
        row = self.only()
        self.assertIsNone(row["status"])
        self.assertIn("the inbox is on fire", row["note"])

    def test_rejected_and_accepted_can_be_read_apart(self):
        self.post(_delivery({"id": "1"}))                        # 204
        self.post(_delivery({"id": "2"}), signature="nope")      # 401
        self.post(_delivery({"id": "3"}), token="unknown")       # 404
        self.assertEqual(len(self.calls(outcome="accepted")), 1)
        self.assertEqual(len(self.calls(outcome="rejected")), 2)
        s = webhooklog.summary(self.m.db)
        self.assertEqual((s["total"], s["accepted"], s["rejected"]), (3, 1, 2))
        self.assertEqual(s["by_status"], {204: 1, 401: 1, 404: 1})


class TestBounds(RecordingCase):
    """Bounded on disk and in count — neither of which is a redaction."""

    def test_a_body_over_the_cap_is_clipped_and_says_so(self):
        raw = b'{"id":"big","data":[],"filler":"' + b"x" * (webhooklog.MAX_BODY + 5_000) + b'"}'
        status, _, _ = self.post(raw)
        self.assertEqual(status, 204)
        row = self.only()
        self.assertTrue(row["truncated"])
        self.assertEqual(row["body_bytes"], len(raw))            # the true length
        self.assertEqual(len(webhooklog.body_of(row)), webhooklog.MAX_BODY)
        self.assertEqual(webhooklog.body_of(row), raw[:webhooklog.MAX_BODY])
        # unlabelled rather than parsed at whatever size the caller chose
        self.assertIsNone(row["delivery_id"])

    def test_the_ring_buffer_keeps_the_newest(self):
        webhooklog.configure(self.m.db, 3)
        for i in range(6):
            self.post(_delivery({"id": str(i)}, delivery_id=f"d{i}", event_id=f"e{i}"))
        rows = self.calls()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["delivery_id"] for r in rows], ["d5", "d4", "d3"])

    def test_zero_records_nothing(self):
        webhooklog.configure(self.m.db, 0)
        self.assertEqual(self.post(_delivery({"id": "1"}))[0], 204)
        self.assertEqual(self.calls(), [])

    def test_turning_it_on_takes_effect_on_the_next_delivery(self):
        """No restart: the delivery being waited for is the next one."""
        webhooklog.configure(self.m.db, 0)
        self.post(_delivery({"id": "1"}))
        webhooklog.configure(self.m.db, 50)
        self.post(_delivery({"id": "2"}, delivery_id="d2", event_id="e2"))
        self.assertEqual([r["delivery_id"] for r in self.calls()], ["d2"])

    def test_the_page_overrides_the_environment_and_hands_it_back(self):
        self.m.settings.webhook_record_keep = 500
        self.assertEqual(webhooklog.effective(self.m.db, self.m.settings),
                         {"keep": 500, "source": "environment", "default": 500})
        webhooklog.configure(self.m.db, 10)
        self.assertEqual(webhooklog.effective(self.m.db, self.m.settings),
                         {"keep": 10, "source": "admin", "default": 500})
        webhooklog.configure(self.m.db, None)
        self.assertEqual(webhooklog.effective(self.m.db, self.m.settings)["source"],
                         "environment")

    def test_an_unreadable_override_falls_back_to_the_environment(self):
        self.m.db.set_meta(webhooklog.OVERRIDE_KEY, "banana")
        self.assertEqual(webhooklog.effective(self.m.db, self.m.settings)["source"],
                         "environment")

    def test_settings_read_the_environment(self):
        self.assertEqual(Settings.from_env({}).webhook_record_keep, 500)
        self.assertEqual(
            Settings.from_env({"PCOMIRROR_WEBHOOK_RECORD_KEEP": "20"}).webhook_record_keep, 20)


class TestItCannotBreakADelivery(RecordingCase):
    """PCO's answer to a 5xx is to send it again. An observation that can fail a
    delivery is a strictly worse version of the problem it was added to explain."""

    def test_a_recorder_that_cannot_write_still_lets_the_delivery_through(self):
        def boom(*a, **k):
            raise RuntimeError("disk full")
        self.m.wsgi.webhook_calls._insert = boom
        status, _, _ = self.post(_delivery({"id": "1"}))
        self.assertEqual(status, 204)
        self.assertEqual(self.m.db.query_one("SELECT count(*) c FROM webhook_event")["c"], 1)
        self.assertIn("disk full", self.m.wsgi.webhook_calls.last_failure)

    def test_a_short_log_says_it_is_short(self):
        self.m.wsgi.webhook_calls.last_failure = "OperationalError: disk full"
        page = AdminSession(self).get("/admin/webhooks/calls")[2]
        self.assertIn(b"log is incomplete", page)


class TestExport(RecordingCase):
    """What a support bundle has to be: complete, honest about what it holds, and
    able to carry a body that is not text."""

    def test_the_document_carries_the_whole_call(self):
        raw = _delivery({"id": "1", "attributes": {"first_name": "Ada"}})
        self.post(raw)
        doc = json.loads(webhooklog.export(self.m.db))
        self.assertEqual(len(doc["calls"]), 1)
        call = doc["calls"][0]
        self.assertEqual(call["body"], raw.decode())
        self.assertEqual(call["headers"]["X-Pco-Webhooks-Authenticity"], _sign(WHSEC, raw))
        self.assertEqual(call["status"], 204)
        self.assertIn("unredacted", doc["note"])

    def test_a_body_that_is_not_utf8_survives_whole(self):
        raw = b"\xff\xfe not json, not text"
        self.post(raw)
        call = json.loads(webhooklog.export(self.m.db))["calls"][0]
        self.assertNotIn("body", call)                  # not mangled into text
        self.assertEqual(base64.b64decode(call["body_base64"]), raw)

    def test_the_export_is_oldest_first_and_holds_everything(self):
        for i in range(5):
            self.post(_delivery({"id": str(i)}, delivery_id=f"d{i}", event_id=f"e{i}"))
        calls = json.loads(webhooklog.export(self.m.db))["calls"]
        self.assertEqual([c["delivery_id"] for c in calls], ["d0", "d1", "d2", "d3", "d4"])

    def test_one_outcome_can_be_exported_on_its_own(self):
        self.post(_delivery({"id": "1"}))
        self.post(_delivery({"id": "2"}), signature="nope")
        calls = json.loads(webhooklog.export(self.m.db, "rejected"))["calls"]
        self.assertEqual([c["status"] for c in calls], [401])

    def test_clear_empties_it(self):
        self.post(_delivery({"id": "1"}))
        self.assertEqual(webhooklog.clear(self.m.db), 1)
        self.assertEqual(self.calls(), [])


class AdminSession:
    """A signed-in operator, for the page tests."""

    def __init__(self, case):
        self.m = case.m
        _, headers, _ = wsgi_call(self.m.wsgi, "POST", "/admin/login",
                                  body=_form(password=SECRET))
        cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        page = self.get("/admin/password", cookie=cookie)[2]
        csrf = re.search(rb'name=csrf value="([^"]+)"', page).group(1).decode()
        _, headers, _ = self.post("/admin/password", _form(
            csrf=csrf, password=GOOD_PASSWORD, confirm=GOOD_PASSWORD), cookie=cookie)
        self.cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def get(self, path, query="", cookie=None):
        return wsgi_get(self.m.wsgi, path, query,
                        headers={"Cookie": f"{adminauth.COOKIE}={cookie or self.cookie}"})

    def post(self, path, body=b"", cookie=None):
        return wsgi_call(self.m.wsgi, "POST", path, body=body,
                         headers={"Cookie": f"{adminauth.COOKIE}={cookie or self.cookie}"})

    def csrf(self, path="/admin/webhooks/calls"):
        return re.search(rb'name=csrf value="([^"]+)"',
                         self.get(path)[2]).group(1).decode()


class TestThePage(RecordingCase):
    def setUp(self):
        super().setUp()
        self.admin = AdminSession(self)

    def test_it_needs_a_session(self):
        status, headers, _ = wsgi_get(self.m.wsgi, "/admin/webhooks/calls")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        # and the download is not a way around that
        status, _, _ = wsgi_get(self.m.wsgi, "/admin/webhooks/calls/download")
        self.assertEqual(status, 303)

    def test_the_console_claims_every_recordings_path(self):
        """An `/admin` path the console does not claim does not 404 — it falls
        through to the API plane and is proxied to Planning Center. A missing
        entry in `PATHS` is therefore a request sent upstream, not a dead link."""
        for path in ("/admin/webhooks/calls", "/admin/webhooks/calls/download",
                     "/admin/webhooks/calls/clear", "/admin/webhooks/calls/configure"):
            self.assertTrue(admin.handles(path), path)

    def test_it_shows_a_recorded_call(self):
        raw = _delivery({"id": "1", "attributes": {"first_name": "Ada"}})
        self.post(raw, signature="deadbeef")
        page = self.admin.get("/admin/webhooks/calls")[2]
        self.assertIn(b"bad signature", page)
        self.assertIn(b"deadbeef", page)                       # the header, verbatim
        self.assertIn(b"Ada", page)                            # the body, verbatim
        self.assertIn(b"/admin/webhooks/calls/download?id=", page)

    def test_an_empty_log_says_so(self):
        self.assertIn(b"Nothing recorded yet", self.admin.get("/admin/webhooks/calls")[2])

    def test_the_download_is_the_whole_log(self):
        self.post(_delivery({"id": "1"}))
        status, headers, body = self.admin.get("/admin/webhooks/calls/download")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn("pcomirror-webhook-calls.json", headers["Content-Disposition"])
        self.assertEqual(len(body["calls"]), 1)

    def test_one_call_downloads_as_json(self):
        raw = _delivery({"id": "1", "attributes": {"first_name": "Ada"}})
        self.post(raw)
        call_id = self.only()["call_id"]
        status, headers, doc = self.admin.get("/admin/webhooks/calls/download",
                                              f"id={call_id}")
        self.assertEqual(status, 200)
        self.assertIn(f"call-{call_id}.json", headers["Content-Disposition"])
        self.assertEqual(doc["calls"][0]["call_id"], call_id)
        self.assertEqual(doc["calls"][0]["body"], raw.decode())

    def test_one_call_downloads_as_its_exact_bytes(self):
        """What re-hashing a refused delivery needs: the bytes, not a rendering
        of them. Sent here as something no JSON encoder could have produced."""
        raw = b"\xff\xfe{not json} \x00\x01"
        self.post(raw)
        call_id = self.only()["call_id"]
        status, headers, body = self.admin.get("/admin/webhooks/calls/download",
                                               f"id={call_id}&raw=1")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn(f"call-{call_id}.bin", headers["Content-Disposition"])
        self.assertEqual(headers["Content-Length"], str(len(raw)))
        self.assertEqual(body, raw)                            # byte for byte

    def test_a_download_of_a_call_that_is_gone_is_not_an_error_page(self):
        status, headers, _ = self.admin.get("/admin/webhooks/calls/download", "id=9999")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin/webhooks/calls")

    def test_clearing_needs_the_csrf_token(self):
        self.post(_delivery({"id": "1"}))
        self.admin.post("/admin/webhooks/calls/clear", _form(csrf="wrong"))
        self.assertEqual(len(self.calls()), 1)
        self.admin.post("/admin/webhooks/calls/clear", _form(csrf=self.admin.csrf()))
        self.assertEqual(self.calls(), [])

    def test_the_keep_setting_is_saved_from_the_page(self):
        self.admin.post("/admin/webhooks/calls/configure",
                        _form(csrf=self.admin.csrf(), keep="42"))
        self.assertEqual(webhooklog.effective(self.m.db, self.m.settings),
                         {"keep": 42, "source": "admin", "default": 500})
        page = self.admin.get("/admin/webhooks/calls")[2]
        self.assertIn(b"keeping the last 42 calls", page)
        self.admin.post("/admin/webhooks/calls/configure",
                        _form(csrf=self.admin.csrf(), reset="1"))
        self.assertEqual(webhooklog.effective(self.m.db, self.m.settings)["source"],
                         "environment")

    def test_a_keep_that_is_not_a_number_is_refused_with_a_sentence(self):
        status, _, page = self.admin.post("/admin/webhooks/calls/configure",
                                          _form(csrf=self.admin.csrf(), keep="lots"))
        self.assertEqual(status, 200)
        self.assertIn(b"whole number", page)

    def test_turning_it_off_from_the_page(self):
        self.admin.post("/admin/webhooks/calls/configure",
                        _form(csrf=self.admin.csrf(), keep="0"))
        self.post(_delivery({"id": "1"}))
        self.assertEqual(self.calls(), [])
        self.assertIn(b"nothing is being recorded",
                      self.admin.get("/admin/webhooks/calls")[2])

    def test_the_page_says_what_the_download_holds(self):
        """A file that outlives the page that offered it needs the warning in it."""
        page = self.admin.get("/admin/webhooks/calls")[2]
        self.assertIn(b"like the database", page)
        self.post(_delivery({"id": "1"}))
        doc = self.admin.get("/admin/webhooks/calls/download")[2]
        self.assertIn("Verbatim", doc["note"])

    def test_the_webhooks_page_and_dashboard_link_to_it(self):
        self.post(_delivery({"id": "1"}))
        self.assertIn(b"/admin/webhooks/calls", self.admin.get("/admin/webhooks")[2])
        self.assertIn(b"/admin/webhooks/calls", self.admin.get("/")[2])

    def test_a_large_body_is_previewed_rather_than_poured_onto_the_page(self):
        """A hundred maximal bodies would be twenty-five megabytes read to render
        a preview of each, so the page reads a preview's worth and says so."""
        raw = b'{"id":"big","data":[],"filler":"' + b"y" * 50_000 + b'"}'
        self.post(raw)
        page = self.admin.get("/admin/webhooks/calls")[2]
        self.assertIn(b"clipped for display", page)
        self.assertIn(str(len(raw)).encode()[:2], page)        # the true size is shown
        self.assertLess(len(page), 25_000)                     # not the whole body
        # and the download still has all of it
        call_id = self.only()["call_id"]
        _, headers, _ = self.admin.get("/admin/webhooks/calls/download", f"id={call_id}&raw=1")
        self.assertEqual(headers["Content-Length"], str(len(raw)))

    def test_a_body_of_bytes_does_not_break_the_page(self):
        """Rendered, not trusted: a recording is bytes somebody else chose."""
        self.post(b"\xff\xfe<script>alert(1)</script>")
        page = self.admin.get("/admin/webhooks/calls")[2]
        self.assertNotIn(b"<script>alert(1)</script>", page)
        self.assertIn(b"&lt;script&gt;", page)


class TestTheServeLogSaysSo(RecordingCase):
    """Kept verbatim and on by default is not something to find out from the
    schema, so `serve` says it at every start — the treatment CORS and the
    secretless receivers get, for the same reason."""

    def _log(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._report_webhook_recording(self.m)
        return out.getvalue()

    def test_it_says_what_is_being_kept_and_where_to_read_it(self):
        said = self._log()
        self.assertIn("500", said)
        self.assertIn("nothing redacted", said)
        self.assertIn("/admin/webhooks/calls", said)

    def test_it_says_where_the_number_came_from(self):
        webhooklog.configure(self.m.db, 25)
        self.assertIn("set on /admin/webhooks/calls", self._log())

    def test_it_is_silent_when_recording_is_off(self):
        webhooklog.configure(self.m.db, 0)
        self.assertEqual(self._log(), "")


class TestHeadersOf(unittest.TestCase):
    def test_wsgi_names_come_back_as_headers(self):
        got = webhooklog.headers_of({
            "HTTP_X_PCO_WEBHOOKS_AUTHENTICITY": "abc", "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": "12", "REQUEST_METHOD": "POST", "wsgi.input": io.BytesIO()})
        self.assertEqual(got, {"Content-Length": "12", "Content-Type": "application/json",
                               "X-Pco-Webhooks-Authenticity": "abc"})

    def test_nothing_that_is_not_a_header_gets_in(self):
        got = webhooklog.headers_of({"PATH_INFO": "/pco/webhooks/x", "SERVER_NAME": "h"})
        self.assertEqual(got, {})


if __name__ == "__main__":
    unittest.main()
