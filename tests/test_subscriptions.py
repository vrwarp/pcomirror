"""Webhook subscriptions: one receiver for many events, and who gets to set them.

Three things are pinned here, because each has a failure mode that is silent:

  * a receiver URL carrying several event types verifies each against the right
    secret — get this wrong and half the deliveries 401 with nothing to say why;
  * `PCOMIRROR_SUBSCRIPTIONS` stops being applied once the operator page has
    taken the list over — get this wrong and a restart quietly undoes whatever
    somebody came to the page to fix, at the moment nobody is watching;
  * an event for a resource the mirror holds no table for is *recorded*, not
    dead-lettered — get this wrong and the queue that is supposed to mean
    "something broke" fills with events that were only ever going to be filed.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
import urllib.parse

from base import build, wsgi_call, wsgi_get
from pcomirror import adminauth, pcoevents, webhooks
from pcomirror.config import Settings, parse_subscriptions
from pcomirror.db import Database

SECRET = "sec"
GOOD_PASSWORD = "a-long-enough-password"


def _sign(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _delivery(event_name, payload, delivery_id="d1", event_id="ev1"):
    return {"id": delivery_id, "attempt": 1,
            "data": [{"id": event_id, "type": "Event",
                      "attributes": {"name": event_name, "payload": json.dumps(payload)}}]}


class TestOneReceiverManyEvents(unittest.TestCase):
    """Planning Center makes a subscription per event name but lets them share a
    URL — its own console does exactly that. Each carries its own secret."""

    def setUp(self):
        self.m, self.fake = build()
        self.token = "shared-token-01"
        webhooks.upsert_subscription(
            self.m.db, "sub-person", "people.v2.events.person.updated", "whsec_person",
            self.token)
        webhooks.upsert_subscription(
            self.m.db, "sub-email", "people.v2.events.email.created", "whsec_email",
            self.token)

    def _post(self, event_name, payload, secret, event_id="ev1", delivery_id="d1"):
        raw = json.dumps(_delivery(event_name, payload, delivery_id, event_id)).encode()
        return self.m.webhooks.receive(self.token, raw, _sign(secret, raw))

    def test_two_events_share_one_token(self):
        rows = self.m.db.query(
            "SELECT * FROM webhook_subscription WHERE url_token=?", (self.token,))
        self.assertEqual(len(rows), 2)

    def test_each_secret_is_accepted_on_the_shared_url(self):
        person = {"id": "1", "type": "Person",
                  "attributes": {"first_name": "Ada", "last_name": "L", "status": "active",
                                 "created_at": "2026-01-01T00:00:00Z",
                                 "updated_at": "2026-01-01T00:00:00Z"}}
        self.assertEqual(self._post("people.v2.events.person.updated", person,
                                    "whsec_person")[0], 204)
        email = {"id": "9", "type": "Email",
                 "attributes": {"address": "a@x.org", "created_at": "2026-01-01T00:00:00Z",
                                "updated_at": "2026-01-01T00:00:00Z"},
                 "relationships": {"person": {"data": {"type": "Person", "id": "1"}}}}
        self.assertEqual(self._post("people.v2.events.email.created", email, "whsec_email",
                                    event_id="ev2", delivery_id="d2")[0], 204)
        self.m.webhooks.drain()
        self.assertEqual(self.m.db.query_one(
            "SELECT first_name FROM person WHERE pco_id='1'")["first_name"], "Ada")
        self.assertEqual(self.m.db.query_one(
            "SELECT address FROM email WHERE pco_id='9'")["address"], "a@x.org")

    def test_a_secret_from_neither_subscription_is_still_401(self):
        self.assertEqual(self._post("people.v2.events.person.updated", {"id": "1"},
                                    "whsec_wrong")[0], 401)

    def test_the_signing_secret_picks_the_subscription_not_the_payload(self):
        """A body claiming to be an email event, signed with the person secret,
        is attributed to the person subscription — the half of the request an
        attacker controls does not get to choose the key it is checked against."""
        self._post("people.v2.events.email.created", {"id": "9"}, "whsec_person")
        row = self.m.db.query_one("SELECT subscription_pco_id FROM webhook_delivery")
        self.assertEqual(row["subscription_pco_id"], "sub-person")

    def test_delivery_stamps_last_event_at(self):
        self._post("people.v2.events.person.updated", {"id": "1"}, "whsec_person")
        row = self.m.db.query_one(
            "SELECT last_event_at FROM webhook_subscription WHERE subscription_pco_id='sub-person'")
        self.assertIsNotNone(row["last_event_at"])
        untouched = self.m.db.query_one(
            "SELECT last_event_at FROM webhook_subscription WHERE subscription_pco_id='sub-email'")
        self.assertIsNone(untouched["last_event_at"])

    def test_pausing_one_event_leaves_the_others_receiving(self):
        webhooks.set_active(self.m.db, "sub-person", False)
        self.assertEqual(self._post("people.v2.events.person.updated", {"id": "1"},
                                    "whsec_person")[0], 401)
        self.assertEqual(self._post("people.v2.events.email.created", {"id": "9"},
                                    "whsec_email", event_id="e2", delivery_id="d2")[0], 204)

    def test_receivers_folds_subscriptions_by_url(self):
        found = webhooks.receivers(self.m.db)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["url_token"], self.token)
        self.assertEqual(len(found[0]["subscriptions"]), 2)


class TestUnmappedEventsAreRecorded(unittest.TestCase):
    def setUp(self):
        self.m, _ = build()
        self.token = "workflow-token-1"
        webhooks.upsert_subscription(
            self.m.db, "sub-wf", "people.v2.events.workflow_card.created", "whsec_wf",
            self.token)

    def test_event_without_a_table_is_kept_not_dead_lettered(self):
        raw = json.dumps(_delivery("people.v2.events.workflow_card.created",
                                   {"id": "77", "type": "WorkflowCard"})).encode()
        self.assertEqual(self.m.webhooks.receive(self.token, raw, _sign("whsec_wf", raw))[0], 204)
        self.m.webhooks.drain()
        row = self.m.db.query_one("SELECT status, payload FROM webhook_event WHERE event_id='ev1'")
        self.assertEqual(row["status"], "ignored")
        self.assertIn('"77"', row["payload"])           # the evidence survives
        self.assertEqual(self.m.db.query_one(
            "SELECT count(*) c FROM webhook_dead_letter")["c"], 0)

    def test_a_real_failure_still_dead_letters(self):
        """The point of not dead-lettering an unmapped event is that the queue
        keeps meaning "something broke". So something breaking must still land."""
        self.m.db.execute(
            "INSERT INTO webhook_event(event_id,event_name,resource_type,action,payload,"
            "process_attempts) VALUES(?,?,?,?,?,?)",
            ("bad", "people.v2.events.person.updated", "person", "updated", "{not json", 7))
        self.m.webhooks.drain()
        self.assertEqual(self.m.db.query_one(
            "SELECT count(*) c FROM webhook_dead_letter")["c"], 1)


class TestEventsForNewlyMirroredResources(unittest.TestCase):
    """The resources the People webhook console offers that had no table before."""

    def setUp(self):
        self.m, _ = build()
        self.token = "everything-01"

    def _send(self, event, payload, secret="whsec", event_id="ev1", delivery_id="d1"):
        webhooks.upsert_subscription(self.m.db, f"sub-{event}", event, secret, self.token)
        raw = json.dumps(_delivery(event, payload, delivery_id, event_id)).encode()
        code, _ = self.m.webhooks.receive(self.token, raw, _sign(secret, raw))
        self.assertEqual(code, 204)
        self.m.webhooks.drain()

    def test_note_lands_in_its_own_table(self):
        self._send("people.v2.events.note.created",
                   {"id": "5", "type": "Note",
                    "attributes": {"note": "spoke after the service",
                                   "created_at": "2026-01-01T00:00:00Z",
                                   "updated_at": "2026-01-01T00:00:00Z"},
                    "relationships": {"person": {"data": {"type": "Person", "id": "1"}}}})
        row = self.m.db.query_one("SELECT note, person_pco_id FROM note WHERE pco_id='5'")
        self.assertEqual((row["note"], row["person_pco_id"]), ("spoke after the service", "1"))

    def test_list_result_projects_its_list_from_the_self_link(self):
        """PCO serves a list result only under its list, and the payload carries
        the owning id in `links.self` — the same shape as a household membership."""
        self._send("people.v2.events.list_result.created",
                   {"id": "88", "type": "ListResult",
                    "attributes": {"created_at": "2026-01-01T00:00:00Z",
                                   "updated_at": "2026-01-01T00:00:00Z"},
                    "relationships": {"person": {"data": {"type": "Person", "id": "1"}}},
                    "links": {"self": "https://api.planningcenteronline.com/people/v2"
                                      "/lists/42/list_results/88"}})
        row = self.m.db.query_one("SELECT list_pco_id FROM list_result WHERE pco_id='88'")
        self.assertEqual(row["list_pco_id"], "42")

    def test_form_submission_projects_its_form_from_the_self_link(self):
        self._send("people.v2.events.form_submission.created",
                   {"id": "31", "type": "FormSubmission",
                    "attributes": {"created_at": "2026-01-01T00:00:00Z",
                                   "updated_at": "2026-01-01T00:00:00Z"},
                    "links": {"self": "https://api.planningcenteronline.com/people/v2"
                                      "/forms/7/form_submissions/31"}})
        row = self.m.db.query_one("SELECT form_pco_id FROM form_submission WHERE pco_id='31'")
        self.assertEqual(row["form_pco_id"], "7")

    def test_list_refreshed_drops_the_walk_record_for_that_list(self):
        """`refreshed` says the results changed, not the list. Nothing about the
        List's own attributes need move, so the only useful response is to make
        the mirror re-walk that list's results."""
        self.m.db.execute(
            "INSERT INTO nested_walk_state(resource_type,parent_pco_id,row_count) VALUES(?,?,?)",
            ("list_result", "42", 3))
        self._send("people.v2.events.list.refreshed",
                   {"id": "42", "type": "List",
                    "attributes": {"name": "Newcomers", "created_at": "2026-01-01T00:00:00Z",
                                   "updated_at": "2026-01-01T00:00:00Z"}})
        self.assertFalse(self.m.ingestor.parent_walked("list_result", "42"))
        self.assertEqual(self.m.db.query_one(
            "SELECT name FROM list WHERE pco_id='42'")["name"], "Newcomers")

    def test_a_destroyed_list_takes_its_results_with_it(self):
        """The newly walked collections join the ownership cascade like any other
        per-parent child: `GET /lists/{id}/list_results` is the only place these
        rows exist, so once the list is gone nothing will ever tombstone them."""
        self._send("people.v2.events.list_result.created",
                   {"id": "88", "type": "ListResult",
                    "attributes": {"created_at": "2026-01-01T00:00:00Z",
                                   "updated_at": "2026-01-01T00:00:00Z"},
                    "links": {"self": "https://api.planningcenteronline.com/people/v2"
                                      "/lists/42/list_results/88"}})
        self.m.writer.upsert("list", "42", {
            "id": "42", "type": "List",
            "attributes": {"name": "Newcomers", "created_at": "2026-01-01T00:00:00Z",
                           "updated_at": "2026-01-01T00:00:00Z"}}, "backfill")
        self.assertIsNone(self.m.db.query_one(
            "SELECT deleted_at FROM list_result WHERE pco_id='88'")["deleted_at"])
        self.m.writer.tombstone("list", "42", "2026-02-01T00:00:00Z", "destroyed")
        row = self.m.db.query_one(
            "SELECT deleted_at, tombstone_reason FROM list_result WHERE pco_id='88'")
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(row["tombstone_reason"], self.m.writer.OWNER_DELETED)

    def test_every_catalogued_event_is_handled(self):
        """Nothing in the offered list may be a dead end: each is either applied
        to the mirror, the merge path, or deliberately recorded."""
        for name in pcoevents.builtin():
            verdict, why = pcoevents.handling(name)
            self.assertIn(verdict, ("mirrored", "merge", "recorded"), name)
            self.assertTrue(why, name)
        recorded = [n for n in pcoevents.builtin()
                    if pcoevents.handling(n)[0] == "recorded"]
        self.assertEqual(recorded, [], f"catalogued but unmirrored: {recorded}")


