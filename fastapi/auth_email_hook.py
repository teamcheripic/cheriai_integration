"""
auth_email_hook.py — Supabase Send Email Hook receiver.

Supabase Auth can be configured to POST every outbound auth email
(magic link, OTP, signup confirmation, recovery, etc.) to a webhook
instead of sending them itself. We use that hook here to deliver
those emails through Resend using the same admin-editable templates
that live in public.email_templates.

Flow:
    Supabase Auth        →  POST /auth/send-email-hook (this handler)
    (generates OTP)          verify signature, load template, render,
                             send via Resend, return 200.

Configuration (Supabase dashboard):
    Authentication → Hooks → Send Email Hook
      URL:     https://<railway-host>/auth/send-email-hook
      Secret:  copy the "v1,whsec_..." value shown in the dashboard
               into app_config.send_email_hook_secret (or
               SEND_EMAIL_HOOK_SECRET on Railway).

Signature verification (Standard Webhooks):
    headers:  webhook-id, webhook-timestamp, webhook-signature
    signed:   "{id}.{timestamp}.{body}"
    algo:     HMAC-SHA256, base64 encoded, prefixed "v1,"
    secret:   raw bytes after stripping "v1,whsec_" prefix and base64-decoding.

Returning a non-2xx tells Supabase the email failed → user sees
the auth error immediately. Do NOT return 200 on a delivery failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from app_config import get_config
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

# Supabase's max drift window for accepting a hook request. Older-than-5-min
# payloads are rejected as replays.
_MAX_TIMESTAMP_DRIFT_SECONDS = 5 * 60

# Map every Supabase email_action_type to a template slug in
# public.email_templates. Unmapped types fall through to _FALLBACK_SLUG
# (auth_otp) so nothing silently fails to send.
_ACTION_TO_SLUG: dict[str, str] = {
    "signup": "auth_otp",
    "login": "auth_otp",
    "magiclink": "auth_otp",
    "email": "auth_otp",
    # Below can grow into their own templates when we design them:
    "recovery": "auth_otp",
    "invite": "auth_otp",
    "email_change": "auth_otp",
    "email_change_new": "auth_otp",
    "reauthentication": "auth_otp",
}
_FALLBACK_SLUG = "auth_otp"


class HookVerificationError(Exception):
    """Raised when the Standard Webhooks signature does not match."""


def _decode_secret(raw: str) -> bytes:
    """
    Supabase presents the secret as "v1,whsec_<base64>". Standard Webhooks
    keeps the base64-decoded bytes as the HMAC key. Accept either the
    prefixed or the raw-base64 form so ops can paste either.
    """
    raw = raw.strip()
    if raw.startswith("v1,whsec_"):
        raw = raw[len("v1,whsec_"):]
    elif raw.startswith("whsec_"):
        raw = raw[len("whsec_"):]
    try:
        return base64.b64decode(raw)
    except Exception as e:
        raise HookVerificationError(f"secret is not valid base64: {e}") from e


def verify_signature(headers: dict[str, str], body: bytes, secret: str) -> None:
    """Verify Standard Webhooks headers. Raises HookVerificationError on any mismatch."""
    lower = {k.lower(): v for k, v in headers.items()}
    hook_id = lower.get("webhook-id")
    hook_ts = lower.get("webhook-timestamp")
    hook_sig = lower.get("webhook-signature")
    if not (hook_id and hook_ts and hook_sig):
        raise HookVerificationError("missing webhook-id / webhook-timestamp / webhook-signature header")

    try:
        ts_int = int(hook_ts)
    except ValueError as e:
        raise HookVerificationError(f"webhook-timestamp is not an integer: {hook_ts}") from e
    if abs(time.time() - ts_int) > _MAX_TIMESTAMP_DRIFT_SECONDS:
        raise HookVerificationError("webhook-timestamp outside allowed drift window (possible replay)")

    key = _decode_secret(secret)
    signed = f"{hook_id}.{hook_ts}.".encode("utf-8") + body
    mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")

    # `webhook-signature` can carry multiple space-separated values, each
    # prefixed with a scheme (e.g. "v1,<sig> v2,<sig>"). Match any.
    provided = {s.split(",", 1)[1] for s in hook_sig.split() if "," in s}
    if not any(hmac.compare_digest(mac, p) for p in provided):
        raise HookVerificationError("signature mismatch")


async def _build_auth_render_context(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """
    Assemble the {{variable}} substitution context for an auth email.
    Returns (ctx, recipient_email).
    """
    user = payload.get("user") or {}
    email_data = payload.get("email_data") or {}

    recipient = user.get("email") or ""
    display_name = (
        (user.get("user_metadata") or {}).get("full_name")
        or (user.get("user_metadata") or {}).get("name")
        or (recipient.split("@")[0] if recipient else "there")
    )

    app_base = await _cfg_app_base_url()
    redirect = email_data.get("redirect_to") or app_base

    ctx = {
        "name": display_name,
        # The 6-digit code Supabase generated. `token` is the plaintext
        # OTP; `token_hash` is the version used in confirmation URLs.
        "otp_code": str(email_data.get("token") or ""),
        "token_hash": str(email_data.get("token_hash") or ""),
        "confirmation_url": (
            f"{app_base}/auth/v1/verify"
            f"?token={email_data.get('token_hash', '')}"
            f"&type={email_data.get('email_action_type', 'magiclink')}"
            f"&redirect_to={redirect}"
        ),
        "app_url": app_base,
        "redirect_url": redirect,
        "logo_url": await _cfg_logo_url(),
        "support_email": await _cfg_support_email(),
        "unsubscribe_url": await _cfg_unsubscribe_url(),
        # Kept empty so any generic template variable substitutes cleanly.
        "headline": "Your CheriPic login code",
        "headline_short": "Your login code",
        "matches_count": "",
        "interests_count": "",
        "matches_plural": "",
        "interests_plural": "",
    }
    return ctx, recipient


async def handle_send_email(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Called after signature verification has passed. Picks the template by
    email_action_type, renders it, sends via Resend.

    Returns a status dict for logging. Raises on delivery failure so the
    caller can respond 5xx and let Supabase's auth flow surface the error.
    """
    ctx, recipient = await _build_auth_render_context(payload)
    if not recipient:
        raise ValueError("no recipient email in payload")

    action = (payload.get("email_data") or {}).get("email_action_type") or ""
    slug = _ACTION_TO_SLUG.get(action, _FALLBACK_SLUG)

    template = await _load_template(slug)
    if not template:
        raise RuntimeError(
            f"email_templates row missing for slug='{slug}' — apply migration 008 "
            "in Supabase SQL editor"
        )

    subject = _apply_template_vars(template["subject"], ctx)
    html = _apply_template_vars(template["html_body"], ctx)
    text = _apply_template_vars(template.get("text_body") or "", ctx)

    async with httpx.AsyncClient() as client:
        ok, provider_id = await send_email(client, recipient, subject, html, text)
    if not ok:
        raise RuntimeError("resend call failed — see previous log line")

    logger.info(
        "[auth-hook] delivered action=%s to=%s slug=%s resend_id=%s",
        action, recipient, slug, provider_id,
    )
    return {
        "ok": True,
        "action": action,
        "slug": slug,
        "to": recipient,
        "provider_id": provider_id,
    }


async def get_hook_secret() -> str:
    """Read the shared secret from app_config with env fallback."""
    return await get_config(
        "send_email_hook_secret",
        env_key="SEND_EMAIL_HOOK_SECRET",
        default="",
    ) or ""


def parse_payload(body: bytes) -> dict[str, Any]:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"malformed JSON body: {e}") from e
