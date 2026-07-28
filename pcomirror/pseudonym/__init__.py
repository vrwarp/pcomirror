"""Replace the people in a Planning Center payload with believable strangers.

A divergence log is only useful if somebody can read it, and only safe if it can
be handed to somebody. Those pull in opposite directions: strip the values and
the log stops being diagnosable; keep them and every export carries a church's
children's phone numbers out of the building.

Pseudonyms give both. Every real value becomes a plausible fake one, the same
fake one every time, so what survives is the *structure* — which records share a
surname, which people are in which household, which of two responses has a member
the other does not, whether a flag differs. That is what a divergence is made of.
What does not survive is who anybody is.

What this deliberately does **not** hide is the shape of the graph itself: record
ids are kept, so two responses can be lined up against each other and so an
operator can go and look at the real record. Ids are meaningless without access
to the organization they came from; names are not.

    from pcomirror import pseudonym
    p = pseudonym.Pseudonymiser(secret)
    safe = p.document(body)

The secret is per install, generated on first use and stored in `mirror_meta`.
It never appears in an export. Two installs pseudonymise the same person
differently; one install always pseudonymises them the same way, for ever — which
is what makes two logs taken a month apart comparable.
"""
from __future__ import annotations

import secrets

from . import fields, pools, values
from .fields import kind_of
from .values import REDACTED, Chooser, pseudonymise

__all__ = ["Pseudonymiser", "REDACTED", "kind_of", "fields", "pools", "values",
           "SECRET_META_KEY", "secret_for"]

#: Where the per-install secret lives. Not a credential for anything — losing it
#: costs only the ability to compare a new log against an old one.
SECRET_META_KEY = "pseudonym_secret"


def secret_for(db) -> bytes:
    """The install's pseudonym secret, minted on first use.

    Kept in the database rather than the environment so it survives a restart
    without an operator having to know it exists, and so a pseudonym stays stable
    across the life of the deployment. A rotated secret is not a disaster — it
    just means logs from before and after no longer line up.
    """
    held = db.get_meta(SECRET_META_KEY)
    if held:
        return bytes.fromhex(held)
    minted = secrets.token_bytes(32)
    db.set_meta(SECRET_META_KEY, minted.hex())
    return minted


class Pseudonymiser:
    """Applies `values.pseudonymise` across a whole JSON:API document."""

    def __init__(self, secret: bytes):
        self._chooser = Chooser(secret)

    def value(self, attribute: str, raw):
        """One attribute, by name — the entry point everything else is built on."""
        return pseudonymise(self._chooser, kind_of(attribute), raw)

    def attributes(self, attrs) -> dict:
        if not isinstance(attrs, dict):
            return attrs
        return {name: self.value(name, raw) for name, raw in attrs.items()}

    def resource(self, obj):
        """One JSON:API resource, copied rather than edited in place.

        `relationships` and `links` are carried through untouched: they are ids
        and paths, which is the graph this is meant to preserve. `meta` is left
        alone for the same reason — the mirror's own `meta.mirror` block is
        diagnostic, not personal.
        """
        if not isinstance(obj, dict):
            return obj
        out = dict(obj)
        if "attributes" in out:
            out["attributes"] = self.attributes(out.get("attributes"))
        return out

    def document(self, body):
        """A whole response: `data` (object or list) and `included`."""
        if not isinstance(body, dict):
            return body
        out = dict(body)
        data = out.get("data")
        if isinstance(data, dict):
            out["data"] = self.resource(data)
        elif isinstance(data, list):
            out["data"] = [self.resource(item) for item in data]
        if isinstance(out.get("included"), list):
            out["included"] = [self.resource(item) for item in out["included"]]
        return out
