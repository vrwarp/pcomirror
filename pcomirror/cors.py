"""Cross-origin access for the API plane (DESIGN §8.5) — configurable, off by default.

A browser is the one caller that cannot take the mirror up on its base-URL swap
unaided. `fetch('http://pcomirror.lan:8080/people/v2/people')` from a page served
anywhere else is refused *before it is sent*, whatever credential it holds, until
this service says which pages may read its answers. That set of origins is a fact
about somebody's deployment, so it is configuration: `PCOMIRROR_CORS_ORIGINS`
sets the default and **`/admin/cors` overrides it, persistently**.

The page winning is the same shape as the subscription list and the divergence
rate, for the same reason: whoever can reach the console when a browser app
stops working is rarely whoever can edit the container's environment and restart
it, and re-applying the environment on the next start would silently undo the fix
at the hour nobody is watching. `build` is the single validator both go through,
so the two cannot come to mean different things by the same words. An override
takes effect on the next request; a preflight a browser already cached outlives
it until `Access-Control-Max-Age` runs out, which is why that number is on the
form.

**Off unless one of the two names an origin, and off means silent** — no
`Access-Control-*` header on any response and `OPTIONS` left as the `405` it
already was. A browser reads that as "not for me", which is the truth on an
install that never asked for it, and the absence of a permissive default is the
whole safety story here: a mirror of a church's people database must not be
readable by every page its operator happens to visit.

Five things here are load-bearing and each is easy to get wrong:

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
* **A header value is latin-1, prose or not.** Every sentence this module writes
  goes out as a header, and a character outside latin-1 does not arrive mangled —
  it raises inside the WSGI server midway through the header block and truncates
  the response. One em dash in a refusal's reason therefore took the
  `Access-Control-Allow-Origin` beside it off a *different* refusal's response,
  turning "this one header is not allowed" into "no origin is allowed" with the
  explanation for neither. `_explain` is the only way a reason reaches a header.
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

import json
import re
import urllib.parse
from dataclasses import asdict, dataclass

#: Any origin. Spelled the way the header spells it, and — per the CORS spec —
#: incompatible with credentials, which `build` refuses rather than emits.
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

#: A refusal names both places the policy can be set, rather than guessing which
#: one is in force: the page overrides the environment, and a message that named
#: the wrong one would send an operator to edit a variable nothing is reading.
_WHERE = {
    "origins": "set the allowed origins at /admin/cors or in PCOMIRROR_CORS_ORIGINS",
    "methods": "set the allowed methods at /admin/cors or in PCOMIRROR_CORS_METHODS",
    "headers": "set the allowed request headers at /admin/cors or in PCOMIRROR_CORS_HEADERS",
}

#: Request headers a browser may send without asking, so refusing one would fail
#: a preflight over a header the caller never chose to add.
SAFELISTED_REQUEST_HEADERS = frozenset({
    "accept", "accept-language", "content-language", "content-type", "range",
})

#: Headers an HTTP client library adds on its own account: three to step around
#: the browser's cache, two to revalidate what it cached itself. `axios`'s cache
#: interceptor sends `Cache-Control`, `Pragma` and `Expires` on *every* request
#: under its default `cacheTakeover`, and the conditional pair whenever an entry
#: goes stale. None is on the browser's safelist, so every one of them is
#: preflighted; none carries authority a read could act on.
#:
#: They are allowed whatever the policy lists, for the reason
#: `SAFELISTED_REQUEST_HEADERS` exists: refusing one fails the preflight over a
#: header the application never chose to send, and an operator naming the headers
#: *their* app uses is not thinking about `Pragma`. A tuple rather than a set
#: because they are advertised verbatim: string hashing is seeded per process, so a
#: set would spell `Access-Control-Allow-Headers` differently after every restart.
CLIENT_CACHE_REQUEST_HEADERS = (
    "Cache-Control", "Pragma", "Expires", "If-None-Match", "If-Modified-Since")

#: What `allows_header` will not refuse, whatever the policy says.
_NEVER_REFUSED = SAFELISTED_REQUEST_HEADERS | {
    h.lower() for h in CLIENT_CACHE_REQUEST_HEADERS}

#: Where an operator's policy is kept once they save one on the page. Absent means
#: "whatever the environment said", which is what a fresh install and a
#: `docker run -e …` both expect.
OVERRIDE_KEY = "cors_policy"

#: One validator, two vocabularies. It is the same rule whether the value came
#: from the environment or from a form, but "bad PCOMIRROR_CORS_ORIGINS entry" is
#: the wrong sentence to show somebody typing in a box, and "Origins" is the wrong
#: one to show somebody reading a container log.
ENV_NAMES = {
    "origins": "PCOMIRROR_CORS_ORIGINS", "methods": "PCOMIRROR_CORS_METHODS",
    "headers": "PCOMIRROR_CORS_HEADERS", "expose": "PCOMIRROR_CORS_EXPOSE_HEADERS",
    "max_age": "PCOMIRROR_CORS_MAX_AGE",
    "allow_credentials": "PCOMIRROR_CORS_ALLOW_CREDENTIALS",
}
FORM_NAMES = {
    "origins": "Origins", "methods": "Methods", "headers": "Request headers",
    "expose": "Readable response headers", "max_age": "Preflight cache",
    "allow_credentials": "Credentials",
}

#: A syntactically valid origin: scheme, host, optional port, nothing else. The
#: gate that keeps request-supplied text out of a response header — an `Origin`
#: that does not match this cannot be allowed, so it can never be echoed.
_ORIGIN_RE = re.compile(
    r"^https?://(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._~-]+)(?::[0-9]{1,5})?$")

#: The same, with a leading `*.` label — a configured wildcard for subdomains.
_PATTERN_RE = re.compile(
    r"^https?://\*(?:\.[A-Za-z0-9._~-]+)(?::[0-9]{1,5})?$")

_PRINTABLE = re.compile(r"[^\x20-\x7e]")

#: Room for a whole refusal sentence, since `_explain` bounds the assembled
#: message rather than only the request-supplied words inside it.
_DIAGNOSTIC_LIMIT = 300


def _safe(value: str, limit: int = 120) -> str:
    """Request text made fit for a response header: printable ASCII, bounded."""
    return _PRINTABLE.sub("", value or "")[:limit]


def _explain(headers: dict, reason: str) -> None:
    """Say why the policy refused, in something a header can actually carry.

    A header value goes on the wire as latin-1 (PEP 3333), and a character
    outside it is not a header that arrives mangled — it is `UnicodeEncodeError`
    raised *inside the server* as the header block is written, which truncates the
    response to whatever preceded it. An em dash in this sentence therefore
    deleted the `Access-Control-Allow-Origin` beside it, and the browser reported
    the one thing the sentence existed to explain: no permission header at all. A
    refusal to allow one header became a refusal of every origin, and the reason
    was unreadable in both.

    So the whole assembled message goes through `_safe`, not just the words taken
    from the request. Writing the prose in ASCII is the fix; this is what keeps it
    fixed the next time somebody reaches for an em dash.
    """
    headers[DIAGNOSTIC_HEADER] = _safe(reason, _DIAGNOSTIC_LIMIT)


def _normalise(origin: str) -> str:
    """A request's `Origin` in the form the configured list is held in.

    Scheme and host are case-insensitive and browsers already send them folded;
    doing it here as well costs nothing and means a hand-made request cannot dodge
    the list by shouting.
    """
    return (origin or "").strip().lower()


def _pattern(raw: str, name: str = "PCOMIRROR_CORS_ORIGINS") -> str:
    """Validate and normalise one configured origin. Raises ValueError, loudly.

    A typo here is silent in the worst way — the browser reports a CORS failure
    and the server reports nothing at all — so `https://app.example.org/`, with
    the trailing slash a copy-paste from the address bar leaves behind, is
    refused at the point it is set instead of never matching.
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
    raise ValueError(f"bad {name} entry {value!r}: {hint}")


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
        if not lowered or lowered in _NEVER_REFUSED:
            return True
        return ANY in self.headers or lowered in {h.lower() for h in self.headers}

    @property
    def advertised_headers(self) -> tuple[str, ...]:
        """What `Access-Control-Allow-Headers` says.

        The browser decides from this string and nothing else. Leniency inside
        `allows_header` that does not reach it is leniency the one party acting on
        the answer never hears about: the preflight is refused by the browser,
        before the request, with the server's own diagnostic saying the header was
        fine. So everything that will not be refused is named here.

        The browser's safelist is left out on purpose — those headers need no
        permission, which is what makes them the safelist, and listing them would
        pad the value a browser caches with the rest of the preflight.
        """
        if ANY in self.headers:
            return (ANY,)
        named = {h.lower() for h in self.headers}
        return self.headers + tuple(
            h for h in CLIENT_CACHE_REQUEST_HEADERS if h.lower() not in named)

    def refused_headers(self, asked: str) -> list[str]:
        """Which of an `Access-Control-Request-Headers` list the policy will not take."""
        names = [n.strip() for n in (asked or "").split(",") if n.strip()]
        return [n for n in names if not self.allows_header(n)]