class TestEnvVersusAdmin(unittest.TestCase):
    def setUp(self):
        self.m, _ = build()
        self.specs = parse_subscriptions(
            "sub1:people.v2.events.person.updated:env-token-01:whsec_env")

    def test_environment_applies_while_it_owns_the_list(self):
        applied = webhooks.apply_env(self.m.db, self.specs)
        self.assertEqual([a["outcome"] for a in applied], ["registered"])
        self.assertEqual(self.m.db.query_one(
            "SELECT managed FROM webhook_subscription WHERE subscription_pco_id='sub1'"
        )["managed"], "env")

    def test_taking_over_stops_the_environment_being_applied(self):
        webhooks.apply_env(self.m.db, self.specs)
        webhooks.upsert_subscription(self.m.db, "sub1",
                                     "people.v2.events.person.updated", "whsec_admin",
                                     "env-token-01", managed="admin")
        webhooks.take_over(self.m.db)
        applied = webhooks.apply_env(self.m.db, self.specs)
        self.assertEqual([a["outcome"] for a in applied], ["skipped"])
        # the operator's secret survives the restart the environment would have won
        self.assertEqual(self.m.db.query_one(
            "SELECT authenticity_secret FROM webhook_subscription "
            "WHERE subscription_pco_id='sub1'")["authenticity_secret"], "whsec_admin")

    def test_handing_back_lets_the_environment_win_again(self):
        webhooks.take_over(self.m.db)
        webhooks.hand_back(self.m.db)
        applied = webhooks.apply_env(self.m.db, self.specs)
        self.assertEqual([a["outcome"] for a in applied], ["registered"])

    def test_no_specs_is_not_a_takeover_signal(self):
        self.assertEqual(webhooks.apply_env(self.m.db, []), [])
        self.assertTrue(webhooks.env_is_authoritative(self.m.db))


