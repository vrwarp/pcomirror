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

from . import diagnostics

#: Default for `PcoClient.request(api_version=…)`: send the configured People pin.
#: Distinct from `None`, which means *send no version header at all* — a
#: different instruction, and one a plain default could not express.
KEEP_API_VERSION = "\0keep"


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    #: Filled in by `PcoClient.request` once the exchange settles. A transport
    #: builds a Response without them, so they default rather than being required.
    request_id: str | None = None
    duration_ms: int | None = None
    attempts: int = 1

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
    def __init__(self, settings, limiter, transport: Transport, recorder=None):
        self.s = settings
        self.limiter = limiter
        self.transport = transport
        # This is the only layer that sees *why* an exchange went wrong — the
        # exception, the status, how many sends it took, and PCO's request id.
        # By the time a failure reaches a caller it is a status code with none of
        # that attached, which is exactly the shape of the outage nobody could
        # explain afterwards.
        self.recorder = recorder or diagnostics.NullRecorder()

    def _url(self, path: str, params: dict | None, base: str | None = None) -> str:
        url = (base or self.s.pco_base_url).rstrip("/") + "/" + path.lstrip("/")
        if params:
            flat = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(flat, safe="")
        return url

    def request(self, method: str, path: str, params: dict | None = None,
                json_body: Any = None, priority: str = "reconcile",
                max_attempts: int = 6, record_outcome: bool = True,
                max_wait: float | None = None, base: str | None = None,
                api_version: str | None = KEEP_API_VERSION) -> Response:
        """`record_outcome=False` when the caller logs the final result itself.

        `base` overrides the People base URL for the one caller that needs a
        different app — the webhooks app, which is where the subscription and
        available-event endpoints live. Everything else about the exchange (auth,
        the pinned API version, the shared limiter, the retry rules) is identical,
        which is exactly why it is a parameter and not a second client.

        Intermediate attempts are recorded either way — a retry that eventually
        worked is invisible from outside this method, and it is exactly the
        evidence that explains the request *next* to it timing out. The final
        outcome is different: `_write_through` knows things this layer does not
        (which record, whether the mirror then accepted it), so when it is going
        to write a richer line, this one would only be a worse duplicate.
        """
        headers = {
            "Authorization": self.s.auth_header(),
            "User-Agent": self.s.user_agent,
            "Accept": "application/json",
        }
        # A PCO version string names a dated revision *of one product*, so the
        # People pin is meaningless — and rejected — anywhere else. `None` omits
        # the header and lets PCO answer at the organization's own version.
        version = self.s.api_version if api_version is KEEP_API_VERSION else api_version
        if version:
            headers["X-PCO-API-Version"] = version
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        url = self._url(path, params, base)

        idempotent = method == "GET"
        target = diagnostics.redact_target(path, params)
        for attempt in range(max_attempts):
            self.limiter.acquire(priority, max_wait)
            started = time.monotonic()
            try:
                resp = self.transport.send(method, url, headers, body)
            except Exception as exc:
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
                last = not idempotent or attempt == max_attempts - 1
                if not last or record_outcome:
                    self.recorder.upstream_attempt(
                        method, target, attempt=attempt, error=exc, will_retry=not last,
                        duration_ms=int((time.monotonic() - started) * 1000))
                if last:
                    raise
                time.sleep(min(30.0, 2 ** attempt))
                continue
            elapsed = int((time.monotonic() - started) * 1000)
            self.limiter.on_response(resp.headers)
            if resp.status == 429:
                # Safe for a write as much as a read, and the only failure here
                # that is: a limiter refuses *before* the request reaches
                # anything that could apply it, so the record cannot already
                # exist and a replay cannot make a second one.
                ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                self.limiter.on_429(float(ra) if ra else None)
                self.recorder.upstream_attempt(
                    method, target, status=429, attempt=attempt, duration_ms=elapsed,
                    headers=resp.headers, will_retry=attempt < max_attempts - 1)
                time.sleep(min(60.0, 2 ** attempt))
                continue
            if resp.status in (500, 502, 503, 504):
                # The same rule as the dropped socket above, for the same reason.
                # A 502 or 504 comes from whatever sits *in front of* PCO and is
                # sent after the request reached it, so the write may well have
                # landed and only the answer went missing — a replay creating a
                # second Person is at least as likely as one repairing a failure.
                # Only the guard above used to check this, which left the status
                # path quietly replaying every write it was written to protect.
                will_retry = idempotent and attempt < max_attempts - 1
                if will_retry or record_outcome:
                    self.recorder.upstream_attempt(
                        method, target, status=resp.status, attempt=attempt,
                        duration_ms=elapsed, headers=resp.headers, will_retry=will_retry)
                if not idempotent:
                    return self._settle(resp, elapsed, attempt)
                time.sleep(min(30.0, 2 ** attempt))
                continue
            if not resp.ok and record_outcome:
                # A 4xx is PCO declining, not PCO failing: never retried, and worth
                # one line because "it was rejected" and "it never arrived" are the
                # two stories a caller cannot tell apart from a failed write.
                self.recorder.upstream_attempt(
                    method, target, status=resp.status, attempt=attempt,
                    duration_ms=elapsed, headers=resp.headers)
            return self._settle(resp, elapsed, attempt)
        return resp  # last response (still an error) after retries

    @staticmethod
    def _settle(resp: Response, elapsed: int, attempt: int) -> Response:
        """Stamp what the exchange cost onto the response that came out of it.

        Carried on the response rather than on the client: the WSGI server is
        threaded, and "the last request" is not something a shared object can
        answer correctly for two callers at once.
        """
        resp.request_id = diagnostics.pick_headers(resp.headers).get("x-request-id")
        resp.duration_ms = elapsed
        resp.attempts = attempt + 1
        return resp

    def get(self, path: str, params: dict | None = None, priority: str = "reconcile",
            max_wait: float | None = None, base: str | None = None) -> Response:
        return self.request("GET", path, params=params, priority=priority,
                            max_wait=max_wait, base=base)
