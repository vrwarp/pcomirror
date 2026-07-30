"""Cross-origin access (DESIGN §8.5).

Every assertion here is one a browser makes for itself and reports as the same
one-line console message, so the failure modes are otherwise indistinguishable:
an unauthenticated preflight, headers on the error responses, `Vary: Origin`,
and the two planes the policy must never reach.
"""
from __future__ import annotations

import re
import unittest
import urllib.parse

from base import build, wsgi_call, wsgi_get
from pcomirror import adminauth, cors
from pcomirror.app import Mirror
from pcomirror.config import Settings
from pcomirror.serving import Application

ORIGIN = "https://app.example.org"


def policy(**kw) -> cors.Policy:
    kw.setdefault("origins", (ORIGIN,))
    return cors.Policy(**kw)


def mirror(pol: cors.Policy | None = None, allow_anonymous: bool = True):
    m, fake = build(allow_anonymous=allow_anonymous)
    m.settings.cors = pol if pol is not None else policy()
    fake.add_person("1", "Ada", "Lovelace", "2026-01-01T00:00:00Z")
    m.ingestor.backfill("person")
    return m, fake


def preflight(app, path="/people/v2/people", origin=ORIGIN, method="GET", headers=None):
    env = {}
    if origin is not None:
        env["Origin"] = origin
    if method is not None:
        env["Access-Control-Request-Method"] = method
    if headers is not None:
        env["Access-Control-Request-Headers"] = headers
    return wsgi_call(app, "OPTIONS", path, headers=env)


class TestOffByDefault(unittest.TestCase):
    """No origins configured means silent, not permissive."""

    def setUp(self):
        self.m, _ = mirror(cors.Policy())

    def test_no_headers_on_a_read(self):
        _, headers, _ = wsgi_get(self.m.wsgi, "/people/v2/people", headers={"Origin": ORIGIN})
        self.assertFalse([k for k in headers if k.lower().startswith("access-control-")])
        self.assertNotIn("Vary", headers)

    def test_options_stays_405(self):
        status, headers, _ = preflight(self.m.wsgi)
        self.assertEqual(status, 405)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_settings_default_is_off(self):
        self.assertFalse(Settings().cors.enabled)
        self.assertFalse(cors.from_env({}).enabled)


class TestAllowedOrigin(unittest.TestCase):
    def setUp(self):
        self.m, _ = mirror()

    def test_echoes_the_origin_and_varies(self):
        _, headers, body = wsgi_get(self.m.wsgi, "/people/v2/people",
                                    headers={"Origin": ORIGIN})
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(headers["Vary"], "Origin")
        self.assertEqual(headers["Access-Control-Expose-Headers"],
                         "X-Mirror-Source, Location")
        self.assertNotIn("Access-Control-Allow-Credentials", headers)
        self.assertEqual(len(body["data"]), 1)

    def test_another_origin_gets_no_permission_but_still_varies(self):
        _, headers, body = wsgi_get(self.m.wsgi, "/people/v2/people",
                                    headers={"Origin": "https://evil.example.net"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        # Set even on a refusal, or a shared cache serves this response — headers
        # and all — to a page from the origin that *is* allowed.
        self.assertEqual(headers["Vary"], "Origin")
        self.assertEqual(len(body["data"]), 1)      # the request itself still works

    def test_no_origin_header_is_not_a_cors_request(self):
        _, headers, _ = wsgi_get(self.m.wsgi, "/people/v2/people")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers["Vary"], "Origin")

    def test_case_folded_and_exact_on_scheme_host_port(self):
        p = cors.from_env({"PCOMIRROR_CORS_ORIGINS": "HTTPS://App.Example.ORG"})
        self.assertTrue(p.allows_origin(ORIGIN))
        self.assertTrue(p.allows_origin("HTTPS://APP.EXAMPLE.ORG"))
        self.assertFalse(p.allows_origin("http://app.example.org"))       # scheme
        self.assertFalse(p.allows_origin("https://app.example.org:8443"))  # port
        self.assertFalse(p.allows_origin("https://app.example.org.evil.net"))

    def test_health_probes_are_eligible(self):
        _, headers, _ = wsgi_get(self.m.wsgi, "/healthz", headers={"Origin": ORIGIN})
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)


