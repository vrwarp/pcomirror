"""The operator page: bootstrap login, forced password change, keys, stats."""
from __future__ import annotations

import json
import re
import unittest
import urllib.parse

from base import build, wsgi_call, wsgi_get
from pcomirror import adminauth, apikeys, diagnostics, divergence

SECRET = "sec"                      # base.build() sets pco_secret="sec"
GOOD_PASSWORD = "a-long-enough-password"


def _form(**fields) -> bytes:
    return urllib.parse.urlencode(fields).encode()


class AdminCase(unittest.TestCase):
    def setUp(self):
        self.m, self.fake = build(allow_anonymous=False)
        adminauth._clear_failures()     # module-level throttle is process-wide

    # -- helpers ----------------------------------------------------------
    def get(self, path, query="", cookie=None):
        headers = {"Cookie": f"{adminauth.COOKIE}={cookie}"} if cookie else None
        return wsgi_get(self.m.wsgi, path, query, headers=headers)

    def post(self, path, body=b"", cookie=None):
        headers = {"Cookie": f"{adminauth.COOKIE}={cookie}"} if cookie else None
        return wsgi_call(self.m.wsgi, "POST", path, body=body, headers=headers)

    def _cookie_value(self, headers) -> str:
        return headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def login(self, password=SECRET):
        status, headers, _ = self.post("/admin/login", _form(password=password))
        self.assertEqual(status, 303)
        return self._cookie_value(headers)

    def csrf(self, cookie) -> str:
        _, _, page = self.get("/admin/password", cookie=cookie)
        return re.search(rb'name=csrf value="([^"]+)"', page).group(1).decode()

    def configured_login(self):
        """Complete the bootstrap flow and return a normal session cookie."""
        cookie = self.login(SECRET)
        _, headers, _ = self.post("/admin/password", _form(
            csrf=self.csrf(cookie), password=GOOD_PASSWORD, confirm=GOOD_PASSWORD),
            cookie=cookie)
        return self._cookie_value(headers)


class TestLogin(AdminCase):
    def test_root_serves_the_login_page(self):
        status, headers, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Sign in", page)
        self.assertIn(b"PCO_SECRET", page)          # tells the operator what to use

    def test_login_page_never_prints_the_secret(self):
        self.m.settings.pco_secret = "zz-distinctive-secret-zz"
        _, _, page = self.get("/")
        self.assertIn(b"PCO_SECRET", page)                  # names the variable
        self.assertNotIn(b"zz-distinctive-secret-zz", page)  # never its value

    def test_wrong_password_rejected(self):
        status, _, page = self.post("/admin/login", _form(password="nope"))
        self.assertEqual(status, 200)
        self.assertIn(b"Incorrect password", page)

    def test_bootstrap_login_forces_password_change(self):
        cookie = self.login(SECRET)
        status, headers, _ = self.get("/", cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin/password")

    def test_session_cookie_is_hardened(self):
        _, headers, _ = self.post("/admin/login", _form(password=SECRET))
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_session_token_is_not_stored_verbatim(self):
        cookie = self.login()
        stored = self.m.db.query_one("SELECT token_hash FROM admin_session")["token_hash"]
        self.assertNotIn(cookie.encode(), bytes(stored))

    def test_lockout_after_repeated_failures(self):
        for _ in range(adminauth.MAX_FAILURES):
            self.post("/admin/login", _form(password="nope"))
        _, _, page = self.post("/admin/login", _form(password=SECRET))
        self.assertIn(b"Too many failed attempts", page)

    def test_empty_pco_secret_admits_nobody(self):
        m, _ = build(allow_anonymous=False)
        m.settings.pco_secret = ""
        _, _, page = wsgi_get(m.wsgi, "/")
        self.assertIn(b"no way to sign in", page)
        status, _, _ = wsgi_call(m.wsgi, "POST", "/admin/login", body=_form(password=""))
        self.assertEqual(status, 200)               # not a redirect: no session issued

    def test_logout_clears_the_session(self):
        cookie = self.configured_login()
        status, headers, _ = self.post("/admin/logout", cookie=cookie)
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.get("/", cookie=cookie)[0], 200)   # back to login page


