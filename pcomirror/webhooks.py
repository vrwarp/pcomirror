"""Webhook receiver & async processing (DESIGN §6).

Edge is thin: verify HMAC over the raw bytes, durably capture (per-event dedup),
ack fast. All real work is async in `process_event`, which dispatches through the
canonical writer and enqueues hydration for thin payloads.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets

from . import registry
from .config import now_iso

BURST_THRESHOLD = 200  # pending per-id hydrations above which we defer to a sweep

# A url_token is the last path segment of the receiver URL, so keep it to
# characters that survive a URL untouched (DESIGN §6, `POST /pco/webhooks/<url_token>`).
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def verify(secret: str, raw: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, signature.strip()):
        return True
    # some senders use base64; accept it too, constant-time
    import base64
    expected_b64 = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected_b64, signature.strip())


def parse_event_name(name: str) -> tuple[str, str]:
    parts = name.split(".")
    # people.v2.events.<resource>.<action>
    return (parts[-2], parts[-1]) if len(parts) >= 2 else (name, "")


def upsert_subscription(db, subscription_id: str, event_name: str, secret: str,
                        url_token: str | None = None) -> tuple[str, bool]:
    """Register (or refresh) a subscription row; returns `(url_token, created)`.

    Idempotent by `subscription_pco_id` so it can run on every container start:
    a repeat registration updates the event and secret but *keeps the existing
    token* unless a new one is passed explicitly — the receiver URL already
    registered at PCO must never change underneath it.

    Passing `url_token` lets the caller pick the token up front, so the receiver
    URL is known before the subscription exists at PCO (which is what supplies
    the `authenticity_secret`), breaking that ordering cycle.
    """
    if url_token is not None and not TOKEN_RE.match(url_token):
        raise ValueError(f"invalid url_token {url_token!r}: expected 8-64 chars of [A-Za-z0-9_-]")
    row = db.query_one(
        "SELECT url_token FROM webhook_subscription WHERE subscription_pco_id=?", (subscription_id,))
    token = url_token or (row["url_token"] if row else secrets.token_hex(16))
    resource, action = parse_event_name(event_name)
    db.execute(
        "INSERT INTO webhook_subscription"
        "(subscription_pco_id,event_name,resource,action,url_token,authenticity_secret) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(subscription_pco_id) DO UPDATE SET "
        "event_name=excluded.event_name, resource=excluded.resource, action=excluded.action, "
        "url_token=excluded.url_token, authenticity_secret=excluded.authenticity_secret, active=1",
        (subscription_id, event_name, resource, action, token, secret))
    return token, row is None


class WebhookProcessor:
    def __init__(self, db, writer, ingestor):
        self.db = db
        self.writer = writer
        self.ingestor = ingestor

    # -- edge: verify + capture + ack -------------------------------------
    def receive(self, url_token: str, raw: bytes, signature: str | None) -> tuple[int, str]:
        sub = self.db.query_one(
            "SELECT * FROM webhook_subscription WHERE url_token=? AND active=1", (url_token,))
        if sub is None:
            return 404, "unknown token"                         # NOT 410
        if not verify(sub["authenticity_secret"], raw, signature):
            return 401, "bad signature"                         # NOT 410
        try:
            env = json.loads(raw)
        except json.JSONDecodeError:
            return 503, "unparseable"                           # pre-capture: PCO retries safely
        delivery_id = env.get("id") or f"nodelivery-{now_iso()}"
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO webhook_delivery(delivery_id,subscription_pco_id,signature,raw_body,attempt) "
                "VALUES(?,?,?,?,?)",
                (delivery_id, sub["subscription_pco_id"], signature, raw, env.get("attempt")))
            for item in env.get("data", []):
                name = item.get("attributes", {}).get("name", sub["event_name"])
                res, act = parse_event_name(name)
                payload = item.get("attributes", {}).get("payload")
                if isinstance(payload, str):
                    payload = payload  # keep as string; parsed at process time
                else:
                    payload = json.dumps(payload)
                self.db.execute(
                    "INSERT OR IGNORE INTO webhook_event"
                    "(event_id,delivery_id,event_name,resource_type,action,pco_id,payload) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (item["id"], delivery_id, name, res, act,
                     json.loads(payload).get("id") if payload else None, payload))
        except Exception:
            return 503, "capture failed"
        return 204, "ok"

    # -- async: drain the inbox -------------------------------------------
    def drain(self, limit: int = 500) -> int:
        rows = self.db.query(
            "SELECT * FROM webhook_event WHERE status='pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY received_at LIMIT ?",
            (now_iso(), limit))
        n = 0
        for row in rows:
            self.process_event(dict(row))
            n += 1
        return n

    def process_event(self, ev: dict) -> None:
        res_token, action = ev["resource_type"], ev["action"]
        r = registry.by_event_resource(res_token)
        try:
            payload = json.loads(ev["payload"]) if ev["payload"] else {}
        except json.JSONDecodeError:
            return self._dead(ev, "unparseable payload")
        if r is None and res_token != "person_merger":
            return self._dead(ev, f"unmapped resource {res_token}")
        try:
            if res_token == "person_merger":
                self._handle_merge(payload)
            elif action == "destroyed":
                uat = (payload.get("attributes") or {}).get("updated_at")
                self.writer.tombstone(r.table, payload["id"], uat, "destroyed")
            else:  # created | updated
                self.writer.route(payload, "webhook")
                if self._is_thin(r, payload):
                    self._enqueue_or_defer(r, payload["id"])
            self.db.execute(
                "UPDATE webhook_event SET status='done', processed_at=? WHERE event_id=?",
                (now_iso(), ev["event_id"]))
        except Exception as e:  # noqa: BLE001
            self._retry_or_dead(ev, str(e))

    def _handle_merge(self, payload: dict) -> None:
        a = payload.get("attributes", {})
        keep, gone = a.get("person_to_keep_id"), a.get("person_to_remove_id")
        if gone:
            self.writer.tombstone("person", gone, None, "merged", merged_into=keep)
        if keep:
            self.ingestor.enqueue_hydration("person", keep, reason="merge_survivor")
        self.db.execute(
            "INSERT OR IGNORE INTO person_merger(pco_id,raw,source,api_version) VALUES(?,?,?,?)",
            (payload.get("id", f"merge-{keep}-{gone}"), json.dumps(payload),
             "webhook", self.writer.api_version))

    def _is_thin(self, r, payload: dict) -> bool:
        # webhook payloads never embed includes; if we project children/relationships
        # via includes, we must hydrate to get them.
        return bool(r.includes)

    def _enqueue_or_defer(self, r, pco_id: str) -> None:
        pending = self.db.query_one(
            "SELECT count(*) c FROM hydration_task WHERE resource_type=?", (r.name,))["c"]
        if pending >= BURST_THRESHOLD:
            # burst: defer to a sweep instead of N per-id GETs (DESIGN §6.3)
            self.ingestor._set(r.name, next_run_at=now_iso())
            return
        self.ingestor.enqueue_hydration(r.name, pco_id)

    def _retry_or_dead(self, ev: dict, err: str) -> None:
        attempts = ev["process_attempts"] + 1
        if attempts >= 8:
            self._dead(ev, err)
        else:
            delay = min(3600, 5 * 2 ** attempts)
            self.db.execute(
                "UPDATE webhook_event SET process_attempts=?, last_error=?, "
                "next_attempt_at=strftime('%Y-%m-%dT%H:%M:%SZ','now',?) WHERE event_id=?",
                (attempts, err, f"+{delay} seconds", ev["event_id"]))

    def _dead(self, ev: dict, err: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO webhook_dead_letter(event_id,event_name,payload,last_error,attempts) "
            "VALUES(?,?,?,?,?)",
            (ev["event_id"], ev["event_name"], ev["payload"], err, ev["process_attempts"] + 1))
        self.db.execute("UPDATE webhook_event SET status='dead', last_error=? WHERE event_id=?",
                        (err, ev["event_id"]))