class TestAnyOrigin(unittest.TestCase):
    def test_wildcard_echoes_wildcard_and_does_not_vary(self):
        m, _ = mirror(cors.Policy(origins=("*",)))
        _, headers, _ = wsgi_get(m.wsgi, "/people/v2/people",
                                 headers={"Origin": "https://anything.example.net"})
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        # One identical answer for every origin, so a cache may share it.
        self.assertNotIn("Vary", headers)

    def test_a_malformed_origin_is_never_echoed(self):
        """`*` allows every origin; it does not allow every *string*.

        The value lands in a response header, so an `Origin` carrying a newline
        would be response splitting rather than a mismatch. Only something that
        parses as an origin can be allowed — and under `*` the echo is the literal
        wildcard anyway.
        """
        m, _ = mirror(cors.Policy(origins=("*",)))
        for bad in ("https://a\r\nX-Evil: 1", "not-an-origin", "https://a/path", ""):
            self.assertFalse(cors.Policy(origins=("*",)).allows_origin(bad), bad)
        _, headers, _ = wsgi_get(m.wsgi, "/people/v2/people",
                                 headers={"Origin": "https://a b"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)


class TestWildcardSubdomains(unittest.TestCase):
    def setUp(self):
        self.p = cors.from_env({"PCOMIRROR_CORS_ORIGINS": "https://*.example.org"})

    def test_matches_subdomains_at_any_depth(self):
        self.assertTrue(self.p.allows_origin("https://app.example.org"))
        self.assertTrue(self.p.allows_origin("https://a.b.example.org"))

    def test_refuses_the_bare_domain_a_lookalike_and_a_port(self):
        # The bare domain is a different origin the operator did not name.
        self.assertFalse(self.p.allows_origin("https://example.org"))
        self.assertFalse(self.p.allows_origin("https://evil-example.org"))
        self.assertFalse(self.p.allows_origin("https://app.example.org.evil.net"))
        self.assertFalse(self.p.allows_origin("https://app.example.org:8443"))
        self.assertFalse(self.p.allows_origin("http://app.example.org"))

    def test_a_port_on_the_pattern_is_part_of_it(self):
        p = cors.from_env({"PCOMIRROR_CORS_ORIGINS": "http://*.local:8080"})
        self.assertTrue(p.allows_origin("http://app.local:8080"))
        self.assertFalse(p.allows_origin("http://app.local"))


class TestNullOrigin(unittest.TestCase):
    def test_opt_in_only(self):
        self.assertFalse(policy().allows_origin("null"))
        p = cors.from_env({"PCOMIRROR_CORS_ORIGINS": "null"})
        self.assertTrue(p.allows_origin("null"))
        self.assertFalse(p.allows_origin(ORIGIN))


class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.m, _ = mirror()

    def test_answers_without_a_credential(self):
        """The one request in the exchange that cannot carry the API key.

        Browsers strip `Authorization` from the probe, so a preflight routed
        through the key check would 401 — and the browser would report the *real*
        request as a CORS failure with that 401 nowhere in sight.
        """
        m, _ = mirror(allow_anonymous=False)      # no keys exist, so a GET is 401
        status, headers, _ = preflight(m.wsgi, method="PATCH")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertIn("PATCH", headers["Access-Control-Allow-Methods"])
        got, _, _ = wsgi_get(m.wsgi, "/people/v2/people", headers={"Origin": ORIGIN})
        self.assertEqual(got, 401)

    def test_advertises_methods_headers_and_a_cache_lifetime(self):
        _, headers, body = preflight(self.m.wsgi, headers="authorization,content-type")
        self.assertEqual(headers["Access-Control-Allow-Methods"], "GET, POST, PATCH, DELETE")
        self.assertEqual(headers["Access-Control-Allow-Headers"], "Authorization, Content-Type")
        self.assertEqual(headers["Access-Control-Max-Age"], "600")
        self.assertNotIn(cors.DIAGNOSTIC_HEADER, headers)
        self.assertEqual(body, {})

    def test_refused_origin_gets_no_permission_and_a_reason(self):
        status, headers, _ = preflight(self.m.wsgi, origin="https://evil.example.net")
        self.assertEqual(status, 200)     # a preflight must be 2xx to be readable at all
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Access-Control-Allow-Methods", headers)
        self.assertIn("PCOMIRROR_CORS_ORIGINS", headers[cors.DIAGNOSTIC_HEADER])

    def test_method_outside_the_policy(self):
        m, _ = mirror(policy(methods=("GET",)))
        _, headers, _ = preflight(m.wsgi, method="DELETE")
        # The advertised set is what the browser compares against, and the
        # mismatch is what it names: "Method DELETE is not allowed by …".
        self.assertEqual(headers["Access-Control-Allow-Methods"], "GET")
        self.assertIn("DELETE", headers[cors.DIAGNOSTIC_HEADER])
        self.assertIn("PCOMIRROR_CORS_METHODS", headers[cors.DIAGNOSTIC_HEADER])

    def test_header_outside_the_policy(self):
        _, headers, _ = preflight(self.m.wsgi, headers="authorization, x-custom")
        self.assertIn("x-custom", headers[cors.DIAGNOSTIC_HEADER])
        self.assertIn("PCOMIRROR_CORS_HEADERS", headers[cors.DIAGNOSTIC_HEADER])

    def test_safelisted_headers_are_never_refused(self):
        """A browser adds `Accept` and `Content-Type` on its own account, so
        refusing one would fail a preflight over a header nobody chose to send."""
        _, headers, _ = preflight(self.m.wsgi, headers="accept, accept-language, range")
        self.assertNotIn(cors.DIAGNOSTIC_HEADER, headers)

    def test_a_reason_cannot_inject_a_header(self):
        _, headers, _ = preflight(self.m.wsgi, origin="https://a\r\nX-Evil: 1")
        for value in headers.values():
            self.assertNotIn("\n", value)
            self.assertNotIn("\r", value)
        self.assertNotIn("X-Evil", headers)

    def test_options_without_the_request_method_is_not_a_preflight(self):
        status, headers, _ = preflight(self.m.wsgi, method=None)
        self.assertEqual(status, 405)
        self.assertNotIn("Access-Control-Allow-Methods", headers)

    def test_wildcard_headers_pass_anything(self):
        m, _ = mirror(policy(headers=("*",)))
        _, headers, _ = preflight(m.wsgi, headers="x-custom")
        self.assertEqual(headers["Access-Control-Allow-Headers"], "*")
        self.assertNotIn(cors.DIAGNOSTIC_HEADER, headers)


class TestFailuresAreReadable(unittest.TestCase):
    """A 401 or 400 without the headers is reported to a developer as a CORS
    error, and the sentence saying what was actually wrong never arrives."""

    def test_401_carries_the_headers(self):
        m, _ = mirror(allow_anonymous=False)
        status, headers, body = wsgi_get(m.wsgi, "/people/v2/people",
                                        headers={"Origin": ORIGIN})
        self.assertEqual(status, 401)
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertIn("errors", body)

    def test_400_and_404_carry_the_headers(self):
        m, _ = mirror()
        for path, query, expected in (("/people/v2/people", "where[nope]=1", 400),
                                      ("/people/v2/people/999", "", 404)):
            status, headers, _ = wsgi_get(m.wsgi, path, query, headers={"Origin": ORIGIN})
            self.assertEqual(status, expected)
            self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)

    def test_500_carries_the_headers(self):
        m, _ = mirror()
        m.wsgi.route = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        status, headers, _ = wsgi_get(m.wsgi, "/people/v2/people", headers={"Origin": ORIGIN})
        self.assertEqual(status, 500)
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)


