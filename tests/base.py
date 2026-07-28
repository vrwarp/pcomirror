"""Shared test setup: build a Mirror wired to the in-process FakePCO."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))                       # tests/ (fakepco)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))      # repo root (pcomirror)

from pcomirror.app import Mirror          # noqa: E402
from pcomirror.config import Settings     # noqa: E402
from fakepco import FakePCO               # noqa: E402


def build(fake: FakePCO | None = None, allow_anonymous: bool = True,
          diagnostic_keep: int | None = None) -> tuple[Mirror, FakePCO]:
    """`allow_anonymous` defaults on so the serving tests can stay about serving;
    the api_key plane has its own suite (test_apikeys.py).

    `diagnostic_keep` is left at the production default so every other suite
    exercises the recorder incidentally — a log that only runs in its own tests
    is one that breaks the first time something else changes around it.
    """
    fake = fake or FakePCO()
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    settings = Settings(db_path=path, rate_target_rps=1_000_000.0,
                        pco_app_id="app", pco_secret="sec",
                        allow_anonymous=allow_anonymous)
    if diagnostic_keep is not None:
        settings.diagnostic_keep = diagnostic_keep
    return Mirror(settings, transport=fake), fake


def wsgi_get(app, path, query="", headers=None):
    return _wsgi(app, "GET", path, query, b"", headers)


def wsgi_call(app, method, path, query="", body=b"", headers=None):
    return _wsgi(app, method, path, query, body, headers)


def _wsgi(app, method, path, query, body, headers):
    import io
    env = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
    }
    for k, v in (headers or {}).items():
        env["HTTP_" + k.upper().replace("-", "_")] = v
    captured = {}

    def start_response(status, hdrs):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(hdrs)

    chunks = app(env, start_response)
    raw = b"".join(chunks)
    import json
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw                       # HTML (the admin page) comes back as bytes
    return captured["status"], captured["headers"], parsed
