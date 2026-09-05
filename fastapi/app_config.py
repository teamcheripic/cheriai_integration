"""
app_config.py — 60-second cached reader for the public.app_config table.

Why this exists:
    Rotating a secret in Railway env vars is a redeploy. Rotating it in the
    admin panel should be a save. This module lets the FastAPI backend read
    RESEND_API_KEY (etc.) from the DB with a small cache so we don't spam
    Supabase, and quietly falls back to the env var when the DB row is
    missing or empty. That means a fresh install that hasn't populated
    app_config yet keeps working off env vars — no chicken-and-egg.

Contract:
    get_config(key, env_key=None, default=None) -> str | None
      1. If the cache has key and value is non-empty → return it
      2. Else re-fetch the whole table (cheap — a handful of rows)
      3. If still empty → try os.getenv(env_key or key.upper()) → return that
      4. Else → default

    invalidate_config_cache()
      Force the next get_config to re-fetch. Call this from the
      /admin/app-config write endpoint so admins see their edit instantly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

_PG_BASE = f"{SUPABASE_URL}/rest/v1"
_PG_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

CACHE_TTL_SECONDS = 60.0

_cache: dict[str, str | None] = {}
_cache_at: float = 0.0
_lock = asyncio.Lock()


async def _refetch() -> None:
    """Pull all rows from app_config into the in-memory cache."""
    global _cache, _cache_at
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{_PG_BASE}/app_config",
                params={"select": "key,value"},
                headers=_PG_HEADERS,
            )
            resp.raise_for_status()
            rows: list[dict[str, Any]] = resp.json()
        _cache = {r["key"]: (r.get("value") or None) for r in rows}
        _cache_at = time.monotonic()
    except Exception as e:
        # A dead DB shouldn't tank the process — the env-var fallback path
        # in get_config() still delivers a usable value. Log loudly the
        # first time so it's visible in Railway logs.
        logger.warning("app_config refetch failed (falling back to env): %r", e)
        _cache_at = time.monotonic()  # avoid tight retry loop


async def _ensure_fresh() -> None:
    if time.monotonic() - _cache_at < CACHE_TTL_SECONDS and _cache:
        return
    async with _lock:
        if time.monotonic() - _cache_at < CACHE_TTL_SECONDS and _cache:
            return  # another coroutine already refreshed while we waited
        await _refetch()


async def get_config(
    key: str,
    env_key: str | None = None,
    default: str | None = None,
) -> str | None:
    """
    Resolve one config value.

    Order: app_config row (if value non-empty) → env var (env_key or KEY.upper())
    → default.
    """
    await _ensure_fresh()
    db_val = _cache.get(key)
    if db_val:  # both "" and None fall through to env
        return db_val
    env_name = env_key or key.upper()
    env_val = os.getenv(env_name)
    if env_val:
        return env_val
    return default


async def get_int(key: str, env_key: str | None = None, default: int = 0) -> int:
    raw = await get_config(key, env_key=env_key, default=None)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("app_config key=%s value=%r is not an int, using default %d", key, raw, default)
        return default


async def get_bool(key: str, env_key: str | None = None, default: bool = False) -> bool:
    raw = await get_config(key, env_key=env_key, default=None)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def invalidate_config_cache() -> None:
    """Reset the cache so the next get_config triggers a refetch."""
    global _cache_at
    _cache_at = 0.0