def build(origins=None, methods=None, headers=None, expose=None, max_age=None,
          allow_credentials=False, names=None) -> Policy:
    """Validate one set of values into a policy. Raises ValueError on anything malformed.

    **The one validator.** The environment and the operator page both come through
    here, so the two cannot drift into meaning different things by the same words —
    the same reason `divergence/rules.py` was lifted out of the golden test. Only
    the vocabulary differs, by `names`: a container log wants
    `PCOMIRROR_CORS_ORIGINS`, a form wants "Origins".

    Loud rather than lenient, for the same reason `parse_subscriptions` is: a
    policy that quietly did not mean what it said fails only in a browser somebody
    else is holding.

    Every list argument takes either the comma-separated string the environment
    uses or an already-split sequence, and `None` means "not given, use the
    default" — distinct from an empty value, which `expose` honours as "none".
    """
    names = names or ENV_NAMES
    origins_raw = _listish(origins)
    if not origins_raw:
        return Policy()
    allowed = tuple(dict.fromkeys(_pattern(o, names["origins"]) for o in origins_raw))
    if ANY in allowed and len(allowed) > 1:
        raise ValueError(
            f"{names['origins']} lists '*' beside a specific origin, which is a "
            f"contradiction: '*' already allows every origin. Keep one or the other.")

    verbs = tuple(dict.fromkeys(m.upper() for m in _listish(methods))) or DEFAULT_METHODS
    unsupported = [m for m in verbs if m not in SUPPORTED_METHODS]
    if unsupported:
        raise ValueError(
            f"{names['methods']} names {', '.join(unsupported)}, which this service "
            f"does not serve — advertising it would promise a method that answers 405. "
            f"Choose from {', '.join(SUPPORTED_METHODS)} (OPTIONS is always answered).")

    # Blank means "the default" for methods and request headers, where an empty
    # list would refuse every real request — but it means "nothing" for `expose`,
    # because exposing no response header is a coherent thing to want.
    sends = tuple(dict.fromkeys(_listish(headers))) or DEFAULT_HEADERS
    reads = tuple(dict.fromkeys(_listish(expose))) if expose is not None else DEFAULT_EXPOSE

    seconds = DEFAULT_MAX_AGE
    if max_age is not None and str(max_age).strip():
        try:
            seconds = int(str(max_age).strip())
        except ValueError:
            raise ValueError(f"{names['max_age']} takes a number of seconds, got "
                             f"{max_age!r}") from None
        if seconds < 0:
            raise ValueError(f"{names['max_age']} cannot be negative (0 = do not "
                             f"cache preflights)")

    credentials = (allow_credentials if isinstance(allow_credentials, bool)
                   else _truthy(allow_credentials))
    if credentials:
        # Not a policy of ours — browsers themselves refuse the combination, so a
        # service that emitted it would fail every request while looking configured.
        for field, values in (("origins", allowed), ("headers", sends), ("expose", reads)):
            if ANY in values:
                raise ValueError(
                    f"{names['allow_credentials']} cannot be combined with '*' in "
                    f"{names[field]}: a browser rejects a wildcard on a credentialed "
                    f"request. Name the origins and headers you mean.")
    return Policy(origins=allowed, methods=verbs, headers=sends, expose=reads,
                  max_age=seconds, allow_credentials=credentials)


