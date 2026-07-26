"""Resource registry — the single, data-driven source of truth.

Each mirrored PCO resource is declared once here: its JSON:API type, physical
table, endpoint, sync method, projected columns, relationships, and query surface.
`db.py` builds the SQLite tables from these declarations (the shared column
contract in DESIGN.md §4 / docs/schema.sqlite.sql, plus each resource's
projections); the ingestion, webhook, and serving layers all read the same
registry so schema and behaviour never drift apart.

Adding a resource = adding one `Resource(...)` entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# A projection is a queryable column derived from `raw`.
#   ("col", "TEXT", "json",  "$.attributes.first_name")  -> GENERATED from raw ->> path
#   ("col", "TEXT", "expr",  "<sql expression over raw>") -> GENERATED from an expression
#   ("col", "TEXT", "plain", None)                        -> plain column, writer-filled
Projection = tuple[str, str, str, str | None]


@dataclass(frozen=True)
class Rel:
    """A relationship the serving layer can `include` by joining local tables."""
    target: str            # target resource name (registry key)
    kind: str              # "many" (child fk -> us) | "one" (we hold the fk) | "json"
    local_fk: str | None = None   # for kind="one": our column holding the target id
    child_fk: str | None = None   # for kind="many": the child column pointing back at us
    via: str | None = None        # optional join table resource (e.g. household_membership)
    via_local_fk: str | None = None
    via_target_fk: str | None = None
    # for kind="json": a JSON path on `raw` holding resource identifiers. PCO
    # returns a person's households inline on the Person payload and a household's
    # members inline on the Household, and offers no bulk endpoint for the join
    # rows, so each side's array *is* the edge.
    json_path: str | None = None


@dataclass(frozen=True)
class Search:
    """A PCO `where[search_*]` filter. Each arm has its own matching rule, and the
    rules are not the same — every one below was measured against the live API
    rather than reasoned about (the evidence is in `tests/golden/`).

    `names` are SQL expressions matched with **word-prefix** semantics: the needle's
    words must be a run of prefixes starting at a word boundary. `children` are
    `(resource, child_fk, column, mode)` tuples matched with an EXISTS subquery,
    where `mode` is one of:

      * ``contains``      - normalised substring; how PCO matches email addresses
      * ``digits_suffix`` - the stored number ends with the digits typed (phone)
      * ``digits_exact``  - the same digits, punctuation discounted (E.164)
    """
    names: tuple[str, ...] = ()
    children: tuple[tuple[str, str, str, str], ...] = ()


# The name haystacks, each matched from its own first word (see `db.name_matches`).
#
# There is no `attributes.search_name` on a PCO Person — the attribute does not
# exist, so the column projected from it was always NULL. Nor does PCO index
# "<nickname> <surname>" as a phrase: a nickname on its own finds the person,
# that pairing finds nobody. Every field is therefore its own haystack, and the
# full name is listed separately so a two-word needle like "ada by" can match
# across the given name and the surname.
_PERSON_NAMES = (
    "name",
    "coalesce(first_name,'') || ' ' || coalesce(last_name,'')",
    "first_name",
    "last_name",
    "nickname",
    "given_name",
)


@dataclass(frozen=True)
class Resource:
    name: str                       # registry key, e.g. "person"
    type: str                       # JSON:API type, e.g. "Person"
    table: str                      # physical table name (singular)
    endpoint: str                   # e.g. "/people"
    tier: str = "full"              # full | lite | passthrough
    method: str = "incremental"     # incremental | merger_poll | reference_periodic | passthrough_only
    timestamped: bool = True        # has attributes.updated_at
    supports_uat_filter: bool = True  # where[updated_at] usable (False -> descending walk, e.g. address)
    owner_rel: str | None = None    # for children: relationship name pointing at the owner (e.g. "person")
    projections: tuple[Projection, ...] = ()
    includes: tuple[str, ...] = ()  # default includes for backfill / hydration
    relationships: dict[str, Rel] = field(default_factory=dict)
    can_query_by: tuple[str, ...] = ()
    can_order_by: tuple[str, ...] = ()
    search_filters: dict[str, Search] = field(default_factory=dict)
    # PCO attribute name -> our column, where the two differ (`primary` is a
    # reserved-ish word, so the projection is `is_primary`).
    col_aliases: dict[str, str] = field(default_factory=dict)
    incr_interval_s: int = 300
    audit_interval_s: int | None = None
    priority: int = 2


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------

_CHILD = lambda: {}  # noqa: E731 (placeholder, children declare no includes)

RESOURCES: dict[str, Resource] = {}


def _reg(r: Resource) -> Resource:
    RESOURCES[r.name] = r
    return r


_reg(Resource(
    name="person", type="Person", table="person", endpoint="/people",
    tier="full", method="incremental", incr_interval_s=60, audit_interval_s=86400, priority=1,
    projections=(
        ("first_name", "TEXT", "json", "$.attributes.first_name"),
        ("last_name", "TEXT", "json", "$.attributes.last_name"),
        ("name", "TEXT", "json", "$.attributes.name"),
        ("nickname", "TEXT", "json", "$.attributes.nickname"),
        # Searched by PCO's `where[search_name]`; `middle_name`, measured the same
        # way, is not — so it is not projected.
        ("given_name", "TEXT", "json", "$.attributes.given_name"),
        ("status", "TEXT", "json", "$.attributes.status"),
        ("membership", "TEXT", "json", "$.attributes.membership"),
        ("gender", "TEXT", "json", "$.attributes.gender"),
        ("grade", "INTEGER", "json", "$.attributes.grade"),
        ("birthdate", "TEXT", "json", "$.attributes.birthdate"),
        ("anniversary", "TEXT", "json", "$.attributes.anniversary"),
        ("remote_id", "TEXT", "json", "$.attributes.remote_id"),
        ("child", "INTEGER", "json", "$.attributes.child"),
        ("primary_campus_id", "TEXT", "json", "$.relationships.primary_campus.data.id"),
        ("marital_status_id", "TEXT", "json", "$.relationships.marital_status.data.id"),
        ("name_prefix_id", "TEXT", "json", "$.relationships.name_prefix.data.id"),
        ("name_suffix_id", "TEXT", "json", "$.relationships.name_suffix.data.id"),
    ),
    includes=("emails", "phone_numbers", "addresses", "field_data",
              "households", "primary_campus", "marital_status"),
    relationships={
        "emails": Rel("email", "many", child_fk="person_pco_id"),
        "phone_numbers": Rel("phone_number", "many", child_fk="person_pco_id"),
        "addresses": Rel("address", "many", child_fk="person_pco_id"),
        "field_data": Rel("field_datum", "many", child_fk="person_pco_id"),
        "primary_campus": Rel("campus", "one", local_fk="primary_campus_id"),
        "marital_status": Rel("marital_status", "one", local_fk="marital_status_id"),
        "households": Rel("household", "json",
                          json_path="$.relationships.households.data"),
    },
    can_query_by=("created_at", "updated_at", "id", "remote_id", "primary_campus_id",
                  "status", "first_name", "last_name", "nickname", "given_name",
                  "child", "grade", "gender", "membership", "birthdate", "anniversary"),
    can_order_by=("created_at", "updated_at", "first_name", "last_name", "remote_id",
                  "birthdate", "anniversary", "nickname", "given_name", "child",
                  "grade", "gender", "membership", "status"),
    search_filters={
        "search_name": Search(names=_PERSON_NAMES),
        "search_name_or_email": Search(
            names=_PERSON_NAMES,
            children=(("email", "person_pco_id", "address", "contains"),)),
        "search_phone_number": Search(
            children=(("phone_number", "person_pco_id", "number", "digits_suffix"),)),
        "search_phone_number_e164": Search(
            children=(("phone_number", "person_pco_id", "e164", "digits_exact"),)),
        "search_name_or_email_or_phone_number": Search(
            names=_PERSON_NAMES,
            children=(("email", "person_pco_id", "address", "contains"),
                      ("phone_number", "person_pco_id", "number", "digits_suffix"))),
    },
))

# --- person-owned children (fk person_pco_id via relationships.person) ---
_reg(Resource(
    name="email", type="Email", table="email", endpoint="/emails",
    tier="full", owner_rel="person", incr_interval_s=120, priority=1,
    projections=(
        ("person_pco_id", "TEXT", "json", "$.relationships.person.data.id"),
        ("address", "TEXT", "json", "$.attributes.address"),
        ("location", "TEXT", "json", "$.attributes.location"),
        ("is_primary", "INTEGER", "json", "$.attributes.primary"),
        ("blocked", "INTEGER", "json", "$.attributes.blocked"),
    ),
    can_query_by=("created_at", "updated_at", "address", "location", "primary", "blocked"),
    can_order_by=("created_at", "updated_at", "address"),
    col_aliases={"primary": "is_primary"},
))
_reg(Resource(
    name="phone_number", type="PhoneNumber", table="phone_number", endpoint="/phone_numbers",
    tier="full", owner_rel="person", incr_interval_s=120, priority=1,
    projections=(
        ("person_pco_id", "TEXT", "json", "$.relationships.person.data.id"),
        ("number", "TEXT", "json", "$.attributes.number"),
        ("e164", "TEXT", "json", "$.attributes.e164"),
        ("location", "TEXT", "json", "$.attributes.location"),
        ("is_primary", "INTEGER", "json", "$.attributes.primary"),
    ),
    can_query_by=("created_at", "updated_at", "number", "location", "primary"),
    can_order_by=("created_at", "updated_at"),
    col_aliases={"primary": "is_primary"},
))
_reg(Resource(
    name="address", type="Address", table="address", endpoint="/addresses",
    tier="full", owner_rel="person", supports_uat_filter=False,  # /addresses has no where[updated_at]
    incr_interval_s=300, priority=2,
    projections=(
        ("person_pco_id", "TEXT", "json", "$.relationships.person.data.id"),
        ("street_line_1", "TEXT", "json", "$.attributes.street_line_1"),
        ("street_line_2", "TEXT", "json", "$.attributes.street_line_2"),
        ("city", "TEXT", "json", "$.attributes.city"),
        ("state", "TEXT", "json", "$.attributes.state"),
        ("zip", "TEXT", "json", "$.attributes.zip"),
        ("location", "TEXT", "json", "$.attributes.location"),
        ("is_primary", "INTEGER", "json", "$.attributes.primary"),
    ),
    can_query_by=("created_at", "updated_at", "city", "state", "zip",
                  "street_line_1", "street_line_2", "location", "primary"),
    can_order_by=("created_at", "updated_at"),
    col_aliases={"primary": "is_primary"},
))

# --- custom fields ---
_reg(Resource(
    name="field_datum", type="FieldDatum", table="field_datum", endpoint="/field_data",
    tier="full", owner_rel="person", incr_interval_s=300, priority=2,
    includes=("field_definition",),
    projections=(
        ("customizable_type", "TEXT", "json", "$.relationships.customizable.data.type"),
        ("customizable_id", "TEXT", "json", "$.relationships.customizable.data.id"),
        # polymorphic owner: person id only when the customizable is a Person
        ("person_pco_id", "TEXT", "expr",
         "CASE WHEN raw ->> '$.relationships.customizable.data.type' = 'Person' "
         "THEN raw ->> '$.relationships.customizable.data.id' END"),
        ("field_definition_id", "TEXT", "json", "$.relationships.field_definition.data.id"),
        ("field_option_id", "TEXT", "json", "$.relationships.field_option.data.id"),
        ("value", "TEXT", "json", "$.attributes.value"),
        ("file_url", "TEXT", "json", "$.attributes.file"),
        # typed columns: writer-filled from field_definition.data_type (re-projected on def change)
        ("value_text", "TEXT", "plain", None),
        ("value_number", "REAL", "plain", None),
        ("value_date", "TEXT", "plain", None),
        ("value_bool", "INTEGER", "plain", None),
    ),
    relationships={
        # PCO serves `/field_data/{id}/field_definition` and offers
        # `include=field_definition`, even though it puts no such entry in the
        # resource's own `links` map. `customizable` is deliberately absent: it is
        # polymorphic (Person or Organization) and a Rel names one target.
        "field_definition": Rel("field_definition", "one", local_fk="field_definition_id"),
    },
    can_query_by=("created_at", "updated_at", "field_definition_id", "value", "file"),
    can_order_by=("created_at", "updated_at", "value", "file"),
    col_aliases={"file": "file_url"},
))
_reg(Resource(
    name="field_definition", type="FieldDefinition", table="field_definition",
    endpoint="/field_definitions", tier="lite", method="reference_periodic",
    timestamped=False, supports_uat_filter=False, incr_interval_s=21600, priority=3,
    projections=(
        ("name", "TEXT", "json", "$.attributes.name"),
        ("slug", "TEXT", "json", "$.attributes.slug"),
        ("data_type", "TEXT", "json", "$.attributes.data_type"),
        ("tab_id", "TEXT", "json", "$.relationships.tab.data.id"),
    ),
    can_query_by=("name", "slug", "data_type", "tab_id"),
    can_order_by=("name", "slug", "data_type", "tab_id"),
))

# --- households (many-to-many) ---
_reg(Resource(
    name="household", type="Household", table="household", endpoint="/households",
    tier="full", incr_interval_s=600, priority=2,
    projections=(
        ("name", "TEXT", "json", "$.attributes.name"),
        ("member_count", "INTEGER", "json", "$.attributes.member_count"),
        ("primary_contact_id", "TEXT", "json", "$.relationships.primary_contact.data.id"),
    ),
    relationships={
        # PCO puts the membership on the Household itself, with no `include`
        # needed, so each side of the edge is read from its own payload. Deriving
        # it by scanning every person instead only ever found the members whose
        # own record had been fetched — a person sideloaded from somebody else's
        # household carries no households array to scan.
        "people": Rel("person", "json", json_path="$.relationships.people.data"),
        "primary_contact": Rel("person", "one", local_fk="primary_contact_id"),
    },
    can_query_by=("created_at", "updated_at", "name"),
    can_order_by=("created_at", "updated_at", "name"),
))
# Deliberately NOT mirrored. `GET /household_memberships` is a 404 — PCO exposes the
# rows only under `/households/{id}/household_memberships`, one household at a time,
# and the payload there carries no `household` relationship (the household id
# appears only inside `links.self`). Mirroring it would mean a per-household walk
# with no incremental signal to drive it, so the endpoints pass through instead and
# the `households` edge is read off the Person payload, where PCO does provide it.
# The table stays declared so the type is known and a payload can still be stored.
_reg(Resource(
    name="household_membership", type="HouseholdMembership", table="household_membership",
    endpoint="/household_memberships", tier="passthrough", method="passthrough_only",
    timestamped=False, supports_uat_filter=False, incr_interval_s=600, priority=2,
    projections=(
        ("household_pco_id", "TEXT", "json", "$.relationships.household.data.id"),
        ("person_pco_id", "TEXT", "json", "$.relationships.person.data.id"),
        ("household_role", "TEXT", "json", "$.attributes.household_role"),
        ("pending", "INTEGER", "json", "$.attributes.pending"),
    ),
    relationships={
        "person": Rel("person", "one", local_fk="person_pco_id"),
        "household": Rel("household", "one", local_fk="household_pco_id"),
    },
    can_query_by=("pending", "household_role"),
    can_order_by=("pending",),
))

# --- reference / config (LITE) ---
for _name, _type, _ep in [
    ("campus", "Campus", "/campuses"),
    ("marital_status", "MaritalStatus", "/marital_statuses"),
    ("name_prefix", "NamePrefix", "/name_prefixes"),
    ("name_suffix", "NameSuffix", "/name_suffixes"),
    ("inactive_reason", "InactiveReason", "/inactive_reasons"),
]:
    _reg(Resource(
        name=_name, type=_type, table=_name, endpoint=_ep,
        # PCO returns these with a `value` and nothing else — no `updated_at`, so
        # they cannot carry the monotonic guard and must be written untimed.
        # Declaring them timestamped left `pco_updated_at` NULL, which made every
        # comparison in that guard false.
        tier="lite", method="reference_periodic", timestamped=False,
        supports_uat_filter=False, incr_interval_s=86400, priority=3,
        projections=(
            ("name", "TEXT", "json", "$.attributes.name"),
            ("value", "TEXT", "json", "$.attributes.value"),
        ),
        can_query_by=("created_at", "updated_at"), can_order_by=("created_at", "updated_at"),
    ))

# --- person_merger: append-only delete log (special) ---
_reg(Resource(
    name="person_merger", type="PersonMerger", table="person_merger", endpoint="/person_mergers",
    tier="passthrough", method="merger_poll", timestamped=False, supports_uat_filter=False,
    incr_interval_s=120, priority=1,
    projections=(
        ("person_to_keep_id", "TEXT", "json", "$.attributes.person_to_keep_id"),
        ("person_to_remove_id", "TEXT", "json", "$.attributes.person_to_remove_id"),
    ),
))


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
_BY_TYPE = {r.type: r for r in RESOURCES.values()}
_BY_TABLE = {r.table: r for r in RESOURCES.values()}
# webhook event resource token (snake) -> resource; PCO uses e.g. "person", "field_datum"
_BY_EVENT_RESOURCE = {r.name: r for r in RESOURCES.values()}


_COL_TYPES = {(r.name, p[0]): p[1] for r in RESOURCES.values() for p in r.projections}


def by_name(name: str) -> Resource:
    return RESOURCES[name]


def col_type(r: Resource, col: str) -> str | None:
    """The declared SQL type of a projected column — so the serving layer can
    coerce `where[child]=true` to the 1/0 the generated column actually holds."""
    return _COL_TYPES.get((r.name, col))


def by_type(jsonapi_type: str) -> Resource | None:
    return _BY_TYPE.get(jsonapi_type)


def by_table(table: str) -> Resource | None:
    return _BY_TABLE.get(table)


def by_event_resource(token: str) -> Resource | None:
    return _BY_EVENT_RESOURCE.get(token)


def mirrored_tables() -> list[str]:
    return [r.table for r in RESOURCES.values()]


def full_and_lite() -> list[Resource]:
    return [r for r in RESOURCES.values() if r.tier in ("full", "lite")]
