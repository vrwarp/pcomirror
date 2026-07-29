"""Command-line entrypoints:  python -m pcomirror <command>"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from . import apikeys, pcoevents, registry, webhooks
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
    """Re-apply PCOMIRROR_SUBSCRIPTIONS so a container needs no follow-up command —
    unless the operator page has taken the list over, in which case say so rather
    than silently undoing what somebody set there."""
    applied = webhooks.apply_env(m.db, m.settings.subscriptions)
    skipped = [r for r in applied if r["outcome"] == "skipped"]
    if skipped:
        # One line, not one per entry: the reason is the same for all of them, and
        # a wall of identical warnings is how the one that matters gets skimmed.
        print(f"[serve] PCOMIRROR_SUBSCRIPTIONS not applied ({len(skipped)} entr"
              f"{'y' if len(skipped) == 1 else 'ies'}): subscriptions are managed from "
              "the operator page. Hand them back at /admin/webhooks to let the "
              "environment set them again.")
    for record in applied:
        if record["outcome"] == "skipped":
            continue
        print(f"[serve] subscription {record['outcome']}: {record['spec'].event} -> "
              f"{_receiver_url(m.settings, record['token'])}")


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


def cmd_repair(args):
    """Re-fetch records the mirror holds thinner than PCO returns.

    Runs on the scheduler too, but a mirror that was degraded before the guard
    existed should not have to wait a quarter of an hour to be put right.
    """
    m = _mirror()
    targets = [args.resource] if args.resource else [r.name for r in registry.full_and_lite()]
    queued = 0
    for name in targets:
        n = m.ingestor.repair_incomplete(name)
        if n:
            print(f"{name}: {n} incomplete record(s) queued")
        queued += n
        n = m.ingestor.repair_dangling(name)
        if n:
            print(f"{name}: {n} re-fetch(es) queued for edges that do not resolve")
        queued += n
    if not queued:
        print("nothing to repair — every record carries the relationships it was "
              "fetched with, and every edge resolves")
        return
    done = 0
    while True:
        n = m.ingestor.drain_hydration()
        done += n
        if n == 0:
            break
    print(f"re-fetched {done} record(s)")


def cmd_reconcile(args):
    m = _mirror()
    targets = [args.resource] if args.resource else [r.name for r in registry.full_and_lite()]
    for name in targets:
        n = m.ingestor.incremental_sweep(name)
        print(f"sweep {name}: {n} applied")
    print(f"mergers: {m.ingestor.merger_poll()} applied")
    if args.audit:
        for r in registry.full_and_lite():
            if r.audit_interval_s:
                print(f"audit {r.name}: {m.ingestor.delete_audit(r.name)} tombstoned")


def cmd_drift(args):
    m = _mirror()
    for r in registry.full_and_lite():
        print(m.ingestor.drift_probe(r.name))


def cmd_add_subscription(args):
    """Register webhook subscription records locally (secrets from PCO).

    `--event` may be repeated: Planning Center makes one subscription per event
    name, but they can all point at one receiver URL, so several events on one
    `--url-token` is the normal shape rather than a special case. With more than
    one event the local id is derived per event, since PCO issues a separate id
    for each and there is no single one to name them all by.
    """
    m = _mirror()
    events = args.event
    ids = [args.subscription_id] if len(events) == 1 else [
        f"{args.subscription_id}:{'.'.join(pcoevents.parse(e))}" for e in events]
    # Settled once, before the loop, and never invented when one already exists.
    # Deriving it per call would mint a fresh token for each event — three
    # receivers where one was asked for — and re-running would rotate the URL PCO
    # is already delivering to, which is the one thing this must never do.
    token = args.url_token or _existing_token(m, ids) or webhooks.mint_token()
    for sub_id, event in zip(ids, events):
        upsert_subscription(m.db, sub_id, event, args.secret, token, managed="admin")
        print(f"subscription {event} -> {_receiver_url(m.settings, token)}")


def _existing_token(m: Mirror, subscription_ids: list[str]) -> str | None:
    for sub_id in subscription_ids:
        row = m.db.query_one(
            "SELECT url_token FROM webhook_subscription WHERE subscription_pco_id=?", (sub_id,))
        if row:
            return row["url_token"]
    return None


def cmd_list_subscriptions(args):
    m = _mirror()
    rows = webhooks.listing(m.db)
    if not rows:
        print("no webhook subscriptions")
        return
    source = "operator page" if not webhooks.env_is_authoritative(m.db) else "environment"
    print(f"subscriptions are managed by the {source}\n")
    print(f"{'ID':28} {'EVENT':46} {'FROM':6} {'AUTH':9} {'STATE':8} LAST EVENT")
    for r in rows:
        # What actually authenticates a delivery: the signature, the URL being
        # unguessable, or nothing.
        auth = ("signature" if not webhooks.is_unverified(r)
                else "url" if webhooks.token_is_credential(r["url_token"]) else "NONE")
        print(f"{r['subscription_pco_id'][:28]:28} {r['event_name'][:46]:46} "
              f"{r['managed']:6} {auth:9} {'active' if r['active'] else 'inactive':8} "
              f"{r['last_event_at'] or 'never'}")
    print()
    unprotected = set(webhooks.unprotected_tokens(m.db))
    for rec in webhooks.receivers(m.db):
        note = ("  ** nothing authenticates a delivery here **"
                if rec["url_token"] in unprotected else "")
        print(f"{_receiver_url(m.settings, rec['url_token'])}  "
              f"({len(rec['subscriptions'])} event(s)){note}")


def cmd_remove_subscription(args):
    m = _mirror()
    if webhooks.delete_subscription(m.db, args.subscription_id):
        print(f"removed {args.subscription_id}")
    else:
        sys.exit(f"no subscription {args.subscription_id}")


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


def _warn_unverified_receivers(m: Mirror) -> None:
    """Say, at every start, which receiver URLs authenticate a delivery with nothing.

    Not every secretless receiver: one whose token is unguessable has moved its
    authentication into the URL, which is a bearer credential and not news. What
    is worth interrupting somebody about is the combination that checks nothing —
    no secret and a token that could be guessed.

    Said at every start rather than once when it was configured, because the
    person reading the log on a Tuesday is not the person who set it up. Same
    treatment as PCOMIRROR_ALLOW_ANONYMOUS, for the same reason.
    """
    for token in webhooks.unprotected_tokens(m.db):
        print(f"[serve] {_receiver_url(m.settings, token)} has no authenticity secret and "
              "a guessable token, so NOTHING authenticates a delivery to it. Paste the "
              "secret Planning Center shows, or move it to a receiver with a minted token.")


def cmd_serve(args):
    m = _mirror()
    _apply_env_subscriptions(m)
    if m.settings.allow_anonymous:
        print("[serve] PCOMIRROR_ALLOW_ANONYMOUS is set — /people/v2 is served "
              "without an API key. Do not expose this service publicly.")
    elif not apikeys.any_enabled(m.db):
        print("[serve] no API keys configured — /people/v2 will return 401. "
              "Create one with `pcomirror create-api-key --name <app>`.")
    _warn_unverified_receivers(m)
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
    rp = sub.add_parser("repair", help="re-fetch records held thinner than PCO returns")
    rp.add_argument("resource", nargs="?"); rp.set_defaults(func=cmd_repair)
    s = sub.add_parser("serve")
    s.add_argument("--no-scheduler", action="store_true")
    s.add_argument("--backfill", action="store_true",
                   help="run an initial backfill for any un-backfilled resource before serving")
    s.set_defaults(func=cmd_serve)
    a = sub.add_parser("add-subscription")
    a.add_argument("--subscription-id", required=True)
    a.add_argument("--event", required=True, action="append",
                   help="event name; repeat to point several event types at one receiver")
    a.add_argument("--secret", default="",
                   help="the subscription's authenticity_secret, from Planning Center. "
                        "Leave it out to authenticate on the URL alone — in which case "
                        "leave --url-token out too, so a minted token makes the URL a "
                        "bearer credential rather than a guess.")
    a.add_argument("--url-token", help="receiver-URL token to use (8-64 chars of [A-Za-z0-9_-]); "
                                       "pick one to know the URL before registering at PCO. "
                                       "Default: keep the existing token, else generate one.")
    a.set_defaults(func=cmd_add_subscription)
    sub.add_parser("list-subscriptions").set_defaults(func=cmd_list_subscriptions)
    rs = sub.add_parser("remove-subscription")
    rs.add_argument("--subscription-id", required=True)
    rs.set_defaults(func=cmd_remove_subscription)
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