class TestEventCatalogue(unittest.TestCase):
    def setUp(self):
        self.m, _ = build()

    def test_builtin_covers_the_console_list(self):
        names = set(pcoevents.builtin())
        for expected in ("people.v2.events.person.updated",
                         "people.v2.events.form_submission.created",
                         "people.v2.events.list.refreshed",
                         "people.v2.events.list_result.destroyed",
                         "people.v2.events.note.created",
                         "people.v2.events.person_merger.created"):
            self.assertIn(expected, names)

    def test_catalogue_defaults_to_builtin_and_remembers_a_refresh(self):
        self.assertEqual(pcoevents.catalogue(self.m.db)["source"], "built in")
        pcoevents.store(self.m.db, ["people.v2.events.person.updated",
                                    "people.v2.events.workflow_card.created"],
                        "2026-07-01T00:00:00Z")
        held = pcoevents.catalogue(self.m.db)
        self.assertEqual(held["source"], "planning center")
        self.assertIn("people.v2.events.workflow_card.created", held["events"])
        pcoevents.forget(self.m.db)
        self.assertEqual(pcoevents.catalogue(self.m.db)["source"], "built in")

    def test_a_corrupt_stored_catalogue_falls_back_rather_than_breaking_the_page(self):
        self.m.db.set_meta(pcoevents.CATALOGUE_KEY, "not json")
        self.assertEqual(pcoevents.catalogue(self.m.db)["source"], "built in")

    def test_webhooks_base_url_is_derived_from_the_people_one(self):
        self.assertEqual(
            pcoevents.webhooks_base(Settings(
                pco_base_url="https://api.planningcenteronline.com/people/v2")),
            "https://api.planningcenteronline.com/webhooks/v2")
        self.assertEqual(
            pcoevents.webhooks_base(Settings(pco_base_url="http://stub.local/people/v2")),
            "http://stub.local/webhooks/v2")
        self.assertEqual(
            pcoevents.webhooks_base(Settings(pco_webhooks_base_url="http://elsewhere/hooks/")),
            "http://elsewhere/hooks")

    def test_settings_read_the_override_from_the_environment(self):
        s = Settings.from_env({"PCO_WEBHOOKS_BASE_URL": "http://x/webhooks/v2"})
        self.assertEqual(s.pco_webhooks_base_url, "http://x/webhooks/v2")

    def test_refresh_pages_and_stores(self):
        seen = []

        class Client:
            s = Settings()

            def get(self, path, params=None, priority=None, base=None, max_wait=None):
                seen.append((path, base, params["offset"]))
                page = ([{"attributes": {"name": f"people.v2.events.r{i}.created"}}
                         for i in range(100)] if params["offset"] == 0 else
                        [{"attributes": {"name": "people.v2.events.tail.created"}}])
                return type("R", (), {"ok": True, "status": 200,
                                      "json": lambda self, p=page: {"data": p}})()

        names = pcoevents.refresh(self.m.db, Client(), "2026-07-01T00:00:00Z")
        self.assertEqual([s[2] for s in seen], [0, 100])
        self.assertEqual(seen[0][1], "https://api.planningcenteronline.com/webhooks/v2")
        self.assertIn("people.v2.events.tail.created", names)
        self.assertEqual(pcoevents.catalogue(self.m.db)["source"], "planning center")


