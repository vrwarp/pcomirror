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

The key is derived from the PCO credential. Nothing is minted and nothing extra
has to be kept: anyone holding the token can read the real records anyway, so a
key derived from it guards exactly what it should and adds no new secret to lose.
It also means the mapping survives a rebuild — throw the database away, backfill
again, and yesterday's log still lines up with today's.

Two consequences worth knowing. Two organizations pseudonymise the same person
differently, which is right. And **rotating the PAT re-pseudonymises everything**,
so logs from before and after a rotation cannot be compared — an acceptable price
for having no second secret, but not a surprise anybody should meet in the middle
of an investigation.
"""
from __future__ import annotations

import hashlib
import hmac

from . import fields, pools, values
from .fields import kind_of
from .values import REDACTED_PREFIX, Chooser, pseudonymise, redacted

__all__ = ["Pseudonymiser", "REDACTED_PREFIX", "redacted", "kind_of",
           "fields", "pools", "values", "secret_for", "derive_key"]

#: Domain separator, so the pseudonym key is a *derivative* of the credential and
#: never the credential itself. HMAC does not leak its key, but a key that is
#: literally the PAT is one careless log line away from being the PAT.
_KEY_INFO = b"pcomirror/pseudonym/v1"

#: Used when no credential is configured at all — tests and a bare dev box, where
#: there is no real organization and so nothing to protect. Named so that a
#: pseudonym produced under it is obviously not protecting anything.
_NO_CREDENTIAL = b"pcomirror-insecure-development-key"


def derive_key(credential: str | bytes) -> bytes:
    """A pseudonym key from the PCO credential, which never appears in a log."""
    raw = credential.encode() if isinstance(credential, str) else (credential or b"")
    return hmac.new(raw or _NO_CREDENTIAL, _KEY_INFO, hashlib.sha256).digest()


def secret_for(settings) -> bytes:
    """The install's pseudonym key.

    Both halves of the Personal Access Token go in, so two organizations on the
    same host never share a mapping even if one of them is misconfigured.
    """
    app_id = getattr(settings, "pco_app_id", "") or ""
    secret = getattr(settings, "pco_secret", "") or ""
    return derive_key(f"{app_id}:{secret}" if (app_id or secret) else "")


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
