"""Which kind of thing each Planning Center attribute holds.

The whole safety of this package rests on one property: **an attribute nobody has
classified is never passed through.** Planning Center adds fields, and the day it
adds one this table has not heard of, the failure has to be a redaction rather
than a leak. So `kind_of` returns `OPAQUE` for anything unlisted, and the caller
turns that into a marker rather than a value.

The opposite mistake is worth naming too. Booleans, numbers and timestamps are
not identifying, and they are exactly what a divergence log is *for* — the bug
that started this work was `primary: true` against `primary: false`, and a
pseudonymiser that scrubbed those would have produced a log with the evidence
removed. They pass through untouched, deliberately.
"""
from __future__ import annotations

# -- kinds -----------------------------------------------------------------
FIRST = "first"
LAST = "last"
NICK = "nick"
FULL_NAME = "full_name"
HOUSEHOLD_NAME = "household_name"
EMAIL = "email"
PHONE = "phone"
STREET = "street"
CITY = "city"
STATE = "state"
POSTCODE = "postcode"
DATE = "date"
URL = "url"
#: Free text a human wrote. Never fabricated — a plausible-looking medical note
#: is worse than an obvious placeholder, because somebody will read it as real.
FREE_TEXT = "free_text"
#: Safe as it stands: not identifying, and load-bearing for diagnosis.
KEEP = "keep"
#: Unclassified. Always redacted; never guessed at.
OPAQUE = "opaque"

#: Attribute name -> kind. Names are PCO's, matched case-insensitively and
#: without regard to which resource type they appeared on: `location` means the
#: same thing on an Email as on an Address, and a name that ever holds a person's
#: details must not be safe on one type and not another.
BY_ATTRIBUTE = {
    # names
    "first_name": FIRST, "given_name": FIRST, "middle_name": FIRST,
    "last_name": LAST,
    "nickname": NICK,
    "name": FULL_NAME, "person_name": FULL_NAME, "primary_contact_name": FULL_NAME,
    # contact
    "address": EMAIL,                       # the attribute on an Email resource
    "number": PHONE, "e164": PHONE, "international": PHONE, "national": PHONE,
    "street": STREET, "street_line_1": STREET, "street_line_2": STREET,
    "city": CITY, "state": STATE, "zip": POSTCODE, "postal_code": POSTCODE,
    # dates a person could be found by
    "birthdate": DATE, "anniversary": DATE,
    # things that point at a person
    "avatar": URL, "demographic_avatar_url": URL, "remote_id": OPAQUE,
    "stripe_customer_identifier": OPAQUE, "login_identifier": OPAQUE,
    # free text
    "medical_notes": FREE_TEXT, "value": FREE_TEXT, "config": FREE_TEXT,
    # not identifying, and the substance of most divergences
    "primary": KEEP, "blocked": KEEP, "location": KEEP, "child": KEEP,
    "status": KEEP, "grade": KEEP, "gender": KEEP, "membership": KEEP,
    "school_type": KEEP, "directory_status": KEEP, "passed_background_check": KEEP,
    "pending": KEEP, "household_role": KEEP, "member_count": KEEP,
    "data_type": KEEP, "slug": KEEP, "sequence": KEEP, "carrier": KEEP,
    "country_code": KEEP, "country_name": KEEP, "can_create_forms": KEEP,
    "can_email_lists": KEEP, "accounting_administrator": KEEP,
    "site_administrator": KEEP, "people_permissions": KEEP,
    "created_at": KEEP, "updated_at": KEEP, "inactivated_at": KEEP,
    "file_size": KEEP, "file_content_type": KEEP,
    "person_to_keep_id": KEEP, "person_to_remove_id": KEEP,
    "person_id": KEEP, "household_id": KEEP, "primary_contact_id": KEEP,
    "field_definition_id": KEEP, "primary_campus_id": KEEP, "gender_id": KEEP,
    "tab_id": KEEP,
}


def kind_of(attribute: str) -> str:
    """The kind an attribute holds, or `OPAQUE` if it has never been classified."""
    return BY_ATTRIBUTE.get(str(attribute).strip().lower(), OPAQUE)