class TestExcludedPlanes(unittest.TestCase):
    """Two paths are never cross-origin readable, whatever is configured."""

    def setUp(self):
        self.m, _ = mirror(cors.Policy(origins=("*",)))

    def test_the_operator_console_never_carries_the_headers(self):
        for path in ("/", "/admin/login", "/admin/diagnostics", "/admin/webhooks"):
            _, headers, _ = wsgi_get(self.m.wsgi, path, headers={"Origin": ORIGIN})
            self.assertNotIn("Access-Control-Allow-Origin", headers, path)

    def test_the_console_preflight_is_not_answered_either(self):
        _, headers, _ = preflight(self.m.wsgi, path="/")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Access-Control-Allow-Methods", headers)

    def test_the_webhook_receiver_never_carries_the_headers(self):
        status, headers, _ = wsgi_call(self.m.wsgi, "POST", "/pco/webhooks/nope",
                                       body=b"{}", headers={"Origin": ORIGIN})
        self.assertEqual(status, 404)          # unknown token, as ever
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_eligibility_is_decided_by_path(self):
        app = self.m.wsgi
        self.assertTrue(app._cors_eligible("/people/v2/people"))
        self.assertTrue(app._cors_eligible("/check-ins/v2/check_ins"))
        self.assertTrue(app._cors_eligible("/healthz"))
        self.assertFalse(app._cors_eligible("/"))
        self.assertFalse(app._cors_eligible("/admin/keys/create"))
        self.assertFalse(app._cors_eligible("/pco/webhooks/token"))


