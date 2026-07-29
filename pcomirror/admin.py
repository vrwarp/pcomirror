"""Operator page at `/` — login, password change, API keys, cache statistics.

Server-rendered HTML with no JavaScript and no external assets, so it works
behind any reverse proxy and adds no supply chain. It has its own session auth
and is deliberately *outside* the api_key plane: an API key is for machines, this
is for the human running the mirror.
"""
from __future__ import annotations

import html
import json
import urllib.parse

from . import adminauth, adminstats, apikeys, diagnostics, divergence

PATHS = ("/", "/admin/login", "/admin/logout", "/admin/password",
         "/admin/keys/create", "/admin/keys/revoke", "/admin/diagnostics",
         "/admin/divergence", "/admin/divergence/download", "/admin/divergence/clear",
         "/admin/divergence/configure")


def handles(path: str) -> bool:
    return path in PATHS


def _form(body: bytes) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _cookie(environ) -> str | None:
    raw = environ.get("HTTP_COOKIE", "")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == adminauth.COOKIE:
            return value or None
    return None


def _is_https(environ) -> bool:
    forwarded = (environ.get("HTTP_X_FORWARDED_PROTO") or "").split(",")[0].strip().lower()
    return forwarded == "https" or environ.get("wsgi.url_scheme") == "https"


def _set_cookie(token: str, environ, max_age: int | None = None) -> str:
    bits = [f"{adminauth.COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
    if _is_https(environ):
        bits.append("Secure")
    bits.append(f"Max-Age={adminauth.SESSION_HOURS * 3600 if max_age is None else max_age}")
    return "; ".join(bits)


E = html.escape


def _esc(value, dash: str = "—") -> str:
    return E(str(value)) if value not in (None, "") else dash


# -- chrome ---------------------------------------------------------------
_CSS = """
:root{color-scheme:light dark;--fg:#111;--muted:#666;--line:#d8d8d8;--bg:#fff;
--accent:#0b5;--warn:#b40;--card:#f7f7f7}
@media (prefers-color-scheme:dark){:root{--fg:#e8e8e8;--muted:#999;--line:#333;
--bg:#161616;--card:#1f1f1f}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1.25rem;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
main{max-width:1000px;margin:0 auto}
h1{font-size:1.25rem;margin:0 0 .25rem} h2{font-size:1rem;margin:2rem 0 .5rem}
.sub{color:var(--muted);margin:0 0 1.5rem}
table{width:100%;border-collapse:collapse;margin:.5rem 0;overflow-x:auto;display:block}
th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid var(--line);
white-space:nowrap}
th{color:var(--muted);font-weight:600}
.cards{display:flex;flex-wrap:wrap;gap:.75rem;margin:.5rem 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:.6rem .9rem;min-width:9rem}
.card b{display:block;font-size:1.3rem} .card span{color:var(--muted);font-size:.8rem}
form{margin:.5rem 0} label{display:block;margin:.6rem 0 .2rem;color:var(--muted)}
input,select{font:inherit;padding:.45rem .55rem;background:var(--bg);color:var(--fg);
border:1px solid var(--line);border-radius:4px;min-width:16rem;max-width:100%}
button{font:inherit;padding:.45rem .9rem;border:1px solid var(--line);border-radius:4px;
background:var(--card);color:var(--fg);cursor:pointer}
button.link{border:0;background:none;padding:0;text-decoration:underline}
.msg{padding:.6rem .8rem;border-radius:4px;border:1px solid var(--line);margin:1rem 0}
.err{border-color:var(--warn);color:var(--warn)}
.ok{border-color:var(--accent)}
.secret{word-break:break-all;white-space:normal;background:var(--card);
padding:.6rem;border-radius:4px;border:1px solid var(--accent)}
.warn{color:var(--warn)} .muted{color:var(--muted)}
nav{float:right;color:var(--muted)}
"""


def _page(title: str, body: str, nav: str = "") -> bytes:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)} · pcomirror</title><style>{_CSS}</style></head>"
            f"<body><main>{nav}<h1>pcomirror</h1>{body}</main></body></html>").encode()


def _headers(extra: dict | None = None) -> dict:
    h = {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        # No scripts at all; styles are inline in the document.
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                   "form-action 'self'; frame-ancestors 'none'",
    }
    h.update(extra or {})
    return h


def _redirect(to: str, extra: dict | None = None):
    headers = _headers({"Location": to})
    headers.update(extra or {})
    return 303, headers, b""


