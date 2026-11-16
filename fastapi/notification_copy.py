"""
notification_copy.py — push copy per event, editable in the admin panel.

Rows live in public.notification_templates (migration 065): title, body,
click url, enabled, dedup window. The backend's built-in copy
(transactional_email.NOTIFICATION_REGISTRY and a few direct senders) is the
fallback, so a missing table/row never silences a push.

    cfg = await push_template("interest_received", NOTIFICATION_REGISTRY.get(slug))
    # → dict(title, body, url, dedup_window_hours) | None (push turned off)

60-second cache, invalidated by POST /admin/invalidate-app-config-cache.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

_TTL = 60.0
_cache: dict[str, dict[str, Any]] = {}
_cache_at = 0.0
_available = True  # False once the table is known to be missing (pre-065)


def invalidate() -> None:
    global _cache_at
    _cache_at = 0.0


async def _refresh() -> None:
    global _cache, _cache_at, _available
    if time.monotonic() - _cache_at < _TTL:
        return
    _cache_at = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/notification_templates",
                params={"select": "slug,title,body,url,enabled,dedup_window_hours"},
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            )
        if r.status_code == 404 or (r.status_code >= 400 and "PGRST205" in r.text):
            _available = False
            _cache = {}
            return
        r.raise_for_status()
        _available = True
        _cache = {row["slug"]: row for row in (r.json() or []) if row.get("slug")}
    except Exception as e:
        logger.warning("[notification_copy] refresh failed, using built-in copy: %r", e)


async def push_template(slug: str, default: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The push copy for `slug`. Admin row wins; `default` otherwise.
    Returns None when the admin turned this push off, or when there is
    neither a row nor a default (email-only event)."""
    await _refresh()
    row = _cache.get(slug)
    if row is None:
        return default
    if row.get("enabled") is False:
        return None
    merged = dict(default or {})
    for k in ("title", "body", "url"):
        if row.get(k):
            merged[k] = row[k]
    if row.get("dedup_window_hours") is not None:
        merged["dedup_window_hours"] = float(row["dedup_window_hours"])
    return merged
