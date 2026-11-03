"""
kyc_retention.py — daily deletion of KYC images after the retention window
(compliance contract, "KYC retention job").

For each member whose final KYC decision (`verification_final_status` in
verified / rejected, decided at `kyc_reviewed_at`) is older than app_config
`kyc_image_retention_days` (default 90):
  * skip if the member has an active legal hold;
  * delete the KYC images from storage — the `kyc` bucket, or legacy
    `avatars/<uid>/kyc-*` objects — referenced by kyc_face_url / kyc_id_url,
    plus anything else under `kyc/<uid>/`;
  * null kyc_face_url and kyc_id_url. Decision metadata (status, scores,
    reviewer, kyc_dob…) is kept.

On by default; ENABLE_KYC_RETENTION=false turns it off. Cron override
KYC_RETENTION_CRON (UTC, default "45 3 * * *").

Before migration 056 there is no kyc_reviewed_at (no reliable decision
time), so the job logs and does nothing rather than guess. Logs counts only.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

import compliance
from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger("cheripic.kyc_retention")

_REST = f"{SUPABASE_URL}/rest/v1"
_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}
DEFAULT_RETENTION_DAYS = 90
_PAGE = 500


def retention_enabled() -> bool:
    raw = (os.getenv("ENABLE_KYC_RETENTION") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def kyc_object_for(url: Optional[str], user_id: str) -> Optional[tuple[str, str]]:
    """(bucket, path) for a KYC image the job may delete: `kyc/<uid>/…` or a
    legacy `avatars/<uid>/kyc-…`. Anything else (other members' folders,
    profile photos, foreign hosts) → None."""
    from media import parse_storage_ref

    ref = parse_storage_ref(url or "")
    if not ref:
        return None
    bucket, path = ref
    if not path.startswith(f"{user_id}/"):
        return None
    if bucket == "kyc":
        return bucket, path
    if bucket == "avatars" and path.rsplit("/", 1)[-1].startswith("kyc-"):
        return bucket, path
    return None


async def _retention_days() -> int:
    try:
        from app_config import get_int

        days = await get_int("kyc_image_retention_days", env_key="KYC_IMAGE_RETENTION_DAYS",
                             default=DEFAULT_RETENTION_DAYS)
        return days if days >= 1 else DEFAULT_RETENTION_DAYS
    except Exception:
        return DEFAULT_RETENTION_DAYS


async def _remove(client: httpx.AsyncClient, bucket: str, paths: list[str]) -> bool:
    if not paths:
        return True
    r = await client.request(
        "DELETE", f"{SUPABASE_URL}/storage/v1/object/{bucket}", json={"prefixes": paths}, headers=_HEADERS
    )
    if r.status_code >= 300 and r.status_code != 404:
        logger.warning("[kyc-retention] storage remove %s failed [%s]", bucket, r.status_code)
        return False
    return True


async def _list_kyc_folder(client: httpx.AsyncClient, user_id: str) -> list[str]:
    r = await client.post(
        f"{SUPABASE_URL}/storage/v1/object/list/kyc",
        json={"prefix": f"{user_id}/", "limit": 1000, "offset": 0},
        headers=_HEADERS,
    )
    if r.status_code >= 300:
        return []  # bucket not created yet (048) or empty
    return [f"{user_id}/{o['name']}" for o in (r.json() or []) if o.get("name")]


async def purge_member(client: httpx.AsyncClient, row: dict[str, Any]) -> bool:
    """Delete one member's KYC images and null the URL columns. False when a
    storage delete failed (the URLs are then kept so the next run retries)."""
    uid = row["user_id"]
    by_bucket: dict[str, set[str]] = {}
    for col in ("kyc_face_url", "kyc_id_url"):
        obj = kyc_object_for(row.get(col), uid)
        if obj:
            by_bucket.setdefault(obj[0], set()).add(obj[1])
    by_bucket.setdefault("kyc", set()).update(await _list_kyc_folder(client, uid))
    ok = True
    for bucket, paths in by_bucket.items():
        ok = await _remove(client, bucket, sorted(paths)) and ok
    if not ok:
        return False
    u = await client.patch(
        f"{_REST}/user_profiles",
        params={"user_id": f"eq.{uid}"},
        json={"kyc_face_url": None, "kyc_id_url": None},
        headers={**_HEADERS, "Prefer": "return=minimal"},
    )
    if u.status_code >= 300:
        logger.warning("[kyc-retention] url null-out failed [%s]", u.status_code)
        return False
    return True


async def run_kyc_retention(now: Optional[datetime] = None) -> dict[str, int]:
    counts = {"candidates": 0, "purged": 0, "held": 0, "errors": 0}
    days = await _retention_days()
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(
                f"{_REST}/user_profiles",
                params={
                    "select": "user_id,kyc_face_url,kyc_id_url",
                    "verification_final_status": "in.(verified,rejected)",
                    "kyc_reviewed_at": f"lt.{cutoff}",
                    "or": "(kyc_face_url.not.is.null,kyc_id_url.not.is.null)",
                    "limit": str(_PAGE),
                },
                headers=_HEADERS,
            )
            if r.status_code >= 300:
                if compliance.is_missing(r) or r.status_code == 400:
                    logger.info("[kyc-retention] kyc_reviewed_at missing (056 not applied); nothing done")
                else:
                    logger.warning("[kyc-retention] candidate query failed [%s]", r.status_code)
                    counts["errors"] += 1
                return counts
            for row in r.json() or []:
                counts["candidates"] += 1
                if await compliance.has_legal_hold(row["user_id"]):
                    counts["held"] += 1
                    continue
                try:
                    if await purge_member(client, row):
                        counts["purged"] += 1
                    else:
                        counts["errors"] += 1
                except Exception as e:
                    counts["errors"] += 1
                    logger.warning("[kyc-retention] one member failed: %s", type(e).__name__)
    except Exception as e:
        counts["errors"] += 1
        logger.warning("[kyc-retention] run failed: %s", type(e).__name__)
    logger.info("[kyc-retention] done (retention %dd) %s", days, counts)
    return counts


def schedule(scheduler) -> bool:
    if not retention_enabled():
        return False
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_kyc_retention,
        CronTrigger.from_crontab((os.getenv("KYC_RETENTION_CRON") or "45 3 * * *").strip(), timezone="UTC"),
        id="kyc_retention",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    logger.info("KYC retention job armed.")
    return True
