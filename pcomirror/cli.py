"""Command-line entrypoints:  python -m pcomirror <command>"""
from __future__ import annotations

import argparse
import secrets
import signal
import sys
import threading
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from . import registry
from .app import Mirror
from .config import Settings


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
    token = secrets.token_hex(16)
    m.db.execute(
        "INSERT INTO webhook_subscription"
        "(subscription_pco_id,event_name,resource,action,url_token,authenticity_secret) "
        "VALUES(?,?,?,?,?,?)",
        (args.subscription_id, args.event, args.event.split(".")[-2],
         args.event.split(".")[-1], token, args.secret))
    print(f"subscription {args.event} -> {m.settings.public_base_url}"
          f"{m.settings.webhook_path_prefix}/{token}")


def cmd_serve(args):
    m = _mirror()
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
    a.add_argument("--secret", required=True); a.set_defaults(func=cmd_add_subscription)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
