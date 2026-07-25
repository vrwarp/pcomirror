"""Local API-key plane (DESIGN §8.4).

The second of the two strictly separated credential planes: these keys
authenticate *apps to pcomirror*. The upstream PCO PAT lives only server-side and
is never exposed to or selectable by callers — a caller's key decides what it may
ask for, never which PCO credential is used.

Keys are shown once at creation and stored only as a SHA-256 digest. A plain hash
(not a password KDF) is the right call here: the secret is 256 bits of `secrets`
entropy, so there is no dictionary to attack and nothing for bcrypt/scrypt to
slow down.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from .config import now_iso

TOKEN_PREFIX = "pcm"          # human-recognisable in logs and secret scanners
PREFIX_LEN = 8                # public half, used for the O(1) row lookup

SCOPE_WRITE = "write"
SCOPE_PASSTHROUGH = "passthrough"
DEFAULT_SCOPES = "read:*"

# Writing last_used_at on every request would put a write in front of every read;
# a coarse timestamp is all it is for.
LAST_USED_RESOLUTION_S = 60


def hash_key(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def parse_scopes(raw: str | None) -> set[str]:
    return {s.strip() for s in (raw or "").split(",") if s.strip()}


def allows_read(scopes: set[str], endpoint: str) -> bool:
    """`read:*` covers everything; `read:<endpoint>` covers one collection."""
    return "read:*" in scopes or f"read:{endpoint}" in scopes


def create(db, name: str, scopes: str = DEFAULT_SCOPES) -> str:
    """Mint a key, store its digest, and return the secret — the only time it exists."""
    for _ in range(10):
        prefix = secrets.token_hex(PREFIX_LEN // 2)
        if not db.query_one("SELECT 1 FROM api_key WHERE prefix=?", (prefix,)):
            break
    else:                                                   # pragma: no cover
        raise RuntimeError("could not allocate a unique key prefix")
    # hex, not token_urlsafe: urlsafe's alphabet includes '_', which would collide
    # with the field separator and make parsing depend on the random bytes.
    key = f"{TOKEN_PREFIX}_{prefix}_{secrets.token_hex(32)}"
    db.execute(
        "INSERT INTO api_key(id,prefix,key_hash,name,scopes) VALUES(?,?,?,?,?)",
        (secrets.token_hex(8), prefix, hash_key(key), name, scopes))
    return key


def bearer_token(header: str | None) -> str | None:
    """Accept `Authorization: Bearer <key>`; the bare key is also allowed so
    curl-by-hand and simple clients do not need the ceremony."""
    if not header:
        return None
    value = header.strip()
    scheme, _, rest = value.partition(" ")
    if scheme.lower() == "bearer":
        return rest.strip() or None
    return value if value.startswith(f"{TOKEN_PREFIX}_") else None


def authenticate(db, header: str | None):
    """Return the api_key row for a valid credential, else None.

    Comparison is constant-time, and a prefix collision (possible: the column has
    no UNIQUE constraint) simply means checking each candidate.
    """
    key = bearer_token(header)
    if not key:
        return None
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or len(parts[1]) != PREFIX_LEN:
        return None
    digest = hash_key(key)
    rows = db.query(
        "SELECT * FROM api_key WHERE prefix=? AND disabled_at IS NULL", (parts[1],))
    for row in rows:
        if hmac.compare_digest(bytes(row["key_hash"]), digest):
            _touch(db, row)
            return row
    return None


def _touch(db, row) -> None:
    stamp = now_iso()
    last = row["last_used_at"]
    if last and last >= _floor(stamp):
        return
    db.execute("UPDATE api_key SET last_used_at=? WHERE id=?", (stamp, row["id"]))


def _floor(stamp: str) -> str:
    """The oldest last_used_at we consider current (ISO-8601 sorts lexically)."""
    from datetime import datetime, timedelta, timezone
    t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (t - timedelta(seconds=LAST_USED_RESOLUTION_S)).strftime("%Y-%m-%dT%H:%M:%SZ")


def any_enabled(db) -> bool:
    return db.query_one("SELECT 1 FROM api_key WHERE disabled_at IS NULL") is not None


def listing(db) -> list:
    return db.query(
        "SELECT prefix,name,scopes,created_at,last_used_at,disabled_at "
        "FROM api_key ORDER BY created_at")


def revoke(db, prefix: str) -> bool:
    if not db.query_one("SELECT 1 FROM api_key WHERE prefix=? AND disabled_at IS NULL", (prefix,)):
        return False
    db.execute("UPDATE api_key SET disabled_at=? WHERE prefix=?", (now_iso(), prefix))
    return True
