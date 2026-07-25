"""Admin credential + session handling for the operator page.

The bootstrap password is `PCO_SECRET`. That is not a security claim — it is an
acknowledgement: anything that can read the container's environment already holds
the PAT, so the PAT is the weakest link and gating the admin page behind a *new*
secret would be theatre until the operator sets one. So the first login with the
bootstrap password is forced through a password change, after which `PCO_SECRET`
stops working as a credential.

Stored passwords use PBKDF2-HMAC-SHA256 (stdlib). Unlike an API key, this is a
human-chosen password — low entropy, worth the key-stretching.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timedelta, timezone

from .config import now_iso

ITERATIONS = 600_000
SALT_BYTES = 16
SESSION_HOURS = 12
COOKIE = "pcomirror_admin"
MIN_PASSWORD_LEN = 12

# Single account, so a global throttle is enough. It does mean an attacker can
# lock the operator out for a minute at a time; that is preferred to leaving an
# unthrottled password prompt on the root path.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60
_throttle_lock = threading.Lock()
_failures = {"count": 0, "until": 0.0}


def _derive(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)


def is_configured(db) -> bool:
    """True once the operator has set their own password."""
    return db.query_one("SELECT 1 FROM admin_account WHERE id=1") is not None


def set_password(db, password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(SALT_BYTES)
    db.execute(
        "INSERT INTO admin_account(id,password_hash,password_salt,iterations,updated_at) "
        "VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "password_hash=excluded.password_hash, password_salt=excluded.password_salt, "
        "iterations=excluded.iterations, updated_at=excluded.updated_at",
        (_derive(password, salt), salt, ITERATIONS, now_iso()))


def locked_out() -> int:
    """Seconds remaining on the failed-login lockout, 0 if none."""
    import time
    with _throttle_lock:
        remaining = _failures["until"] - time.monotonic()
    return int(remaining) + 1 if remaining > 0 else 0


def _record_failure() -> None:
    import time
    with _throttle_lock:
        _failures["count"] += 1
        if _failures["count"] >= MAX_FAILURES:
            _failures["count"] = 0
            _failures["until"] = time.monotonic() + LOCKOUT_SECONDS


def _clear_failures() -> None:
    with _throttle_lock:
        _failures["count"] = 0
        _failures["until"] = 0.0


def verify(db, settings, password: str) -> tuple[bool, bool]:
    """Return `(ok, used_bootstrap)`.

    Before the operator sets a password, PCO_SECRET is accepted. An empty
    PCO_SECRET is never a valid credential — otherwise an unconfigured install
    would admit an empty password.
    """
    if locked_out():
        return False, False
    row = db.query_one("SELECT * FROM admin_account WHERE id=1")
    if row is None:
        secret = (settings.pco_secret or "").strip()
        ok = bool(secret) and hmac.compare_digest(secret, password)
        _clear_failures() if ok else _record_failure()
        return ok, ok
    expected = bytes(row["password_hash"])
    candidate = _derive(password, bytes(row["password_salt"]), row["iterations"])
    ok = hmac.compare_digest(expected, candidate)
    _clear_failures() if ok else _record_failure()
    return ok, False


# -- sessions -------------------------------------------------------------
def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def create_session(db, must_change: bool) -> str:
    prune_sessions(db)
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO admin_session(token_hash,csrf,must_change_password,expires_at) "
        "VALUES(?,?,?,?)",
        (_hash_token(token), secrets.token_urlsafe(24), 1 if must_change else 0, _expiry()))
    return token


def session_for(db, token: str | None):
    if not token:
        return None
    row = db.query_one("SELECT * FROM admin_session WHERE token_hash=?", (_hash_token(token),))
    if row is None or row["expires_at"] <= now_iso():
        return None
    return row


def clear_must_change(db, token: str) -> None:
    db.execute("UPDATE admin_session SET must_change_password=0 WHERE token_hash=?",
               (_hash_token(token),))


def destroy_session(db, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM admin_session WHERE token_hash=?", (_hash_token(token),))


def destroy_all_sessions(db) -> None:
    """Used after a password change — every other session must re-authenticate."""
    db.execute("DELETE FROM admin_session", ())


def prune_sessions(db) -> None:
    db.execute("DELETE FROM admin_session WHERE expires_at <= ?", (now_iso(),))


def check_csrf(session, submitted: str | None) -> bool:
    return bool(submitted) and hmac.compare_digest(session["csrf"], submitted)
