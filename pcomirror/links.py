"""Rewrite PCO URLs in served payloads so callers stay on the mirror.

The mirror's promise is a base-URL + credential swap, which breaks the moment a
response hands back `https://api.planningcenteronline.com/...`: the caller has a
pcomirror API key, not a PCO PAT, so following that link fails. Every PCO *API*
URL we serve is therefore rewritten to a mirror-relative path.

Two kinds of URL are deliberately left alone, because they are not API endpoints
and the mirror cannot serve them:

  * `links.html`  -> PCO's web UI for a record (a human deep-link)
  * avatar URLs   -> PCO's image CDN; the mirror does not proxy blobs
"""
from __future__ import annotations

import urllib.parse

from . import registry

# PCO's API host. `Settings.pco_base_url` normally points here, but a caller may
# override it (a proxy, a test double), so both are matched.
CANONICAL_HOST = "api.planningcenteronline.com"
MIRROR_PREFIX = "/people/v2"


def _hosts(settings) -> set[str]:
    hosts = {CANONICAL_HOST}
    parsed = urllib.parse.urlparse(getattr(settings, "pco_base_url", "") or "")
    if parsed.netloc:
        hosts.add(parsed.netloc)
    return hosts


def api_root(settings) -> str:
    """The PCO API root — `pco_base_url` with the People product path removed.

    Derived rather than configured for the same reason the webhooks base is: an
    operator who points `PCO_BASE_URL` at a stand-in should not have to remember
    a second setting to keep the other products on the same host.
    """
    base = (getattr(settings, "pco_base_url", "") or "").rstrip("/")
    if base.endswith(MIRROR_PREFIX):
        base = base[: -len(MIRROR_PREFIX)]
    return base or f"https://{CANONICAL_HOST}"


def to_mirror_path(url, settings) -> object:
    """Absolute PCO API URL -> mirror-relative path. Anything else is returned
    untouched (other hosts, the web UI, avatars, already-relative).

    Every product, not only People. A pass-through to `/check-ins/v2/…` comes
    back with absolute `links.next` URLs, and the caller holds a pcomirror key
    rather than a PCO PAT — so a link left absolute is one they cannot follow.
    The host check is what keeps the web UI (`people.planningcenteronline.com`)
    and the avatar CDN out of this: they are different hosts, not different paths.
    """
    if not isinstance(url, str) or "//" not in url:
        return url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc not in _hosts(settings):
        return url
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def link_map(r, pco_id: str, html: object = None, parent_id: object = None) -> dict:
    """The link set for a mirrored record, generated from the registry.

    Generated rather than echoed: PCO returns a different `links` map for a list
    page than for a single-resource fetch, so echoing it made a record's shape
    depend on whether it happened to arrive via backfill or reconcile.

    A resource PCO only exposes under a parent is linked **through** that parent.
    `GET /household_memberships/{id}` is a 404 upstream exactly as the collection
    is, so a top-level self link would be one that works against the mirror and
    404s against the API it stands in for — and PCO's own `links.self` is the
    parented form, which is where the owning id is read back out of.
    """
    if r.parent and parent_id:
        parent = registry.by_name(r.parent)
        base = (f"{MIRROR_PREFIX}/{parent.endpoint.strip('/')}/{parent_id}"
                f"/{r.endpoint.strip('/')}/{pco_id}")
    else:
        base = f"{MIRROR_PREFIX}/{r.endpoint.strip('/')}/{pco_id}"
    links = {"self": base}
    for name in r.relationships:
        links[name] = f"{base}/{name}"
    if html:
        links["html"] = html                 # PCO's web UI — deliberately absolute
    return links


def rewrite_relationships(obj, settings) -> None:
    """Point `relationships.<rel>.links.*` at the mirror, in place."""
    rels = obj.get("relationships")
    if not isinstance(rels, dict):
        return
    for rel in rels.values():
        if isinstance(rel, dict) and isinstance(rel.get("links"), dict):
            rel["links"] = {k: to_mirror_path(v, settings) for k, v in rel["links"].items()}


def rewrite_resource(obj, settings) -> dict:
    """Rewrite one JSON:API resource object in place (used for pass-through,
    where there is no registry entry to generate a link map from)."""
    if not isinstance(obj, dict):
        return obj
    if isinstance(obj.get("links"), dict):
        obj["links"] = {k: (v if k == "html" else to_mirror_path(v, settings))
                        for k, v in obj["links"].items()}
    rewrite_relationships(obj, settings)
    return obj


def rewrite_document(doc, settings):
    """Rewrite a whole JSON:API document — `data` (object or list) and `included`.

    Applied to pass-through responses so a proxied payload reads the same as a
    mirrored one. Unknown shapes are returned untouched.
    """
    if not isinstance(doc, dict):
        return doc
    data = doc.get("data")
    if isinstance(data, dict):
        rewrite_resource(data, settings)
    elif isinstance(data, list):
        for item in data:
            rewrite_resource(item, settings)
    for item in doc.get("included") or []:
        rewrite_resource(item, settings)
    if isinstance(doc.get("links"), dict):
        doc["links"] = {k: to_mirror_path(v, settings) for k, v in doc["links"].items()}
    return doc


def mirrors_relationship(r, name: str) -> bool:
    return name in r.relationships


def known_endpoint(segment: str) -> bool:
    return any(x.endpoint.strip("/") == segment for x in registry.RESOURCES.values())
