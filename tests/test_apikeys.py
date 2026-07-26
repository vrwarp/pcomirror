"""The local api_key plane (DESIGN §8.4): minting, verification, scope enforcement."""
from __future__ import annotations

import base64
import unittest

from base import build, wsgi_call, wsgi_get
from pcomirror import apikeys


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


class TestKeyLifecycle(unittest.TestCase):
    def setUp(self):
        self.m, _ = build()

    def test_key_shape_and_storage(self):
        key = apikeys.create(self.m.db, "app", "read:*")
        scheme, prefix, secret = key.split("_")
        self.assertEqual(scheme, "pcm")
        self.assertEqual(len(prefix), apikeys.PREFIX_LEN)
        self.assertGreaterEqual(len(secret), 40)
        row = self.m.db.query_one("SELECT * FROM api_key WHERE prefix=?", (prefix,))
        # only the digest is kept — the secret must not be recoverable
        self.assertEqual(bytes(row["key_hash"]), apikeys.hash_key(key))
        self.assertNotIn(secret, str(tuple(row)))

    def test_authenticate_round_trip(self):
        key = apikeys.create(self.m.db, "app", "read:*")
        row = apikeys.authenticate(self.m.db, f"Bearer {key}")
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "app")

    def test_bare_key_without_bearer_is_accepted(self):
        key = apikeys.create(self.m.db, "app")
        self.assertIsNotNone(apikeys.authenticate(self.m.db, key))

    def test_http_basic_carries_the_key_in_either_field(self):
        """Every existing PCO client authenticates with Basic `app_id:secret`.

        The mirror's promise is a base-URL + credential swap; if Basic were
        refused, pointing a PCO client at it would be a code change instead.
        """
        key = apikeys.create(self.m.db, "app", "read:*")
        for raw in (f"{key}:unused", f"app_id:{key}"):
            encoded = base64.b64encode(raw.encode()).decode()
            row = apikeys.authenticate(self.m.db, f"Basic {encoded}")
            self.assertIsNotNone(row, msg=raw)
            self.assertEqual(row["name"], "app")

    def test_basic_rejects_a_pco_pat_and_malformed_encodings(self):
        # The two credential planes stay separate: a real PCO PAT is not a way in.
        apikeys.create(self.m.db, "app")
        pat = base64.b64encode(b"pco_app_id:pco_secret").decode()
        for bad in (f"Basic {pat}", "Basic not-base64!", "Basic ",
                    "Basic " + base64.b64encode(b"\xff\xfe").decode()):
            self.assertIsNone(apikeys.authenticate(self.m.db, bad), msg=bad)

    def test_rejects_garbage_and_wrong_secret(self):
        key = apikeys.create(self.m.db, "app")
        prefix = key.split("_")[1]
        for bad in (None, "", "Bearer ", "not-a-key", "pcm_short",
                    f"pcm_{prefix}_wrongsecret", key + "x"):
            self.assertIsNone(apikeys.authenticate(self.m.db, bad), msg=bad)

    def test_revoked_key_stops_working(self):
        key = apikeys.create(self.m.db, "app")
        prefix = key.split("_")[1]
        self.assertTrue(apikeys.revoke(self.m.db, prefix))
        self.assertIsNone(apikeys.authenticate(self.m.db, key))
        self.assertFalse(apikeys.revoke(self.m.db, prefix))       # already revoked
        self.assertFalse(apikeys.any_enabled(self.m.db))

    def test_last_used_recorded(self):
        key = apikeys.create(self.m.db, "app")
        self.assertIsNone(self.m.db.query_one("SELECT last_used_at FROM api_key")["last_used_at"])
        apikeys.authenticate(self.m.db, key)
        self.assertIsNotNone(self.m.db.query_one("SELECT last_used_at FROM api_key")["last_used_at"])

    def test_scope_helpers(self):
        self.assertTrue(apikeys.allows_read({"read:*"}, "people"))
        self.assertTrue(apikeys.allows_read({"read:people"}, "people"))
        self.assertFalse(apikeys.allows_read({"read:people"}, "emails"))
        self.assertFalse(apikeys.allows_read({"write"}, "people"))
        self.assertEqual(apikeys.parse_scopes("read:*, write ,"), {"read:*", "write"})