class AdminApp:
    def __init__(self, db, settings, recorder=None):
        self.db, self.s = db, settings
        # Only for `last_failure` — the events themselves are read from the table,
        # so the page works the same whether or not recording is currently on.
        self.recorder = recorder

    # -- entry ------------------------------------------------------------
    def handle(self, method, path, qs, body, environ):
        token = _cookie(environ)
        session = adminauth.session_for(self.db, token)

        if path == "/admin/login":
            return self._login(method, body, environ)
        if path == "/admin/logout":
            adminauth.destroy_session(self.db, token)
            return _redirect("/", {"Set-Cookie": _set_cookie("", environ, max_age=0)})

        if session is None:
            if path == "/":
                return self._login_page(qs)
            return _redirect("/")

        # A bootstrap login may do exactly one thing: set a real password.
        if session["must_change_password"] and path != "/admin/password":
            return _redirect("/admin/password")

        if path == "/admin/password":
            return self._password(method, body, session, token, environ)
        if path == "/admin/keys/create":
            return self._create_key(method, body, session)
        if path == "/admin/keys/revoke":
            return self._revoke_key(method, body, session)
        if path == "/admin/diagnostics":
            return self._diagnostics_page(qs)
        if path == "/admin/divergence":
            return self._divergence_page(qs, session)
        if path == "/admin/divergence/download":
            return self._divergence_download()
        if path == "/admin/divergence/clear":
            return self._divergence_clear(method, body, session)
        if path == "/admin/divergence/configure":
            return self._divergence_configure(method, body, session)
        return self._dashboard(session, qs)

    # -- login ------------------------------------------------------------
    def _login_page(self, qs, error: str = ""):
        locked = adminauth.locked_out()
        if locked:
            error = f"Too many failed attempts. Try again in {locked}s."
        configured = adminauth.is_configured(self.db)
        hint = ("" if configured else
                "<p class=muted>No password set yet — sign in with your "
                "<code>PCO_SECRET</code>, then choose a new one.</p>")
        if not configured and not (self.s.pco_secret or "").strip():
            hint = ("<p class='msg err'>No password is set and <code>PCO_SECRET</code> "
                    "is empty, so there is no way to sign in. Set <code>PCO_SECRET</code> "
                    "and restart.</p>")
        msg = f"<p class='msg err'>{E(error)}</p>" if error else ""
        return 200, _headers(), _page("Sign in", f"""
<p class=sub>operator console</p>{msg}{hint}
<form method=post action=/admin/login>
  <label for=p>Password</label>
  <input id=p name=password type=password autocomplete=current-password required autofocus>
  <p><button type=submit>Sign in</button></p>
</form>""")

    def _login(self, method, body, environ):
        if method != "POST":
            return _redirect("/")
        password = _form(body).get("password", "")
        ok, bootstrap = adminauth.verify(self.db, self.s, password)
        if not ok:
            return self._login_page({}, "Incorrect password.")
        token = adminauth.create_session(self.db, must_change=bootstrap)
        target = "/admin/password" if bootstrap else "/"
        return _redirect(target, {"Set-Cookie": _set_cookie(token, environ)})

    # -- password ---------------------------------------------------------
    def _password(self, method, body, session, token, environ):
        forced = bool(session["must_change_password"])
        if method == "POST":
            form = _form(body)
            if not adminauth.check_csrf(session, form.get("csrf")):
                return self._password_page(session, "Session expired — try again.")
            new, confirm = form.get("password", ""), form.get("confirm", "")
            if new != confirm:
                return self._password_page(session, "The two passwords do not match.")
            if not forced:
                ok, _ = adminauth.verify(self.db, self.s, form.get("current", ""))
                if not ok:
                    return self._password_page(session, "Current password is incorrect.")
            try:
                adminauth.set_password(self.db, new)
            except ValueError as e:
                return self._password_page(session, str(e))
            # Every existing session — including any an attacker holds — dies here.
            adminauth.destroy_all_sessions(self.db)
            fresh = adminauth.create_session(self.db, must_change=False)
            return _redirect("/?changed=1", {"Set-Cookie": _set_cookie(fresh, environ)})
        return self._password_page(session)

    def _password_page(self, session, error: str = ""):
        forced = bool(session["must_change_password"])
        msg = f"<p class='msg err'>{E(error)}</p>" if error else ""
        intro = ("<p class='msg'>You signed in with <code>PCO_SECRET</code>. Choose a "
                 "password before continuing — <code>PCO_SECRET</code> will stop working "
                 "as a login once you do.</p>" if forced else "")
        current = "" if forced else """
  <label for=c>Current password</label>
  <input id=c name=current type=password autocomplete=current-password required>"""
        return 200, _headers(), _page("Set password", f"""
<p class=sub>choose a password</p>{intro}{msg}
<form method=post action=/admin/password>
  <input type=hidden name=csrf value="{E(session['csrf'])}">{current}
  <label for=p>New password</label>
  <input id=p name=password type=password autocomplete=new-password required
         minlength={adminauth.MIN_PASSWORD_LEN}>
  <label for=c2>Confirm</label>
  <input id=c2 name=confirm type=password autocomplete=new-password required>
  <p class=muted>At least {adminauth.MIN_PASSWORD_LEN} characters.</p>
  <p><button type=submit>Save password</button></p>
</form>""", nav="" if forced else "<nav><a href=/>back</a></nav>")

    # -- api keys ---------------------------------------------------------
    def _create_key(self, method, body, session):
        if method != "POST":
            return _redirect("/")
        form = _form(body)
        if not adminauth.check_csrf(session, form.get("csrf")):
            return self._dashboard(session, {}, error="Session expired — try again.")
        name = (form.get("name") or "").strip()
        if not name:
            return self._dashboard(session, {}, error="A key needs a name.")
        scopes = ",".join(s for s in (
            form.get("read") or "", form.get("write") or "", form.get("passthrough") or "") if s)
        if not scopes:
            return self._dashboard(session, {}, error="Select at least one scope.")
        key = apikeys.create(self.db, name, scopes)
        return self._dashboard(session, {}, new_key=key, new_key_name=name)

    def _revoke_key(self, method, body, session):
        if method != "POST":
            return _redirect("/")
        form = _form(body)
        if not adminauth.check_csrf(session, form.get("csrf")):
            return self._dashboard(session, {}, error="Session expired — try again.")
        prefix = (form.get("prefix") or "").strip()
        if apikeys.revoke(self.db, prefix):
            return self._dashboard(session, {}, notice=f"Revoked key {prefix}.")
        return self._dashboard(session, {}, error=f"No active key with prefix {prefix}.")

    # -- dashboard --------------------------------------------------------
    def _dashboard(self, session, qs, error="", notice="", new_key="", new_key_name=""):
        st = adminstats.collect(self.db)
        csrf = E(session["csrf"])
        banners = ""
        if qs.get("changed"):
            banners += "<p class='msg ok'>Password updated.</p>"
        if notice:
            banners += f"<p class='msg ok'>{E(notice)}</p>"
        if error:
            banners += f"<p class='msg err'>{E(error)}</p>"
        if new_key:
            banners += (f"<div class='msg ok'><b>New key for {E(new_key_name)}</b>"
                        f"<p class=secret>{E(new_key)}</p>"
                        "<p class=muted>Copy it now — only its hash is stored, so this is "
                        "the one time it can be shown.</p></div>")
        if self.s.allow_anonymous:
            banners += ("<p class='msg err'>PCOMIRROR_ALLOW_ANONYMOUS is set: "
                        "<code>/people/v2</code> is served without an API key.</p>")

        return 200, _headers(), _page("Admin", "".join([
            "<p class=sub>operator console</p>", banners,
            self._stats_section(st), self._diagnostics_section(),
            self._divergence_section(), self._keys_section(csrf),
            self._webhooks_section(st),
        ]), nav=f"<nav><form method=post action=/admin/logout style='display:inline'>"
                f"<button class=link type=submit>sign out</button></form> · "
                f"<a href=/admin/diagnostics>diagnostics</a> · "
                f"<a href=/admin/divergence>divergence</a> · "
                f"<a href=/admin/password>password</a></nav>")

    def _stats_section(self, st) -> str:
        q, storage = st["queues"], st["storage"]
        cards = [
            ("mirrored records", f"{st['total_live']:,}"),
            ("tombstoned", f"{st['total_tombstoned']:,}"),
            ("on disk", adminstats.human_bytes(storage["total_bytes"])),
            ("webhook events", f"{st['webhooks']['total']:,}"),
            ("hydration queue", f"{q['hydration_pending']:,}"),
            ("active API keys", f"{st['api_keys']:,}"),
        ]
        cards_html = "".join(f"<div class=card><b>{E(v)}</b><span>{E(k)}</span></div>"
                             for k, v in cards)
        rows = []
        for r in st["resources"]:
            drift = "—" if r["drift"] is None else (
                f"<span class=warn>{r['drift']:+d}</span>" if r["drift"] else "0")
            errors = (f"<span class=warn>{r['errors']}</span>" if r["errors"]
                      else str(r["errors"]))
            rows.append(
                f"<tr><td>{E(r['endpoint'])}</td><td>{r['live']:,}</td>"
                f"<td>{r['tombstoned']:,}</td><td>{_esc(r['oldest_synced'])}</td>"
                f"<td>{_esc(r['backfilled_at'], 'never')}</td>"
                f"<td>{_esc(r['last_sweep'], 'never')}</td>"
                f"<td>{drift}</td><td>{errors}</td></tr>")
        return f"""
<h2>Cache</h2>
<div class=cards>{cards_html}</div>
<p class=muted>{E(storage['path'])} · pass-through cache
  {q['passthrough_cached']:,} rows ({q['passthrough_expired']:,} expired)</p>
<table><tr><th>resource<th>live<th>tombstoned<th>oldest sync<th>backfilled
  <th>last sweep<th>drift<th>errors</tr>{''.join(rows)}</table>
<p class=muted>Drift is mirror count minus PCO's reported total at the last probe;
  anything non-zero means a sweep is due.</p>"""

    def _keys_section(self, csrf: str) -> str:
        rows = []
        for k in apikeys.listing(self.db):
            state = ("<span class=muted>revoked</span>" if k["disabled_at"]
                     else f"""<form method=post action=/admin/keys/revoke style='display:inline'>
                     <input type=hidden name=csrf value="{csrf}">
                     <input type=hidden name=prefix value="{E(k['prefix'])}">
                     <button class=link type=submit>revoke</button></form>""")
            rows.append(f"<tr><td>{E(k['prefix'])}</td><td>{_esc(k['name'])}</td>"
                        f"<td>{E(k['scopes'])}</td><td>{_esc(k['last_used_at'], 'never')}</td>"
                        f"<td>{state}</td></tr>")
        table = (f"<table><tr><th>prefix<th>name<th>scopes<th>last used<th></tr>"
                 f"{''.join(rows)}</table>" if rows else "<p class=muted>No keys yet.</p>")
        return f"""
<h2>API keys</h2>{table}
<form method=post action=/admin/keys/create>
  <input type=hidden name=csrf value="{csrf}">
  <label for=n>New key for</label>
  <input id=n name=name placeholder="dashboard" required>
  <label>Scopes</label>
  <label class=muted><input type=checkbox name=read value="read:*" checked> read:* —
    every mirrored collection</label>
  <label class=muted><input type=checkbox name=write value="write"> write —
    POST/PATCH/DELETE through to PCO</label>
  <label class=muted><input type=checkbox name=passthrough value="passthrough">
    passthrough — spend the server's PCO credential on cache misses</label>
  <p><button type=submit>Create key</button></p>
</form>"""

    # -- diagnostics ------------------------------------------------------
    #: Filters offered on the log page. `write.` first because a mutation is the
    #: thing an operator comes here about — the reads are context around it.
    _FILTERS = (("", "everything"), ("write.", "writes"), ("upstream.", "upstream failures"))

    def _severity_class(self, severity: str) -> str:
        return {"error": "warn", "warning": "warn"}.get(severity, "muted")

    def _event_rows(self, events) -> str:
        rows = []
        for e in events:
            when = _esc(e["at"])
            what = f"{_esc(e['method'], '')} {_esc(e['target'], '')}".strip()
            status = _esc(e["status"])
            timing = f"{e['duration_ms']} ms" if e["duration_ms"] is not None else "—"
            if (e["attempts"] or 1) > 1:
                timing += f" · {e['attempts']} sends"
            detail = _esc(e["detail"], "")
            if e["error_type"]:
                detail += (f" <span class=warn>{E(e['error_type'])}"
                           f"{': ' + E(e['error_detail']) if e['error_detail'] else ''}</span>")
            # The one field Planning Center's own support can look up.
            req = (f"<code>{E(e['pco_request_id'])}</code>" if e["pco_request_id"] else "—")
            rows.append(
                f"<tr><td>{when}</td>"
                f"<td class={self._severity_class(e['severity'])}>{_esc(e['kind'])}</td>"
                f"<td>{E(what)}</td><td>{status}</td><td>{E(timing)}</td>"
                f"<td>{_esc(e['pco_id'])}</td><td>{req}</td>"
                f"<td style='white-space:normal'>{detail}</td></tr>")
        return "".join(rows)

    _EVENT_HEAD = ("<tr><th>when<th>kind<th>request<th>status<th>timing"
                   "<th>record<th>pco request id<th>what happened</tr>")

    def _diagnostics_section(self) -> str:
        """The dashboard's summary — enough to know whether to click through."""
        s = diagnostics.summary(self.db)
        if not s["total"]:
            return ("<h2>Diagnostics</h2><p class=muted>Nothing recorded yet. Every write "
                    "and every upstream failure lands here — an empty log means neither has "
                    "happened since the mirror started. "
                    "<a href=/admin/diagnostics>Open the log</a>.</p>")
        cards = [
            ("writes", f"{s['writes']:,}"),
            ("errors", f"{s['errors']:,}"),
            ("warnings", f"{s['warnings']:,}"),
            ("indeterminate writes", f"{s['indeterminate']:,}"),
        ]
        cards_html = "".join(
            f"<div class=card><b class={'warn' if v != '0' and k != 'writes' else ''}>{E(v)}</b>"
            f"<span>{E(k)}</span></div>" for k, v in cards)
        recent = diagnostics.recent(self.db, limit=8)
        alarm = ("<p class='msg err'>An indeterminate write is one Planning Center may or may "
                 "not have applied — the response was lost, or the mirror could not record it. "
                 "Each one needs checking upstream by hand.</p>" if s["indeterminate"] else "")
        return f"""
<h2>Diagnostics</h2>
<div class=cards>{cards_html}</div>{alarm}
<table>{self._EVENT_HEAD}{self._event_rows(recent)}</table>
<p class=muted>{s['total']:,} events from {_esc(s['oldest'])} to {_esc(s['newest'])} ·
  <a href=/admin/diagnostics>full log</a></p>"""

    def _diagnostics_page(self, qs):
        """The whole log, with the two filters worth having.

        Server-rendered like everything else here: no script, so it works with
        the page's `default-src 'none'` policy and through any proxy.
        """
        kind = (qs.get("kind", [""])[0] or "").strip()
        if kind not in {k for k, _ in self._FILTERS}:
            kind = ""
        severity = (qs.get("severity", [""])[0] or "").strip()
        if severity not in ("", "info", "warning", "error"):
            severity = ""
        try:
            limit = int(qs.get("limit", ["200"])[0])
        except ValueError:
            limit = 200

        events = diagnostics.recent(self.db, limit=limit, kind_prefix=kind, severity=severity)
        s = diagnostics.summary(self.db)

        def tab(value, label, param, current):
            query = urllib.parse.urlencode(
                {k: v for k, v in
                 {"kind": kind, "severity": severity, param: value}.items() if v})
            href = f"/admin/diagnostics{'?' + query if query else ''}"
            shown = f"<b>{E(label)}</b>" if value == current else E(label)
            return f"<a href='{href}'>{shown}</a>"

        kinds = " · ".join(tab(v, label, "kind", kind) for v, label in self._FILTERS)
        sevs = " · ".join(tab(v, label, "severity", severity) for v, label in
                          (("", "any"), ("error", "errors"), ("warning", "warnings")))
        body = (f"<table>{self._EVENT_HEAD}{self._event_rows(events)}</table>"
                if events else "<p class=muted>No events match this filter.</p>")
        # A log that is quietly short is worse than no log, so say so.
        failure = getattr(self.recorder, "last_failure", None)
        incomplete = (f"<p class='msg err'>Recording has failed at least once "
                      f"({E(failure)}), so this log is incomplete.</p>" if failure else "")
        return 200, _headers(), _page("Diagnostics", f"""
<p class=sub>what was asked of Planning Center, and what came back</p>{incomplete}
<p class=muted>show: {kinds} &nbsp;|&nbsp; severity: {sevs}</p>
{body}
<p class=muted>Showing {len(events):,} of {s['total']:,} kept. Bodies, headers and
  query values are never recorded — only which filters were in play. Raise
  <code>PCOMIRROR_DIAGNOSTIC_KEEP</code> to keep more history.</p>""",
            nav="<nav><a href=/>back</a></nav>")


    # -- divergence -------------------------------------------------------
    def _divergence_section(self) -> str:
        """The dashboard's line on it — mostly, whether it is even running."""
        s = divergence.summary(self.db)
        rate = divergence.effective(self.db, self.s)
        if not rate["per_minute"]:
            return ("<h2>Divergence</h2><p class=muted>Off. Have the mirror re-ask "
                    "Planning Center about the reads it serves and record anything "
                    "they disagree about — <a href=/admin/divergence>turn it on</a>. "
                    "It spends PCO budget, so it is meant to be switched on while "
                    "chasing something.</p>")
        cards = [("divergences", f"{s['divergence']:,}"),
                 ("stale (self-healing)", f"{s['staleness']:,}"),
                 ("requests kept", f"{s['samples']:,}"),
                 ("checked", f"{s['checked']:,}")]
        cards_html = "".join(
            f"<div class=card><b class={'warn' if k == 'divergences' and v != '0' else ''}>"
            f"{E(v)}</b><span>{E(k)}</span></div>" for k, v in cards)
        alarm = ("<p class='msg err'>A <b>divergence</b> is a difference at the same "
                 "<code>updated_at</code>: the sweep filters that record out and the "
                 "monotonic writer would refuse it, so nothing converges on it on its "
                 "own. Stale rows need no action.</p>" if s["divergence"] else "")
        return f"""
<h2>Divergence</h2>
<div class=cards>{cards_html}</div>{alarm}
<p class=muted>checking {rate['per_minute']}/min · last checked
  {_esc(s['last_checked'], 'never')} · {s['samples']:,} distinct requests kept
  across {s['shapes']:,} shapes, from {s['requests_seen']:,} reads ·
  <a href=/admin/divergence>full log</a></p>"""

    _REPORT_HEAD = ("<tr><th>when<th>verdict<th>request<th>status"
                    "<th>differences<th>pco request id</tr>")

    def _report_rows(self, reports) -> str:
        rows = []
        for r in reports:
            diffs = json.loads(r["differences"] or "[]")
            shown = "".join(
                f"<div><code>{E(d['pointer'])}</code> "
                f"mirror=<b>{E(json.dumps(d['mirror']))}</b> "
                f"pco=<b>{E(json.dumps(d['pco']))}</b>"
                f"{' — ' + E(d['note']) if d.get('note') else ''}</div>"
                for d in diffs[:6])
            if len(diffs) > 6:
                shown += f"<div class=muted>…and {len(diffs) - 6} more</div>"
            klass = "warn" if r["verdict"] == "divergence" else "muted"
            rows.append(
                f"<tr><td>{_esc(r['at'])}</td><td class={klass}>{E(r['verdict'])}</td>"
                f"<td>{_esc(r['path'])}</td>"
                f"<td>{_esc(r['mirror_status'])} vs {_esc(r['pco_status'])}</td>"
                f"<td style='white-space:normal'>{shown}</td>"
                f"<td>{('<code>' + E(r['pco_request_id']) + '</code>') if r['pco_request_id'] else '—'}"
                f"</td></tr>")
        return "".join(rows)

    def _divergence_page(self, qs, session, error=""):
        verdict = (qs.get("verdict", [""])[0] or "").strip()
        if verdict not in ("", "divergence", "staleness"):
            verdict = ""
        reports = divergence.recent(self.db, limit=200, verdict=verdict)
        s = divergence.summary(self.db)
        csrf = E(session["csrf"])
        tabs = " · ".join(
            f"<a href='/admin/divergence{('?verdict=' + v) if v else ''}'>"
            f"{('<b>' + E(label) + '</b>') if v == verdict else E(label)}</a>"
            for v, label in (("", "everything"), ("divergence", "divergences"),
                             ("staleness", "stale")))
        body = (f"<table>{self._REPORT_HEAD}{self._report_rows(reports)}</table>"
                if reports else "<p class=muted>Nothing recorded — the mirror has "
                                "agreed with Planning Center on everything checked.</p>")
        banners = ""
        if qs.get("saved"):
            banners += "<p class='msg ok'>Saved.</p>"
        if error:
            banners += f"<p class='msg err'>{E(error)}</p>"
        return 200, _headers(), _page("Divergence", f"""
<p class=sub>where the mirror and Planning Center disagree</p>{banners}
{self._divergence_controls(csrf)}
<h2>Log</h2>
<p class=muted>show: {tabs}</p>
{body}
<p class=muted>Showing {len(reports):,} of {s['total']:,} kept. Every request
  checked is one a caller really made — nothing here is synthesised. Values are
  pseudonymised, consistent per organization and reversible by nobody, so this is
  safe to send on; record ids and structure are real.</p>
<form method=get action=/admin/divergence/download style='display:inline'>
  <button type=submit>Download the log</button></form>
<form method=post action=/admin/divergence/clear style='display:inline'>
  <input type=hidden name=csrf value="{csrf}">
  <button type=submit>Clear it</button></form>""",
            nav="<nav><a href=/>back</a></nav>")


    def _divergence_controls(self, csrf: str) -> str:
        """On, off, and how hard — from the page, not from a restart.

        The environment sets the default; this override wins and persists,
        because the person who wants this on at 9pm while chasing something is
        rarely the person who can edit the container's environment and restart
        it. `0` is off, and is spelled that way rather than as a separate switch
        so there is one number to reason about instead of two settings that can
        disagree.
        """
        rate = divergence.effective(self.db, self.s)
        state = (f"<b>on</b>, {rate['per_minute']} checks per minute"
                 if rate["per_minute"] else "<b>off</b>")
        source = ("set here" if rate["source"] == "admin"
                  else "from <code>PCOMIRROR_SHADOW_PER_MINUTE</code>")
        revert = ("" if rate["source"] != "admin" else f"""
  <button type=submit name=reset value=1>Back to the environment default
    ({rate['default']}/min)</button>""")
        return f"""
<h2>Checking</h2>
<p class=muted>Currently {state} — {source}. Each check re-asks Planning Center
  about one read the mirror has served, and spends that request from the same
  budget everything else here shares. <code>0</code> turns it off.</p>
<form method=post action=/admin/divergence/configure>
  <input type=hidden name=csrf value="{csrf}">
  <label for=r>Checks per minute</label>
  <input id=r name=per_minute type=number min=0 max={divergence.MAX_PER_MINUTE}
         value="{rate['per_minute']}" required>
  <p><button type=submit>Save</button>{revert}</p>
</form>"""

    def _divergence_configure(self, method, body, session):
        if method != "POST":
            return _redirect("/admin/divergence")
        form = _form(body)
        if not adminauth.check_csrf(session, form.get("csrf")):
            return self._divergence_page({}, session, error="Session expired — try again.")
        if form.get("reset"):
            divergence.configure(self.db, None)
            return _redirect("/admin/divergence?saved=1")
        try:
            chosen = int((form.get("per_minute") or "").strip())
        except ValueError:
            return self._divergence_page(
                {}, session, error="Checks per minute has to be a whole number.")
        if chosen < 0 or chosen > divergence.MAX_PER_MINUTE:
            return self._divergence_page(
                {}, session,
                error=f"Choose between 0 and {divergence.MAX_PER_MINUTE} checks per minute.")
        divergence.configure(self.db, chosen)
        return _redirect("/admin/divergence?saved=1")

    def _divergence_download(self):
        payload = divergence.export(self.db)
        return 200, _headers({
            "Content-Type": "application/json; charset=utf-8",
            "Content-Disposition": 'attachment; filename="pcomirror-divergence.json"',
        }), payload

    def _divergence_clear(self, method, body, session):
        if method != "POST":
            return _redirect("/admin/divergence")
        if not adminauth.check_csrf(session, _form(body).get("csrf")):
            return self._divergence_page({}, session)
        divergence.clear(self.db)
        return _redirect("/admin/divergence")

    def _webhooks_section(self, st) -> str:
        w = st["webhooks"]
        statuses = ", ".join(f"{E(k)} {v:,}" for k, v in sorted(w["by_status"].items())) or "none"
        rows = "".join(
            f"<tr><td>{E(s['event_name'])}</td><td>…/{E(s['url_token'])}</td>"
            f"<td>{'active' if s['active'] else 'inactive'}</td>"
            f"<td>{_esc(s['last_event_at'], 'never')}</td></tr>"
            for s in w["subscriptions"])
        table = (f"<table><tr><th>event<th>receiver<th>state<th>last event</tr>{rows}</table>"
                 if rows else "<p class=muted>No subscriptions registered.</p>")
        return f"""
<h2>Webhooks</h2>{table}
<p class=muted>{w['deliveries']:,} deliveries · events by status: {statuses} ·
  {w['dead_letters']:,} dead-lettered · last received {_esc(w['last_received'], 'never')}</p>"""
