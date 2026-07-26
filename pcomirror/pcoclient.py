"""PCO HTTP client (DESIGN §5.1).

Every request carries the PAT auth, the required User-Agent, and the pinned
X-PCO-API-Version, passes through the shared limiter, feeds the rate headers back
to it, and honours 429 Retry-After. The `transport` is injectable so tests drive a
fake PCO in-process with zero network.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    def send(self, method: str, url: str, headers: dict, body: bytes | None) -> Response: ...


class UrllibTransport:
    """Production transport over urllib (respects HTTP(S)_PROXY env)."""

    def __init__(self, ca_bundle: str | None = None):
        self._ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None

    def send(self, method: str, url: str, headers: dict, body: bytes | None) -> Response:
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                return Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers or {}), e.read())


class PcoClient:
    def __init__(self, settings, limiter, transport: Transport):
        self.s = settings
        self.limiter = limiter
        self.transport = transport

    def _url(self, path: str, params: dict | None) -> str:
        url = self.s.pco_base_url.rstrip("/") + "/" + path.lstrip("/")
        if params:
            flat = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(flat, safe="")
        return url

    def request(self, method: str, path: str, params: dict | None = None,
                json_body: Any = None, priority: str = "reconcile",
                max_attempts: int = 6) -> Response:
        headers = {
            "Authorization": self.s.auth_header(),
            "User-Agent": self.s.user_agent,
            "X-PCO-API-Version": self.s.api_version,
            "Accept": "application/json",
        }
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        url = self._url(path, params)

        idempotent = method == "GET"
        for attempt in range(max_attempts):
            self.limiter.acquire(priority)
            try:
                resp = self.transport.send(method, url, headers, body)
            except Exception:
                # A reset socket, a DNS blip or a timeout is exactly as transient as
                # a 503 and arrives as neither a status nor a body. Long operations
                # feel this: a backfill or a per-parent walk is hundreds of requests
                # over minutes, and one dropped connection used to abort the whole
                # thing.
                #
                # Retried for GET only. A write that reached PCO but whose response
                # was lost is indistinguishable from one that never arrived, and
                # replaying it would create a second record — so a write surfaces
                # the error and lets the caller decide.
                if not idempotent or attempt == max_attempts - 1:
                    raise
                time.sleep(min(30.0, 2 ** attempt))
                continue
            self.limiter.on_response(resp.headers)
            if resp.status == 429:
                ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                self.limiter.on_429(float(ra) if ra else None)
                time.sleep(min(60.0, 2 ** attempt))
                continue
            if resp.status in (500, 502, 503, 504):
                time.sleep(min(30.0, 2 ** attempt))
                continue
            return resp
        return resp  # last response (still an error) after retries

    def get(self, path: str, params: dict | None = None, priority: str = "reconcile") -> Response:
        return self.request("GET", path, params=params, priority=priority)
