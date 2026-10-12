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


async def _communication_live() -> bool:
    """
    Global dedup kill-switch (app_config key: communication_live).
    Default FALSE while testing — every send fires immediately,
    nothing is suppressed. Flip to TRUE in prod via the admin panel
    (or set COMMUNICATION_LIVE=true in Railway env) so the normal
    per-pair dedup windows apply.

    Read via the 60-second cached app_config layer, so flipping the
    admin toggle propagates within a minute without a redeploy.
    """
    from app_config import get_bool
    return await get_bool("communication_live", default=False)


async def _already_sent_recently(
    client: httpx.AsyncClient,
    user_id: str,
    campaign: str,
    hours: int,
    related_user_id: str | None = None,
) -> bool:
    """
    Dedup check — has this campaign been delivered to this user recently?

    Per-pair variant (migration 039): when `related_user_id` is provided,
    the window is scoped to (user_id, campaign, related_user_id) so a
    popular receiver who gets interests from several senders in the
    same window sees each one — only RE-SENDS from the SAME sender get
    suppressed. When `related_user_id` is None (welcome_*, payment_*,
    assessment_completed), falls back to the legacy per-(user,campaign)
    dedup, matching rows where related_user_id IS NULL.
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
        f"{_PG_BASE}/sent_emails",
        params=params,
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
    related_user_id: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "user_id": user_id,
        "email": email,
        "campaign": campaign,
        "subject": subject,
        "provider": "resend",
        "provider_id": provider_id,
        "status": status,
    }
    if related_user_id:
        payload["related_user_id"] = related_user_id
    resp = await client.post(
        f"{_PG_BASE}/sent_emails",
        json=payload,
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
    related_user_id: str | None = None,
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

        # 2. Dedup (per-pair when related_user_id is provided). Entirely
        # skipped while communication_live=false (testing mode) so every
        # send fires immediately. Flip the admin toggle (or set
        # COMMUNICATION_LIVE=true env) to enable the normal windows.
        if await _communication_live() and await _already_sent_recently(
            client, user_id, template_slug, dedup_window_hours,
            related_user_id=related_user_id,
        ):
            logger.info(
                "[transactional_email] skipping %s to %s (related=%s) — sent in last %dh",
                template_slug, user_id, related_user_id, dedup_window_hours,
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
            related_user_id=related_user_id,
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

        # ---- Companion push notification ------------------------------------
        # 2026-10-12 refactor: channel decisions live in NOTIFICATION_REGISTRY
        # below. If the slug has a push_config, we fire push after email so
        # every call site that already emails ALSO pushes — zero per-site
        # code changes. Slugs NOT in the registry stay email-only (welcome_*,
        # payment_*, membership_upgraded, assessment_completed, etc).
        try:
            push_cfg = NOTIFICATION_REGISTRY.get(template_slug)
            if push_cfg is not None:
                # Lazy import so transactional_email.py doesn't require the
                # push module when ONESIGNAL env isn't set — push_notifications
                # itself early-returns False in that case.
                from push_notifications import send_push_notification
                title = _apply_template_vars(push_cfg["title"], ctx)
                body = _apply_template_vars(push_cfg["body"], ctx)
                url_tpl = push_cfg.get("url")
                url = _apply_template_vars(url_tpl, ctx) if url_tpl else None
                await send_push_notification(
                    user_id=user_id,
                    title=title,
                    body=body,
                    campaign=template_slug,
                    url=url,
                    dedup_window_hours=push_cfg.get("dedup_window_hours", 1),
                    related_user_id=related_user_id,
                )
        except Exception as e:
            logger.warning(
                "[transactional_email] companion push failed for %s to %s: %r",
                template_slug, user_id, e,
            )

        return ok


# =============================================================================
# NOTIFICATION_REGISTRY — single source of truth for which campaigns
# push alongside their email, with the push copy templated against the
# same {{var}} context the email uses. Add a slug here → every call to
# send_transactional_email(slug) auto-fires push after email. Omit a
# slug → email only. Reported 2026-10-12.
# =============================================================================
NOTIFICATION_REGISTRY: dict[str, dict[str, Any]] = {
    # --- Matching flow (both channels) --------------------------------
    "interest_received": {
        "title": "{{other_name}} is interested in you",
        "body":  "Open your requests to decide.",
        "url":   "/#/matching?tab=received",
        "dedup_window_hours": 1,
    },
    "interest_accepted": {
        "title": "{{other_name}} accepted your interest ✨",
        "body":  "Tap to open your Red Room.",
        "url":   "/#/matching?tab=sent",
        "dedup_window_hours": 1,
    },
    "interest_declined": {
        "title": "An update on your interest",
        "body":  "They weren't ready this time — keep going.",
        "url":   "/#/matching",
        "dedup_window_hours": 1,
    },
    "connection_lost": {
        "title": "A connection has moved on",
        "body":  "They partnered elsewhere. Discover is open.",
        "url":   "/#/matching",
        "dedup_window_hours": 24,
    },
    "partner_proposal_received": {
        "title": "{{other_name}} wants to make it official 💞",
        "body":  "Open your Red Room to review and accept.",
        "url":   "/#/chat/{{other_user_id}}",
        "dedup_window_hours": 1,
    },
    "partner_proposal_accepted": {
        "title": "You and {{other_name}} are official 💞",
        "body":  "New introductions paused. Tap to open your Red Room.",
        "url":   "/#/chat/{{other_user_id}}",
        "dedup_window_hours": 24,
    },
    "partner_proposal_declined": {
        "title": "An update on your Make-it-Official request",
        "body":  "{{other_name}} wasn't ready this time.",
        "url":   "/#/matching",
        "dedup_window_hours": 1,
    },
    "relationship_ended": {
        "title": "An update on your partnership",
        "body":  "{{other_name}} has ended your CheriPic partnership. Tap for details.",
        "url":   "/#/matching",
        "dedup_window_hours": 24,
    },
    # --- Red Room messaging --------------------------------------------
    # Explicit push wire remains in /chat/notify-unread-message (that
    # endpoint intentionally does NOT call send_transactional_email per
    # 2026-10-12 spec: push only, no email).
    # --- Verification (both channels) ----------------------------------
    "verify_approved": {
        "title": "You're verified on CheriPic ✓",
        "body":  "Full access unlocked. Open the app to meet connections.",
        "url":   "/#/profile",
        "dedup_window_hours": 24,
    },
    "verify_needs_attention": {
        "title": "Action required on your verification",
        "body":  "Please re-submit your documents. Tap to review.",
        "url":   "/#/profile",
        "dedup_window_hours": 24,
    },
    "verify_revoked": {
        "title": "Important update on your verification",
        "body":  "Your verified status was revoked. Open profile for details.",
        "url":   "/#/profile",
        "dedup_window_hours": 24,
    },
    # --- Subscription lifecycle (both channels) ------------------------
    "membership_cancelled": {
        "title": "Your CheriPic subscription has been cancelled",
        "body":  "You'll keep access until the current billing period ends.",
        "url":   "/#/profile",
        "dedup_window_hours": 24,
    },
    "premium_ended": {
        "title": "Your CheriPic Premium access has ended",
        "body":  "You're back on the Free tier. Tap to re-upgrade anytime.",
        "url":   "/#/pricing",
        "dedup_window_hours": 24,
    },
    # NOTE: scenarios below are intentionally OMITTED from the registry
    # per user spec 2026-10-12 — email only, no push:
    #   assessment_completed, welcome_free, welcome_premium_lite,
    #   welcome_premium, membership_upgraded, payment_successful,
    #   payment_failed, verification_submitted
}
