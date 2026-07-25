"""Operator page at `/` — login, password change, API keys, cache statistics.

Server-rendered HTML with no JavaScript and no external assets, so it works
behind any reverse proxy and adds no supply chain. It has its own session auth
and is deliberately *outside* the api_key plane: an API key is for machines, this
is for the human running the mirror.
"""
from __future__ import annotations

import html
import urllib.parse

from . import adminauth, adminstats, apikeys

PATHS = ("/", "/admin/login", "/admin/logout", "/admin/password",
         "/admin/keys/create", "/admin/keys/revoke")


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
    def __init__(self, db, settings):
        self.db, self.s = db, settings

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
            self._stats_section(st), self._keys_section(csrf), self._webhooks_section(st),
        ]), nav=f"<nav><form method=post action=/admin/logout style='display:inline'>"
                f"<button class=link type=submit>sign out</button></form> · "
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
