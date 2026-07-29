"""Which webhook events Planning Center People emits, and what the mirror does
with each.

Planning Center's model is **one subscription per event name**: a
`WebhookSubscription` carries a single `name`, a `url`, and its own
`authenticity_secret` (`/webhooks/v2/open_api/2022-10-20`). Nothing says those
URLs have to be distinct, which is why PCO's own console lets you tick a dozen
events under one webhook — it creates a dozen subscriptions pointing at the same
address. So **one receiver URL serves many event types**, and the mirror is built
that way: `webhook_subscription.url_token` is not unique, and the receiver
resolves which subscription a delivery came from by the secret that signed it.

The catalogue below is the picker's own list, and it is a *default*, not a
gate. An operator may type any event name, and `refresh()` replaces the built-in
list with whatever `GET /webhooks/v2/available_events` currently returns — the
one authoritative answer, and the only one that stays right when PCO adds an
event. Nothing here decides whether an event is *accepted*; it decides what the
admin page offers and what it says will happen to each.

What happens to an arriving event is decided by the registry, not by this file:

  * `mirrored`  — the resource has a table, so the payload is written to it and
                  `destroyed` tombstones it.
  * `merge`     — `person_merger`, which tombstones the losing person and
                  re-fetches the survivor.
  * `recorded`  — no table for it: captured in the inbox, marked `ignored`, and
                  visible on the admin page. Deliberately not dead-lettered — an
                  event the mirror has no use for is not a failure, and burying
                  it in the dead-letter queue hides the ones that are.
"""
from __future__ import annotations

import json

from . import registry

#: Where a catalogue fetched from PCO is kept, so the page does not spend a
#: request every time it is opened.
CATALOGUE_KEY = "webhook_available_events"

#: The path, on the *webhooks* app rather than People, that answers with the
#: real list for this organization.
AVAILABLE_EVENTS_PATH = "/available_events"

#: Every People event Planning Center's webhook console offers, resource ->
#: actions. Note the actions are not uniform: a form submission and a person
#: merger are only ever `created`, a note and a list result are never `updated`,
#: and a list has a fourth action of its own (`refreshed`).
PEOPLE_EVENTS: dict[str, tuple[str, ...]] = {
    "address":          ("created", "destroyed", "updated"),
    "email":            ("created", "destroyed", "updated"),
    "field_datum":      ("created", "destroyed", "updated"),
    "field_definition": ("created", "destroyed", "updated"),
    "form_submission":  ("created",),
    "household":        ("created", "destroyed", "updated"),
    "list":             ("created", "destroyed", "refreshed", "updated"),
    "list_result":      ("created", "destroyed"),
    "note":             ("created", "destroyed"),
    "person":           ("created", "destroyed", "updated"),
    "person_merger":    ("created",),
    "phone_number":     ("created", "destroyed", "updated"),
}

APP = "people"
VERSION = "v2"


def event_name(resource: str, action: str) -> str:
    return f"{APP}.{VERSION}.events.{resource}.{action}"


def builtin() -> list[str]:
    return [event_name(r, a) for r, actions in PEOPLE_EVENTS.items() for a in actions]


def catalogue(db) -> dict:
    """The event list to offer, and where it came from.

    The built-in list is what the console showed when this was written; a
    refresh replaces it with what PCO says *now*, which is the only version that
    survives Planning Center adding an event.
    """
    held = db.get_meta(CATALOGUE_KEY)
    if held:
        try:
            payload = json.loads(held)
            names = [str(n) for n in payload.get("events") or [] if n]
            if names:
                return {"events": sorted(set(names)), "source": "planning center",
                        "fetched_at": payload.get("fetched_at")}
        except (ValueError, AttributeError):
            pass
    return {"events": sorted(builtin()), "source": "built in", "fetched_at": None}


def store(db, names, fetched_at: str) -> None:
    db.set_meta(CATALOGUE_KEY, json.dumps(
        {"events": sorted(set(names)), "fetched_at": fetched_at}))


def forget(db) -> None:
    db.execute("DELETE FROM mirror_meta WHERE key=?", (CATALOGUE_KEY,))


def refresh(db, client, now: str) -> list[str]:
    """Ask Planning Center what it can send, and remember the answer.

    Paged, because an organization with several apps enabled has more events
    than one page holds, and a truncated catalogue is worse than none — it looks
    complete.
    """
    names, offset = [], 0
    while True:
        resp = client.get(AVAILABLE_EVENTS_PATH, {"per_page": 100, "offset": offset},
                          base=webhooks_base(client.s), priority="reconcile")
        if not resp.ok:
            raise RuntimeError(f"available_events failed: HTTP {resp.status}")
        body = resp.json() or {}
        data = body.get("data") or []
        for item in data:
            name = (item.get("attributes") or {}).get("name")
            if name:
                names.append(str(name))
        if len(data) < 100:
            break
        offset += 100
    if not names:
        raise RuntimeError("available_events returned nothing")
    store(db, names, now)
    return sorted(set(names))


def webhooks_base(settings) -> str:
    """The webhooks app's base URL, derived from the People one it sits beside.

    Configurable, but derived by default: an operator who has pointed
    `PCO_BASE_URL` at a stand-in for testing should not have to remember a
    second setting to keep the two on the same host.
    """
    explicit = (getattr(settings, "pco_webhooks_base_url", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    base = (settings.pco_base_url or "").rstrip("/")
    marker = f"/{APP}/{VERSION}"
    if base.endswith(marker):
        base = base[: -len(marker)]
    return f"{base}/webhooks/{VERSION}"


# -- what the mirror will do with one ---------------------------------------
def parse(name: str) -> tuple[str, str]:
    """`people.v2.events.person.updated` -> `("person", "updated")`."""
    parts = (name or "").split(".")
    return (parts[-2], parts[-1]) if len(parts) >= 2 else (name, "")


def handling(name: str) -> tuple[str, str]:
    """`(verdict, one-line explanation)` for an event name.

    Asked of the registry rather than of a list here, so a resource added to the
    registry is immediately reported as mirrored without this file changing.
    """
    resource, action = parse(name)
    if resource == "person_merger":
        return "merge", "tombstones the person who lost the merge, re-fetches the survivor"
    r = registry.by_event_resource(resource)
    if r is None:
        return "recorded", "no table for it — kept in the inbox, applied to nothing"
    if action == "destroyed":
        return "mirrored", f"tombstones the {resource} in the mirror"
    if action == "refreshed":
        children = [c.name for c in registry.RESOURCES.values()
                    if c.method == "nested_walk" and c.parent == r.name]
        if children:
            return "mirrored", (f"written to `{r.table}`, and its "
                                f"{', '.join(children)} rows re-read on the next request "
                                f"for them — a refresh changes the contents, not the record")
        return "mirrored", f"written to `{r.table}`"
    return "mirrored", f"written to `{r.table}`"


def grouped(names) -> list[tuple[str, list[tuple[str, str]]]]:
    """Events as the console shows them: resource, then its actions."""
    by_resource: dict[str, list[tuple[str, str]]] = {}
    for name in sorted(names):
        resource, action = parse(name)
        by_resource.setdefault(resource, []).append((action, name))
    return sorted(by_resource.items())