class TestSchemaMigration(unittest.TestCase):
    """An existing mirror was created when a receiver served exactly one event —
    `url_token` was UNIQUE, and SQLite will not drop the index that made it so."""

    OLD = """
    CREATE TABLE webhook_subscription (
      subscription_pco_id TEXT PRIMARY KEY, event_name TEXT NOT NULL, resource TEXT,
      action TEXT, url_token TEXT UNIQUE NOT NULL, authenticity_secret TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1, last_event_at TEXT,
      created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );
    INSERT INTO webhook_subscription(subscription_pco_id,event_name,resource,action,
      url_token,authenticity_secret,last_event_at)
    VALUES('sub1','people.v2.events.person.updated','person','updated','tok-aaaa-01',
           'whsec_a','2026-07-01T00:00:00Z');
    """

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "old.db")
        conn = sqlite3.connect(self.path)
        conn.executescript(self.OLD)
        conn.commit()
        conn.close()
        self.db = Database(self.path)
        self.db.init_schema()

    def tearDown(self):
        self.db.close()

    def test_existing_rows_survive_the_rebuild(self):
        row = self.db.query_one("SELECT * FROM webhook_subscription")
        self.assertEqual(row["subscription_pco_id"], "sub1")
        self.assertEqual(row["authenticity_secret"], "whsec_a")
        self.assertEqual(row["last_event_at"], "2026-07-01T00:00:00Z")
        self.assertEqual(row["managed"], "env")     # the column it did not have

    def test_a_second_event_may_now_share_the_token(self):
        webhooks.upsert_subscription(self.db, "sub2", "people.v2.events.person.created",
                                     "whsec_b", "tok-aaaa-01")
        self.assertEqual(self.db.query_one(
            "SELECT count(*) c FROM webhook_subscription WHERE url_token='tok-aaaa-01'")["c"], 2)

    def test_migration_is_idempotent(self):
        self.db.init_schema()
        self.db.init_schema()
        self.assertEqual(self.db.query_one(
            "SELECT count(*) c FROM webhook_subscription")["c"], 1)
        self.assertIsNone(self.db.query_one(
            "SELECT name FROM sqlite_master WHERE name='webhook_subscription_old'"))


