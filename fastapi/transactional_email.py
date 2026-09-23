"""
transactional_email.py — one entry point for every trigger-based email.

Every trigger point in the backend (Stripe webhook, KYC status change,
account restrict/reactivate, deletion flow, welcome-on-register, etc.)
calls the same function:

    await send_transactional_email(
        user_id      = "<uuid>",
        template_slug= "welcome_free" | "payment_successful" | ...,
        extra_vars   = {"amount": "59", "plan_name": "Premium Lite", ...},
    )

Guarantees:
  • Loads recipient email + nick_name from user_profiles once
  • Loads the email_templates row (falls back to a minimal wrapper if
    missing so a trigger doesn't blow up if migration 022 wasn't run)
  • Renders {{variable}} substitution using the standard context (name,
    app_url, logo_url, support_email, unsubscribe_url) merged with
    per-trigger extra_vars
  • Sends via Resend (reuses cron_daily_email.send_email)
  • Records the send in sent_emails so we can audit "did user X get
    the welcome email?"
  • Dedup: skips if we've already sent this template to this user in
    the last `dedup_window_hours` (default 1h) to prevent Stripe
    webhook double-fires from spamming

Everything is async and best-effort — callers should await but treat
a False return as informational, not fatal.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL
from cron_daily_email import (
    _apply_template_vars,
    _cfg_app_base_url,
    _cfg_logo_url,
    _cfg_support_email,
    _cfg_unsubscribe_url,
    _load_template,
    send_email,
)

logger = logging.getLogger(__name__)

_PG_BASE = f"{SUPABASE_URL}/rest/v1"
_PG_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


async def _fetch_recipient(client: httpx.AsyncClient, user_id: str) -> dict[str, Any] | None:
    """Return {email, nick_name, full_name} or None."""
    resp = await client.get(
        f"{_PG_BASE}/user_profiles",
        params={"select": "email,nick_name,full_name", "user_id": f"eq.{user_id}", "limit": "1"},
        headers=_PG_HEADERS,
        timeout=10.0,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


async def _already_sent_recently(
    client: httpx.AsyncClient, user_id: str, campaign: str, hours: int
) -> bool:
    """Dedup check — has this campaign been delivered to this user recently?"""
    if hours <= 0:
        return False
    resp = await client.get(
        f"{_PG_BASE}/sent_emails",
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


async def _record_send(
    client: httpx.AsyncClient,
    user_id: str,
    email: str,
    campaign: str,
    subject: str,
    provider_id: str | None,
    status: str,
) -> None:
    resp = await client.post(
        f"{_PG_BASE}/sent_emails",
        json={
            "user_id": user_id,
            "email": email,
            "campaign": campaign,
            "subject": subject,
            "provider": "resend",
            "provider_id": provider_id,
            "status": status,
        },
        headers={**_PG_HEADERS, "Prefer": "return=minimal"},
        timeout=10.0,
    )
    if resp.status_code >= 300:
        logger.warning("[transactional_email] audit insert failed [%s]: %s", resp.status_code, resp.text[:200])


async def send_transactional_email(
    user_id: str,
    template_slug: str,
    extra_vars: dict[str, Any] | None = None,
    dedup_window_hours: int = 1,
) -> bool:
    """
    Send `template_slug` to `user_id`. Returns True on success, False on
    any skip/failure. Never raises — callers can `await` and continue.

    extra_vars is merged into the standard render context; use it for
    template-specific values like {{amount}}, {{plan_name}},
    {{rejection_reason}}, etc.

    dedup_window_hours: skip if the same (user_id, template_slug) was
    successfully sent within this many hours. Set to 0 to disable
    dedup for emails that legitimately can fire multiple times (e.g.
    payment_successful for separate invoices).
    """
    async with httpx.AsyncClient() as client:
        # 1. Recipient
        try:
            profile = await _fetch_recipient(client, user_id)
        except Exception as e:
            logger.warning("[transactional_email] profile fetch failed for %s: %r", user_id, e)
            return False
        if not profile or not profile.get("email"):
            logger.info("[transactional_email] no email on file for %s — skip", user_id)
            return False

        recipient = profile["email"]
        display_name = (
            profile.get("nick_name")
            or profile.get("full_name")
            or recipient.split("@")[0]
        )

        # 2. Dedup
        if await _already_sent_recently(client, user_id, template_slug, dedup_window_hours):
            logger.info(
                "[transactional_email] skipping %s to %s — sent in last %dh",
                template_slug, user_id, dedup_window_hours,
            )
            return False

        # 3. Load template
        template = await _load_template(template_slug)
        if not template:
            logger.warning(
                "[transactional_email] template '%s' missing — apply migration 022",
                template_slug,
            )
            return False

        # 4. Assemble context
        app_base = await _cfg_app_base_url()
        ctx: dict[str, Any] = {
            "name": display_name,
            "app_url": app_base,
            "logo_url": await _cfg_logo_url(),
            "support_email": await _cfg_support_email(),
            "unsubscribe_url": await _cfg_unsubscribe_url(),
        }
        if extra_vars:
            ctx.update({k: ("" if v is None else str(v)) for k, v in extra_vars.items()})

        # 5. Render
        subject = _apply_template_vars(template["subject"], ctx)
        html = _apply_template_vars(template["html_body"], ctx)
        text = _apply_template_vars(template.get("text_body") or "", ctx)

        # 6. Send + audit
        ok, provider_id = await send_email(client, recipient, subject, html, text)
        await _record_send(
            client,
            user_id=user_id,
            email=recipient,
            campaign=template_slug,
            subject=subject,
            provider_id=provider_id,
            status="sent" if ok else "failed",
        )
        if ok:
            logger.info(
                "[transactional_email] sent %s to %s (%s) resend_id=%s",
                template_slug, user_id, recipient, provider_id,
            )
        else:
            logger.warning(
                "[transactional_email] send failed for %s to %s",
                template_slug, user_id,
            )
        return ok
