"""Constant-time comparisons must treat non-ASCII input as a mismatch, not a crash.

`hmac.compare_digest` raises TypeError on a `str` pair with non-ASCII characters;
the password, CSRF field and webhook signature header are all caller-controlled.
"""
import hashlib
import hmac
import unittest

import base  # noqa: F401  (puts the repo root on sys.path)
from pcomirror import adminauth, webhooks


class TestNonAsciiCompare(unittest.TestCase):
    def test_same_is_constant_time_equality(self):
        self.assertTrue(adminauth._same("abc", "abc"))
        self.assertFalse(adminauth._same("abc", "abd"))
        self.assertTrue(adminauth._same("pässword", "pässword"))
        self.assertFalse(adminauth._same("pässword", "password"))

    def test_csrf_with_non_ascii_is_rejected_not_raised(self):
        session = {"csrf": "tokenvalue"}
        self.assertTrue(adminauth.check_csrf(session, "tokenvalue"))
        self.assertFalse(adminauth.check_csrf(session, "tokén"))

    def test_signature_with_non_ascii_is_rejected_not_raised(self):
        raw = b'{"x":1}'
        good = hmac.new(b"sekret", raw, hashlib.sha256).hexdigest()
        self.assertTrue(webhooks.verify("sekret", raw, good))
        self.assertTrue(webhooks.verify("sekret", raw, f"  {good}\n"))
        self.assertFalse(webhooks.verify("sekret", raw, good[:-1] + "é"))
        self.assertFalse(webhooks.verify("sekret", raw, None))


if __name__ == "__main__":
    unittest.main()