class TestEnforcement(unittest.TestCase):
    """Auth on (the default), exercised through the real WSGI stack."""

    def setUp(self):
        self.m, self.fake = build(allow_anonymous=False)
        self.fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        self.m.ingestor.backfill("person")

    def test_no_key_is_401_with_challenge(self):
        status, headers, body = wsgi_get(self.m.wsgi, "/people/v2/people")
        self.assertEqual(status, 401)
        # Both accepted schemes are offered, so a Basic client is not left guessing.
        self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))

    def test_a_basic_key_works_through_the_whole_stack(self):
        key = apikeys.create(self.m.db, "tally", "read:*")
        encoded = base64.b64encode(f"{key}:unused".encode()).decode()
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people",
                                   headers={"Authorization": f"Basic {encoded}"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["meta"]["total_count"], 1)

    def test_fresh_install_401_explains_itself(self):
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people")
        self.assertIn("create-api-key", body["errors"][0]["detail"])

    def test_invalid_key_401_does_not_leak_the_bootstrap_hint(self):
        apikeys.create(self.m.db, "app")
        _, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", headers=_auth("pcm_dead_beef"))
        self.assertNotIn("create-api-key", body["errors"][0]["detail"])

    def test_valid_key_reads(self):
        key = apikeys.create(self.m.db, "app", "read:*")
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/people", headers=_auth(key))
        self.assertEqual(status, 200)
        self.assertEqual(body["data"][0]["id"], "1")

    def test_read_scope_is_per_endpoint(self):
        key = apikeys.create(self.m.db, "app", "read:people")
        self.assertEqual(wsgi_get(self.m.wsgi, "/people/v2/people", headers=_auth(key))[0], 200)
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/emails", headers=_auth(key))
        self.assertEqual(status, 403)
        self.assertIn("read:emails", body["errors"][0]["detail"])

    def test_single_and_nested_reads_need_the_scope(self):
        key = apikeys.create(self.m.db, "app", "read:emails")
        self.assertEqual(wsgi_get(self.m.wsgi, "/people/v2/people/1", headers=_auth(key))[0], 403)
        self.assertEqual(
            wsgi_get(self.m.wsgi, "/people/v2/people/1/emails", headers=_auth(key))[0], 403)

    def test_writes_need_the_write_scope(self):
        key = apikeys.create(self.m.db, "app", "read:*")
        body = b'{"data":{"type":"Person","attributes":{"first_name":"B"}}}'
        status, _, payload = wsgi_call(self.m.wsgi, "POST", "/people/v2/people",
                                       body=body, headers=_auth(key))
        self.assertEqual(status, 403)
        self.assertIn("write", payload["errors"][0]["detail"])
        # and the write never reached PCO
        self.assertEqual(len(self.fake.data["Person"]), 1)

    def test_write_scope_allows_the_write(self):
        key = apikeys.create(self.m.db, "app", "read:*,write")
        body = b'{"data":{"type":"Person","attributes":{"first_name":"B","last_name":"C"}}}'
        status, _, _ = wsgi_call(self.m.wsgi, "POST", "/people/v2/people",
                                 body=body, headers=_auth(key))
        self.assertIn(status, (200, 201))

    def test_passthrough_needs_its_own_scope(self):
        """Spending the server's PCO credential is a separate privilege."""
        key = apikeys.create(self.m.db, "app", "read:*")
        # unmirrored type -> pass-through
        status, _, body = wsgi_get(self.m.wsgi, "/people/v2/workflows", headers=_auth(key))
        self.assertEqual(status, 403)
        self.assertIn("passthrough", body["errors"][0]["detail"])
        # and a mirror miss that asks to fall back to PCO
        self.assertEqual(wsgi_get(self.m.wsgi, "/people/v2/people/999", "passthrough=1",
                                  headers=_auth(key))[0], 403)

    def test_passthrough_scope_allows_it(self):
        key = apikeys.create(self.m.db, "app", "read:*,passthrough")
        status, _, _ = wsgi_get(self.m.wsgi, "/people/v2/people/999", "passthrough=1",
                                headers=_auth(key))
        self.assertNotEqual(status, 403)

    def test_health_and_readiness_stay_public(self):
        self.assertEqual(wsgi_get(self.m.wsgi, "/healthz")[0], 200)
        self.assertEqual(wsgi_get(self.m.wsgi, "/readyz")[0], 200)

    def test_webhook_receiver_stays_public(self):
        """It authenticates with its own HMAC; an API key must not be required."""
        status, _, _ = wsgi_call(self.m.wsgi, "POST", "/pco/webhooks/sometoken", body=b"{}")
        self.assertEqual(status, 404)          # unknown token, NOT 401

    def test_revoked_key_is_rejected_by_the_api(self):
        key = apikeys.create(self.m.db, "app", "read:*")
        self.assertEqual(wsgi_get(self.m.wsgi, "/people/v2/people", headers=_auth(key))[0], 200)
        apikeys.revoke(self.m.db, key.split("_")[1])
        self.assertEqual(wsgi_get(self.m.wsgi, "/people/v2/people", headers=_auth(key))[0], 401)


class TestAnonymousEscapeHatch(unittest.TestCase):
    def test_allow_anonymous_serves_without_a_key(self):
        m, fake = build(allow_anonymous=True)
        fake.add_person("1", "Ada", "L", "2026-01-01T00:00:00Z")
        m.ingestor.backfill("person")
        self.assertEqual(wsgi_get(m.wsgi, "/people/v2/people")[0], 200)


if __name__ == "__main__":
    unittest.main()