class TestCredentials(unittest.TestCase):
    def test_allowed_credentials_echo_the_origin(self):
        m, _ = mirror(policy(allow_credentials=True))
        _, headers, _ = wsgi_get(m.wsgi, "/people/v2/people", headers={"Origin": ORIGIN})
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(headers["Access-Control-Allow-Credentials"], "true")
        self.assertEqual(headers["Vary"], "Origin")

    def test_never_a_wildcard_beside_credentials(self):
        """A browser rejects the combination outright, so a service that emitted
        it would fail every request while looking configured."""
        for var in ("PCOMIRROR_CORS_ORIGINS", "PCOMIRROR_CORS_HEADERS",
                    "PCOMIRROR_CORS_EXPOSE_HEADERS"):
            env = {"PCOMIRROR_CORS_ORIGINS": ORIGIN,
                   "PCOMIRROR_CORS_ALLOW_CREDENTIALS": "1", var: "*"}
            with self.assertRaises(ValueError) as caught:
                cors.from_env(env)
            self.assertIn(var, str(caught.exception))

    def test_a_hand_built_wildcard_policy_with_credentials_still_echoes_concretely(self):
        p = cors.Policy(origins=("*",), allow_credentials=True)
        self.assertEqual(p.echo(ORIGIN), ORIGIN)
        self.assertTrue(p.varies_by_origin)


