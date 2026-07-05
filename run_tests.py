#!/usr/bin/env python3
"""Run the whole test suite from the repo root:  python3 run_tests.py

Also runs the SQL writer-semantics harness (docs/schema_test_sqlite.py).
Exits non-zero on any failure.
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)


def main() -> int:
    loader = unittest.defaultTestLoader
    suite = loader.discover(os.path.join(ROOT, "tests"), pattern="test_*.py",
                            top_level_dir=os.path.join(ROOT, "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    harness = subprocess.run([sys.executable, os.path.join(ROOT, "docs", "schema_test_sqlite.py")])
    ok = result.wasSuccessful() and harness.returncode == 0
    print("\nALL GREEN" if ok else "\nFAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