def from_env(env) -> Policy:
    """The environment's policy — the default the operator page may override."""
    return build(origins=env.get("PCOMIRROR_CORS_ORIGINS"),
                 methods=env.get("PCOMIRROR_CORS_METHODS"),
                 headers=env.get("PCOMIRROR_CORS_HEADERS"),
                 expose=env.get("PCOMIRROR_CORS_EXPOSE_HEADERS"),
                 max_age=env.get("PCOMIRROR_CORS_MAX_AGE"),
                 allow_credentials=env.get("PCOMIRROR_CORS_ALLOW_CREDENTIALS"),
                 names=ENV_NAMES)


def _listish(value) -> list[str]:
    """A comma-separated string, or an already-split sequence, as a clean list."""
    if value is None:
        return []
    if isinstance(value, str):
        return _items(value)
    return [str(v).strip() for v in value if str(v).strip()]


def _items(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _truthy(v) -> bool:
    return bool(v) and str(v).strip().lower() in ("1", "true", "yes", "on")


# -- the stored policy: the page wins, the environment is the default ---------
def effective(db, settings) -> dict:
    """The policy in force, and where it came from.

    The environment sets the default and an operator may override it from
    `/admin/cors`; the override wins and persists, the same shape as the
    divergence rate and the subscription list. A restart re-applies the
    environment to *nothing* while an override is stored, so a policy fixed at 9pm
    survives the container coming back at 3am.
    """
    default = getattr(settings, "cors", None) or Policy()
    held = db.get_meta(OVERRIDE_KEY)
    if held is None:
        return {"policy": default, "source": "environment", "default": default,
                "stored_unreadable": False}
    try:
        stored = decode(held)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        # Only reachable by editing the row by hand — `configure` stores what
        # `build` validated. Fall back to the environment, which is the narrower
        # of the two by default, and say so rather than serving a policy nobody
        # can read.
        return {"policy": default, "source": "environment", "default": default,
                "stored_unreadable": True}
    return {"policy": stored, "source": "admin", "default": default,
            "stored_unreadable": False}


def configure(db, policy: Policy | None) -> None:
    """Store an operator's policy, or clear it back to the environment's."""
    if policy is None:
        db.execute("DELETE FROM mirror_meta WHERE key=?", (OVERRIDE_KEY,))
    else:
        db.set_meta(OVERRIDE_KEY, encode(policy))


def encode(policy: Policy) -> str:
    return json.dumps(asdict(policy), sort_keys=True)


def decode(raw: str) -> Policy:
    """Re-validate a stored policy through `build`.

    Not `Policy(**json)`: the row is text in a database, and an origin that
    reached it another way must not become one this service echoes.
    """
    held = json.loads(raw)
    if not isinstance(held, dict):
        raise ValueError("a stored CORS policy must be an object")
    return build(origins=held.get("origins"), methods=held.get("methods"),
                 headers=held.get("headers"), expose=held.get("expose", []),
                 max_age=held.get("max_age"),
                 allow_credentials=bool(held.get("allow_credentials")),
                 names=FORM_NAMES)


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
        _explain(headers,
                 f"origin {_safe(origin)!r} is not allowed: {_WHERE['origins']}"
                 if policy.enabled else
                 f"cross-origin access is off: {_WHERE['origins']}")
        return 200, headers, {}
    # The allowed sets go out whole, whatever was asked for. When the ask is
    # outside them the mismatch *is* the refusal, and the browser names it exactly.
    headers["Access-Control-Allow-Methods"] = ", ".join(policy.methods)
    headers["Access-Control-Allow-Headers"] = ", ".join(policy.advertised_headers)
    headers["Access-Control-Max-Age"] = str(policy.max_age)
    refused = policy.refused_headers(asked_headers)
    if asked_method and not policy.allows_method(asked_method):
        _explain(headers,
                 f"method {_safe(asked_method, 16)} is not allowed: {_WHERE['methods']}")
    elif refused:
        _explain(headers,
                 f"header{'s' if len(refused) > 1 else ''} "
                 f"{', '.join(_safe(h, 40) for h in refused[:5])} "
                 f"{'are' if len(refused) > 1 else 'is'} not allowed: {_WHERE['headers']}")
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
