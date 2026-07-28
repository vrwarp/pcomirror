"""Turning one real value into one believable fake one, reproducibly.

Two properties, and they pull against each other:

**Stable.** The same input must always give the same output, everywhere it
appears — that is what preserves the *graph*. Two records that shared a surname
still share one; a household whose members were a family still looks like one; a
number that appeared twice still appears twice. Without that a divergence log is
a bag of unrelated rows and the interesting part — which records relate to which
— is destroyed along with the names.

**Not reversible.** A plain hash of a name over a thousand-name pool is a lookup
table anybody can rebuild: hash all thousand, compare, read off the answer. So
selection is an HMAC under a secret generated per install, which never leaves the
machine and is never exported with a log. Same install, same pseudonym forever;
a log on its own says nothing about who anybody is.

Shape is preserved where a reader would otherwise be misled — a phone number
keeps its digit count and punctuation, an email stays a valid address, a date
stays a date — and *not* preserved where preserving it would leak: lengths of
free text, and the exact spelling of anything. A bug that depends on the precise
length of a surname would be masked by this, which is the price.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from . import pools
from .fields import (CITY, DATE, EMAIL, FIRST, FREE_TEXT, FULL_NAME, HOUSEHOLD_NAME,
                     KEEP, LAST, NICK, OPAQUE, PHONE, POSTCODE, STATE, STREET, URL)

#: What an unclassified or free-text value becomes. Deliberately not plausible:
#: nobody should ever read one of these and think it is content.
REDACTED = "«redacted»"

_DIGIT = re.compile(r"\d")
_HOUSEHOLD_SUFFIX = re.compile(r"^(.*?)(\s+Household)$", re.IGNORECASE)
#: A leading `+` and its dialling code, which survive pseudonymisation.
_COUNTRY_CODE = re.compile(r"^\s*\+\d{1,3}")


def _norm(value: str) -> str:
    """The form two spellings of the same thing must agree on."""
    return " ".join(str(value).split()).casefold()


class Chooser:
    """Keyed, deterministic selection from a fixed list."""

    def __init__(self, secret: bytes):
        self._secret = secret

    def digest(self, kind: str, value: str) -> int:
        mac = hmac.new(self._secret, f"{kind}\0{_norm(value)}".encode(), hashlib.sha256)
        return int.from_bytes(mac.digest(), "big")

    def pick(self, kind: str, value: str, pool) -> str:
        return pool[self.digest(kind, value) % len(pool)]

    def number(self, kind: str, value: str, low: int, high: int) -> int:
        span = max(1, high - low + 1)
        return low + self.digest(kind, value) % span


def _first(c: Chooser, v: str) -> str:
    return c.pick(FIRST, v, pools.FIRST_NAMES)


def _last(c: Chooser, v: str) -> str:
    return c.pick(LAST, v, pools.LAST_NAMES)


def _full_name(c: Chooser, v: str) -> str:
    """`"Nathaniel Reed"` -> `"Marcus Ellery"`, consistently with its parts.

    Rebuilt word by word through the same per-kind mappings the individual
    attributes use, so a person's `name` agrees with their `first_name` and
    `last_name` in the same document. A `Household`'s name is the same shape with
    a fixed suffix, and keeping that suffix is what makes it still read as a
    household rather than a person.
    """
    household = _HOUSEHOLD_SUFFIX.match(str(v).strip())
    if household:
        return f"{_last(c, household.group(1))}{household.group(2)}"
    words = str(v).split()
    if not words:
        return ""
    if len(words) == 1:
        return _first(c, words[0])
    return " ".join([_first(c, words[0]), *(_last(c, w) for w in words[1:])])


def _email(c: Chooser, v: str) -> str:
    """A different address that is still an address.

    The local part is built from the same name pools, so two addresses belonging
    to one pseudonymised person look like they do. Anything that is not
    recognisably `local@domain` is redacted rather than guessed at — a malformed
    address is often exactly the bug.
    """
    raw = str(v).strip()
    if raw.count("@") != 1 or not all(raw.split("@")):
        return REDACTED
    local, _, _domain = raw.partition("@")
    person = f"{_first(c, local)}.{_last(c, local)}".lower()
    tag = c.number(EMAIL, raw, 1, 99)
    return f"{person}{tag}@{c.pick('email_domain', raw, pools.EMAIL_DOMAINS)}"


def _phone(c: Chooser, v: str) -> str:
    """The same shape, different digits.

    Punctuation, spacing, a leading `+` and the digit *count* all survive,
    because the mirror's phone search matches on a digits suffix and its length
    rules are exactly the kind of thing that goes wrong. The digits themselves
    are replaced one for one, keyed on the whole original so the same number
    always becomes the same fake number.
    """
    raw = str(v)
    if not _DIGIT.search(raw):
        return REDACTED
    # A leading country code is kept. It identifies a country, not a person, and
    # the mirror's two phone filters turn on it — `search_phone_number` matches a
    # digits *suffix* while `search_phone_number_e164` is exact, so a scrambled
    # dialling code would hide exactly the bugs this log exists to find.
    keep = _COUNTRY_CODE.match(raw)
    head, rest = (keep.group(0), raw[keep.end():]) if keep else ("", raw)
    replacement = iter(
        str(c.number(PHONE, f"{raw}#{i}", 0, 9)) for i in range(len(rest)))
    return head + _DIGIT.sub(lambda _: next(replacement), rest)


def _street(c: Chooser, v: str) -> str:
    number = c.number(STREET, v, 1, 9999)
    name = c.pick("street_name", v, pools.STREET_NAMES)
    kind = c.pick("street_type", v, pools.STREET_TYPES)
    return f"{number} {name} {kind}"


def _postcode(c: Chooser, v: str) -> str:
    """Digits swapped, format kept — a UK postcode stays UK-shaped."""
    raw = str(v)
    if not _DIGIT.search(raw):
        return REDACTED
    replacement = iter(
        str(c.number(POSTCODE, f"{raw}#{i}", 0, 9)) for i in range(len(raw)))
    return _DIGIT.sub(lambda _: next(replacement), raw)


def _date(c: Chooser, v: str) -> str:
    """A real date, near the real one.

    Shifted by a keyed offset inside a year rather than randomised outright, so
    anything derived from it — a grade band, an age check, a birthday sweep —
    still lands in roughly the right place and a bug in that logic is still
    visible. The exact day is gone, which is the point.
    """
    raw = str(v).strip()
    # The tail is bounded to the characters a time can be made of. It was `.*`
    # once, which let anything after the date through verbatim — an unclassified
    # fragment surviving under the one kind that looked safest.
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(T[0-9:.+\-Z]*)?$", raw)
    if not match:
        return REDACTED
    year, _month, _day, tail = match.groups()
    shifted_month = c.number(DATE, f"{raw}#m", 1, 12)
    shifted_day = c.number(DATE, f"{raw}#d", 1, 28)
    return f"{year}-{shifted_month:02d}-{shifted_day:02d}{tail or ''}"


_GENERATORS = {
    FIRST: _first,
    LAST: _last,
    NICK: lambda c, v: c.pick(NICK, v, pools.NICKNAMES),
    FULL_NAME: _full_name,
    HOUSEHOLD_NAME: _full_name,
    EMAIL: _email,
    PHONE: _phone,
    STREET: _street,
    CITY: lambda c, v: c.pick(CITY, v, pools.CITIES),
    STATE: lambda c, v: c.pick(STATE, v, pools.STATES),
    POSTCODE: _postcode,
    DATE: _date,
    URL: lambda c, v: f"https://avatars.example/{c.digest(URL, v) % 10**12:012d}",
}


def pseudonymise(chooser: Chooser, kind: str, value):
    """One value, replaced according to its kind.

    Non-strings are returned untouched whatever their kind: a boolean, a number
    and a null carry no identity, and they are most of what a divergence is
    actually about. An empty string stays empty — "this field was blank" is a
    fact about the data that a pseudonym would erase.
    """
    if kind == KEEP or not isinstance(value, str):
        return value
    if not value.strip():
        return value
    if kind in (FREE_TEXT, OPAQUE):
        return REDACTED
    generator = _GENERATORS.get(kind)
    return generator(chooser, value) if generator else REDACTED
