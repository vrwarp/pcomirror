"""Webhook receiver & async processing (DESIGN §6).

Edge is thin: verify HMAC over the raw bytes, durably capture (per-event dedup),
ack fast. All real work is async in `process_event`, which dispatches through the
canonical writer and enqueues hydration for thin payloads.

**One receiver URL, many event types.** Planning Center's model is one
subscription per event name — a WebhookSubscription carries a single `name`, a
single `url`, and its own `authenticity_secret` — but nothing requires those URLs
to differ, and PCO's own console points every event you tick at one address. So
several subscription rows may share a `url_token`, and the receiver works out
which one is delivering from **the secret that signed the body**: it is the one
piece of the exchange only the right subscription can produce. Falling back to the
event name in the payload would be trusting the attacker-supplied half of the
request to choose the key it is checked against.
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

#: Set once the operator saves anything on the subscriptions page. From then on
#: the page is authoritative and PCOMIRROR_SUBSCRIPTIONS is not applied — the
#: same shape as the divergence override, and for the same reason: whoever needs
#: to fix a webhook at 9pm is rarely whoever can edit the container's environment
#: and restart it. Cleared by handing control back, which takes effect on the
#: next `serve`.
OVERRIDE_KEY = "subscriptions_managed_here"


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


def is_unverified(sub) -> bool:
    """Does this subscription skip the signature check?

    A blank `authenticity_secret` means yes. There is no separate switch on
    purpose: the secret is the only thing a check could be made of, so "no
    secret" and "no check" are the same fact, and two settings that can disagree
    about it is how a receiver ends up verifying against the empty string.

    Takes either shape of row. The receiver holds the whole subscription because
    it has to verify against the secret; everything that only *displays* one goes
    through `listing`, which computes this in SQL and never selects the secret at
    all — a page cannot leak a value it was never handed.

    It is a real thing to want — a sender that cannot sign, a stand-in during a
    rebuild, a LAN-only box behind something that already authenticates — and a
    real thing to be careful with: an unverified receiver applies whatever is
    posted to its URL to the mirror, so the URL token becomes the only secret
    there is. The admin page and the `serve` log both say so out loud.
    """
    if "unverified" in sub.keys():
        return bool(sub["unverified"])
    return not (sub["authenticity_secret"] or "").strip()


def _by_event_name(subs, env: dict):
    """The subscription whose event this delivery claims to be, else the first.

    Used only to label an unchecked delivery. Deliveries carry one event name —
    PCO makes a subscription per name — so on a receiver holding several
    unchecked ones this is what stops them all being filed under whichever sorted
    first.
    """
    data = env.get("data") or []
    name = (data[0].get("attributes") or {}).get("name") if data else None
    return next((s for s in subs if s["event_name"] == name), subs[0])


def parse_event_name(name: str) -> tuple[str, str]:
    parts = name.split(".")
    # people.v2.events.<resource>.<action>
    return (parts[-2], parts[-1]) if len(parts) >= 2 else (name, "")


def upsert_subscription(db, subscription_id: str, event_name: str, secret: str,
                        url_token: str | None = None,
                        managed: str = "env") -> tuple[str, bool]:
    """Register (or refresh) a subscription row; returns `(url_token, created)`.

    Idempotent by `subscription_pco_id` so it can run on every container start:
    a repeat registration updates the event and secret but *keeps the existing
    token* unless a new one is passed explicitly — the receiver URL already
    registered at PCO must never change underneath it.

    Passing `url_token` lets the caller pick the token up front, so the receiver
    URL is known before the subscription exists at PCO (which is what supplies
    the `authenticity_secret`), breaking that ordering cycle. Several
    subscriptions may name the same token: that is a receiver serving several
    event types, which is the normal shape rather than an error.

    `managed` records who wrote the row — 'env' or 'admin' — so the page can say
    where each subscription came from.

    `secret` may be blank, which turns the signature check off for this
    subscription (`is_unverified`). Deliberately not rejected here: it is a
    choice an operator is allowed to make, and the place to argue about it is
    where they can see the consequence — the page and the `serve` log — not an
    exception from a function that also runs at container start.
    """
    if url_token is not None and not TOKEN_RE.match(url_token):
        raise ValueError(f"invalid url_token {url_token!r}: expected 8-64 chars of [A-Za-z0-9_-]")
    if not (subscription_id or "").strip():
        raise ValueError("a subscription needs an id")
    if not (event_name or "").strip():
        raise ValueError("a subscription needs an event name")
    row = db.query_one(
        "SELECT url_token FROM webhook_subscription WHERE subscription_pco_id=?", (subscription_id,))
    token = url_token or (row["url_token"] if row else secrets.token_hex(16))
    resource, action = parse_event_name(event_name)
    db.execute(
        "INSERT INTO webhook_subscription"
        "(subscription_pco_id,event_name,resource,action,url_token,authenticity_secret,managed) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(subscription_pco_id) DO UPDATE SET "
        "event_name=excluded.event_name, resource=excluded.resource, action=excluded.action, "
        "url_token=excluded.url_token, authenticity_secret=excluded.authenticity_secret, "
        "managed=excluded.managed, active=1",
        (subscription_id, event_name, resource, action, token, secret, managed))
    return token, row is None


def delete_subscription(db, subscription_id: str) -> bool:
    """Forget a subscription. Deliveries already captured keep their id."""
    before = db.query_one("SELECT 1 FROM webhook_subscription WHERE subscription_pco_id=?",
                          (subscription_id,))
    db.execute("DELETE FROM webhook_subscription WHERE subscription_pco_id=?", (subscription_id,))
    return before is not None


def set_active(db, subscription_id: str, active: bool) -> bool:
    before = db.query_one("SELECT 1 FROM webhook_subscription WHERE subscription_pco_id=?",
                          (subscription_id,))
    db.execute("UPDATE webhook_subscription SET active=? WHERE subscription_pco_id=?",
               (1 if active else 0, subscription_id))
    return before is not None


def listing(db) -> list:
    """Every subscription, grouped-friendly: receiver first, then event.

    Whether a subscription is checked comes back as a computed `unverified`
    flag and the secret itself is never selected. Everything that renders a
    subscription reads this, so no page or log line is ever holding the value it
    would be a disaster to print.
    """
    return db.query(
        "SELECT subscription_pco_id, event_name, resource, action, url_token, active, "
        "       last_event_at, managed, created_at, "
        "       trim(coalesce(authenticity_secret,'')) = '' AS unverified "
        "FROM webhook_subscription ORDER BY url_token, event_name")


def receivers(db) -> list[dict]:
    """Subscriptions folded into the receiver URLs they share.

    This is the unit an operator actually thinks in — one address pasted into
    Planning Center, carrying however many event types they ticked.
    """
    grouped: dict[str, dict] = {}
    for row in listing(db):
        bucket = grouped.setdefault(row["url_token"], {
            "url_token": row["url_token"], "subscriptions": [],
            "active": 0, "inactive": 0, "last_event_at": None, "managed": set()})
        bucket["subscriptions"].append(dict(row))
        bucket["active" if row["active"] else "inactive"] += 1
        bucket["managed"].add(row["managed"])
        if row["last_event_at"] and (bucket["last_event_at"] or "") < row["last_event_at"]:
            bucket["last_event_at"] = row["last_event_at"]
    for bucket in grouped.values():
        bucket["managed"] = "both" if len(bucket["managed"]) > 1 else next(iter(bucket["managed"]))
    return [grouped[k] for k in sorted(grouped)]


# -- who owns the list: the environment, or the page ------------------------
def env_is_authoritative(db) -> bool:
    """Should `PCOMIRROR_SUBSCRIPTIONS` be applied on this `serve` start?

    Only until the operator saves something on the page. After that the stored
    list wins, because re-applying the environment would silently undo whatever
    they came to the page to fix — and a restart is exactly when nobody is
    watching.
    """
    return db.get_meta(OVERRIDE_KEY) != "1"


def take_over(db) -> None:
    db.set_meta(OVERRIDE_KEY, "1")


def hand_back(db) -> None:
    db.execute("DELETE FROM mirror_meta WHERE key=?", (OVERRIDE_KEY,))


def apply_env(db, specs) -> list[dict]:
    """Apply `PCOMIRROR_SUBSCRIPTIONS`, unless the page has taken over.

    Returns one record per spec describing what happened, so the caller can say
    it out loud rather than leaving an operator to guess why their environment
    variable appears to do nothing.
    """
    if not specs:
        return []
    if not env_is_authoritative(db):
        return [{"spec": s, "outcome": "skipped", "token": None} for s in specs]
    out = []
    for spec in specs:
        token, created = upsert_subscription(
            db, spec.subscription_id, spec.event, spec.secret,
            spec.url_token or None, managed="env")
        out.append({"spec": spec, "outcome": "registered" if created else "updated",
                    "token": token})
    return out


class WebhookProcessor:
    def __init__(self, db, writer, ingestor):
        self.db = db
        self.writer = writer
        self.ingestor = ingestor

    # -- edge: verify + capture + ack -------------------------------------
    def receive(self, url_token: str, raw: bytes, signature: str | None) -> tuple[int, str]:
        """Verify, capture, ack. One token may carry several subscriptions.

        The signature is what selects which of them is delivering: every
        candidate secret is tried, and the one that verifies is by construction
        the subscription Planning Center sent this from. The loop does not break
        on a match, so how long it takes does not say *which* subscription
        matched — only the per-comparison constant-time guarantee is `verify`'s.

        A subscription with a **blank secret** is not checked at all (see
        `is_unverified`). Signed subscriptions are still tried first, so adding
        one alongside them does not stop the signed ones being attributed
        correctly — but it does mean this URL now accepts whatever is posted to
        it, which is the whole point and also the whole risk.
        """
        subs = self.db.query(
            "SELECT * FROM webhook_subscription WHERE url_token=? AND active=1 "
            "ORDER BY event_name, subscription_pco_id", (url_token,))
        if not subs:
            return 404, "unknown token"                         # NOT 410
        matched = None
        for candidate in subs:
            if not is_unverified(candidate) and \
                    verify(candidate["authenticity_secret"], raw, signature) and matched is None:
                matched = candidate
        open_subs = [s for s in subs if is_unverified(s)]
        if matched is None and not open_subs:
            return 401, "bad signature"                         # NOT 410
        try:
            env = json.loads(raw)
        except json.JSONDecodeError:
            return 503, "unparseable"                           # pre-capture: PCO retries safely
        # Attribution only. With no signature there is nothing that can name the
        # sender, so the delivery's own event name is the best available guess at
        # which unchecked subscription it belongs to — and it is only a label on
        # the delivery row, never a decision about whether to accept it.
        sub = matched or _by_event_name(open_subs, env)
        delivery_id = env.get("id") or f"nodelivery-{now_iso()}"
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO webhook_delivery(delivery_id,subscription_pco_id,signature,raw_body,attempt) "
                "VALUES(?,?,?,?,?)",
                # `signature` is NOT NULL and a sender with nothing to sign with
                # may send no header at all. Storing the absence as an empty
                # string keeps the audit row; letting the insert fail would have
                # answered 503 and had PCO redeliver, forever.
                (delivery_id, sub["subscription_pco_id"], signature or "", raw,
                 env.get("attempt")))
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
            self.db.execute(
                "UPDATE webhook_subscription SET last_event_at=? WHERE subscription_pco_id=?",
                (now_iso(), sub["subscription_pco_id"]))
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
            # Not a failure. Planning Center offers events for resources this
            # mirror holds no table for, and an operator may legitimately
            # subscribe to one — to see it arriving, or because they ticked the
            # whole list. Dead-lettering those buried the events that really did
            # break among ones that were only ever going to be filed.
            return self._ignore(ev, f"no table for {res_token}")
        try:
            if res_token == "person_merger":
                self._handle_merge(payload)
            elif action == "destroyed":
                uat = (payload.get("attributes") or {}).get("updated_at")
                self.writer.tombstone(r.table, payload["id"], uat, "destroyed")
            else:  # created | updated | refreshed
                self.writer.route(payload, "webhook")
                if self._is_thin(r, payload):
                    self._enqueue_or_defer(r, payload["id"])
                if action == "refreshed":
                    self._forget_child_walks(r, payload["id"])
            self.db.execute(
                "UPDATE webhook_event SET status='done', processed_at=? WHERE event_id=?",
                (now_iso(), ev["event_id"]))
        except Exception as e:  # noqa: BLE001
            self._retry_or_dead(ev, str(e))

    def _handle_merge(self, payload: dict) -> None:
        a = payload.get("attributes", {})
        keep, gone = a.get("person_to_keep_id"), a.get("person_to_remove_id")
        merger_id = payload.get("id", f"merge-{keep}-{gone}")
        # A merge the poll already applied is not news. Without this a redelivery,
        # or simply the poll getting there first, queues the survivor for another
        # hydration — one PCO request for an answer we have.
        if not self.ingestor._merger_is_new(merger_id):
            return
        if gone:
            self.writer.tombstone("person", gone, None, "merged", merged_into=keep)
        if keep:
            self.ingestor.enqueue_hydration("person", keep, reason="merge_survivor")
        self.ingestor._record_merger(merger_id, payload, "webhook")

    def _forget_child_walks(self, r, pco_id: str) -> None:
        """`refreshed` says the *contents* changed, not the record.

        `people.v2.events.list.refreshed` fires when a list is re-run, and the
        payload is the List itself — whose own attributes may be identical. The
        thing that changed is its results, which are walked per parent, so the
        answer is to drop this parent's walk record: the read path re-walks a
        parent it has never seen, and now it has never seen this one. Without
        this, the only visible effect of a refresh event would be a `refreshed_at`
        that moved.
        """
        for child in registry.RESOURCES.values():
            if child.method == "nested_walk" and child.parent == r.name:
                self.db.execute(
                    "DELETE FROM nested_walk_state WHERE resource_type=? AND parent_pco_id=?",
                    (child.name, pco_id))

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

    def _ignore(self, ev: dict, why: str) -> None:
        """Kept, applied to nothing, and counted — not buried in the dead letters.

        The row stays in the inbox with the payload intact, so adding a table for
        the resource later leaves the evidence that it was arriving all along.
        """
        self.db.execute(
            "UPDATE webhook_event SET status='ignored', processed_at=?, last_error=? "
            "WHERE event_id=?", (now_iso(), why, ev["event_id"]))

    def _dead(self, ev: dict, err: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO webhook_dead_letter(event_id,event_name,payload,last_error,attempts) "
            "VALUES(?,?,?,?,?)",
            (ev["event_id"], ev["event_name"], ev["payload"], err, ev["process_attempts"] + 1))
        self.db.execute("UPDATE webhook_event SET status='dead', last_error=? WHERE event_id=?",
                        (err, ev["event_id"]))