class TestAddSubscriptionCommand(unittest.TestCase):
    """`add-subscription` mints a token only when there is not one already. The
    URL PCO is delivering to must survive a re-run, and several events named in
    one command must land on one receiver rather than three."""

    def setUp(self):
        from pcomirror import cli
        self.cli = cli
        self.m, _ = build()
        # The command opens its own Mirror from the environment; point it at this
        # one so what runs is the command itself rather than a copy of its logic.
        self._real_mirror = cli._mirror
        cli._mirror = lambda: self.m
        self.addCleanup(setattr, cli, "_mirror", self._real_mirror)

    def _add(self, sub_id, events, secret, url_token=None):
        with contextlib.redirect_stdout(io.StringIO()):     # it prints the URLs it settles on
            self.cli.cmd_add_subscription(type("Args", (), {
                "subscription_id": sub_id, "event": events,
                "secret": secret, "url_token": url_token})())
        return {r["url_token"] for r in webhooks.listing(self.m.db)
                if r["subscription_pco_id"].startswith(sub_id)}

    def test_rerunning_keeps_the_registered_url(self):
        first = self._add("sub1", ["people.v2.events.person.updated"], "s1")
        again = self._add("sub1", ["people.v2.events.person.updated"], "s2")
        self.assertEqual(first, again)
        self.assertEqual(self.m.db.query_one(
            "SELECT authenticity_secret FROM webhook_subscription "
            "WHERE subscription_pco_id='sub1'")["authenticity_secret"], "s2")

    def test_several_events_land_on_one_receiver(self):
        self._add("sub_m", ["people.v2.events.email.created",
                            "people.v2.events.note.created",
                            "people.v2.events.list.refreshed"], "s3")
        found = webhooks.receivers(self.m.db)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]["subscriptions"]), 3)


