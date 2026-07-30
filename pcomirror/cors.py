"""Cross-origin access for the API plane (DESIGN §8.5) — configurable, off by default.

A browser is the one caller that cannot take the mirror up on its base-URL swap
unaided. `fetch('http://pcomirror.lan:8080/people/v2/people')` from a page served
anywhere else is refused *before it is sent*, whatever credential it holds, until
this service says which pages may read its answers. That set of origins is a fact
about somebody's deployment, so it is configuration: `PCOMIRROR_CORS_ORIGINS`.

**Off unless that names something, and off means silent** — no `Access-Control-*`
header on any response and `OPTIONS` left as the `405` it already was. A browser
reads that as "not for me", which is the truth on an install that never asked for
it, and the absence of a permissive default is the whole safety story here: a
mirror of a church's people database must not be readable by every page its
operator happens to visit.

Four things here are load-bearing and each is easy to get wrong:

* **A preflight carries no credential.** Browsers strip `Authorization` from the
  `OPTIONS` probe, so a preflight answered through `_authenticate` would `401` —
  and the browser then reports the *real* request as an opaque CORS failure with
  that 401 nowhere in sight. Preflight is answered before authentication and says
  nothing about data: only which methods and headers the policy allows.
* **Error responses need the headers too.** A `401`/`403`/`500` without
  `Access-Control-Allow-Origin` cannot be read by the page that caused it, so a
  developer sees "CORS error" where the server actually said "key lacks the
  'write' scope". The headers go on every response, not just the ones that worked.
* **`Vary: Origin` is not optional** once the answer depends on the origin. Any
  shared cache in front of this service would otherwise hand one origin's
  response — headers and all — to a page from another.
* **The operator console is out of scope, permanently.** `/` and `/admin/**`
  authenticate with a `SameSite=Strict` session cookie; CORS there would invite
  cross-site requests against a human's live session, and the console runs no
  JavaScript, so nothing legitimate would ever ask. That is not a setting.

The origin of a request is attacker-chosen text that ends up in a response
header, so every value taken from the request is either matched against a
validated pattern (`_ORIGIN_RE`) or sanitised (`_safe`) before it is echoed.
Without that, `Origin: https://a\\r\\nSet-Cookie: …` is a response-splitting hole
rather than a mismatch.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

#: Any origin. Spelled the way the header spells it, and — per the CORS spec —
#: incompatible with credentials, which `from_env` refuses rather than emits.
ANY = "*"

#: What a browser sends for a page with no origin of its own: a `file://` page, a
#: sandboxed iframe, some redirect chains. Opt-in only, and worth knowing that it
#: is not much of a boundary — anything can arrange to send it.
NULL = "null"

#: The methods the serving layer implements. Configuring one it does not would
#: advertise a method that then answers `405`, so the parser refuses it.
#: `OPTIONS` is not listed because it is not a method a caller may aim at data —
#: it is the preflight itself, and is always answered.
SUPPORTED_METHODS = ("GET", "POST", "PATCH", "DELETE")

DEFAULT_METHODS = SUPPORTED_METHODS
#: `Authorization` because that is where the API key goes, `Content-Type` because
#: a JSON:API body needs `application/vnd.api+json`, which is not a value the
#: browser's safelist covers.
DEFAULT_HEADERS = ("Authorization", "Content-Type")
#: Response headers a page may actually read. `X-Mirror-Source` is how a caller
#: tells a mirrored answer from a pass-through one, and `Location` is where a
#: `201` puts the record it just created — both useless if unreadable.
DEFAULT_EXPOSE = ("X-Mirror-Source", "Location")
#: Ten minutes of preflight caching: long enough that a busy page is not
#: preflighting every request, short enough that a policy change takes effect
#: while the operator is still watching.
DEFAULT_MAX_AGE = 600

#: Where a refusal says what it refused. Browsers word their own diagnostics from
#: the *absence* of a header, which is the clearest thing a page can be told; this
#: is for the operator holding curl, who would otherwise see a bare 200.
DIAGNOSTIC_HEADER = "X-Mirror-Cors"

#: Request headers a browser may send without asking, so refusing one would fail
#: a preflight over a header the caller never chose to add.
SAFELISTED_REQUEST_HEADERS = frozenset({
    "accept", "accept-language", "content-language", "content-type", "range",
})

#: A syntactically valid origin: scheme, host, optional port, nothing else. The
#: gate that keeps request-supplied text out of a response header — an `Origin`
#: that does not match this cannot be allowed, so it can never be echoed.
_ORIGIN_RE = re.compile(
    r"^https?://(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._~-]+)(?::[0-9]{1,5})?$")

#: The same, with a leading `*.` label — a configured wildcard for subdomains.
_PATTERN_RE = re.compile(
    r"^https?://\*(?:\.[A-Za-z0-9._~-]+)(?::[0-9]{1,5})?$")

_PRINTABLE = re.compile(r"[^\x20-\x7e]")


def _safe(value: str, limit: int = 120) -> str:
    """Request text made fit for a response header: printable ASCII, bounded."""
    return _PRINTABLE.sub("", value or "")[:limit]


def _normalise(origin: str) -> str:
    """A request's `Origin` in the form the configured list is held in.

    Scheme and host are case-insensitive and browsers already send them folded;
    doing it here as well costs nothing and means a hand-made request cannot dodge
    the list by shouting.
    """
    return (origin or "").strip().lower()


def _pattern(raw: str) -> str:
    """Validate and normalise one configured origin. Raises ValueError, loudly.

    A typo here is silent in the worst way — the browser reports a CORS failure
    and the server reports nothing at all — so `https://app.example.org/`, with
    the trailing slash a copy-paste from the address bar leaves behind, fails
    startup instead of never matching.
    """
    value = (raw or "").strip()
    if value == ANY or value.lower() == NULL:
        return value.lower()
    folded = value.lower()
    if _ORIGIN_RE.match(folded) or _PATTERN_RE.match(folded):
        return folded
    hint = "an origin is scheme://host[:port] — no path, no trailing slash"
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https"):
        hint = "an origin needs an http:// or https:// scheme"
    elif parts.path == "/" and not (parts.query or parts.fragment):
        # By far the most common way this goes wrong: the address bar puts the
        # slash there, and an origin does not have one.
        hint = "drop the trailing slash — an origin ends at the host or port"
    elif parts.path or parts.query or parts.fragment:
        hint = "an origin carries no path, query or fragment — drop everything after the host"
    elif "*" in value:
        hint = ("a wildcard may only replace the first label: https://*.example.org "
                "(which matches its subdomains, but not example.org itself)")
    raise ValueError(f"bad PCOMIRROR_CORS_ORIGINS entry {value!r}: {hint}")


def _matches(pattern: str, origin: str) -> bool:
    """Does one configured entry allow this (already validated) origin?"""
    if pattern == origin:
        return True
    scheme, _, hostport = pattern.partition("://")
    if not hostport.startswith("*."):
        return False
    o_scheme, _, o_hostport = origin.partition("://")
    if o_scheme != scheme:
        return False
    # `.example.org` — so `a.example.org` and `a.b.example.org` match while
    # `example.org` (a different origin the operator did not name) and
    # `evil-example.org` (a different site entirely) do not. The port rides along
    # in the suffix, because two ports are two origins.
    suffix = hostport[1:]
    return o_hostport.endswith(suffix) and len(o_hostport) > len(suffix)


@dataclass(frozen=True)
class Policy:
    """Who may read this service from a browser, and what they may send."""

    origins: tuple[str, ...] = ()
    methods: tuple[str, ...] = DEFAULT_METHODS
    headers: tuple[str, ...] = DEFAULT_HEADERS
    expose: tuple[str, ...] = DEFAULT_EXPOSE
    max_age: int = DEFAULT_MAX_AGE
    allow_credentials: bool = False

    @property
    def enabled(self) -> bool:
        """No origins is off, and off is silent — see the module docstring."""
        return bool(self.origins)

    @property
    def any_origin(self) -> bool:
        return ANY in self.origins

    @property
    def varies_by_origin(self) -> bool:
        """Whether the response depends on which origin asked.

        `*` alone does not: every origin gets the identical header, so a cache may
        share it. Anything else does, credentials included — with them on, the
        echo is the concrete origin even under `*`.
        """
        return not self.any_origin or self.allow_credentials

    def allows_origin(self, origin: str | None) -> bool:
        candidate = _normalise(origin or "")
        if not candidate:
            return False
        # Syntax first: an origin that is not one cannot be allowed, and so can
        # never reach a response header.
        if candidate != NULL and not _ORIGIN_RE.match(candidate):
            return False
        if self.any_origin:
            return True
        return any(_matches(p, candidate) for p in self.origins)

    def echo(self, origin: str) -> str:
        """The `Access-Control-Allow-Origin` value for an allowed origin.

        `*` unless the origin matters — credentials make it matter, and a browser
        rejects `*` outright when the request carries them.
        """
        if self.any_origin and not self.allow_credentials:
            return ANY
        return _normalise(origin)

    def allows_method(self, method: str) -> bool:
        return method.upper() in self.methods

    def allows_header(self, name: str) -> bool:
        lowered = name.strip().lower()
        if not lowered or lowered in SAFELISTED_REQUEST_HEADERS:
            return True
        return ANY in self.headers or lowered in {h.lower() for h in self.headers}

    def refused_headers(self, asked: str) -> list[str]:
        """Which of an `Access-Control-Request-Headers` list the policy will not take."""
        names = [n.strip() for n in (asked or "").split(",") if n.strip()]
        return [n for n in names if not self.allows_header(n)]


def from_env(env) -> Policy:
    """Build the policy from the environment. Raises ValueError on anything malformed.

    Loud rather than lenient, for the same reason `parse_subscriptions` is: a CORS
    policy that quietly did not mean what it said fails only in a browser
    somebody else is holding.
    """
    origins_raw = _items(env.get("PCOMIRROR_CORS_ORIGINS"))
    if not origins_raw:
        return Policy()
    origins = tuple(dict.fromkeys(_pattern(o) for o in origins_raw))
    if ANY in origins and len(origins) > 1:
        raise ValueError(
            "PCOMIRROR_CORS_ORIGINS lists '*' beside a specific origin, which is a "
            "contradiction: '*' already allows every origin. Keep one or the other.")

    methods = tuple(dict.fromkeys(m.upper() for m in _items(
        env.get("PCOMIRROR_CORS_METHODS")))) or DEFAULT_METHODS
    unsupported = [m for m in methods if m not in SUPPORTED_METHODS]
    if unsupported:
        raise ValueError(
            f"PCOMIRROR_CORS_METHODS names {', '.join(unsupported)}, which this service "
            f"does not serve — advertising it would promise a method that answers 405. "
            f"Choose from {', '.join(SUPPORTED_METHODS)} (OPTIONS is always answered).")

    headers = tuple(dict.fromkeys(_items(env.get("PCOMIRROR_CORS_HEADERS")))) or DEFAULT_HEADERS
    # Blank means "the default" for methods and request headers, where an empty
    # list would refuse every real request — but it means "nothing" here, because
    # exposing no response header is a coherent thing to want.
    expose_raw = env.get("PCOMIRROR_CORS_EXPOSE_HEADERS")
    expose = (tuple(dict.fromkeys(_items(expose_raw))) if expose_raw is not None
              else DEFAULT_EXPOSE)

    max_age = DEFAULT_MAX_AGE
    if (env.get("PCOMIRROR_CORS_MAX_AGE") or "").strip():
        try:
            max_age = int(env["PCOMIRROR_CORS_MAX_AGE"])
        except ValueError:
            raise ValueError("PCOMIRROR_CORS_MAX_AGE takes a number of seconds, got "
                             f"{env['PCOMIRROR_CORS_MAX_AGE']!r}") from None
        if max_age < 0:
            raise ValueError("PCOMIRROR_CORS_MAX_AGE cannot be negative (0 = do not "
                             "cache preflights)")

    credentials = _truthy(env.get("PCOMIRROR_CORS_ALLOW_CREDENTIALS"))
    if credentials:
        # Not a policy of ours — browsers themselves refuse the combination, so a
        # service that emitted it would fail every request while looking configured.
        for name, values in (("PCOMIRROR_CORS_ORIGINS", origins),
                             ("PCOMIRROR_CORS_HEADERS", headers),
                             ("PCOMIRROR_CORS_EXPOSE_HEADERS", expose)):
            if ANY in values:
                raise ValueError(
                    f"PCOMIRROR_CORS_ALLOW_CREDENTIALS cannot be combined with '*' in "
                    f"{name}: a browser rejects a wildcard on a credentialed request. "
                    f"Name the origins and headers you mean.")
    return Policy(origins=origins, methods=methods, headers=headers, expose=expose,
                  max_age=max_age, allow_credentials=credentials)


def _items(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _truthy(v: str | None) -> bool:
    return bool(v) and v.strip().lower() in ("1", "true", "yes", "on")


def add_vary(headers: dict, token: str = "Origin") -> None:
    """Merge a token into whatever `Vary` is already there, whatever its case.

    A pass-through relays PCO's own headers, `Vary` among them, and replacing that
    with `Vary: Origin` would tell a cache the response does not vary on the thing
    PCO just said it does.
    """
    for key in list(headers):
        if key.lower() == "vary":
            present = {t.strip().lower() for t in str(headers[key]).split(",")}
            if token.lower() not in present and ANY not in present:
                headers[key] = f"{headers[key]}, {token}"
            return
    headers["Vary"] = token


def attach(headers: dict, policy: Policy, origin: str | None) -> dict:
    """Put the actual-request CORS headers on a response, in place.

    Called for every response on a cross-origin-eligible path — including the
    failures, which are the ones a developer most needs to be able to read.
    """
    if not policy.enabled:
        return headers
    if policy.varies_by_origin:
        add_vary(headers)
    if not policy.allows_origin(origin):
        return headers
    headers["Access-Control-Allow-Origin"] = policy.echo(origin or "")
    if policy.allow_credentials:
        headers["Access-Control-Allow-Credentials"] = "true"
    if policy.expose:
        headers["Access-Control-Expose-Headers"] = ", ".join(policy.expose)
    return headers


def is_preflight(method: str, environ) -> bool:
    """A browser asking permission, rather than a caller asking for data."""
    return (method == "OPTIONS"
            and bool(environ.get("HTTP_ORIGIN"))
            and bool(environ.get("HTTP_ACCESS_CONTROL_REQUEST_METHOD")))


def preflight(policy: Policy, environ) -> tuple[int, dict, dict]:
    """Answer the `OPTIONS` probe. No authentication, and no data.

    Always `200`, and a refusal is spelled as the *missing* permission rather
    than as an error status: a browser fails a preflight that lacks the header it
    needs, and says which header that was — the most useful sentence anyone gets
    out of this exchange. A non-2xx would replace it with "does not have HTTP ok
    status", which names nothing. `DIAGNOSTIC_HEADER` carries the reason in words
    for whoever is holding curl instead of a browser.

    The body is an empty JSON object: browsers discard a preflight body, and
    keeping one means the response still carries the `Content-Length` every other
    response here carries.
    """
    origin = environ.get("HTTP_ORIGIN") or ""
    asked_method = (environ.get("HTTP_ACCESS_CONTROL_REQUEST_METHOD") or "").strip().upper()
    asked_headers = environ.get("HTTP_ACCESS_CONTROL_REQUEST_HEADERS") or ""
    # `no-store` keeps an intermediary from holding this answer: it is a policy
    # decision an operator may change at any moment. It does not shorten the
    # preflight's own cache lifetime, which is `Access-Control-Max-Age` below and
    # nothing else — the CORS preflight cache is not the HTTP cache.
    headers: dict[str, str] = {"Cache-Control": "no-store"}
    attach(headers, policy, origin)
    if "Access-Control-Allow-Origin" not in headers:
        headers[DIAGNOSTIC_HEADER] = (
            f"origin {_safe(origin)!r} is not in PCOMIRROR_CORS_ORIGINS"
            if policy.enabled else "cross-origin access is off (PCOMIRROR_CORS_ORIGINS is unset)")
        return 200, headers, {}
    # The allowed sets go out whole, whatever was asked for. When the ask is
    # outside them the mismatch *is* the refusal, and the browser names it exactly.
    headers["Access-Control-Allow-Methods"] = ", ".join(policy.methods)
    headers["Access-Control-Allow-Headers"] = ", ".join(policy.headers)
    headers["Access-Control-Max-Age"] = str(policy.max_age)
    refused = policy.refused_headers(asked_headers)
    if asked_method and not policy.allows_method(asked_method):
        headers[DIAGNOSTIC_HEADER] = (
            f"method {_safe(asked_method, 16)} is not in PCOMIRROR_CORS_METHODS")
    elif refused:
        headers[DIAGNOSTIC_HEADER] = (
            f"header{'s' if len(refused) > 1 else ''} "
            f"{', '.join(_safe(h, 40) for h in refused[:5])} "
            f"{'are' if len(refused) > 1 else 'is'} not in PCOMIRROR_CORS_HEADERS")
    return 200, headers, {}


def describe(policy: Policy) -> str:
    """One line for the `serve` log, so the policy in force is visible at startup."""
    if not policy.enabled:
        return "off"
    where = "any origin" if policy.any_origin else ", ".join(policy.origins)
    bits = [where, "methods " + "/".join(policy.methods)]
    if policy.allow_credentials:
        bits.append("credentials allowed")
    return "; ".join(bits)
