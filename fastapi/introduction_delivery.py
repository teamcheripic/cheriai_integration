"""
introduction_delivery.py — the server delivers introductions (migration 063).

"Let introductions come to you … receive introductions by push notification
and email. No swiping needed." (Membership copy; handoff 10C.4). Matching runs
in the database: public.deliver_introductions(uid) picks and records new
introductions for one member with exactly the rules get_introductions uses
(eligibility, preferences, blocks, exes, official status, tier_config
allowance). This job runs it for every member who has Connect switched on and
introductions not paused, then, for each member who received new people:

  * inserts one in-app `match_available` notification (generic copy), and
  * sends one push (OneSignal; respects notify_push_enabled, generic
    lock-screen text, opens Connect → Introductions).

The daily email nudge (cron_daily_email.py) already emails members about
unread `match_available` notifications, so email follows automatically.

On by default; ENABLE_INTRO_DELIVERY=false turns it off. Crontab override
INTRO_DELIVERY_CRON (UTC, default 08:00). INTRO_DELIVERY_MAX caps members per
run (default 5000). Before 063 is applied the RPC is missing and the run is a
logged no-op.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

REST = f"{SUPABASE_URL}/rest/v1"
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}
PAGE = 1000

TITLE = "New introductions are ready"
BODY = "Open CheriPic to meet them."
URL = "/#/matching"


def delivery_enabled() -> bool:
    raw = (os.getenv("ENABLE_INTRO_DELIVERY") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _max_members() -> int:
    try:
        return max(1, int(os.getenv("INTRO_DELIVERY_MAX") or "5000"))
    except ValueError:
        return 5000


def wants_introductions(bio: Any) -> bool:
    """Connect switched on and introductions not paused (same rule as
    connect_visible(uid, true); the RPC re-checks everything)."""
    b = bio
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except ValueError:
            return False
    if not isinstance(b, dict) or b.get("connect_active") is not True:
        return False
    return b.get("intros_paused") is not True


async def _members(client: httpx.AsyncClient, limit: int) -> list[str]:
    out: list[str] = []
    offset = 0
    while len(out) < limit:
        r = await client.get(
            f"{REST}/user_profiles",
            params={"select": "user_id,bio", "bio": "ilike.*connect_active*",
                    "order": "user_id.asc", "limit": str(PAGE), "offset": str(offset)},
            headers=HEADERS,
        )
        r.raise_for_status()
        rows = r.json() or []
        out.extend(row["user_id"] for row in rows if row.get("user_id") and wants_introductions(row.get("bio")))
        if len(rows) < PAGE:
            break
        offset += PAGE
    return out[:limit]


async def _deliver(client: httpx.AsyncClient, user_id: str) -> Optional[int]:
    """New introductions for one member; None when 063 isn't deployed."""
    r = await client.post(f"{REST}/rpc/deliver_introductions", json={"p_user": user_id}, headers=HEADERS)
    if r.status_code == 404 or (r.status_code >= 400 and "PGRST202" in r.text):
        return None
    r.raise_for_status()
    try:
        return int(r.json() or 0)
    except (TypeError, ValueError):
        return 0


async def _notify(client: httpx.AsyncClient, user_id: str, added: int) -> None:
    try:
        await client.post(
            f"{REST}/notifications",
            json={"user_id": user_id, "type": "match_available", "title": TITLE, "body": BODY},
            headers={**HEADERS, "Prefer": "return=minimal"},
        )
    except Exception as e:
        logger.warning("[intro-delivery] notification insert failed for a member: %r", e)
    try:
        from push_notifications import send_push_notification

        await send_push_notification(
            user_id=user_id, title=TITLE, body=BODY, campaign="match_available",
            url=URL, data={"added": added}, dedup_window_hours=20,
        )
    except Exception as e:
        logger.warning("[intro-delivery] push failed for a member: %r", e)


async def run_intro_delivery() -> dict[str, int]:
    counts = {"members": 0, "delivered_members": 0, "introductions": 0, "errors": 0}
    async with httpx.AsyncClient(timeout=30.0) as client:
        members = await _members(client, _max_members())
        counts["members"] = len(members)
        for uid in members:
            try:
                added = await _deliver(client, uid)
            except Exception as e:
                counts["errors"] += 1
                logger.warning("[intro-delivery] delivery failed for a member: %r", e)
                continue
            if added is None:
                logger.info("[intro-delivery] deliver_introductions not deployed (migration 063) — skipped")
                return counts
            if added > 0:
                counts["delivered_members"] += 1
                counts["introductions"] += added
                await _notify(client, uid, added)
    logger.info("[intro-delivery] done %s", counts)
    return counts


def schedule(scheduler) -> bool:
    if not delivery_enabled():
        return False
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_intro_delivery,
        CronTrigger.from_crontab((os.getenv("INTRO_DELIVERY_CRON") or "0 8 * * *").strip(), timezone="UTC"),
        id="intro_delivery",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    logger.info("Introduction delivery job armed.")
    return True
