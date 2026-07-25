"""Entrypoint helpers (PUID/PGID). The privilege drop itself needs root and a
container, so what's covered here is the parsing and the ownership targeting."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "docker-entrypoint.py")
_spec = importlib.util.spec_from_file_location("docker_entrypoint", _PATH)
entrypoint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entrypoint)


class EnvCase(unittest.TestCase):
    """Each helper reads os.environ directly, so restore it between cases."""

    def setUp(self):
        self._saved = dict(os.environ)
        for k in ("PUID", "PGID", "PCOMIRROR_DB"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)


class TestIdEnv(EnvCase):
    def test_defaults_when_unset_or_blank(self):
        self.assertEqual(entrypoint._id_env("PUID", 10001), 10001)
        os.environ["PUID"] = "   "
        self.assertEqual(entrypoint._id_env("PUID", 10001), 10001)

    def test_parses_and_strips(self):
        os.environ["PUID"] = " 1026 "
        self.assertEqual(entrypoint._id_env("PUID", 10001), 1026)

    def test_zero_is_allowed(self):
        os.environ["PUID"] = "0"          # explicit root, if someone insists
        self.assertEqual(entrypoint._id_env("PUID", 10001), 0)

    def test_rejects_non_integer_and_negative(self):
        for bad in ("abc", "1000:1000", "-1"):
            os.environ["PUID"] = bad
            with self.assertRaises(SystemExit, msg=bad):
                entrypoint._id_env("PUID", 10001)


class TestDataDir(EnvCase):
    def test_default(self):
        self.assertEqual(entrypoint.data_dir(), "/data")

    def test_follows_pcomirror_db(self):
        os.environ["PCOMIRROR_DB"] = "/mnt/nas/pco/mirror.db"
        self.assertEqual(entrypoint.data_dir(), "/mnt/nas/pco")

    def test_bare_filename_is_cwd(self):
        os.environ["PCOMIRROR_DB"] = "pcomirror.db"
        self.assertEqual(entrypoint.data_dir(), ".")

    def test_empty_falls_back_to_default(self):
        os.environ["PCOMIRROR_DB"] = ""
        self.assertEqual(entrypoint.data_dir(), "/data")


class TestTakeOwnership(unittest.TestCase):
    def test_creates_missing_directory(self):
        base = tempfile.mkdtemp()
        target = os.path.join(base, "data")
        entrypoint.take_ownership(target, os.getuid(), os.getgid())   # no-op chown
        self.assertTrue(os.path.isdir(target))

    def test_targets_dir_and_immediate_children_only(self):
        base = tempfile.mkdtemp()
        open(os.path.join(base, "pcomirror.db"), "w").close()
        open(os.path.join(base, "pcomirror.db-wal"), "w").close()
        os.mkdir(os.path.join(base, "nested"))
        open(os.path.join(base, "nested", "deep"), "w").close()

        # a uid/gid that cannot match the real owner, so every path needs a chown
        uid, gid = os.getuid() + 1, os.getgid() + 1
        seen = []
        real_chown = entrypoint.os.chown
        try:
            entrypoint.os.chown = lambda p, u, g: seen.append(p)
            entrypoint.take_ownership(base, uid, gid)
        finally:
            entrypoint.os.chown = real_chown

        self.assertIn(base, seen)
        self.assertIn(os.path.join(base, "pcomirror.db"), seen)
        self.assertIn(os.path.join(base, "pcomirror.db-wal"), seen)
        self.assertIn(os.path.join(base, "nested"), seen)
        self.assertNotIn(os.path.join(base, "nested", "deep"), seen)   # not recursive

    def test_skips_paths_already_owned(self):
        base = tempfile.mkdtemp()
        open(os.path.join(base, "pcomirror.db"), "w").close()
        seen = []
        real_chown = entrypoint.os.chown
        try:
            entrypoint.os.chown = lambda p, u, g: seen.append(p)
            entrypoint.take_ownership(base, os.getuid(), os.getgid())
        finally:
            entrypoint.os.chown = real_chown
        self.assertEqual(seen, [])          # restarts must not re-chown


class TestCheckWritable(unittest.TestCase):
    def test_passes_on_writable_dir(self):
        entrypoint.check_writable(tempfile.mkdtemp())      # no SystemExit

    def test_exits_with_actionable_message(self):
        base = tempfile.mkdtemp()
        target = os.path.join(base, "locked")
        os.mkdir(target, mode=0o500)
        if os.access(target, os.W_OK):
            self.skipTest("running as root: mode bits do not restrict access")
        with self.assertRaises(SystemExit) as cm:
            entrypoint.check_writable(target)
        message = str(cm.exception)
        self.assertIn("PUID/PGID", message)
        self.assertIn("chown", message)


if __name__ == "__main__":
    unittest.main()
