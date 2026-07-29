"""Mirror/cache statistics for the admin page.

The mirror *is* the cache, so "cache statistics" means: how much is in it, how
stale is it, is it drifting from PCO, and are the things that keep it fresh
(sweeps, webhooks, hydration) healthy.
"""
from __future__ import annotations

import os

from . import registry


def _one(db, sql, params=()):
    row = db.query_one(sql, params)
    return row[0] if row else None


def resource_rows(db) -> list[dict]:
    """Per mirrored resource: size, freshness, drift and sweep health."""
    out = []
    for r in registry.RESOURCES.values():
        live = _one(db, f"SELECT count(*) FROM {r.table} WHERE deleted_at IS NULL") or 0
        dead = _one(db, f"SELECT count(*) FROM {r.table} WHERE deleted_at IS NOT NULL") or 0
        oldest = _one(db, f"SELECT min(last_synced_at) FROM {r.table} WHERE deleted_at IS NULL")
        newest = _one(db, f"SELECT max(last_synced_at) FROM {r.table} WHERE deleted_at IS NULL")
        st = db.query_one(
            "SELECT * FROM mirror_sync_state WHERE resource_type=?", (r.name,))
        drift = None
        if st and st["total_count_last"] is not None and st["mirror_count_last"] is not None:
            drift = st["mirror_count_last"] - st["total_count_last"]
        out.append({
            "name": r.name,
            "endpoint": r.endpoint.strip("/"),
            "live": live,
            "tombstoned": dead,
            "oldest_synced": oldest,
            "newest_synced": newest,
            "backfilled_at": st["backfill_completed_at"] if st else None,
            "last_sweep": st["last_sweep_completed_at"] if st else None,
            "drift": drift,
            "errors": st["consecutive_errors"] if st else 0,
            "last_error": st["last_error"] if st else None,
        })
    return out


def webhook_stats(db) -> dict:
    by_status = {row["status"]: row["n"] for row in db.query(
        "SELECT status, count(*) n FROM webhook_event GROUP BY status")}
    return {
        "by_status": by_status,
        "total": sum(by_status.values()),
        "dead_letters": _one(db, "SELECT count(*) FROM webhook_dead_letter") or 0,
        "deliveries": _one(db, "SELECT count(*) FROM webhook_delivery") or 0,
        "last_received": _one(db, "SELECT max(received_at) FROM webhook_event"),
        "subscriptions": db.query(
            "SELECT event_name, url_token, active, last_event_at, "
            "       trim(coalesce(authenticity_secret,'')) = '' AS unverified "
            "FROM webhook_subscription ORDER BY event_name"),
        # Receivers that apply whatever is posted to them. Counted here rather
        # than worked out in the template because the dashboard raises it as an
        # alarm, and an alarm that is computed in two places eventually disagrees
        # with itself.
        "unverified_tokens": [r["url_token"] for r in db.query(
            "SELECT DISTINCT url_token FROM webhook_subscription "
            "WHERE active=1 AND trim(coalesce(authenticity_secret,'')) = '' "
            "ORDER BY url_token")],
    }


def queue_stats(db) -> dict:
    return {
        "hydration_pending": _one(db, "SELECT count(*) FROM hydration_task") or 0,
        "passthrough_cached": _one(db, "SELECT count(*) FROM passthrough_cache") or 0,
        "passthrough_expired": _one(
            db, "SELECT count(*) FROM passthrough_cache "
                "WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')") or 0,
    }


def storage_stats(db) -> dict:
    """On-disk size of the SQLite file plus its WAL."""
    total = 0
    files = {}
    for suffix in ("", "-wal", "-shm"):
        path = db.path + suffix
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        files[os.path.basename(path)] = size
        total += size
    return {"path": db.path, "files": files, "total_bytes": total}


def collect(db) -> dict:
    resources = resource_rows(db)
    return {
        "resources": resources,
        "total_live": sum(r["live"] for r in resources),
        "total_tombstoned": sum(r["tombstoned"] for r in resources),
        "webhooks": webhook_stats(db),
        "queues": queue_stats(db),
        "storage": storage_stats(db),
        "api_keys": _one(db, "SELECT count(*) FROM api_key WHERE disabled_at IS NULL") or 0,
    }


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"                                    # pragma: no cover
