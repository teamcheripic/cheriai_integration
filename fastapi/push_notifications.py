"""
push_notifications.py — one entry point for every OneSignal push send.

Mirrors transactional_email.send_transactional_email exactly:

    await send_push_notification(
        user_id      = "<uuid>",
        title        = "Someone is interested in you",
        body         = "Tap to open your requests",
        data         = {"url": "/matching?tab=received"},   # click target
        campaign     = "interest_received",   # for audit + dedup
    )

Design decisions (recorded 2026-10-11):
  • Target OneSignal by `external_id = user_id`. The frontend calls
    OneSignal.login(user_id) on sign-in so the SDK aliases the device
    subscription to the user's UUID. One send call reaches every
    device that user has installed on.
  • Dedup via a dedicated public.sent_push audit table (migration
    037). We don't piggyback on sent_emails because the dedup windows
    for push and email differ for the same scenario (push is cheaper
    and feels less spammy, so windows can be shorter).
  • Fire-and-forget everywhere. Any failure (no OneSignal env, user
    opted out, network blip) returns False — never raises. Caller
    is expected to await and ignore.
  • No cost unless the user opted in. OneSignal's free tier covers
    web push for unlimited devices up to a reasonable DAU; nothing in
    this module creates ongoing cost once idle.

Env (set on Railway):
    ONESIGNAL_APP_ID        — your app's UUID from OneSignal dashboard
    ONESIGNAL_REST_API_KEY  — the REST (not SDK) key — SERVER-only
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

ONESIGNAL_APP_ID = os.getenv("ONESIGNAL_APP_ID") or ""
ONESIGNAL_REST_API_KEY = os.getenv("ONESIGNAL_REST_API_KEY") or ""
ONESIGNAL_API_URL = "https://api.onesignal.com/notifications"

_PG_BASE = f"{SUPABASE_URL}/rest/v1"
_PG_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


async def _already_pushed_recently(
    client: httpx.AsyncClient, user_id: str, campaign: str, hours: int
) -> bool:
    """Dedup — has this (user_id, campaign) push been sent in the last `hours`?"""
    if hours <= 0:
        return False
    resp = await client.get(
        f"{_PG_BASE}/sent_push",
        params={
            "select": "sent_at",
            "user_id": f"eq.{user_id}",
            "campaign": f"eq.{campaign}",
            "status": "eq.sent",
            "order": "sent_at.desc",
            "limit": "1",
        },
        headers=_PG_HEADERS,
        timeout=10.0,
    )
    if resp.status_code >= 300:
        # If the audit table isn't available, don't block the send — just lose dedup.
        return False
    rows = resp.json()
    if not rows:
        return False
    try:
        last = datetime.fromisoformat(rows[0]["sent_at"].replace("Z", "+00:00"))
    except Exception:
        return False
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    return age_h < hours


async def _record_push_send(
    client: httpx.AsyncClient,
    user_id: str,
    campaign: str,
    title: str,
    provider_id: str | None,
    status: str,
) -> None:
    """Write an audit row. Best-effort; never raises."""
    try:
        resp = await client.post(
            f"{_PG_BASE}/sent_push",
            json={
                "user_id": user_id,
                "campaign": campaign,
                "title": title,
                "provider": "onesignal",
                "provider_id": provider_id,
                "status": status,
            },
            headers={**_PG_HEADERS, "Prefer": "return=minimal"},
            timeout=10.0,
        )
        if resp.status_code >= 300:
            logger.warning(
                "[push] audit insert failed [%s]: %s",
                resp.status_code, resp.text[:200],
            )
    except Exception as e:
        logger.warning("[push] audit insert threw: %r", e)


async def send_push_notification(
    user_id: str,
    title: str,
    body: str,
    *,
    campaign: str,
    data: dict[str, Any] | None = None,
    url: str | None = None,
    dedup_window_hours: int = 1,
) -> bool:
    """
    Send a push to every device subscribed to this user via OneSignal.

    Returns True on successful send, False on any skip/failure. Never
    raises — callers should fire-and-forget alongside their email send.

    Args:
      user_id — Supabase user uuid. Must match the external_id the
        frontend assigned via OneSignal.login(user_id).
      title, body — push copy.
      campaign — scenario key (same vocabulary as sent_emails.campaign:
        'interest_received', 'interest_accepted', ...). Used for the
        audit row AND dedup check.
      data — arbitrary JSON payload the frontend handler can read on
        click. Typically includes {"url": "/path"} for in-app routing.
      url — convenience; if provided, auto-merged into `data` as
        {"url": url} AND set as OneSignal's web_url for the default
        click handler. Pass this when you want the browser to navigate
        on push click.
      dedup_window_hours — skip if the same (user_id, campaign) push
        was already sent inside this window. Default 1h. Set 0 to
        disable dedup for a one-off (rare).
    """
    if not ONESIGNAL_APP_ID or not ONESIGNAL_REST_API_KEY:
        logger.info("[push] ONESIGNAL env not configured — skipping push for %s", user_id)
        return False
    if not user_id:
        return False

    async with httpx.AsyncClient() as client:
        if await _already_pushed_recently(client, user_id, campaign, dedup_window_hours):
            logger.info(
                "[push] skipping %s to %s — pushed in last %dh",
                campaign, user_id, dedup_window_hours,
            )
            return False

        payload_data: dict[str, Any] = {"campaign": campaign}
        if data:
            payload_data.update(data)
        if url:
            payload_data["url"] = url

        payload: dict[str, Any] = {
            "app_id": ONESIGNAL_APP_ID,
            # Target by external_id (what the frontend set via
            # OneSignal.login). include_aliases is the modern API —
            # the deprecated include_external_user_ids still works but
            # OneSignal docs recommend include_aliases for new code.
            "include_aliases": {"external_id": [user_id]},
            "target_channel": "push",
            "headings": {"en": title},
            "contents": {"en": body},
            "data": payload_data,
        }
        if url:
            payload["web_url"] = url

        try:
            resp = await client.post(
                ONESIGNAL_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Key {ONESIGNAL_REST_API_KEY}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=15.0,
            )
        except Exception as e:
            logger.warning("[push] send threw for %s: %r", user_id, e)
            await _record_push_send(client, user_id, campaign, title, None, "failed")
            return False

        provider_id: str | None = None
        ok = False
        try:
            j = resp.json()
            provider_id = j.get("id")
            # OneSignal returns 200 with id present on success.
            # It can also return 200 with "errors" and no recipients
            # (external_id doesn't exist, user never subscribed, etc).
            ok = resp.status_code < 300 and bool(provider_id) and not j.get("errors")
        except Exception:
            ok = False

        await _record_push_send(
            client,
            user_id=user_id,
            campaign=campaign,
            title=title,
            provider_id=provider_id,
            status="sent" if ok else "failed",
        )

        if ok:
            logger.info(
                "[push] sent %s to %s (onesignal_id=%s)",
                campaign, user_id, provider_id,
            )
        else:
            # Common reasons: user never granted push permission, OR
            # user_id has no OneSignal subscription aliased to it yet.
            # NOT an error worth paging on.
            logger.info(
                "[push] no delivery for %s to %s (status=%s detail=%s)",
                campaign, user_id, resp.status_code, resp.text[:200],
            )
        return ok
