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
import re
import urllib.parse

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


#: Query parameters that describe the *request* rather than carry a value from
#: the data. Named explicitly, so a parameter nobody thought about is treated as
#: a filter value and redacted — the same default-deny rule as `fields.kind_of`.
_STRUCTURAL = frozenset({"order", "include", "per_page", "offset", "page", "per-page"})


def _is_structural(key: str) -> bool:
    return key.lower() in _STRUCTURAL or key.lower().startswith("fields[")


def _filtered_on(key: str) -> str:
    """The attribute a filter parameter filters on.

    `where[last_name]` → `last_name`; a comparison suffix like
    `where[created_at][gte]` keeps the leading name, which is the one that says
    what the value is.
    """
    inner = re.findall(r"\[([^\[\]]+)\]", key)
    return inner[0] if inner else key


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

    def query(self, params) -> dict:
        """Query parameters, keeping the ones that say what the request *was*.

        Two different things live in a query string. `order`, `include`,
        `per_page`, `offset` and `fields[…]` describe the request — they are the
        reason for storing it at all, and they name schema, never people, so they
        pass through. Anything else is a filter *value*, and a filter value is
        routinely a person's surname or email address, so it goes through the same
        classification as an attribute of the same name: `where[last_name]=Ochoa`
        is exactly as identifying as `attributes.last_name`.

        A filter nobody classified lands on `OPAQUE` by default and is redacted to
        a hash, which still answers the question the log is usually asking — were
        these two requests looking for the same thing?
        """
        if not isinstance(params, dict):
            return {}
        out = {}
        for key, raw in params.items():
            name = str(key)
            out[name] = raw if _is_structural(name) else self.value(_filtered_on(name), raw)
        return out

    def link(self, url):
        """A link, with its *query* pseudonymised and its path left alone.

        The path is ids and structure, which is the graph this is meant to
        preserve. The query is not: a collection's `links.self` echoes the
        request that produced it, so `where[last_name]=Lovelace` was reaching the
        export verbatim while every copy of that surname in the body next to it
        was correctly replaced. Nothing pointed at it — links were exempted as
        "ids and paths", which is true right up until one carries a search term.
        """
        if not isinstance(url, str) or "?" not in url:
            return url
        path, _, query = url.partition("?")
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        safe = [(k, self.query({k: v})[k]) for k, v in pairs]
        return path + "?" + urllib.parse.urlencode(safe)

    def links(self, value):
        if not isinstance(value, dict):
            return value
        return {name: self.link(url) for name, url in value.items()}

    def resource(self, obj):
        """One JSON:API resource, copied rather than edited in place.

        `relationships` are carried through untouched: they are ids, which is the
        graph this is meant to preserve. `meta` is left alone for the same reason
        — the mirror's own `meta.mirror` block is diagnostic, not personal.
        """
        if not isinstance(obj, dict):
            return obj
        out = dict(obj)
        if "attributes" in out:
            out["attributes"] = self.attributes(out.get("attributes"))
        if "links" in out:
            out["links"] = self.links(out.get("links"))
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
        if "links" in out:
            # Where the leak actually was: a collection's `self`/`next`/`prev`
            # carry the query string that produced the page, filter values and all.
            out["links"] = self.links(out.get("links"))
        return out