class TestParsing(unittest.TestCase):
    """A malformed policy fails startup. The alternative is a policy that does
    not mean what it says, diagnosed only in a browser somebody else is holding."""

    def test_from_env_defaults(self):
        p = cors.from_env({"PCOMIRROR_CORS_ORIGINS": f"{ORIGIN}, https://b.example.org"})
        self.assertEqual(p.origins, (ORIGIN, "https://b.example.org"))
        self.assertEqual(p.methods, ("GET", "POST", "PATCH", "DELETE"))
        self.assertEqual(p.headers, ("Authorization", "Content-Type"))
        self.assertEqual(p.expose, ("X-Mirror-Source", "Location"))
        self.assertEqual(p.max_age, 600)
        self.assertFalse(p.allow_credentials)

    def test_every_knob(self):
        p = cors.from_env({
            "PCOMIRROR_CORS_ORIGINS": ORIGIN,
            "PCOMIRROR_CORS_METHODS": "get, patch",
            "PCOMIRROR_CORS_HEADERS": "Authorization",
            "PCOMIRROR_CORS_EXPOSE_HEADERS": "X-Mirror-Source",
            "PCOMIRROR_CORS_MAX_AGE": "0",
            "PCOMIRROR_CORS_ALLOW_CREDENTIALS": "yes",
        })
        self.assertEqual(p.methods, ("GET", "PATCH"))
        self.assertEqual(p.headers, ("Authorization",))
        self.assertEqual(p.expose, ("X-Mirror-Source",))
        self.assertEqual(p.max_age, 0)
        self.assertTrue(p.allow_credentials)

    def test_an_empty_expose_list_is_respected(self):
        p = cors.from_env({"PCOMIRROR_CORS_ORIGINS": ORIGIN,
                           "PCOMIRROR_CORS_EXPOSE_HEADERS": ""})
        self.assertEqual(p.expose, ())

    def test_bad_origins(self):
        for value, hint in (
                (f"{ORIGIN}/", "trailing slash"),           # the copy-paste from a browser bar
                (f"{ORIGIN}/people", "path"),
                ("app.example.org", "scheme"),
                ("https://*.*.example.org", "wildcard"),
                ("https://ap*.example.org", "wildcard"),
                ("*.example.org", "scheme"),
                ("ftp://app.example.org", "scheme")):
            with self.assertRaises(ValueError, msg=value) as caught:
                cors.from_env({"PCOMIRROR_CORS_ORIGINS": value})
            self.assertIn(hint, str(caught.exception), value)

    def test_a_wildcard_beside_a_named_origin_is_a_contradiction(self):
        with self.assertRaises(ValueError) as caught:
            cors.from_env({"PCOMIRROR_CORS_ORIGINS": f"*, {ORIGIN}"})
        self.assertIn("contradiction", str(caught.exception))

    def test_an_unserved_method_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            cors.from_env({"PCOMIRROR_CORS_ORIGINS": ORIGIN,
                           "PCOMIRROR_CORS_METHODS": "GET, PUT"})
        self.assertIn("PUT", str(caught.exception))

    def test_bad_max_age(self):
        for value in ("-1", "soon"):
            with self.assertRaises(ValueError):
                cors.from_env({"PCOMIRROR_CORS_ORIGINS": ORIGIN,
                               "PCOMIRROR_CORS_MAX_AGE": value})

    def test_duplicates_collapse_and_blanks_are_ignored(self):
        p = cors.from_env({"PCOMIRROR_CORS_ORIGINS": f"{ORIGIN},,{ORIGIN} ,",
                           "PCOMIRROR_CORS_METHODS": "GET,get"})
        self.assertEqual(p.origins, (ORIGIN,))
        self.assertEqual(p.methods, ("GET",))

    def test_describe(self):
        self.assertEqual(cors.describe(cors.Policy()), "off")
        self.assertIn(ORIGIN, cors.describe(policy()))
        self.assertIn("any origin", cors.describe(cors.Policy(origins=("*",))))
        self.assertIn("credentials", cors.describe(policy(allow_credentials=True)))


class TestVaryMerging(unittest.TestCase):
    """A pass-through relays PCO's own `Vary`; replacing it would tell a cache the
    response does not vary on what PCO just said it does."""

    def test_merges_with_an_existing_vary(self):
        headers = {"Vary": "Accept-Encoding"}
        cors.attach(headers, policy(), ORIGIN)
        self.assertEqual(headers["Vary"], "Accept-Encoding, Origin")

    def test_merges_case_insensitively_and_does_not_duplicate(self):
        headers = {"vary": "origin"}
        cors.attach(headers, policy(), ORIGIN)
        self.assertEqual(headers["vary"], "origin")
        self.assertNotIn("Vary", headers)

    def test_a_wildcard_vary_is_left_alone(self):
        headers = {"Vary": "*"}
        cors.attach(headers, policy(), ORIGIN)
        self.assertEqual(headers["Vary"], "*")


class TestRelayedHeaders(unittest.TestCase):
    """Who may read this service is its own statement, never an upstream's."""

    def test_pco_access_control_headers_are_not_relayed(self):
        m, _ = mirror()
        relayed = m.wsgi._relay_headers({
            "Access-Control-Allow-Origin": "https://somewhere.else",
            "access-control-allow-credentials": "true",
            "Vary": "Accept-Encoding",
            "ETag": '"abc"',
        })
        self.assertEqual(relayed, {"Vary": "Accept-Encoding", "ETag": '"abc"'})


