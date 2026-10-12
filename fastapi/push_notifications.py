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
    client: httpx.AsyncClient,
    user_id: str,
    campaign: str,
    hours: float,
    related_user_id: str | None = None,
) -> bool:
    """
    Dedup — has this (user_id, campaign) push been sent in the last `hours`?

    Per-pair variant (migration 039): when `related_user_id` is set the
    window is scoped to that specific pair, so a popular receiver still
    gets push from each distinct sender while rapid-fire same-pair
    attempts are suppressed. Omitting it falls back to per-(user,campaign)
    dedup matching rows where related_user_id IS NULL.
    """
    if hours <= 0:
        return False
    params = {
        "select": "sent_at",
        "user_id": f"eq.{user_id}",
        "campaign": f"eq.{campaign}",
        "status": "eq.sent",
        "order": "sent_at.desc",
        "limit": "1",
    }
    if related_user_id:
        params["related_user_id"] = f"eq.{related_user_id}"
    else:
        params["related_user_id"] = "is.null"
    resp = await client.get(
        f"{_PG_BASE}/sent_push",
        params=params,
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
    related_user_id: str | None = None,
) -> None:
    """Write an audit row. Best-effort; never raises."""
    try:
        row: dict[str, Any] = {
            "user_id": user_id,
            "campaign": campaign,
            "title": title,
            "provider": "onesignal",
            "provider_id": provider_id,
            "status": status,
        }
        if related_user_id:
            row["related_user_id"] = related_user_id
        resp = await client.post(
            f"{_PG_BASE}/sent_push",
            json=row,
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
    dedup_window_hours: float = 1,
    related_user_id: str | None = None,
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
      related_user_id — the "other party" for interpersonal scenarios
        (sender for interest_received, caller for partner_proposal,
        etc.). When set, dedup is scoped to this exact pair instead
        of per-(user, campaign). Omit for broadcasts, celebration
        emails, and anything that has no counterparty.
    """
    if not ONESIGNAL_APP_ID or not ONESIGNAL_REST_API_KEY:
        logger.info("[push] ONESIGNAL env not configured — skipping push for %s", user_id)
        return False
    if not user_id:
        return False

    # OneSignal rejects web_url that isn't a full http(s) URL ("Option
    # Begin your notification web_url with http:// or https://."). Our
    # per-scenario call sites + the admin test UI pass relative paths
    # like "/#/home" or "/#/matching?tab=received" since the frontend
    # uses HashRouter. Normalize here so every caller is covered: if
    # url is relative, prepend APP_BASE_URL from app_config (default
    # https://cheripic.com). Reported 2026-10-11.
    if url and not (url.startswith("http://") or url.startswith("https://")):
        try:
            from cron_daily_email import _cfg_app_base_url
            base = (await _cfg_app_base_url()).rstrip("/")
            # Hash routes like "/#/home" need "/" preserved between
            # host and hash — "https://cheripic.com" + "/#/home" is
            # exactly what we want. Absolute path "/foo" works the
            # same. If url starts with "#" (bare hash), join with "/".
            if url.startswith("#"):
                url = base + "/" + url
            elif url.startswith("/"):
                url = base + url
            else:
                url = base + "/" + url
        except Exception as e:
            logger.warning("[push] url normalize failed: %r — stripping url entirely so send proceeds", e)
            url = None

    async with httpx.AsyncClient() as client:
        if await _already_pushed_recently(
            client, user_id, campaign, dedup_window_hours,
            related_user_id=related_user_id,
        ):
            logger.info(
                "[push] skipping %s to %s — pushed in last %dh (pair=%s)",
                campaign, user_id, dedup_window_hours, related_user_id or "n/a",
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
            await _record_push_send(
                client, user_id, campaign, title, None, "failed",
                related_user_id=related_user_id,
            )
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
            related_user_id=related_user_id,
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
