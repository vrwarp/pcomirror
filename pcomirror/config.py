"""Configuration — resolved from environment, with safe defaults for local dev."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timezone


def now_iso() -> str:
    """ISO-8601 UTC, second precision with a literal Z — matches PCO's format so
    it compares chronologically as TEXT against `pco_updated_at` (DESIGN §3.1)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truthy(v: str | None) -> bool:
    return bool(v) and v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    db_path: str = "pcomirror.db"
    pco_base_url: str = "https://api.planningcenteronline.com/people/v2"
    pco_app_id: str = ""
    pco_secret: str = ""
    api_version: str = "2026-06-04"
    user_agent: str = "pcomirror/0.1 (+admin@example.org)"
    # rate limiter: initial target (req/s). Adapts from response headers at runtime.
    rate_target_rps: float = 4.0
    rate_util: float = 0.80
    # TLS to PCO: path to a CA bundle (e.g. behind a proxy). Empty = system trust store.
    pco_ca_bundle: str = ""
    # serving
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    webhook_path_prefix: str = "/pco/webhooks"
    # a public base URL for our webhook receiver, used when creating subscriptions
    public_base_url: str = "http://localhost:8080"
    # run an initial backfill on `serve` startup for any resource not yet backfilled
    backfill_on_start: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        s = cls()
        s.db_path = e.get("PCOMIRROR_DB", s.db_path)
        s.pco_base_url = e.get("PCO_BASE_URL", s.pco_base_url)
        s.pco_app_id = e.get("PCO_APP_ID", s.pco_app_id)
        s.pco_secret = e.get("PCO_SECRET", s.pco_secret)
        s.api_version = e.get("PCO_API_VERSION", s.api_version)
        s.user_agent = e.get("PCO_USER_AGENT", s.user_agent)
        s.pco_ca_bundle = e.get("PCO_CA_BUNDLE", s.pco_ca_bundle)
        s.bind_host = e.get("PCOMIRROR_HOST", s.bind_host)
        s.bind_port = int(e.get("PCOMIRROR_PORT", s.bind_port))
        s.public_base_url = e.get("PCOMIRROR_PUBLIC_URL", s.public_base_url)
        s.backfill_on_start = _truthy(e.get("PCOMIRROR_BACKFILL_ON_START"))
        if e.get("PCOMIRROR_RATE_TARGET_RPS"):
            s.rate_target_rps = float(e["PCOMIRROR_RATE_TARGET_RPS"])
        return s

    def auth_header(self) -> str:
        """HTTP Basic for the Personal Access Token (DESIGN §9.1)."""
        raw = f"{self.pco_app_id}:{self.pco_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()
