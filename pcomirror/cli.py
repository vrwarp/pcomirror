"""Command-line entrypoints:  python -m pcomirror <command>"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from . import apikeys, registry
from .app import Mirror
from .config import Settings
from .webhooks import upsert_subscription


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Serve requests concurrently (webhook deliveries + reads); DB access is
    serialized under one lock, so concurrency here is safe."""
    daemon_threads = True
    allow_reuse_address = True


def _backfill_if_needed(m: Mirror) -> None:
    if not m.settings.pco_app_id:
        print("[serve] PCO_APP_ID not set — skipping backfill-on-start")
        return
    for r in registry.full_and_lite():
        st = m.ingestor.state(r.name)
        if st["backfill_completed_at"] is None:
            n = m.ingestor.backfill(r.name)
            print(f"[serve] backfill {r.name}: {n} records")
    m.ingestor.merger_poll()


def _mirror() -> Mirror:
    return Mirror(Settings.from_env())


def _receiver_url(s: Settings, token: str) -> str:
    return f"{s.public_base_url}{s.webhook_path_prefix}/{token}"


def _apply_env_subscriptions(m: Mirror) -> None:
    """Re-apply PCOMIRROR_SUBSCRIPTIONS so a container needs no follow-up command."""
    for spec in m.settings.subscriptions:
        token, created = upsert_subscription(m.db, spec.subscription_id, spec.event,
                                             spec.secret, spec.url_token or None)
        verb = "registered" if created else "updated"
        print(f"[serve] subscription {verb}: {spec.event} -> "
              f"{_receiver_url(m.settings, token)}")


def cmd_init_db(args):
    m = _mirror()
    print(f"initialized schema in {m.settings.db_path} "
          f"({len(registry.mirrored_tables())} mirrored tables)")


def cmd_backfill(args):
    m = _mirror()
    targets = [args.resource] if args.resource else [r.name for r in registry.full_and_lite()]
    for name in targets:
        n = m.ingestor.backfill(name)
        print(f"backfill {name}: {n} records")
    m.ingestor.merger_poll()


def cmd_reconcile(args):
    m = _mirror()
    targets = [args.resource] if args.resource else [r.name for r in registry.full_and_lite()]
    for name in targets:
        n = m.ingestor.incremental_sweep(name)
        print(f"sweep {name}: {n} applied")
    print(f"mergers: {m.ingestor.merger_poll()} applied")
    if args.audit:
        print(f"audit person: {m.ingestor.delete_audit('person')} tombstoned")


def cmd_drift(args):
    m = _mirror()
    for r in registry.full_and_lite():
        print(m.ingestor.drift_probe(r.name))


def cmd_add_subscription(args):
    """Register a webhook subscription record locally (secret from PCO)."""
    m = _mirror()
    token, _ = upsert_subscription(m.db, args.subscription_id, args.event,
                                   args.secret, args.url_token)
    print(f"subscription {args.event} -> {_receiver_url(m.settings, token)}")


def cmd_create_api_key(args):
    """Mint a local API key. The secret is printed once and never stored."""
    m = _mirror()
    key = apikeys.create(m.db, args.name, args.scopes)
    print(f"name:   {args.name}\nscopes: {args.scopes}\nkey:    {key}\n"
          "\nStore it now — only its hash is kept, so it cannot be shown again.")


def cmd_list_api_keys(args):
    m = _mirror()
    rows = apikeys.listing(m.db)
    if not rows:
        print("no API keys")
        return
    print(f"{'PREFIX':10} {'NAME':20} {'SCOPES':28} {'LAST USED':21} STATE")
    for r in rows:
        state = "revoked" if r["disabled_at"] else "active"
        print(f"{r['prefix']:10} {(r['name'] or '-'):20} {r['scopes']:28} "
              f"{(r['last_used_at'] or 'never'):21} {state}")


def cmd_revoke_api_key(args):
    m = _mirror()
    if apikeys.revoke(m.db, args.prefix):
        print(f"revoked {args.prefix}")
    else:
        sys.exit(f"no active key with prefix {args.prefix}")


def cmd_serve(args):
    m = _mirror()
    _apply_env_subscriptions(m)
    if m.settings.allow_anonymous:
        print("[serve] PCOMIRROR_ALLOW_ANONYMOUS is set — /people/v2 is served "
              "without an API key. Do not expose this service publicly.")
    elif not apikeys.any_enabled(m.db):
        print("[serve] no API keys configured — /people/v2 will return 401. "
              "Create one with `pcomirror create-api-key --name <app>`.")
    if m.settings.backfill_on_start or args.backfill:
        _backfill_if_needed(m)
    sched = None
    if not args.no_scheduler:
        from .scheduler import Scheduler
        sched = Scheduler(m)
        sched.start()
    srv = make_server(m.settings.bind_host, m.settings.bind_port, m.wsgi,
                      server_class=_ThreadingWSGIServer)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())   # graceful `docker stop`
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threading.Thread(target=srv.serve_forever, name="pcomirror-wsgi", daemon=True).start()
    print(f"serving on http://{m.settings.bind_host}:{m.settings.bind_port} "
          f"(scheduler {'on' if sched else 'off'})", flush=True)
    try:
        stop.wait()
    finally:
        print("shutting down…", flush=True)
        srv.shutdown()
        if sched:
            sched.stop()
        m.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="pcomirror")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(func=cmd_init_db)
    b = sub.add_parser("backfill"); b.add_argument("resource", nargs="?"); b.set_defaults(func=cmd_backfill)
    rc = sub.add_parser("reconcile"); rc.add_argument("resource", nargs="?")
    rc.add_argument("--audit", action="store_true"); rc.set_defaults(func=cmd_reconcile)
    sub.add_parser("drift").set_defaults(func=cmd_drift)
    s = sub.add_parser("serve")
    s.add_argument("--no-scheduler", action="store_true")
    s.add_argument("--backfill", action="store_true",
                   help="run an initial backfill for any un-backfilled resource before serving")
    s.set_defaults(func=cmd_serve)
    a = sub.add_parser("add-subscription")
    a.add_argument("--subscription-id", required=True); a.add_argument("--event", required=True)
    a.add_argument("--secret", required=True)
    a.add_argument("--url-token", help="receiver-URL token to use (8-64 chars of [A-Za-z0-9_-]); "
                                       "pick one to know the URL before registering at PCO. "
                                       "Default: keep the existing token, else generate one.")
    a.set_defaults(func=cmd_add_subscription)
    k = sub.add_parser("create-api-key")
    k.add_argument("--name", required=True, help="which app this key is for")
    k.add_argument("--scopes", default=apikeys.DEFAULT_SCOPES,
                   help="comma-separated: read:* or read:<endpoint>, write, passthrough "
                        f"(default: {apikeys.DEFAULT_SCOPES})")
    k.set_defaults(func=cmd_create_api_key)
    sub.add_parser("list-api-keys").set_defaults(func=cmd_list_api_keys)
    rv = sub.add_parser("revoke-api-key")
    rv.add_argument("--prefix", required=True); rv.set_defaults(func=cmd_revoke_api_key)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