class TestInternalCallers(unittest.TestCase):
    def test_the_divergence_replay_is_unaffected(self):
        """`serve_json` bypasses the WSGI entry point, so a policy cannot change
        what the checker compares against PCO."""
        m, _ = mirror()
        status, body = m.wsgi.serve_json("/people")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 1)


class AdminCase(unittest.TestCase):
    """Signed in on the console, with the CORS pages reachable."""

    PASSWORD = "a-long-enough-password"

    def setUp(self):
        self.m, _ = mirror(allow_anonymous=False)
        adminauth._clear_failures()         # the throttle is process-wide
        self.cookie = self._sign_in()

    def _cookie(self, headers) -> str:
        return headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def _as(self) -> dict:
        return {"Cookie": f"{adminauth.COOKIE}={self.cookie}"}

    def _sign_in(self) -> str:
        """PCO_SECRET gets you in; the forced password change gets you a session."""
        _, headers, _ = wsgi_call(self.m.wsgi, "POST", "/admin/login", body=b"password=sec")
        self.cookie = self._cookie(headers)
        form = urllib.parse.urlencode({"csrf": self.csrf("/admin/password"),
                                       "password": self.PASSWORD,
                                       "confirm": self.PASSWORD}).encode()
        _, headers, _ = wsgi_call(self.m.wsgi, "POST", "/admin/password", body=form,
                                  headers=self._as())
        return self._cookie(headers)

    def csrf(self, path="/admin/cors") -> str:
        _, _, page = wsgi_get(self.m.wsgi, path, headers=self._as())
        return re.search(rb'name=csrf value="([^"]+)"', page).group(1).decode()

    def page(self, path="/admin/cors") -> bytes:
        return wsgi_get(self.m.wsgi, path, headers=self._as())[2]

    def save(self, csrf=None, **fields):
        body = urllib.parse.urlencode(
            {"csrf": csrf if csrf is not None else self.csrf(), **fields}, doseq=True).encode()
        return wsgi_call(self.m.wsgi, "POST", "/admin/cors/configure",
                         body=body, headers=self._as())

    def acao(self, origin=ORIGIN):
        """What a browser at `origin` is told on a real read."""
        return wsgi_get(self.m.wsgi, "/people/v2/people",
                        headers={"Origin": origin})[1].get("Access-Control-Allow-Origin")


class TestAdminDashboard(AdminCase):
    """The console shows the policy in force. Without it, a blocked `fetch` is
    diagnosed from the browser's side alone, where every cause reads the same."""

    def dashboard(self, pol) -> bytes:
        self.m.settings.cors = pol
        return self.page("/")

    def test_off_says_where_to_turn_it_on(self):
        page = self.dashboard(cors.Policy())
        self.assertIn(b"Browser access", page)
        self.assertIn(b"/admin/cors", page)

    def test_on_shows_the_policy_and_the_wildcard_caveat(self):
        page = self.dashboard(cors.from_env(
            {"PCOMIRROR_CORS_ORIGINS": f"{ORIGIN},https://*.church.org"}))
        self.assertIn(ORIGIN.encode(), page)
        self.assertIn(b"app.church.org", page)          # what the wildcard allows
        self.assertIn(b"600s", page)

    def test_the_open_combination_is_bannered(self):
        self.m.settings.allow_anonymous = True
        page = self.dashboard(cors.Policy(origins=("*",)))
        self.assertIn(b"no credential at all", page)