class TestPasswordChange(AdminCase):
    def test_rejects_short_and_mismatched(self):
        cookie = self.login()
        _, _, page = self.post("/admin/password", _form(
            csrf=self.csrf(cookie), password="short", confirm="short"), cookie=cookie)
        self.assertIn(b"at least", page)
        _, _, page = self.post("/admin/password", _form(
            csrf=self.csrf(cookie), password=GOOD_PASSWORD, confirm="different-enough"),
            cookie=cookie)
        self.assertIn(b"do not match", page)

    def test_requires_csrf(self):
        cookie = self.login()
        _, _, page = self.post("/admin/password", _form(
            password=GOOD_PASSWORD, confirm=GOOD_PASSWORD), cookie=cookie)
        self.assertIn(b"Session expired", page)
        self.assertFalse(adminauth.is_configured(self.m.db))

    def test_change_rotates_every_session(self):
        cookie = self.login()
        _, headers, _ = self.post("/admin/password", _form(
            csrf=self.csrf(cookie), password=GOOD_PASSWORD, confirm=GOOD_PASSWORD),
            cookie=cookie)
        new_cookie = self._cookie_value(headers)
        self.assertNotEqual(new_cookie, cookie)
        self.assertEqual(self.get("/", cookie=cookie)[0], 200)      # old one is dead
        self.assertEqual(self.get("/", cookie=new_cookie)[0], 200)  # new one works
        self.assertIn(b"Cache", self.get("/", cookie=new_cookie)[2])

    def test_pco_secret_stops_working_once_configured(self):
        self.configured_login()
        _, _, page = self.post("/admin/login", _form(password=SECRET))
        self.assertIn(b"Incorrect password", page)
        ok, _ = adminauth.verify(self.m.db, self.m.settings, GOOD_PASSWORD)
        self.assertTrue(ok)

    def test_password_is_not_stored_in_the_clear(self):
        self.configured_login()
        row = self.m.db.query_one("SELECT * FROM admin_account WHERE id=1")
        self.assertNotIn(GOOD_PASSWORD.encode(), bytes(row["password_hash"]))
        self.assertGreaterEqual(row["iterations"], 100_000)

    def test_later_change_requires_the_current_password(self):
        cookie = self.configured_login()
        csrf = self.csrf(cookie)
        _, _, page = self.post("/admin/password", _form(
            csrf=csrf, current="wrong", password="another-good-password",
            confirm="another-good-password"), cookie=cookie)
        self.assertIn(b"Current password is incorrect", page)


class TestKeysAndStats(AdminCase):
    def setUp(self):
        super().setUp()
        self.cookie = self.configured_login()
        self.fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")

    def _dash_csrf(self):
        _, _, page = self.get("/", cookie=self.cookie)
        return re.search(rb'name=csrf value="([^"]+)"', page).group(1).decode()

    def test_create_key_shows_it_once_and_it_works(self):
        _, _, page = self.post("/admin/keys/create", _form(
            csrf=self._dash_csrf(), name="dashboard", read="read:*"), cookie=self.cookie)
        key = re.search(rb"(pcm_[0-9a-f]{8}_[0-9a-f]{64})", page).group(1).decode()
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people",
                                   headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"][0]["id"], "1")
        # a later dashboard load must not reveal it again
        _, _, page2 = self.get("/", cookie=self.cookie)
        self.assertNotIn(key.encode(), page2)

    def test_scope_checkboxes_map_to_scopes(self):
        self.post("/admin/keys/create", _form(
            csrf=self._dash_csrf(), name="rw", read="read:*", write="write"),
            cookie=self.cookie)
        row = self.m.db.query_one("SELECT scopes FROM api_key WHERE name='rw'")
        self.assertEqual(set(row["scopes"].split(",")), {"read:*", "write"})

    def test_key_creation_requires_csrf_and_a_name(self):
        _, _, page = self.post("/admin/keys/create", _form(name="x", read="read:*"),
                               cookie=self.cookie)
        self.assertIn(b"Session expired", page)
        _, _, page = self.post("/admin/keys/create", _form(
            csrf=self._dash_csrf(), name="", read="read:*"), cookie=self.cookie)
        self.assertIn(b"needs a name", page)
        self.assertEqual(self.m.db.query_one("SELECT count(*) c FROM api_key")["c"], 0)

    def test_no_scope_selected_is_rejected(self):
        _, _, page = self.post("/admin/keys/create", _form(
            csrf=self._dash_csrf(), name="x"), cookie=self.cookie)
        self.assertIn(b"at least one scope", page)

    def test_revoke_from_the_page(self):
        key = apikeys.create(self.m.db, "old", "read:*")
        prefix = key.split("_")[1]
        _, _, page = self.post("/admin/keys/revoke", _form(
            csrf=self._dash_csrf(), prefix=prefix), cookie=self.cookie)
        self.assertIn(b"Revoked key", page)
        self.assertIsNone(apikeys.authenticate(self.m.db, key))

    def test_stats_render(self):
        _, _, page = self.get("/", cookie=self.cookie)
        for expected in (b"mirrored records", b"on disk", b"hydration queue",
                         b"Webhooks", b"people"):
            self.assertIn(expected, page)

    def test_anonymous_mode_is_called_out(self):
        m, _ = build(allow_anonymous=True)
        adminauth.set_password(m.db, GOOD_PASSWORD)
        _, headers, _ = wsgi_call(m.wsgi, "POST", "/admin/login",
                                  body=_form(password=GOOD_PASSWORD))
        cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        _, _, page = wsgi_get(m.wsgi, "/", headers={"Cookie": f"{adminauth.COOKIE}={cookie}"})
        self.assertIn(b"PCOMIRROR_ALLOW_ANONYMOUS", page)


