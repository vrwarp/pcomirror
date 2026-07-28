"""The refusal logic behind the live write check (`tools/mutation_guard.py`).

Every test here runs against a transport that raises if it is ever reached, so
the suite proves what the guard *blocks* without any network, any credential, or
any risk. The one thing these tests must never do is demonstrate a successful
write; that only ever happens by hand, against a real organization, following
`docs/mutation-testing.md`.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from mutation_guard import (  # noqa: E402
    LIMITS, SENTINEL_PREFIX, MutationGuard, MutationRefused,
)

PCO = "https://api.planningcenteronline.com/people/v2"
# Both synthetic. A real record id has no business in a repository, least of all
# in the file that exists to keep real records unreachable.
CREATED = "100000001"          # stands for the id a session created
SOMEBODY_ELSE = "100000002"    # stands for a record the guard must never reach


class Unreachable:
    """Fails loudly if the guard ever lets something through to it."""

    def __init__(self):
        self.calls = []

    def send(self, method, url, headers, body):
        self.calls.append((method, url))
        raise AssertionError(f"a blocked request reached the network: {method} {url}")


class Accepting:
    """Records what it was asked to send, for the cases that should pass."""

    class _Resp:
        status, headers, body = 200, {}, b"{}"

    def __init__(self):
        self.calls = []

    def send(self, method, url, headers, body):
        self.calls.append((method, url))
        return self._Resp()


def person(**attrs):
    body = {"type": "Person", "attributes": {"last_name": SENTINEL_PREFIX + "TOKEN", **attrs}}
    return json.dumps({"data": body}).encode()


def patch(pco_id=CREATED, relationships=None, **attrs):
    data = {"type": "Person", "id": pco_id,
            "attributes": {"last_name": SENTINEL_PREFIX + "TOKEN", **attrs}}
    if relationships:
        data["relationships"] = relationships
    return json.dumps({"data": data}).encode()


class GuardBlocks(unittest.TestCase):
    def setUp(self):
        self.inner = Unreachable()
        self.guard = MutationGuard(self.inner)
        self.guard.created_id = CREATED

    def tearDown(self):
        self.assertEqual(self.inner.calls, [], "something reached the network")

    def refuses(self, method, url, body=None):
        with self.assertRaises(MutationRefused):
            self.guard.send(method, url, {}, body)

    # -- nothing at all while disarmed ------------------------------------
    def test_every_write_is_refused_while_disarmed(self):
        self.refuses("POST", f"{PCO}/people", person())
        self.refuses("PATCH", f"{PCO}/people/{CREATED}", patch())
        self.refuses("DELETE", f"{PCO}/people/{CREATED}")

    def test_verbs_outside_the_contract_are_never_allowed(self):
        for method in ("PUT", "HEAD", "OPTIONS", "TRACE"):
            self.guard.arm("create")           # even fully armed
            self.refuses(method, f"{PCO}/people/{CREATED}", person())

    # -- create ------------------------------------------------------------
    def test_create_is_confined_to_the_people_collection(self):
        self.guard.arm("create")
        self.refuses("POST", f"{PCO}/households", person())

    def test_create_requires_the_sentinel(self):
        self.guard.arm("create")
        self.refuses("POST", f"{PCO}/people",
                     json.dumps({"data": {"type": "Person",
                                          "attributes": {"last_name": "Smith"}}}).encode())

    def test_create_refuses_privileged_attributes(self):
        for attr, value in (("people_permissions", "Manager"),
                            ("site_administrator", True),
                            ("login_identifier", "someone@example.org"),
                            ("medical_notes", "anything")):
            self.guard.arm("create")
            self.refuses("POST", f"{PCO}/people", person(**{attr: value}))

    def test_create_refuses_to_link_the_record_to_anything(self):
        self.guard.arm("create")
        body = json.loads(person())
        body["data"]["relationships"] = {"households": {"data": [{"type": "Household", "id": "1"}]}}
        self.refuses("POST", f"{PCO}/people", json.dumps(body).encode())

    def test_create_refuses_a_non_person(self):
        self.guard.arm("create")
        self.refuses("POST", f"{PCO}/people",
                     json.dumps({"data": {"type": "Household",
                                          "attributes": {"last_name": SENTINEL_PREFIX + "X"}}}).encode())

    # -- patch and delete are pinned to the created record -----------------
    def test_patch_and_delete_refuse_any_other_record(self):
        for operation, method, body in (("patch", "PATCH", patch()), ("delete", "DELETE", None)):
            self.guard.arm(operation)
            self.refuses(method, f"{PCO}/people/{SOMEBODY_ELSE}", body)
            self.guard.arm(operation)
            self.refuses(method, f"{PCO}/people", body)        # the whole collection

    def test_patch_refuses_a_body_naming_another_record(self):
        self.guard.arm("patch")
        self.refuses("PATCH", f"{PCO}/people/{CREATED}", patch(pco_id=SOMEBODY_ELSE))

    def test_patch_refuses_to_remove_the_sentinel(self):
        self.guard.arm("patch")
        self.refuses("PATCH", f"{PCO}/people/{CREATED}",
                     json.dumps({"data": {"type": "Person", "id": CREATED,
                                          "attributes": {"last_name": "Smith"}}}).encode())

    def test_patch_refuses_privileged_attributes_and_relationships(self):
        self.guard.arm("patch")
        self.refuses("PATCH", f"{PCO}/people/{CREATED}", patch(people_permissions="Manager"))
        self.guard.arm("patch")
        self.refuses("PATCH", f"{PCO}/people/{CREATED}",
                     patch(relationships={"households": {"data": [{"type": "Household", "id": "1"}]}}))

    def test_nothing_may_be_patched_or_deleted_before_something_is_created(self):
        self.guard.created_id = None
        self.guard.arm("patch")
        self.refuses("PATCH", f"{PCO}/people/{CREATED}", patch())
        self.guard.arm("delete")
        self.refuses("DELETE", f"{PCO}/people/{CREATED}")

    # -- arming is one operation at a time, and is spent -------------------
    def test_arming_one_operation_does_not_arm_another(self):
        self.guard.arm("create")
        self.refuses("PATCH", f"{PCO}/people/{CREATED}", patch())
        self.refuses("DELETE", f"{PCO}/people/{CREATED}")

    def test_an_unknown_operation_cannot_be_armed(self):
        with self.assertRaises(MutationRefused):
            self.guard.arm("merge")

    def test_a_malformed_body_is_refused_rather_than_sent(self):
        for bad in (None, b"", b"not json", b'{"no":"data key"}'):
            self.guard.arm("create")
            self.refuses("POST", f"{PCO}/people", bad)


class GuardAllows(unittest.TestCase):
    """The narrow path that is permitted — and that it closes behind itself."""

    def setUp(self):
        self.inner = Accepting()
        self.guard = MutationGuard(self.inner)

    def test_the_intended_sequence_passes_once_each(self):
        self.guard.arm("create")
        self.guard.send("POST", f"{PCO}/people", {}, person())
        self.guard.created_id = CREATED
        self.guard.arm("patch")
        self.guard.send("PATCH", f"{PCO}/people/{CREATED}", {}, patch(first_name="Updated"))
        self.guard.arm("delete")
        self.guard.send("DELETE", f"{PCO}/people/{CREATED}", {}, None)
        self.assertEqual([c[0] for c in self.inner.calls], ["POST", "PATCH", "DELETE"])

    def test_each_operation_is_spent_and_cannot_repeat(self):
        self.guard.created_id = CREATED
        for operation in ("create", "delete"):
            self.guard.counts[operation] = LIMITS[operation]
            with self.assertRaises(MutationRefused):
                self.guard.arm(operation)

    def test_reads_always_pass_and_never_consume_an_arming(self):
        self.guard.arm("create")
        self.guard.send("GET", f"{PCO}/people/{SOMEBODY_ELSE}", {}, None)
        self.assertEqual(self.guard.armed, "create", "a read must not disarm the guard")
        self.assertEqual(self.guard.counts, {})


class TestChildCollections(unittest.TestCase):
    """Contact details on the test record — and on nothing else.

    Added so the live procedure can observe what PCO does to a resource's
    *siblings*: setting `primary` demotes whatever held it before, silently, and
    the create response does not mention it. That is unobservable without a real
    write, and it is a real divergence when a mirror misses it.
    """

    def setUp(self):
        self.guard = MutationGuard(Unreachable())
        self.guard.created_id = CREATED

    def _body(self, rtype="Email", **attrs):
        return json.dumps({"data": {"type": rtype, "attributes": attrs or {"address": "x@y.z"}}}).encode()

    def _post(self, sub, body=None):
        return self.guard.send("POST", f"{PCO}{sub}", {}, body if body is not None else self._body())

    def test_refused_when_not_armed(self):
        with self.assertRaises(MutationRefused):
            self._post(f"/people/{CREATED}/emails")

    def test_refused_for_a_record_this_session_did_not_create(self):
        self.guard.arm("add_child")
        with self.assertRaises(MutationRefused):
            self._post(f"/people/{SOMEBODY_ELSE}/emails")

    def test_refused_when_nothing_was_created(self):
        self.guard.created_id = None
        self.guard.arm("add_child")
        with self.assertRaises(MutationRefused):
            self._post(f"/people/{CREATED}/emails")

    def test_refused_for_a_collection_outside_the_allowlist(self):
        self.guard.arm("add_child")
        for collection in ("households", "workflow_cards", "notes", "field_data"):
            with self.assertRaises(MutationRefused):
                self._post(f"/people/{CREATED}/{collection}")

    def test_refused_when_the_type_does_not_match_the_collection(self):
        self.guard.arm("add_child")
        with self.assertRaises(MutationRefused):
            self._post(f"/people/{CREATED}/emails", self._body(rtype="PhoneNumber"))

    def test_refused_when_the_body_would_attach_it_to_another_record(self):
        self.guard.arm("add_child")
        body = json.dumps({"data": {"type": "Email", "attributes": {"address": "x@y.z"},
                                    "relationships": {"person": {"data": {"id": SOMEBODY_ELSE}}}}}).encode()
        with self.assertRaises(MutationRefused):
            self._post(f"/people/{CREATED}/emails", body)

    def test_arming_is_spent_so_a_stray_call_cannot_ride_along(self):
        self.guard.arm("add_child")
        # A legitimate call gets through the checks and reaches the transport,
        # which is `Unreachable` here — so the AssertionError *is* the proof that
        # the guard allowed it. What matters is what it left behind.
        with self.assertRaises(AssertionError):
            self._post(f"/people/{CREATED}/emails")
        self.assertIsNone(self.guard.armed)
        with self.assertRaises(MutationRefused):     # the next one is not armed
            self._post(f"/people/{CREATED}/emails")

    def test_the_limit_is_enforced(self):
        for _ in range(LIMITS["add_child"]):
            self.guard.arm("add_child")
            self.guard.counts["add_child"] = self.guard.counts.get("add_child", 0) + 1
        with self.assertRaises(MutationRefused):
            self.guard.arm("add_child")

    def test_a_top_level_create_is_unaffected(self):
        self.guard.arm("create")
        with self.assertRaises(MutationRefused):     # no sentinel surname
            self.guard.send("POST", f"{PCO}/people", {}, json.dumps(
                {"data": {"type": "Person", "attributes": {"last_name": "Real"}}}).encode())


if __name__ == "__main__":
    unittest.main()
