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
    """
    Return {email, nick_name, full_name, notify_email_enabled, notify_push_enabled}
    or None.

    The two notify_* flags are member-controlled from Profile → Communication
    Preferences (migration 043). Both default TRUE in the DB; a NULL value
    (shouldn't exist post-migration but defensive) is treated as enabled.
    """
    resp = await client.get(
        f"{_PG_BASE}/user_profiles",
        params={
            "select": "email,nick_name,full_name,notify_email_enabled,notify_push_enabled",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
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
        # Member communication preferences (migration 043). Both default
        # TRUE server-side and NULL is treated as TRUE for safety.
        email_pref_on = profile.get("notify_email_enabled")
        email_pref_on = True if email_pref_on is None else bool(email_pref_on)
        push_pref_on = profile.get("notify_push_enabled")
        push_pref_on = True if push_pref_on is None else bool(push_pref_on)

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

        # 4. Assemble context — always, even if the email template is
        # missing. The context is also used to render the companion push
        # title/body/url, so missing-template must not block push.
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
        # H14S.16 · neutral third-party copy wins over whatever a caller
        # passed (never "official with someone else" / "committed").
        ctx.update(NEUTRAL_COPY_OVERRIDES.get(template_slug, {}))

        # 3+5+6. Load template, render, send + audit. If the template
        # row is missing we SKIP the email send but still fall through
        # to the companion push below — push is an independent channel
        # and must not be killed by an email-side config miss.
        # Reported 2026-10-12: interest_received had no email_templates
        # row, so the whole function returned early at template load
        # and the receiver got no push either.
        template = await _load_template(template_slug)
        email_ok = False
        if not email_pref_on:
            # Member opted out of email. Skip email entirely; the
            # companion push below still runs if push_pref_on is True —
            # the two channels are independent member choices.
            logger.info(
                "[transactional_email] email skipped for %s (slug=%s) — member opted out of email",
                user_id, template_slug,
            )
        elif not template:
            logger.warning(
                "[transactional_email] template '%s' missing — email skipped; "
                "companion push still runs. Add the row to email_templates "
                "if an email is desired for this slug.",
                template_slug,
            )
        else:
            subject = _apply_template_vars(template["subject"], ctx)
            html = _apply_template_vars(template["html_body"], ctx)
            text = _apply_template_vars(template.get("text_body") or "", ctx)
            # Resend network-timeout or any transient error must NOT
            # abort the companion push below. Reported 2026-10-12: an
            # unhandled httpx.TimeoutException from send_email was
            # propagating out of send_transactional_email, caught by
            # the caller's outer try/except, and silencing push too.
            provider_id: str | None = None
            try:
                email_ok, provider_id = await send_email(client, recipient, subject, html, text)
            except Exception as e:
                email_ok = False
                logger.warning(
                    "[transactional_email] send_email threw for %s to %s: %r",
                    template_slug, user_id, e,
                )
            try:
                await _record_send(
                    client,
                    user_id=user_id,
                    email=recipient,
                    campaign=template_slug,
                    subject=subject,
                    provider_id=provider_id,
                    status="sent" if email_ok else "failed",
                    related_user_id=related_user_id,
                )
            except Exception as e:
                logger.warning(
                    "[transactional_email] _record_send threw for %s to %s: %r",
                    template_slug, user_id, e,
                )
            if email_ok:
                logger.info(
                    "[transactional_email] sent %s to %s resend_id=%s",
                    template_slug, user_id, provider_id,
                )
            else:
                logger.warning(
                    "[transactional_email] send failed for %s to %s",
                    template_slug, user_id,
                )

        # ---- Companion push notification ------------------------------------
        # 2026-10-12 refactor: channel decisions live in NOTIFICATION_REGISTRY
        # below. If the slug has a push_config, we fire push regardless of
        # email outcome so every call site that registers both channels
        # delivers both — a missing email template or a Resend outage must
        # not silence push too.
        try:
            push_cfg = NOTIFICATION_REGISTRY.get(template_slug)
            if push_cfg is not None and not push_pref_on:
                logger.info(
                    "[transactional_email] push skipped for %s (slug=%s) — member opted out of push",
                    user_id, template_slug,
                )
            elif push_cfg is not None:
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

        # Return value reflects email outcome specifically — callers that
        # care about push outcome should read Railway logs or sent_push.
        if not template:
            return False
        return email_ok


# =============================================================================
# H14S.16 · Third parties are never told WHY a connection closed. When a
# member makes it official with someone else, everyone else they were
# connected to / exploring gets the same neutral line as any other ending.
# Applied after extra_vars, so a caller can't reintroduce the reason.
# =============================================================================
NEUTRAL_CONNECTION_ENDED_LINE = "One of your connections on CheriPic has ended."
NEUTRAL_COPY_OVERRIDES: dict[str, dict[str, str]] = {
    "connection_lost": {"context_line": NEUTRAL_CONNECTION_ENDED_LINE},
}


async def send_template_to_address(
    to: str,
    template_slug: str,
    name: str | None = None,
    extra_vars: dict[str, Any] | None = None,
) -> bool:
    """
    Send a template to a known address WITHOUT reading user_profiles —
    for the account-deletion confirmation, where the profile row is already
    gone. No preference check, dedup, audit row or push. Never raises; a
    missing template is a logged soft failure (returns False).
    """
    if not to or "@" not in to:
        return False
    try:
        template = await _load_template(template_slug)
        if not template:
            logger.warning("[transactional_email] template '%s' missing — email skipped", template_slug)
            return False
        ctx: dict[str, Any] = {
            "name": name or to.split("@")[0],
            "app_url": await _cfg_app_base_url(),
            "logo_url": await _cfg_logo_url(),
            "support_email": await _cfg_support_email(),
            "unsubscribe_url": await _cfg_unsubscribe_url(),
        }
        if extra_vars:
            ctx.update({k: ("" if v is None else str(v)) for k, v in extra_vars.items()})
        subject = _apply_template_vars(template["subject"], ctx)
        html = _apply_template_vars(template["html_body"], ctx)
        text = _apply_template_vars(template.get("text_body") or "", ctx)
        async with httpx.AsyncClient() as client:
            ok, _ = await send_email(client, to, subject, html, text)
        return bool(ok)
    except Exception as e:
        logger.warning("[transactional_email] direct send of %s failed: %r", template_slug, e)
        return False


# =============================================================================
# NOTIFICATION_REGISTRY — single source of truth for which campaigns
# push alongside their email, with the push copy templated against the
# same {{var}} context the email uses. Add a slug here → every call to
# send_transactional_email(slug) auto-fires push after email. Omit a
# slug → email only. Reported 2026-10-12.
# =============================================================================
NOTIFICATION_REGISTRY: dict[str, dict[str, Any]] = {
    # --- Matching flow (both channels) --------------------------------
    # Handoff §14: "Use generic lock-screen previews by default for
    # sensitive relationship events." Push copy below therefore never names
    # the other member or discloses why something happened (e.g. that a
    # connection partnered elsewhere); the email/in-app inbox carries the
    # detail once the member opens the app.
    "interest_received": {
        "title": "You have a new interest",
        "body":  "Open CheriPic to review it.",
        "url":   "/#/matching?tab=received",
        "dedup_window_hours": 1,
    },
    "interest_accepted": {
        "title": "Good news on CheriPic ✨",
        "body":  "One of your interests was accepted. Open CheriPic to see it.",
        "url":   "/#/matching?tab=sent",
        "dedup_window_hours": 1,
    },
    "interest_declined": {
        "title": "An update on your interest",
        "body":  "Open CheriPic for details.",
        "url":   "/#/matching",
        "dedup_window_hours": 1,
    },
    "connection_lost": {
        "title": "An update on your connections",
        "body":  "Open CheriPic for details.",
        "url":   "/#/matching",
        "dedup_window_hours": 24,
    },
    "partner_proposal_received": {
        "title": "You have a new request on CheriPic",
        "body":  "Open your Red Room to review it.",
        "url":   "/#/chat/{{other_user_id}}",
        "dedup_window_hours": 1,
    },
    "partner_proposal_accepted": {
        "title": "Your connection is now official 💞",
        "body":  "Open CheriPic to continue in your Red Room.",
        "url":   "/#/chat/{{other_user_id}}",
        "dedup_window_hours": 24,
    },
    "partner_proposal_declined": {
        "title": "An update on your request",
        "body":  "Open CheriPic for details.",
        "url":   "/#/matching",
        "dedup_window_hours": 1,
    },
    "relationship_ended": {
        "title": "An update on your connection",
        "body":  "Open CheriPic for details.",
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
