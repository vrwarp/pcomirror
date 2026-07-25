#!/usr/bin/env python3
"""Container entrypoint: apply PUID/PGID, drop privileges, exec the CLI.

A bind-mounted data directory (a NAS share, typically) arrives owned by whatever
host user created it — not by the image's `appuser` — so the service cannot
create its SQLite file and fails with the famously unhelpful
`sqlite3.OperationalError: unable to open database file`.

Setting PUID/PGID to the owning host user fixes that without running the service
as root: this script starts as root only long enough to take ownership of the
data directory, then permanently drops to PUID:PGID before exec'ing the app. It
is stdlib-only, like the rest of the project — no gosu/su-exec to install.

If the container is started with `--user` (i.e. we are already non-root) there is
nothing to drop and no way to chown, so PUID/PGID are ignored with a warning and
the command runs as-is.
"""
from __future__ import annotations

import grp
import os
import pwd
import sys

# The uid/gid baked into the image; used when PUID/PGID are unset so the default
# behaviour matches the pre-PUID image exactly.
DEFAULT_UID = 10001
DEFAULT_GID = 10001


def log(msg: str) -> None:
    print(f"[entrypoint] {msg}", flush=True)


def _id_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"[entrypoint] {name}={raw!r} is not an integer")
    if value < 0:
        sys.exit(f"[entrypoint] {name}={raw!r} must not be negative")
    return value


def data_dir() -> str:
    """The directory holding the SQLite file — the only path we need to own."""
    return os.path.dirname(os.environ.get("PCOMIRROR_DB") or "/data/pcomirror.db") or "."


def take_ownership(path: str, uid: int, gid: int) -> None:
    """chown `path` and its immediate children (the .db / -wal / -shm set).

    Deliberately not recursive: the data directory holds only the database files,
    and a mount pointed somewhere unexpected should not trigger a deep chown.
    Each chown is skipped when ownership already matches, so restarts are cheap.
    """
    os.makedirs(path, exist_ok=True)
    targets = [path] + [os.path.join(path, name) for name in os.listdir(path)]
    changed = 0
    for target in targets:
        try:
            st = os.stat(target)
            if st.st_uid == uid and st.st_gid == gid:
                continue
            os.chown(target, uid, gid)
            changed += 1
        except OSError as e:
            # A read-only mount is worth reporting, but let the writability check
            # below produce the actionable message.
            log(f"could not chown {target}: {e}")
    if changed:
        log(f"took ownership of {changed} path(s) under {path} for {uid}:{gid}")


def drop_privileges(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    # Keep HOME pointing somewhere the new user can actually write; a stale
    # /root would break anything that caches there.
    try:
        os.environ["HOME"] = pwd.getpwuid(uid).pw_dir
        os.environ["USER"] = pwd.getpwuid(uid).pw_name
    except KeyError:
        os.environ["HOME"] = "/tmp"          # arbitrary uid with no passwd entry
        os.environ.pop("USER", None)


def check_writable(path: str) -> None:
    """Fail with the cause rather than a SQLite traceback five frames deep."""
    if os.access(path, os.W_OK | os.X_OK):
        return
    uid, gid = os.getuid(), os.getgid()
    try:
        st = os.stat(path)
        owner = f"owned by {st.st_uid}:{st.st_gid}, mode {st.st_mode & 0o777:o}"
    except OSError as e:
        owner = f"cannot stat it: {e}"
    sys.exit(
        f"[entrypoint] {path} is not writable by uid {uid}:{gid} ({owner}).\n"
        f"[entrypoint] If it is a bind mount, either set PUID/PGID to the host "
        f"user that owns it, or chown it on the host:\n"
        f"[entrypoint]     chown -R {uid}:{gid} <host path mounted at {path}>")


def describe_identity() -> str:
    uid, gid = os.getuid(), os.getgid()
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = "?"
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = "?"
    return f"uid={uid}({name}) gid={gid}({group})"


def main(argv: list[str]) -> None:
    uid, gid = _id_env("PUID", DEFAULT_UID), _id_env("PGID", DEFAULT_GID)
    directory = data_dir()

    if os.geteuid() == 0:
        take_ownership(directory, uid, gid)
        drop_privileges(uid, gid)
    elif os.environ.get("PUID") or os.environ.get("PGID"):
        # Started with --user; we have no privilege to change identity or owner.
        log(f"already running as {describe_identity()} (started with --user?) — "
            f"ignoring PUID/PGID")

    log(f"running as {describe_identity()}")
    check_writable(directory)
    os.execv(sys.executable, [sys.executable, "-m", "pcomirror", *argv])


if __name__ == "__main__":
    main(sys.argv[1:])
