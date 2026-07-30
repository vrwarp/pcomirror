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

from . import (adminauth, adminstats, apikeys, cors, diagnostics, divergence,
               pcoevents, webhooks)
from .config import now_iso, parse_subscriptions

PATHS = ("/", "/admin/login", "/admin/logout", "/admin/password",
         "/admin/keys/create", "/admin/keys/revoke", "/admin/diagnostics",
         "/admin/cors", "/admin/cors/configure",
         "/admin/divergence", "/admin/divergence/download", "/admin/divergence/clear",
         "/admin/divergence/configure",
         "/admin/webhooks", "/admin/webhooks/add", "/admin/webhooks/remove",
         "/admin/webhooks/toggle", "/admin/webhooks/import", "/admin/webhooks/source",
         "/admin/webhooks/catalogue")


def handles(path: str) -> bool:
    return path in PATHS


def _form(body: bytes) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _form_multi(body: bytes) -> dict[str, list[str]]:
    """Every value, not just the first — a checkbox grid sends one name many times."""
    return urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)


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
    def __init__(self, db, settings, recorder=None, client=None):
        self.db, self.s = db, settings
        # Only for `last_failure` — the events themselves are read from the table,
        # so the page works the same whether or not recording is currently on.
        self.recorder = recorder
        # Only for the one button that asks Planning Center which events it can
        # send. Optional, so the page still renders without a configured client.
        self.client = client

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
        if path == "/admin/cors":
            return self._cors_page(qs, session)
        if path == "/admin/cors/configure":
            return self._cors_configure(method, body, session)
        if path == "/admin/divergence":
            return self._divergence_page(qs, session)
        if path == "/admin/divergence/download":
            return self._divergence_download()
        if path == "/admin/divergence/clear":
            return self._divergence_clear(method, body, session)
        if path == "/admin/divergence/configure":
            return self._divergence_configure(method, body, session)
        if path == "/admin/webhooks":
            return self._webhooks_page(qs, session)
        if path.startswith("/admin/webhooks/"):
            return self._webhooks_action(path, method, body, session)
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
        policy = self._cors_policy()
        if policy.any_origin and self.s.allow_anonymous:
            # Either alone is a documented choice; together they mean any page in
            # any browser that can route here reads the whole organization.
            banners += ("<p class='msg err'>PCOMIRROR_CORS_ORIGINS is <code>*</code> and "
                        "PCOMIRROR_ALLOW_ANONYMOUS is set: any web page loaded in any "
                        "browser that can reach this service may read every mirrored "
                        "person, with no credential at all.</p>")
        # Only the receivers that authenticate a delivery with *nothing*. One
        # with no secret but an unguessable token is a bearer credential, and
        # raising it here would teach an operator to scroll past this banner.
        open_tokens = st["webhooks"]["unprotected_tokens"]
        if open_tokens:
            banners += (f"<p class='msg err'>{len(open_tokens)} webhook receiver"
                        f"{'' if len(open_tokens) == 1 else 's'} have no authenticity "
                        f"secret and a guessable token, so nothing authenticates a "
                        f"delivery to them: {', '.join('…/' + E(t) for t in open_tokens)}. "
                        f"<a href=/admin/webhooks>Review them</a>.</p>")

        return 200, _headers(), _page("Admin", "".join([
            "<p class=sub>operator console</p>", banners,
            self._stats_section(st), self._diagnostics_section(),
            self._divergence_section(), self._keys_section(csrf),
            self._cors_section(), self._webhooks_section(st),
        ]), nav=f"<nav><form method=post action=/admin/logout style='display:inline'>"
                f"<button class=link type=submit>sign out</button></form> · "
                f"<a href=/admin/diagnostics>diagnostics</a> · "
                f"<a href=/admin/divergence>divergence</a> · "
                f"<a href=/admin/webhooks>webhooks</a> · "
                f"<a href=/admin/cors>browser access</a> · "
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

    # -- browser access (CORS) --------------------------------------------
    def _cors_state(self) -> dict:
        return cors.effective(self.db, self.s)

    def _cors_policy(self):
        return self._cors_state()["policy"]

    def _cors_section(self) -> str:
        """The policy in force on the dashboard, with the way in to change it."""
        state = self._cors_state()
        policy, source = state["policy"], state["source"]
        whence = ("set on this page" if source == "admin"
                  else "from <code>PCOMIRROR_CORS_ORIGINS</code>")
        if not policy.enabled:
            return f"""
<h2>Browser access</h2>
<p class=muted>Off ({whence}) — a page served from another origin cannot read
  <code>/people/v2</code>, and <code>OPTIONS</code> answers <code>405</code>.
  <a href=/admin/cors>Allow some origins</a>.</p>"""
        wildcards = [o for o in policy.origins if "*." in o]
        notes = []
        if policy.any_origin:
            notes.append("<span class=warn>Any page may use this API from a browser, with "
                         "any key it holds.</span>")
        if wildcards:
            example = wildcards[0].replace("*.", "app.", 1)
            notes.append(f"A wildcard entry matches subdomains only — "
                         f"<code>{E(wildcards[0])}</code> allows <code>{E(example)}</code>, "
                         f"not the bare domain.")
        return f"""
<h2>Browser access</h2>
<table><tr><th>setting<th>value</tr>{self._cors_rows(policy)}</table>
<p class=muted>{' '.join(notes)} Currently {whence}.
  <a href=/admin/cors>Change who may read this from a browser</a>. This console is
  never cross-origin readable whatever is set there — it authenticates with a
  session cookie, and an API key cannot reach it.</p>"""

    def _cors_rows(self, policy) -> str:
        rows = [
            ("origins", "any origin (<code>*</code>)" if policy.any_origin
             else "<br>".join(E(o) for o in policy.origins) or "—"),
            ("methods", E(", ".join(policy.methods))),
            ("request headers", E(", ".join(policy.headers)) or "—"),
            ("readable response headers", E(", ".join(policy.expose)) or "—"),
            ("preflight cached", f"{policy.max_age}s" if policy.max_age
             else "not cached (0s)"),
            ("credentials", "allowed" if policy.allow_credentials else "not allowed"),
        ]
        return "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)

    def _cors_page(self, qs, session, error: str = "", form: dict | None = None):
        """Who may read the API from a browser — from the page, not from a restart.

        The environment sets the default and this wins once anything is saved
        here, the same shape as the subscription list and the divergence rate: a
        policy fixed at 9pm has to survive the container coming back at 3am, and
        whoever can reach this page is rarely whoever can edit the container's
        environment.

        It takes effect on the next request. Preflights a browser has already
        cached do not, until `Access-Control-Max-Age` runs out — which is why that
        number is on this form rather than buried.
        """
        state = self._cors_state()
        policy, default = state["policy"], state["default"]
        csrf = E(session["csrf"])
        banners = ""
        if qs.get("saved"):
            banners += ("<p class='msg ok'>Saved — it applies to the next request. "
                        "A browser may hold an older preflight for up to "
                        f"{policy.max_age}s.</p>")
        if qs.get("handed_back"):
            banners += ("<p class='msg ok'>Handed back to the environment. "
                        "<code>PCOMIRROR_CORS_*</code> is in force again, from now — "
                        "no restart needed.</p>")
        if error:
            banners += f"<p class='msg err'>{E(error)}</p>"
        if state["stored_unreadable"]:
            banners += ("<p class='msg err'>The stored policy could not be read, so the "
                        "environment's is in force. Save this form to replace it.</p>")
        if policy.any_origin:
            banners += ("<p class='msg err'>Any origin is allowed. Every page in every "
                        "browser that can reach this service may use the API with any key "
                        "it holds" + (", and no key is needed at all while "
                        "PCOMIRROR_ALLOW_ANONYMOUS is set" if self.s.allow_anonymous
                        else "") + ".</p>")

        # The form shows what was submitted when it failed validation, so a typo in
        # one field does not cost the operator the other five.
        f = form or {
            "origins": ", ".join(policy.origins),
            "headers": ", ".join(policy.headers),
            "expose": ", ".join(policy.expose),
            "max_age": str(policy.max_age),
            "methods": list(policy.methods),
            "allow_credentials": policy.allow_credentials,
        }
        picked = {m.upper() for m in f.get("methods") or ()}
        checks = "".join(
            f"<label class=muted><input type=checkbox name=methods value={m}"
            f"{' checked' if m in picked else ''}> {m}</label>"
            for m in cors.SUPPORTED_METHODS)
        revert = ("" if state["source"] != "admin" else """
  <button type=submit name=reset value=1>Hand back to the environment</button>""")
        in_force = ("<p class=muted>In force: the policy set here.</p>"
                    if state["source"] == "admin" else
                    "<p class=muted>In force: the environment's policy. Saving anything "
                    "below takes it over until you hand it back.</p>")
        return 200, _headers(), _page("Browser access", f"""
<p class=sub>who may read this API from a browser (CORS)</p>{banners}
<h2>Now</h2>
<table><tr><th>setting<th>value</tr>{self._cors_rows(policy)}</table>
{in_force}
<h2>Change it</h2>
<form method=post action=/admin/cors/configure>
  <input type=hidden name=csrf value="{csrf}">
  <label for=o>Origins</label>
  <input id=o name=origins value="{E(f.get('origins', ''))}"
         placeholder="https://directory.yourchurch.org, https://*.yourchurch.org">
  <p class=muted>Comma-separated, exactly as a browser sends them —
    <code>scheme://host[:port]</code>, no trailing slash. A leading <code>*.</code>
    matches subdomains (<code>https://*.church.org</code> allows
    <code>app.church.org</code>, not <code>church.org</code>). <code>*</code> allows
    any origin. <code>null</code> covers <code>file://</code> pages and sandboxed
    iframes, and is barely a boundary. <b>Empty turns cross-origin access off</b>,
    which is not the same as handing it back to the environment.</p>
  <label>Methods</label>
  {checks}
  <p class=muted>Blank means all four. Writes still need a key holding the
    <code>write</code> scope, and still proxy to Planning Center with your PAT.</p>
  <label for=h>Request headers a page may send</label>
  <input id=h name=headers value="{E(f.get('headers', ''))}"
         placeholder="Authorization, Content-Type">
  <p class=muted>Blank means the default <code>Authorization, Content-Type</code> —
    the API key, and a JSON:API body. <code>*</code> allows any. The caching
    headers an HTTP library adds by itself
    (<code>{E(', '.join(cors.CLIENT_CACHE_REQUEST_HEADERS))}</code>) are allowed
    whatever is listed here, because a preflight refused over one of those fails a
    request the app never chose to decorate.</p>
  <label for=x>Response headers a page may read</label>
  <input id=x name=expose value="{E(f.get('expose', ''))}"
         placeholder="X-Mirror-Source, Location">
  <p class=muted>Empty exposes none. <code>X-Mirror-Source</code> tells a mirrored
    answer from a pass-through one; <code>Location</code> is where a
    <code>201</code> put the record it created.</p>
  <label for=m>Preflight cache, in seconds</label>
  <input id=m name=max_age type=number min=0 value="{E(f.get('max_age', ''))}">
  <p class=muted>How long a browser may reuse one permission check.
    <code>0</code> makes every request preflight, and makes a change here visible
    immediately.</p>
  <label class=muted><input type=checkbox name=allow_credentials
    {'checked' if f.get('allow_credentials') else ''}> Allow credentials —
    cookies and browser-remembered Basic logins on cross-origin requests</label>
  <p class=muted>Rarely wanted: an API key travels in <code>Authorization</code>,
    which needs none of this. Cannot be combined with <code>*</code>, because a
    browser rejects that pairing outright.</p>
  <p><button type=submit>Save</button>{revert}</p>
</form>
<h2>What this never reaches</h2>
<p class=muted>This console (<code>/</code> and <code>/admin/**</code>) and the
  webhook receiver are never cross-origin readable, whatever is set above. The
  console authenticates with a <code>SameSite=Strict</code> session cookie and runs
  no JavaScript, so nothing legitimate would ask; the receiver authenticates
  deliveries from Planning Center, which is not a browser.</p>
<p class=muted>CORS is a rule browsers keep — <code>curl</code> ignores it — so
  scoped API keys, not this page, are what stands between a caller and the data.
  A key shipped to browser JavaScript is readable by anyone who opens the page, so
  give a browser app one scoped to what it reads.</p>
<p class=muted>The environment default is
  <b>{E(cors.describe(default))}</b>.</p>""",
            nav="<nav><a href=/>back</a></nav>")

    def _cors_configure(self, method, body, session):
        if method != "POST":
            return _redirect("/admin/cors")
        form = _form_multi(body)
        one = {k: (v[0] if v else "") for k, v in form.items()}
        if not adminauth.check_csrf(session, one.get("csrf")):
            return self._cors_page({}, session, error="Session expired — try again.")
        if one.get("reset"):
            cors.configure(self.db, None)
            return _redirect("/admin/cors?handed_back=1")
        submitted = {
            "origins": one.get("origins", ""), "headers": one.get("headers", ""),
            "expose": one.get("expose", ""), "max_age": one.get("max_age", ""),
            "methods": form.get("methods", []),
            "allow_credentials": bool(one.get("allow_credentials")),
        }
        try:
            # The same validator the environment goes through, so the two cannot
            # come to mean different things by the same words.
            policy = cors.build(
                origins=submitted["origins"], methods=submitted["methods"],
                headers=submitted["headers"], expose=submitted["expose"],
                max_age=submitted["max_age"],
                allow_credentials=submitted["allow_credentials"],
                names=cors.FORM_NAMES)
        except ValueError as e:
            return self._cors_page({}, session, error=str(e), form=submitted)
        cors.configure(self.db, policy)
        return _redirect("/admin/cors?saved=1")

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
            # The parameters, not only the path: an ordering difference is
            # meaningless without the `order=` that produced it.
            query = json.loads((r["query"] if "query" in r.keys() else "") or "{}")
            shown_query = "".join(
                f"<div class=muted><code>{E(k)}={E(str(v))}</code></div>"
                for k, v in sorted(query.items()))
            rows.append(
                f"<tr><td>{_esc(r['at'])}</td><td class={klass}>{E(r['verdict'])}</td>"
                f"<td style='white-space:normal'>{_esc(r['path'])}{shown_query}</td>"
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
                 if rows else "<p class=muted>No subscriptions registered — "
                              "<a href=/admin/webhooks>add one</a>.</p>")
        return f"""
<h2>Webhooks</h2>{table}
<p class=muted>{w['deliveries']:,} deliveries · events by status: {statuses} ·
  {w['dead_letters']:,} dead-lettered · last received {_esc(w['last_received'], 'never')} ·
  <a href=/admin/webhooks>manage subscriptions</a></p>"""

    # -- webhooks ---------------------------------------------------------
    def _receiver_url(self, token: str) -> str:
        return f"{self.s.public_base_url.rstrip('/')}{self.s.webhook_path_prefix}/{token}"

    def _webhooks_action(self, path, method, body, session):
        if method != "POST":
            return _redirect("/admin/webhooks")
        form = _form(body)
        if not adminauth.check_csrf(session, form.get("csrf")):
            return self._webhooks_page({}, session, error="Session expired — try again.")
        tail = path[len("/admin/webhooks/"):]
        try:
            if tail == "add":
                return self._webhooks_add(body, session)
            if tail == "import":
                return self._webhooks_import(form, session)
            if tail == "remove":
                webhooks.delete_subscription(self.db, form.get("id", ""))
                webhooks.take_over(self.db)
                return _redirect("/admin/webhooks?saved=1")
            if tail == "toggle":
                webhooks.set_active(self.db, form.get("id", ""), form.get("to") == "on")
                webhooks.take_over(self.db)
                return _redirect("/admin/webhooks?saved=1")
            if tail == "source":
                return self._webhooks_source(form, session)
            if tail == "catalogue":
                return self._webhooks_catalogue(form, session)
        except ValueError as e:
            return self._webhooks_page({}, session, error=str(e))
        return _redirect("/admin/webhooks")

    def _webhooks_add(self, body, session):
        """One receiver, however many event types are ticked.

        Planning Center makes one subscription per event name, so this makes one
        row per ticked box — all pointing at the same receiver URL, which is what
        ticking a column of boxes in PCO's own console does.
        """
        multi = _form_multi(body)
        form = {k: v[0] for k, v in multi.items()}
        events = [e.strip() for e in multi.get("event", []) if e.strip()]
        typed = (form.get("other_event") or "").strip()
        if typed:
            events.append(typed)
        token = (form.get("url_token") or "").strip()
        secret = (form.get("secret") or "").strip()
        if not events:
            return self._webhooks_page({}, session, error="Choose at least one event.")
        # A blank secret is allowed, but only on purpose. An empty field is what a
        # half-finished paste looks like, and the failure it would cause is a
        # receiver that quietly accepts anything — so the box has to be ticked as
        # well, and the two together cannot happen by accident.
        if not secret and not form.get("unverified"):
            return self._webhooks_page(
                {}, session,
                error="Paste the authenticity secret Planning Center shows for this "
                      "webhook — or tick 'no secret' to accept deliveries unchecked.")
        if token and not webhooks.TOKEN_RE.match(token):
            return self._webhooks_page(
                {}, session, error="A receiver token is 8–64 characters of A–Z, a–z, 0–9, - or _.")
        # Settled before the loop, not by the first upsert: every event on this
        # receiver has to name the same token, and the ids derived from it would
        # otherwise disagree about what the receiver was called.
        token = token or webhooks.mint_token()
        chosen = sorted(set(events))
        given_id = (form.get("subscription_id") or "").strip()
        for event in chosen:
            resource, action = pcoevents.parse(event)
            # Planning Center issues an id per event, so there is no single id to
            # name a multi-event receiver by; a derived one is stable, is what a
            # re-tick updates rather than duplicates, and can be replaced with
            # PCO's own by importing a subscriptions list.
            sub_id = given_id if (given_id and len(chosen) == 1) else \
                f"{token}:{resource}.{action}"
            webhooks.upsert_subscription(self.db, sub_id, event, secret, token,
                                         managed="admin")
        webhooks.take_over(self.db)
        return _redirect(f"/admin/webhooks?saved=1&token={urllib.parse.quote(token)}")

    def _webhooks_import(self, form, session):
        """Paste the `PCOMIRROR_SUBSCRIPTIONS` value itself.

        The same parser the environment goes through, so a value that works in
        one place works in the other — and an operator moving off the environment
        variable can paste what they already have instead of retyping it a row at
        a time.
        """
        text = (form.get("subscriptions") or "").strip()
        if not text:
            return self._webhooks_page({}, session, error="Nothing to import.")
        try:
            specs = parse_subscriptions(text)
        except ValueError as e:
            return self._webhooks_page({}, session, error=str(e))
        for spec in specs:
            webhooks.upsert_subscription(self.db, spec.subscription_id, spec.event,
                                         spec.secret, spec.url_token or None, managed="admin")
        webhooks.take_over(self.db)
        return _redirect("/admin/webhooks?saved=1")

    def _webhooks_source(self, form, session):
        if form.get("to") == "environment":
            webhooks.hand_back(self.db)
            return _redirect("/admin/webhooks?handed_back=1")
        webhooks.take_over(self.db)
        return _redirect("/admin/webhooks?saved=1")

    def _webhooks_catalogue(self, form, session):
        if form.get("to") == "builtin":
            pcoevents.forget(self.db)
            return _redirect("/admin/webhooks?saved=1")
        if self.client is None:
            return self._webhooks_page(
                {}, session, error="No Planning Center client is configured.")
        try:
            pcoevents.refresh(self.db, self.client, now_iso())
        except Exception as e:  # noqa: BLE001
            return self._webhooks_page(
                {}, session,
                error=f"Could not read the event list from Planning Center: {e}")
        return _redirect("/admin/webhooks?saved=1")

    def _webhooks_page(self, qs, session, error: str = ""):
        csrf = E(session["csrf"])
        banners = ""
        if qs.get("saved"):
            banners += "<p class='msg ok'>Saved.</p>"
        if qs.get("handed_back"):
            banners += ("<p class='msg ok'>Handed back to the environment. "
                        "<code>PCOMIRROR_SUBSCRIPTIONS</code> is applied again on the "
                        "next start — nothing changes until then.</p>")
        token = (qs.get("token", [""])[0] or "").strip()
        if token:
            banners += (f"<div class='msg ok'><b>Receiver URL</b>"
                        f"<p class=secret>{E(self._receiver_url(token))}</p>"
                        "<p class=muted>Paste this into Planning Center as the webhook URL. "
                        "Every event above is delivered here.</p></div>")
        if error:
            banners += f"<p class='msg err'>{E(error)}</p>"
        return 200, _headers(), _page("Webhooks", "".join([
            "<p class=sub>which events Planning Center sends, and where</p>", banners,
            self._webhooks_source_section(csrf),
            self._webhooks_receivers_section(csrf),
            self._webhooks_add_section(csrf),
            self._webhooks_import_section(csrf),
            self._webhooks_catalogue_section(csrf),
        ]), nav="<nav><a href=/>back</a></nav>")

    def _webhooks_source_section(self, csrf: str) -> str:
        env_wins = webhooks.env_is_authoritative(self.db)
        declared = len(getattr(self.s, "subscriptions", []) or [])
        if env_wins:
            state = (f"<b>the environment</b> — <code>PCOMIRROR_SUBSCRIPTIONS</code> "
                     f"declares {declared} subscription(s) and is re-applied on every start"
                     if declared else
                     "<b>the environment</b> — <code>PCOMIRROR_SUBSCRIPTIONS</code> is "
                     "unset, so nothing is applied on start")
            action = ("<p class=muted>Saving anything below takes the list over: the "
                      "environment stops being applied, so a restart cannot undo what you "
                      "set here.</p>")
        else:
            state = "<b>this page</b> — <code>PCOMIRROR_SUBSCRIPTIONS</code> is not applied"
            action = f"""
<form method=post action=/admin/webhooks/source>
  <input type=hidden name=csrf value="{csrf}">
  <input type=hidden name=to value=environment>
  <button type=submit>Hand back to the environment
    ({declared} subscription(s) declared there)</button>
</form>
<p class=muted>Takes effect on the next start, and re-applies the environment's
  subscriptions over anything here with the same id.</p>"""
        return f"""
<h2>Who sets these</h2>
<p class=muted>Managed by {state}.</p>{action}"""

    def _webhooks_receivers_section(self, csrf: str) -> str:
        found = webhooks.receivers(self.db)
        if not found:
            return ("<h2>Receivers</h2><p class=muted>None. Planning Center has nowhere "
                    "to deliver to, so the mirror is running on its reconciliation sweeps "
                    "alone — correct, but minutes behind instead of seconds.</p>")
        blocks = []
        for rec in found:
            credential = webhooks.token_is_credential(rec["url_token"])
            rows = []
            for s in rec["subscriptions"]:
                verdict, why = pcoevents.handling(s["event_name"])
                toggle = "off" if s["active"] else "on"
                # Three states, not two: checked by a signature, checked by the
                # URL being unguessable, or not checked by anything.
                checked = ("<span class=muted>signature</span>"
                           if not webhooks.is_unverified(s) else
                           "<span class=muted>the URL</span>" if credential else
                           "<span class=warn>nothing</span>")
                rows.append(f"""
<tr><td>{E(s['event_name'])}</td>
    <td class={'muted' if verdict != 'recorded' else 'warn'}>{E(verdict)}</td>
    <td class=muted style='white-space:normal'>{E(why)}</td>
    <td>{checked}</td>
    <td>{'active' if s['active'] else '<span class=muted>paused</span>'}</td>
    <td>{_esc(s['last_event_at'], 'never')}</td>
    <td>{E(s['managed'])}</td>
    <td><form method=post action=/admin/webhooks/toggle style='display:inline'>
      <input type=hidden name=csrf value="{csrf}">
      <input type=hidden name=id value="{E(s['subscription_pco_id'])}">
      <input type=hidden name=to value="{toggle}">
      <button class=link type=submit>{'pause' if s['active'] else 'resume'}</button></form>
    · <form method=post action=/admin/webhooks/remove style='display:inline'>
      <input type=hidden name=csrf value="{csrf}">
      <input type=hidden name=id value="{E(s['subscription_pco_id'])}">
      <button class=link type=submit>remove</button></form></td></tr>""")
            # A receiver is only as checked as its least-checked subscription:
            # one unverified row moves the whole URL's authentication into the
            # token, whatever the others are signed with. Said on the receiver,
            # because the URL is the thing being handed out.
            open_here = [s for s in rec["subscriptions"]
                         if s["active"] and webhooks.is_unverified(s)]
            if not open_here:
                warning = ""
            elif credential:
                # Not an alarm. The URL is doing the authenticating, which is a
                # real security model — but it is one with rules of its own, and
                # they are the rules for a password, not for a path.
                warning = ("<p class=muted>No authenticity secret: this URL <b>is</b> the "
                           "credential. Its token is unguessable, so treat the whole URL "
                           "like a password — serve it over TLS, keep it out of anything "
                           "that logs or forwards URLs, and rotate it by adding a "
                           "subscription on a fresh token.</p>")
            else:
                warning = ("<p class='msg err'>No authenticity secret, and this token is "
                           "short or predictable enough to guess — so <b>nothing "
                           "authenticates</b> a delivery here. Paste the secret Planning "
                           "Center shows, or move these events onto a receiver with a "
                           "minted token (leave the token field blank below).</p>")
            blocks.append(f"""
<h3 style='font-size:.95rem;margin:1.5rem 0 .25rem'>{E(rec['url_token'])}</h3>
<p class=secret>{E(self._receiver_url(rec['url_token']))}</p>{warning}
<table><tr><th>event<th>handling<th>what happens<th>checked<th>state<th>last event
  <th>set by<th></tr>
{''.join(rows)}</table>""")
        return f"""
<h2>Receivers</h2>
<p class=muted>One URL per receiver, however many event types it carries. Planning
  Center makes a subscription per event name but lets them share a URL, and each
  carries its own authenticity secret — the receiver works out which subscription
  a delivery came from by the secret that signed it. A subscription with no secret
  is not checked at all.</p>
{''.join(blocks)}"""

    def _webhooks_add_section(self, csrf: str) -> str:
        cat = pcoevents.catalogue(self.db)
        existing = webhooks.receivers(self.db)
        options = "".join(
            f"<option value=\"{E(r['url_token'])}\">{E(r['url_token'])}</option>"
            for r in existing)
        grid = []
        for resource, actions in pcoevents.grouped(cat["events"]):
            boxes = "".join(
                f"<label class=muted style='display:inline-block;margin:0 1rem 0 0'>"
                f"<input type=checkbox name=event value=\"{E(name)}\"> {E(action)}</label>"
                for action, name in actions)
            verdict, _ = pcoevents.handling(f"people.v2.events.{resource}.created")
            mark = "" if verdict != "recorded" else " <span class=warn>(recorded only)</span>"
            grid.append(f"<tr><td>{E(resource)}{mark}</td><td style='white-space:normal'>"
                        f"{boxes}</td></tr>")
        return f"""
<h2>Add a receiver</h2>
<p class=muted>Tick the events, paste the secret Planning Center shows, and save.
  Leave the token blank to have one minted — the receiver URL is shown after
  saving, and can be chosen up front if you would rather register it at Planning
  Center first.</p>
<form method=post action=/admin/webhooks/add>
  <input type=hidden name=csrf value="{csrf}">
  <label for=tok>Receiver token</label>
  <input id=tok name=url_token list=existing-receivers placeholder="person-events-01"
         pattern="[A-Za-z0-9_-]{{8,64}}">
  <datalist id=existing-receivers>{options}</datalist>
  <label for=sec>Authenticity secret</label>
  <input id=sec name=secret placeholder="whsec_…">
  <p class=muted>If Planning Center issued a different secret per event, add those
    events one at a time — the receiver verifies against every secret registered
    for its URL, so mixed secrets on one URL work.</p>
  <label><input type=checkbox name=unverified value=1> No secret — authenticate
    on the URL alone</label>
  <p class=muted>For a sender that cannot sign. The authentication moves from the
    body's signature to the token in the URL, which is a bearer credential exactly
    as an API key is — so <b>leave the token blank</b> and a {webhooks.CREDENTIAL_MIN_LEN}+
    character random one is minted. Type a short or memorable token here and there
    is nothing left authenticating a delivery; the page will say so.</p>
  <label for=sid>Subscription id <span class=muted>(optional; used only when one
    event is ticked)</span></label>
  <input id=sid name=subscription_id placeholder="Planning Center's id, if you have it">
  <label>Events <span class=muted>({len(cat['events'])} known, {E(cat['source'])})</span></label>
  <table><tr><th>resource<th>actions</tr>{''.join(grid)}</table>
  <label for=other>Another event, by name</label>
  <input id=other name=other_event placeholder="people.v2.events.workflow_card.created">
  <p class=muted>Anything Planning Center will send is accepted here, listed above
    or not.</p>
  <p><button type=submit>Add</button></p>
</form>"""

    def _webhooks_import_section(self, csrf: str) -> str:
        return f"""
<h2>Paste a subscriptions list</h2>
<p class=muted>The <code>PCOMIRROR_SUBSCRIPTIONS</code> value itself, in either
  form — <code>id:event:token:secret</code> separated by commas, or the JSON list.
  Read by the same parser the environment goes through, so what works in one works
  in the other.</p>
<form method=post action=/admin/webhooks/import>
  <input type=hidden name=csrf value="{csrf}">
  <label for=subs>Subscriptions</label>
  <textarea id=subs name=subscriptions rows=4 required
    style="font:inherit;width:100%;padding:.45rem .55rem;background:var(--bg);
           color:var(--fg);border:1px solid var(--line);border-radius:4px"
    placeholder="sub_123:people.v2.events.person.updated:person-events-01:whsec_aaa"></textarea>
  <p><button type=submit>Import</button></p>
</form>"""

    def _webhooks_catalogue_section(self, csrf: str) -> str:
        cat = pcoevents.catalogue(self.db)
        recorded = [n for n in cat["events"] if pcoevents.handling(n)[0] == "recorded"]
        origin = ("built into this release" if cat["source"] == "built in" else
                  f"read from Planning Center at {_esc(cat['fetched_at'])}")
        coverage = ("Every one is applied to the mirror." if not recorded else
                    f"{len(cat['events']) - len(recorded)} are applied to the mirror; "
                    f"{len(recorded)} name a resource with no table here, so they would be "
                    f"captured and applied to nothing.")
        revert = "" if cat["source"] == "built in" else f"""
  <button type=submit name=to value=builtin>Back to the built-in list</button>"""
        return f"""
<h2>Event catalogue</h2>
<p class=muted>{len(cat['events'])} events, {origin}. {coverage} Asking Planning
  Center directly is the only list that stays right when they add one.</p>
<form method=post action=/admin/webhooks/catalogue>
  <input type=hidden name=csrf value="{csrf}">
  <button type=submit>Refresh from Planning Center</button>{revert}
</form>"""
