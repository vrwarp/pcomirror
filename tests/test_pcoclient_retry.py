"""PcoClient: unusual Retry-After values and the settled outcome of a spent retry budget."""
import unittest
from unittest import mock

import base  # noqa: F401  (puts the repo root on sys.path)
from pcomirror import pcoclient
from pcomirror.config import Settings
from pcomirror.pcoclient import PcoClient, Response


class _NoLimit:
    """A limiter that never waits, so a 429's pause does not burn real time."""

    def acquire(self, priority="reconcile", max_wait=None):
        pass

    def on_response(self, headers):
        pass

    def on_429(self, retry_after):
        self.last_retry_after = retry_after


class _Always429:
    def __init__(self, retry_after):
        self.retry_after = retry_after
        self.sends = 0

    def send(self, method, url, headers, body):
        self.sends += 1
        return Response(429, {"Retry-After": self.retry_after, "X-Request-Id": "req-9"}, b"{}")


class TestRetryBudget(unittest.TestCase):
    def test_retry_after_parsing(self):
        self.assertEqual(pcoclient._retry_after_seconds("3"), 3.0)
        self.assertIsNone(pcoclient._retry_after_seconds(None))
        self.assertIsNone(pcoclient._retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT"))

    def test_http_date_retry_after_does_not_crash_and_outcome_is_settled(self):
        transport = _Always429("Wed, 21 Oct 2026 07:28:00 GMT")
        limiter = _NoLimit()
        client = PcoClient(Settings(), limiter, transport)
        with mock.patch("time.sleep"):
            resp = client.request("POST", "/people/v2/people", json_body={}, max_attempts=3)
        self.assertEqual(resp.status, 429)
        self.assertEqual(transport.sends, 3)
        self.assertEqual(resp.attempts, 3)
        self.assertEqual(resp.request_id, "req-9")
        self.assertIsNone(limiter.last_retry_after)
        self.assertIsNotNone(resp.duration_ms)


class TestSettingsRepr(unittest.TestCase):
    def test_repr_does_not_leak_the_pat(self):
        self.assertNotIn("hunter2-pat", repr(Settings(pco_secret="hunter2-pat")))
        self.assertEqual(Settings(pco_secret="x").pco_secret, "x")


if __name__ == "__main__":
    unittest.main()