def _form(**fields) -> bytes:
    return urllib.parse.urlencode(fields).encode()


class TestWebhooksPage(unittest.TestCase):
    def setUp(self):
        self.m, _ = build(allow_anonymous=False)
        adminauth._clear_failures()
        self.cookie = self._configured_login()

    def get(self, path, query="", cookie=None):
        return wsgi_get(self.m.wsgi, path, query,
                        headers={"Cookie": f"{adminauth.COOKIE}={cookie or self.cookie}"})

    def post(self, path, body=b"", cookie=None):
        return wsgi_call(self.m.wsgi, "POST", path, body=body,
                         headers={"Cookie": f"{adminauth.COOKIE}={cookie or self.cookie}"})

    def _configured_login(self):
        _, headers, _ = wsgi_call(self.m.wsgi, "POST", "/admin/login",
                                  body=_form(password=SECRET))
        cookie = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        page = self.get("/admin/password", cookie=cookie)[2]
        csrf = re.search(rb'name=csrf value="([^"]+)"', page).group(1).decode()
        _, headers, _ = self.post("/admin/password", _form(
            csrf=csrf, password=GOOD_PASSWORD, confirm=GOOD_PASSWORD), cookie=cookie)
        return headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def csrf(self):
        page = self.get("/admin/webhooks")[2]
        return re.search(rb'name=csrf value="([^"]+)"', page).group(1).decode()

    def test_page_offers_every_catalogued_event(self):
        page = self.get("/admin/webhooks")[2].decode()
        for name in pcoevents.builtin():
            self.assertIn(f'value="{name}"', page)

    def test_adding_several_events_makes_one_receiver(self):
        body = urllib.parse.urlencode([
            ("csrf", self.csrf()), ("url_token", "person-events-01"), ("secret", "whsec_a"),
            ("event", "people.v2.events.person.updated"),
            ("event", "people.v2.events.person.created"),
            ("event", "people.v2.events.note.created")]).encode()
        status, headers, _ = self.post("/admin/webhooks/add", body)
        self.assertEqual(status, 303)
        self.assertIn("token=person-events-01", headers["Location"])
        found = webhooks.receivers(self.m.db)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]["subscriptions"]), 3)
        self.assertEqual({s["managed"] for s in found[0]["subscriptions"]}, {"admin"})

    def test_the_receiver_url_is_shown_so_it_can_be_pasted_into_pco(self):
        body = urllib.parse.urlencode([
            ("csrf", self.csrf()), ("url_token", "person-events-01"), ("secret", "whsec_a"),
            ("event", "people.v2.events.person.updated")]).encode()
        self.post("/admin/webhooks/add", body)
        page = self.get("/admin/webhooks", "token=person-events-01")[2].decode()
        self.assertIn("/pco/webhooks/person-events-01", page)

    def test_a_minted_token_is_usable_immediately(self):
        body = urllib.parse.urlencode([
            ("csrf", self.csrf()), ("secret", "whsec_a"),
            ("event", "people.v2.events.person.updated")]).encode()
        _, headers, _ = self.post("/admin/webhooks/add", body)
        token = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query)["token"][0]
        raw = json.dumps(_delivery("people.v2.events.person.updated", {"id": "1"})).encode()
        self.assertEqual(self.m.webhooks.receive(token, raw, _sign("whsec_a", raw))[0], 204)

    def test_saving_takes_the_list_over_from_the_environment(self):
        self.assertTrue(webhooks.env_is_authoritative(self.m.db))
        body = urllib.parse.urlencode([
            ("csrf", self.csrf()), ("secret", "whsec_a"),
            ("event", "people.v2.events.person.updated")]).encode()
        self.post("/admin/webhooks/add", body)
        self.assertFalse(webhooks.env_is_authoritative(self.m.db))
        page = self.get("/admin/webhooks")[2]
        self.assertIn(b"Hand back to the environment", page)

    def test_handing_back_restores_the_environment(self):
        webhooks.take_over(self.m.db)
        self.post("/admin/webhooks/source", _form(csrf=self.csrf(), to="environment"))
        self.assertTrue(webhooks.env_is_authoritative(self.m.db))

    def test_importing_the_environment_syntax(self):
        self.post("/admin/webhooks/import", _form(
            csrf=self.csrf(),
            subscriptions="sub1:people.v2.events.person.updated:pasted-token-1:whsec_a,"
                          "sub2:people.v2.events.email.created:pasted-token-1:whsec_b"))
        found = webhooks.receivers(self.m.db)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]["subscriptions"]), 2)

    def test_a_malformed_import_says_so_and_changes_nothing(self):
        status, _, page = self.post("/admin/webhooks/import",
                                    _form(csrf=self.csrf(), subscriptions="nonsense"))
        self.assertEqual(status, 200)
        self.assertIn(b"expected id:event:token:secret", page)
        self.assertEqual(webhooks.receivers(self.m.db), [])

    def test_removing_and_pausing(self):
        self.post("/admin/webhooks/add", urllib.parse.urlencode([
            ("csrf", self.csrf()), ("url_token", "person-events-01"), ("secret", "whsec_a"),
            ("event", "people.v2.events.person.updated")]).encode())
        sub_id = "person-events-01:person.updated"
        self.post("/admin/webhooks/toggle", _form(csrf=self.csrf(), id=sub_id, to="off"))
        self.assertEqual(self.m.db.query_one(
            "SELECT active FROM webhook_subscription WHERE subscription_pco_id=?",
            (sub_id,))["active"], 0)
        self.post("/admin/webhooks/remove", _form(csrf=self.csrf(), id=sub_id))
        self.assertEqual(webhooks.receivers(self.m.db), [])

    def test_a_bad_token_is_refused_with_a_reason(self):
        status, _, page = self.post("/admin/webhooks/add", urllib.parse.urlencode([
            ("csrf", self.csrf()), ("url_token", "short"), ("secret", "whsec_a"),
            ("event", "people.v2.events.person.updated")]).encode())
        self.assertEqual(status, 200)
        self.assertIn(b"8", page)
        self.assertEqual(webhooks.receivers(self.m.db), [])

    def test_no_events_and_no_secret_are_refused(self):
        _, _, page = self.post("/admin/webhooks/add",
                               _form(csrf=self.csrf(), secret="whsec_a"))
        self.assertIn(b"at least one event", page)
        _, _, page = self.post("/admin/webhooks/add", urllib.parse.urlencode([
            ("csrf", self.csrf()), ("event", "people.v2.events.person.updated")]).encode())
        self.assertIn(b"authenticity secret", page)

    def test_an_event_typed_by_hand_is_accepted(self):
        self.post("/admin/webhooks/add", _form(
            csrf=self.csrf(), url_token="typed-token-01", secret="whsec_a",
            other_event="people.v2.events.workflow_card.created"))
        row = self.m.db.query_one(
            "SELECT event_name FROM webhook_subscription WHERE url_token='typed-token-01'")
        self.assertEqual(row["event_name"], "people.v2.events.workflow_card.created")

    def test_the_page_says_what_each_event_will_do(self):
        self.post("/admin/webhooks/add", _form(
            csrf=self.csrf(), url_token="typed-token-01", secret="whsec_a",
            other_event="people.v2.events.workflow_card.created"))
        page = self.get("/admin/webhooks")[2]
        self.assertIn(b"recorded", page)
        self.assertIn(b"no table for it", page)

    def test_a_stale_csrf_is_refused(self):
        status, _, page = self.post("/admin/webhooks/add", _form(
            csrf="wrong", url_token="person-events-01", secret="whsec_a",
            other_event="people.v2.events.person.updated"))
        self.assertEqual(status, 200)
        self.assertIn(b"Session expired", page)
        self.assertEqual(webhooks.receivers(self.m.db), [])

    def test_the_page_needs_a_session(self):
        status, headers, _ = wsgi_get(self.m.wsgi, "/admin/webhooks")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")

    def test_secrets_are_never_rendered(self):
        self.post("/admin/webhooks/add", _form(
            csrf=self.csrf(), url_token="person-events-01", secret="zz-distinctive-zz",
            other_event="people.v2.events.person.updated"))
        page = self.get("/admin/webhooks")[2]
        self.assertNotIn(b"zz-distinctive-zz", page)


if __name__ == "__main__":
    unittest.main()