class TestAdminConfigure(AdminCase):
    """The page wins over the environment, persistently and without a restart —
    the same shape as the subscription list and the divergence rate."""

    def setUp(self):
        super().setUp()
        self.m.settings.cors = cors.Policy(origins=("https://env.example.org",))

    def test_saving_takes_effect_on_the_next_request(self):
        self.assertEqual(self.acao("https://env.example.org"), "https://env.example.org")
        self.assertIsNone(self.acao())
        status, headers, _ = self.save(origins=ORIGIN, max_age="60", methods=["GET"])
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin/cors?saved=1")
        # No restart, no reload of settings: the very next read is served under it.
        self.assertEqual(self.acao(), ORIGIN)
        self.assertIsNone(self.acao("https://env.example.org"))

    def test_it_survives_the_process_it_was_saved_in(self):
        """A policy fixed at 9pm has to still be there when the container comes
        back at 3am — which is exactly when the environment would have won."""
        self.save(origins=ORIGIN)
        rebuilt = Mirror(self.m.settings, transport=None, db=self.m.db)
        state = cors.effective(rebuilt.db, rebuilt.settings)
        self.assertEqual(state["source"], "admin")
        self.assertEqual(state["policy"].origins, (ORIGIN,))
        self.assertEqual(state["default"].origins, ("https://env.example.org",))

    def test_handing_it_back_restores_the_environment(self):
        self.save(origins=ORIGIN)
        status, headers, _ = self.save(reset="1")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin/cors?handed_back=1")
        self.assertEqual(self.acao("https://env.example.org"), "https://env.example.org")
        self.assertIsNone(self.acao())
        self.assertEqual(cors.effective(self.m.db, self.m.settings)["source"], "environment")

    def test_saving_nothing_turns_it_off_rather_than_handing_it_back(self):
        """Two different intentions, and conflating them would mean an operator
        who wanted CORS off got the environment's origins back on the next start."""
        self.save(origins="")
        state = cors.effective(self.m.db, self.m.settings)
        self.assertEqual(state["source"], "admin")
        self.assertFalse(state["policy"].enabled)
        self.assertIsNone(self.acao("https://env.example.org"))
        # And no preflight is answered — off is silent, however it was turned off.
        _, headers, _ = wsgi_call(self.m.wsgi, "OPTIONS", "/people/v2/people", headers={
            "Origin": "https://env.example.org", "Access-Control-Request-Method": "GET"})
        self.assertNotIn("Access-Control-Allow-Methods", headers)

    def test_every_field_round_trips(self):
        self.save(origins=f"{ORIGIN}, https://*.church.org", methods=["GET", "PATCH"],
                  headers="Authorization", expose="X-Mirror-Source", max_age="0",
                  allow_credentials="on")
        p = cors.effective(self.m.db, self.m.settings)["policy"]
        self.assertEqual(p.origins, (ORIGIN, "https://*.church.org"))
        self.assertEqual(p.methods, ("GET", "PATCH"))
        self.assertEqual(p.headers, ("Authorization",))
        self.assertEqual(p.expose, ("X-Mirror-Source",))
        self.assertEqual(p.max_age, 0)
        self.assertTrue(p.allow_credentials)
        _, headers, _ = preflight(self.m.wsgi, headers="authorization")
        self.assertEqual(headers["Access-Control-Allow-Methods"], "GET, PATCH")
        self.assertEqual(headers["Access-Control-Max-Age"], "0")
        self.assertEqual(headers["Access-Control-Allow-Credentials"], "true")

    def test_a_bad_value_is_refused_in_the_form_s_own_words(self):
        """One validator, two vocabularies: an operator typing in a box is not
        told to go and fix an environment variable."""
        status, _, page = self.save(origins=f"{ORIGIN}/")
        self.assertEqual(status, 200)                   # re-rendered, not redirected
        self.assertIn(b"trailing slash", page)
        self.assertIn(b"Origins", page)
        self.assertNotIn(b"PCOMIRROR_CORS_ORIGINS entry", page)
        # And nothing was stored, so the policy in force is untouched.
        self.assertEqual(cors.effective(self.m.db, self.m.settings)["source"], "environment")

    def test_a_refused_save_keeps_what_was_typed(self):
        """A typo in one field must not cost the operator the other five."""
        _, _, page = self.save(origins=f"{ORIGIN}/", headers="X-Custom", max_age="42")
        self.assertIn(b"X-Custom", page)
        self.assertIn(b'value="42"', page)
        self.assertIn(f'value="{ORIGIN}/"'.encode(), page)

    def test_credentials_beside_a_wildcard_is_refused_here_too(self):
        _, _, page = self.save(origins="*", allow_credentials="on")
        self.assertIn(b"rejects a wildcard on a credentialed request", page)
        self.assertEqual(cors.effective(self.m.db, self.m.settings)["source"], "environment")

    def test_an_unserved_method_cannot_be_posted(self):
        _, _, page = self.save(origins=ORIGIN, methods=["GET", "PUT"])
        self.assertIn(b"PUT", page)
        self.assertIn(b"405", page)

    def test_a_save_without_csrf_changes_nothing(self):
        status, _, page = self.save(csrf="wrong", origins=ORIGIN)
        self.assertEqual(status, 200)
        self.assertIn(b"Session expired", page)
        self.assertIsNone(self.acao())

    def test_the_page_needs_a_session(self):
        for path in ("/admin/cors", "/admin/cors/configure"):
            status, headers, _ = wsgi_get(self.m.wsgi, path)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/")

    def test_the_page_names_the_source_and_the_environment_default(self):
        self.assertIn(b"In force: the environment", self.page())
        self.save(origins=ORIGIN)
        page = self.page()
        self.assertIn(b"the policy set here", page)
        self.assertIn(b"https://env.example.org", page)      # still shown as the default
        self.assertIn(b"Hand back to the environment", page)

    def test_an_unreadable_stored_policy_falls_back_and_says_so(self):
        """Only reachable by editing the row by hand — `configure` stores what
        `build` validated — but silently serving a policy nobody can read is the
        one outcome that must not happen."""
        self.m.db.set_meta(cors.OVERRIDE_KEY, '{"origins": ["not-an-origin"]}')
        state = cors.effective(self.m.db, self.m.settings)
        self.assertTrue(state["stored_unreadable"])
        self.assertEqual(state["source"], "environment")
        self.assertEqual(self.acao("https://env.example.org"), "https://env.example.org")
        self.assertIn(b"stored policy could not be read", self.page())

    def test_a_wildcard_saved_here_is_bannered_on_its_own_page(self):
        self.save(origins="*")
        self.assertIn(b"Any origin is allowed", self.page())

    def test_the_console_stays_out_of_reach_of_what_it_configures(self):
        """Configurable from the page, and still never applying to the page."""
        self.save(origins="*")
        for path in ("/", "/admin/cors"):
            _, headers, _ = wsgi_get(self.m.wsgi, path, headers={"Origin": ORIGIN})
            self.assertNotIn("Access-Control-Allow-Origin", headers, path)


