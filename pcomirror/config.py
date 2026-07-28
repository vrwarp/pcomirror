"""Configuration — resolved from environment, with safe defaults for local dev."""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone


def now_iso() -> str:
    """ISO-8601 UTC, second precision with a literal Z — matches PCO's format so
    it compares chronologically as TEXT against `pco_updated_at` (DESIGN §3.1)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truthy(v: str | None) -> bool:
    return bool(v) and v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SubscriptionSpec:
    """One declaratively-configured webhook subscription (PCOMIRROR_SUBSCRIPTIONS)."""
    subscription_id: str
    event: str
    secret: str
    url_token: str = ""     # empty = keep the existing token, or mint one


def parse_subscriptions(value: str | None) -> list[SubscriptionSpec]:
    """Parse `PCOMIRROR_SUBSCRIPTIONS` into specs, applied on every `serve` start.

    Two accepted forms — the compact one for hand-editing in a GUI env field, and
    JSON for secrets that contain the compact form's separators:

      id:event:token:secret , id:event::secret     (empty token = keep/mint)
      [{"id": ..., "event": ..., "token": ..., "secret": ...}, ...]

    Raises ValueError on anything malformed: a typo here means webhooks silently
    404 or fail their signature check, so it must be loud at startup.
    """
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            entries = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"PCOMIRROR_SUBSCRIPTIONS is not valid JSON: {e}") from e
        if not isinstance(entries, list):
            raise ValueError("PCOMIRROR_SUBSCRIPTIONS JSON must be a list of objects")
        out = []
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                raise ValueError(f"PCOMIRROR_SUBSCRIPTIONS[{i}] is not an object")
            missing = [k for k in ("id", "event", "secret") if not e.get(k)]
            if missing:
                raise ValueError(f"PCOMIRROR_SUBSCRIPTIONS[{i}] missing {', '.join(missing)}")
            out.append(SubscriptionSpec(str(e["id"]), str(e["event"]), str(e["secret"]),
                                        str(e.get("token") or "")))
        return out
    out = []
    for entry in (p.strip() for p in value.split(",")):
        if not entry:
            continue
        # maxsplit=3 so a secret may itself contain ':' (a ',' still needs the JSON form).
        parts = entry.split(":", 3)
        if len(parts) != 4:
            raise ValueError(
                f"bad PCOMIRROR_SUBSCRIPTIONS entry {entry!r}: expected id:event:token:secret")
        sub_id, event, token, secret = (p.strip() for p in parts)
        if not (sub_id and event and secret):
            raise ValueError(f"bad PCOMIRROR_SUBSCRIPTIONS entry {entry!r}: "
                             "id, event and secret are required (token may be empty)")
        out.append(SubscriptionSpec(sub_id, event, secret, token))
    return out


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
    # webhook subscriptions declared in the environment, re-applied on every `serve`
    subscriptions: list = field(default_factory=list)
    # serve /people/v2 without an API key — LAN-only escape hatch (DESIGN §8.4)
    allow_anonymous: bool = False
    # How many diagnostic events to keep. Every mutation and every upstream
    # failure writes one, so at this scale a thousand is weeks of history and
    # well under a megabyte. 0 switches recording off entirely.
    diagnostic_keep: int = 1000

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
        s.subscriptions = parse_subscriptions(e.get("PCOMIRROR_SUBSCRIPTIONS"))
        s.allow_anonymous = _truthy(e.get("PCOMIRROR_ALLOW_ANONYMOUS"))
        if e.get("PCOMIRROR_RATE_TARGET_RPS"):
            s.rate_target_rps = float(e["PCOMIRROR_RATE_TARGET_RPS"])
        if e.get("PCOMIRROR_DIAGNOSTIC_KEEP"):
            s.diagnostic_keep = max(0, int(e["PCOMIRROR_DIAGNOSTIC_KEEP"]))
        return s

    def auth_header(self) -> str:
        """HTTP Basic for the Personal Access Token (DESIGN §9.1)."""
        raw = f"{self.pco_app_id}:{self.pco_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()