class TestSyncOnDemand(AdminCase):
    """The page *requests*; the scheduler *runs*. The buttons exist because the
    only other ways to bring a sweep or the id-set audit forward were the CLI
    and waiting — and the person staring at a stale record is on the console,
    not in the container."""

    def setUp(self):
        super().setUp()
        self.cookie = self.configured_login()
        self.fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        self.fake.add_person("2", "Gone", "Person", "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")

    def _dash_csrf(self):
        _, _, page = self.get("/", cookie=self.cookie)
        return re.search(rb'name=csrf value="([^"]+)"', page).group(1).decode()

    def test_the_dashboard_offers_both_buttons(self):
        _, _, page = self.get("/", cookie=self.cookie)
        self.assertIn(b"/admin/sync/sweep", page)
        self.assertIn(b"/admin/sync/audit", page)
        self.assertIn(b"last audit", page)

    def test_sweep_queues_and_the_scheduler_runs_it(self):
        from pcomirror.scheduler import Scheduler
        # As after any sweep: the next one is scheduled well into the future.
        self.m.ingestor._set("person", next_run_at="2027-01-01T00:00:00Z")
        _, _, page = self.post("/admin/sync/sweep", _form(
            csrf=self._dash_csrf(), resource="person"), cookie=self.cookie)
        self.assertIn(b"Queued the reconcile sweep for /people", page)
        st = self.m.ingestor.state("person")
        self.assertLess(st["next_run_at"], "2027-01-01T00:00:00Z",
                        "the request should pull the sweep back to now")
        self.assertIsNone(st["last_sweep_completed_at"])
        Scheduler(self.m).run_once()
        self.assertIsNotNone(self.m.ingestor.state("person")["last_sweep_completed_at"])

    def test_audit_queues_as_an_operator_request_and_buries_a_ghost(self):
        from pcomirror.scheduler import Scheduler
        sched = Scheduler(self.m)
        sched.run_once()
        sched.drain_cold()                     # the cadence audit has just run…
        self.fake.destroy("Person", "2")       # …when the ghost appears
        _, _, page = self.post("/admin/sync/audit", _form(
            csrf=self._dash_csrf(), resource="person"), cookie=self.cookie)
        self.assertIn(b"Queued the id-set audit for /people", page)
        self.assertEqual(self.m.ingestor.audit_requested("person"), "operator")
        sched.drain_cold()                     # no day-long cadence, no cooldown
        self.assertIsNotNone(
            self.m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='2'")["deleted_at"])
        self.assertIsNone(self.m.ingestor.audit_requested("person"),
                          "the completed audit answers the request")

    def test_an_operator_audit_runs_even_where_scheduled_audits_are_off(self):
        """`reconcile --audit` on the CLI has always worked under
        `PCOMIRROR_AUDIT_INTERVAL_HOURS=0`; the button is the same person
        making the same explicit choice."""
        from pcomirror.scheduler import Scheduler
        self.m.settings.audit_interval_hours = 0
        self.fake.destroy("Person", "2")
        self.post("/admin/sync/audit", _form(
            csrf=self._dash_csrf(), resource="person"), cookie=self.cookie)
        Scheduler(self.m).drain_cold()
        self.assertIsNotNone(
            self.m.db.query_one("SELECT deleted_at FROM person WHERE pco_id='2'")["deleted_at"])

    def test_a_pending_request_shows_as_queued_on_the_page(self):
        self.post("/admin/sync/audit", _form(
            csrf=self._dash_csrf(), resource="person"), cookie=self.cookie)
        _, _, page = self.get("/", cookie=self.cookie)
        self.assertIn(b"operator queued", page)

    def test_no_resource_named_means_everything(self):
        self.post("/admin/sync/sweep", _form(csrf=self._dash_csrf()), cookie=self.cookie)
        from pcomirror import registry
        for r in registry.full_and_lite():
            self.assertIsNotNone(self.m.ingestor.state(r.name)["next_run_at"], r.name)
        self.post("/admin/sync/audit", _form(csrf=self._dash_csrf()), cookie=self.cookie)
        for r in registry.full_and_lite():
            if r.audit_interval_s:
                self.assertEqual(self.m.ingestor.audit_requested(r.name), "operator", r.name)

    def test_an_unauditable_resource_is_refused(self):
        _, _, page = self.post("/admin/sync/audit", _form(
            csrf=self._dash_csrf(), resource="note"), cookie=self.cookie)
        self.assertIn(b"No audit for", page)
        self.assertIsNone(self.m.ingestor.audit_requested("note"))

    def test_csrf_is_required(self):
        self.m.ingestor._set("person", next_run_at="2027-01-01T00:00:00Z")
        _, _, page = self.post("/admin/sync/sweep", _form(resource="person"),
                               cookie=self.cookie)
        self.assertIn(b"Session expired", page)
        self.assertEqual(self.m.ingestor.state("person")["next_run_at"],
                         "2027-01-01T00:00:00Z", "a csrf-less post must queue nothing")

    def test_a_get_does_nothing(self):
        status, headers, _ = self.get("/admin/sync/audit", cookie=self.cookie)
        self.assertEqual(status, 303)
        self.assertIsNone(self.m.ingestor.audit_requested("person"))


class TestIsolation(AdminCase):
    def test_admin_pages_need_a_session_not_an_api_key(self):
        key = apikeys.create(self.m.db, "app", "read:*")
        status, headers, _ = wsgi_get(self.m.wsgi, "/admin/keys/create",
                                      headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(status, 303)               # bounced to the login page
        self.assertEqual(headers["Location"], "/")

    def test_admin_session_does_not_unlock_the_json_api(self):
        cookie = self.configured_login()
        status, _, _ = self.get("/people/v2/people", cookie=cookie)
        self.assertEqual(status, 401)

    def test_security_headers(self):
        _, headers, _ = self.get("/")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_health_endpoints_unaffected(self):
        self.assertEqual(self.get("/healthz")[0], 200)


class TestDiagnosticsPage(AdminCase):
    """The log, and the two things it must never do: leak, or open.

    Built after an incident whose cause could only have been read off a stderr
    line nobody captured, in a container that had since been replaced.
    """

    def _api(self, scopes="read:*,write,passthrough") -> dict:
        """A machine credential. This suite runs with `allow_anonymous` off, so a
        request without one 401s before it ever reaches the code being recorded."""
        return {"Authorization": f"Bearer {apikeys.create(self.m.db, 'app', scopes)}"}

    def _write_something(self):
        body = json.dumps({"data": {"type": "Person",
                                    "attributes": {"first_name": "Dana",
                                                   "last_name": "Reed"}}}).encode()
        return wsgi_call(self.m.wsgi, "POST", "/people/v2/people",
                         body=body, headers=self._api())

    def test_the_log_needs_a_session(self):
        status, headers, _ = self.get("/admin/diagnostics")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")

    def test_a_write_shows_up_on_the_page(self):
        self._write_something()
        cookie = self.configured_login()
        status, _, page = self.get("/admin/diagnostics", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"write.applied", page)
        self.assertIn(b"/people", page)

    def test_a_lost_write_is_called_out_on_the_dashboard(self):
        self.fake.unreachable = True
        self._write_something()
        cookie = self.configured_login()
        _, _, page = self.get("/", cookie=cookie)
        self.assertIn(b"Diagnostics", page)
        self.assertIn(b"indeterminate", page)
        # The operator is told what to do about it, not just given a number.
        self.assertIn(b"checking upstream by hand", page)

    def test_an_empty_log_explains_itself(self):
        cookie = self.configured_login()
        _, _, page = self.get("/", cookie=cookie)
        self.assertIn(b"Nothing recorded yet", page)

    def test_filters_narrow_the_list(self):
        self._write_something()
        cookie = self.configured_login()
        _, _, only_upstream = self.get("/admin/diagnostics", "kind=upstream.", cookie=cookie)
        self.assertNotIn(b"write.applied", only_upstream)
        _, _, only_writes = self.get("/admin/diagnostics", "kind=write.", cookie=cookie)
        self.assertIn(b"write.applied", only_writes)

    def test_a_bogus_filter_is_ignored_rather_than_reflected(self):
        cookie = self.configured_login()
        status, _, page = self.get(
            "/admin/diagnostics", urllib.parse.urlencode(
                {"kind": "<script>x</script>", "severity": "nope", "limit": "abc"}),
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn(b"<script>x", page)

    def test_a_searched_for_name_never_reaches_the_page(self):
        """The log records which filter ran, never what somebody typed into it."""
        # A pass-through miss: PCO answers 404, which is recorded — and does not
        # go round the retry ladder, so this test costs nothing in wall clock.
        wsgi_get(self.m.wsgi, "/people/v2/people/404",
                 "where[search_name]=Nathaniel&passthrough=on", headers=self._api())
        recorded = diagnostics.recent(self.m.db, limit=10)
        self.assertTrue(any("where[search_name]" in (r["target"] or "") for r in recorded),
                        "the filter should be recorded even though the name is not")
        cookie = self.configured_login()
        _, _, page = self.get("/admin/diagnostics", cookie=cookie)
        self.assertNotIn(b"Nathaniel", page)

    def test_no_credential_is_rendered(self):
        self._write_something()
        cookie = self.configured_login()
        _, _, page = self.get("/admin/diagnostics", cookie=cookie)
        for secret in (b"Basic ", b"Authorization", GOOD_PASSWORD.encode()):
            self.assertNotIn(secret, page)

    def test_the_page_keeps_the_no_script_policy(self):
        cookie = self.configured_login()
        _, headers, _ = self.get("/admin/diagnostics", cookie=cookie)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_an_incomplete_log_says_so(self):
        self.m.diagnostics.last_failure = "OperationalError: disk I/O error"
        cookie = self.configured_login()
        _, _, page = self.get("/admin/diagnostics", cookie=cookie)
        self.assertIn(b"log is incomplete", page)


class TestDivergencePage(AdminCase):
    """View, download, clear — and never without a session."""

    def setUp(self):
        super().setUp()
        self.m.settings.shadow_per_minute = 5

    def _a_divergence(self):
        self.fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")
        self.fake.data["Person"]["1"]["attributes"]["first_name"] = "Grace"
        self.m.divergence.check(1, "/people/v2/people/1", {})

    def test_every_route_needs_a_session(self):
        for path in ("/admin/divergence", "/admin/divergence/download"):
            status, headers, _ = self.get(path)
            self.assertEqual(status, 303, path)
            self.assertEqual(headers["Location"], "/")
        self.assertEqual(self.post("/admin/divergence/clear")[0], 303)

    def test_the_page_shows_a_divergence(self):
        self._a_divergence()
        cookie = self.configured_login()
        status, _, page = self.get("/admin/divergence", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"divergence", page)
        self.assertIn(b"first_name", page)

    def test_the_page_never_shows_a_real_value(self):
        self._a_divergence()
        cookie = self.configured_login()
        _, _, page = self.get("/admin/divergence", cookie=cookie)
        self.assertNotIn(b"Lovelace", page)
        self.assertNotIn(b"Grace", page)

    def test_the_dashboard_says_when_it_is_off_and_offers_the_switch(self):
        self.m.settings.shadow_per_minute = 0
        cookie = self.configured_login()
        _, _, page = self.get("/", cookie=cookie)
        self.assertIn(b"Off.", page)
        self.assertIn(b"/admin/divergence>turn it on", page)

    def test_the_download_is_a_json_attachment_with_nothing_real_in_it(self):
        self._a_divergence()
        cookie = self.configured_login()
        status, headers, payload = self.get("/admin/divergence/download", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        rendered = json.dumps(payload)
        self.assertNotIn("Lovelace", rendered)
        self.assertNotIn("Grace", rendered)
        self.assertEqual(len(payload["reports"]), 1)

    def test_clearing_needs_the_csrf_token(self):
        self._a_divergence()
        cookie = self.configured_login()
        self.post("/admin/divergence/clear", _form(csrf="wrong"), cookie=cookie)
        self.assertEqual(len(divergence.recent(self.m.db)), 1)

    def test_clearing_empties_the_log(self):
        self._a_divergence()
        cookie = self.configured_login()
        _, _, page = self.get("/admin/divergence", cookie=cookie)
        csrf = re.search(rb'name=csrf value="([^"]+)"', page).group(1).decode()
        status, headers, _ = self.post("/admin/divergence/clear", _form(csrf=csrf), cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(divergence.recent(self.m.db), [])

    def test_the_page_keeps_the_no_script_policy(self):
        cookie = self.configured_login()
        _, headers, _ = self.get("/admin/divergence", cookie=cookie)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])


class TestDivergenceControls(AdminCase):
    """On, off and how hard — without editing the environment and restarting."""

    def _page_csrf(self, cookie):
        _, _, page = self.get("/admin/divergence", cookie=cookie)
        return re.search(rb'name=csrf value="([^"]+)"', page).group(1).decode()

    def _configure(self, cookie, **fields):
        return self.post("/admin/divergence/configure",
                         _form(csrf=self._page_csrf(cookie), **fields), cookie=cookie)

    def test_it_needs_a_session(self):
        self.assertEqual(self.post("/admin/divergence/configure")[0], 303)
        self.assertIsNone(self.m.db.get_meta(divergence.OVERRIDE_KEY))

    def test_turning_it_on_from_the_page(self):
        cookie = self.configured_login()
        status, headers, _ = self._configure(cookie, per_minute="6")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin/divergence?saved=1")
        self.assertTrue(self.m.divergence.enabled)
        self.assertEqual(self.m.divergence.per_minute, 6)

    def test_turning_it_off_from_the_page(self):
        self.m.settings.shadow_per_minute = 10
        cookie = self.configured_login()
        self._configure(cookie, per_minute="0")
        self.assertFalse(self.m.divergence.enabled)

    def test_the_page_says_which_it_is_and_where_it_came_from(self):
        cookie = self.configured_login()
        _, _, page = self.get("/admin/divergence", cookie=cookie)
        self.assertIn(b"PCOMIRROR_SHADOW_PER_MINUTE", page)
        self._configure(cookie, per_minute="6")
        _, _, page = self.get("/admin/divergence", cookie=cookie)
        self.assertIn(b"6 checks per minute", page)
        self.assertIn(b"set here", page)

    def test_reverting_to_the_environment_default(self):
        self.m.settings.shadow_per_minute = 3
        cookie = self.configured_login()
        self._configure(cookie, per_minute="9")
        self._configure(cookie, reset="1")
        self.assertEqual(self.m.divergence.per_minute, 3)
        self.assertIsNone(self.m.db.get_meta(divergence.OVERRIDE_KEY))

    def test_a_number_out_of_range_is_refused_with_a_reason(self):
        cookie = self.configured_login()
        status, _, page = self._configure(cookie, per_minute="10000")
        self.assertEqual(status, 200)
        self.assertIn(b"Choose between 0 and", page)
        self.assertIsNone(self.m.db.get_meta(divergence.OVERRIDE_KEY))

    def test_something_that_is_not_a_number_is_refused(self):
        cookie = self.configured_login()
        status, _, page = self._configure(cookie, per_minute="lots")
        self.assertEqual(status, 200)
        self.assertIn(b"whole number", page)

    def test_it_needs_the_csrf_token(self):
        cookie = self.configured_login()
        self.post("/admin/divergence/configure", _form(csrf="wrong", per_minute="6"),
                  cookie=cookie)
        self.assertIsNone(self.m.db.get_meta(divergence.OVERRIDE_KEY))

    def test_the_dashboard_offers_the_switch_when_it_is_off(self):
        cookie = self.configured_login()
        _, _, page = self.get("/", cookie=cookie)
        self.assertIn(b"/admin/divergence>turn it on", page)


if __name__ == "__main__":
    unittest.main()
