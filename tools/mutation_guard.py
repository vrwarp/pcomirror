"""A transport that refuses every write except the one it has been armed for.

Verifying the write path (`DESIGN.md` §8.4) means writing to a real Planning
Center organization, because that is the only place the behaviour exists. The
danger is obvious: the credential that can create a person can destroy one, and
`DELETE /people/{id}` is immediate.

So the safety lives here, in the transport, rather than in whoever is driving it
being careful. Wrap the real transport in a `MutationGuard`, and:

* `GET` always passes.
* `POST` is allowed only to `/people`, only while armed to create, only once, and
  only for a body whose surname carries the test sentinel.
* `PATCH` and `DELETE` are allowed only for the id *this run created*, only while
  armed for that operation, and within a per-operation limit.
* `PUT` and everything else are refused unconditionally.
* Arming is one operation at a time and is spent on use, so a stray call cannot
  ride along behind an intended one.

No body may grant permissions, set a login identifier, attach the record to any
other record, or remove the sentinel — a test record must never come to resemble
a real one, and must never be reachable from one.

The refusal logic is covered by `tests/test_mutation_guard.py`, which runs
offline against a transport that fails if it is ever reached. Nothing in this
module talks to the network by itself; it only ever wraps something that does.
"""
from __future__ import annotations

import json
import urllib.parse

SENTINEL_PREFIX = "ZZPCOMIRRORTEST-"

#: Attributes a test record may never carry. Permissions and login identifiers
#: would make it a usable account; the rest are personal fields with no business
#: being invented.
FORBIDDEN_ATTRIBUTES = frozenset({
    "people_permissions", "site_administrator", "accounting_administrator",
    "login_identifier", "medical_notes", "avatar",
})

#: How many times each operation may run in a single session. Patching is allowed
#: more than once because finding a value the API actually rejects takes probing,
#: and every attempt is still confined to the record this run created.
LIMITS = {"create": 1, "patch": 4, "delete": 1}


class MutationRefused(RuntimeError):
    """Raised instead of sending. Always the safe outcome."""


class MutationGuard:
    """Wraps a `Transport`; see the module docstring for the rules."""

    def __init__(self, inner, sentinel_prefix: str = SENTINEL_PREFIX):
        self._inner = inner
        self.sentinel_prefix = sentinel_prefix
        self.armed: str | None = None
        self.created_id: str | None = None
        self.counts: dict[str, int] = {}
        self.log: list[tuple[str, str, int]] = []

    # -- arming -----------------------------------------------------------
    def arm(self, operation: str) -> None:
        if operation not in LIMITS:
            raise MutationRefused(f"{operation!r} is not an operation this guard knows")
        used = self.counts.get(operation, 0)
        if used >= LIMITS[operation]:
            raise MutationRefused(
                f"{operation} has already run {used}x, which is its limit for this session")
        self.armed = operation

    def _spend(self, operation: str) -> None:
        self.counts[operation] = self.counts.get(operation, 0) + 1
        self.armed = None

    # -- transport --------------------------------------------------------
    def send(self, method, url, headers, body):
        path = urllib.parse.urlsplit(url).path
        marker = "/people/v2"
        sub = path[path.find(marker) + len(marker):] if marker in path else path

        if method == "GET":
            return self._forward(method, url, headers, body, sub)
        if method == "POST":
            self._check_create(sub, body)
            self._spend("create")
            return self._forward(method, url, headers, body, sub)
        if method == "PATCH":
            self._check_targeted("patch", sub)
            self._check_patch_body(body)
            self._spend("patch")
            return self._forward(method, url, headers, body, sub)
        if method == "DELETE":
            self._check_targeted("delete", sub)
            self._spend("delete")
            return self._forward(method, url, headers, body, sub)
        raise MutationRefused(f"{method} is never allowed by this guard")

    def _forward(self, method, url, headers, body, sub):
        resp = self._inner.send(method, url, headers, body)
        self.log.append((method, sub, resp.status))
        return resp

    # -- checks -----------------------------------------------------------
    def _check_create(self, sub: str, body: bytes) -> None:
        if self.armed != "create":
            raise MutationRefused(f"POST {sub} while not armed to create")
        if sub != "/people":
            raise MutationRefused(f"POST is only ever allowed to /people, not {sub}")
        if self.counts.get("create", 0) >= LIMITS["create"]:
            raise MutationRefused("a person has already been created in this session")
        data = _data(body)
        if data.get("type") != "Person":
            raise MutationRefused("refusing to create anything but a Person")
        attrs = data.get("attributes") or {}
        if not str(attrs.get("last_name", "")).startswith(self.sentinel_prefix):
            raise MutationRefused("refusing to create a person without the test sentinel")
        self._check_shared(data, attrs)

    def _check_targeted(self, operation: str, sub: str) -> None:
        """`PATCH`/`DELETE` may only ever address the record this run created."""
        if self.armed != operation:
            raise MutationRefused(f"{operation.upper()} {sub} while not armed to {operation}")
        if not self.created_id:
            raise MutationRefused(
                f"nothing was created in this session, so nothing may be {operation}ed")
        if sub != f"/people/{self.created_id}":
            raise MutationRefused(
                f"{operation.upper()} is only ever allowed for the person this session "
                f"created ({self.created_id}), not {sub}")
        if self.counts.get(operation, 0) >= LIMITS[operation]:
            raise MutationRefused(f"the {operation} limit for this session is spent")

    def _check_patch_body(self, body: bytes) -> None:
        data = _data(body)
        if data.get("type") != "Person":
            raise MutationRefused("refusing to patch anything but a Person")
        if str(data.get("id")) != str(self.created_id):
            raise MutationRefused(
                f"body id {data.get('id')} is not the person this session created "
                f"({self.created_id})")
        attrs = data.get("attributes") or {}
        if "last_name" in attrs and not str(attrs["last_name"]).startswith(self.sentinel_prefix):
            raise MutationRefused("refusing to remove the test sentinel from the record")
        self._check_shared(data, attrs)

    def _check_shared(self, data: dict, attrs: dict) -> None:
        present = FORBIDDEN_ATTRIBUTES & set(attrs)
        if present:
            raise MutationRefused(
                f"refusing to set privileged or sensitive attributes: {sorted(present)}")
        if data.get("relationships"):
            raise MutationRefused("refusing to attach the test record to any other record")


def _data(body) -> dict:
    if not body:
        raise MutationRefused("refusing to send a write with no body")
    try:
        return json.loads(body)["data"]
    except (ValueError, KeyError, TypeError) as e:
        raise MutationRefused(f"unreadable JSON:API body: {e}") from None