class TestStoredPolicy(unittest.TestCase):
    def test_encode_decode_round_trip(self):
        p = cors.build(origins=f"{ORIGIN},https://*.church.org", methods="get",
                       headers="Authorization", expose="", max_age="30",
                       allow_credentials=True)
        self.assertEqual(cors.decode(cors.encode(p)), p)
        self.assertEqual(p.expose, ())      # an explicitly empty list stays empty

    def test_decode_revalidates(self):
        """The row is text in a database. An origin that reached it another way
        must not become one this service echoes."""
        for raw in ('{"origins": ["https://a\\r\\nX-Evil: 1"]}',
                    '{"origins": ["https://x/"]}',
                    '{"origins": ["https://x"], "methods": ["PUT"]}',
                    '{"origins": ["*"], "allow_credentials": true}',
                    '["not", "an", "object"]'):
            with self.assertRaises(ValueError, msg=raw):
                cors.decode(raw)


class TestPlainApplication(unittest.TestCase):
    def test_settings_without_a_cors_field(self):
        """`Application` is constructed directly in several suites; a settings
        object predating this feature must not raise."""
        class Bare:
            webhook_path_prefix = "/pco/webhooks"
            allow_anonymous = True

        app = Application.__new__(Application)
        app.s = Bare()
        self.assertTrue(app._cors_eligible("/people/v2/people"))
        self.assertFalse(app._cors_eligible("/pco/webhooks/x"))


if __name__ == "__main__":
    unittest.main()
