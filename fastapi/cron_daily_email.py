"""
cron_daily_email.py — the daily "you've got matches waiting" nudge.

What it does:
    1. Reads unread notifications of type match_available / interest_received
       that are at least NOTIFICATION_MIN_AGE_HOURS old — we don't email the
       instant a notification lands, we give the user a chance to open the app
       first.
    2. Joins the recipient's email off user_profiles.
    3. Skips any notification already emailed under this campaign (dedup via
       the sent_emails table — see migrations/003_sent_emails.sql).
    4. Sends a single email per user summarizing what's waiting for them, via
       Resend (https://resend.com).
    5. Records each successful send in sent_emails so the next run is safely
       idempotent — re-running immediately is a no-op.

How it runs on Railway:
    Railway's free/hobby plan does not have a first-class cron. This module is
    imported by main.py and scheduled in-process via APScheduler; see
    ENABLE_DAILY_EMAIL_CRON below and the lifespan handler in main.py.

    It can also still be invoked as a standalone script (Render cron / local
    testing / one-off backfill):
        RESEND_API_KEY=... SUPABASE_URL=... SUPABASE_KEY=... \\
            python cron_daily_email.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL
from app_config import get_config, get_int

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("cron_daily_email")

# --- Configuration ---------------------------------------------------------
# All runtime-tunable values below are pulled from public.app_config via
# app_config.get_config() at call time (60s cache + env fallback). That
# means an admin rotating RESEND_API_KEY in the panel takes effect within
# a minute — no Railway redeploy. See app_config.py + migration 004.
RESEND_ENDPOINT = "https://api.resend.com/emails"

# Which notification types trigger a nudge email. Kept small on purpose:
# match_available   = CheriAI surfaced a new profile in Discover
# interest_received = someone tapped Interested on the user
NUDGEABLE_TYPES = ("match_available", "interest_received")

# Campaign id — matches the sent_emails.campaign column and the unique index.
CAMPAIGN = "daily_match_nudge"


async def _cfg_resend_api_key() -> str:
    return await get_config("resend_api_key", env_key="RESEND_API_KEY", default="") or ""


async def _cfg_from_address() -> str:
    return await get_config("resend_from", env_key="RESEND_FROM", default="CheriPic <noreply@cheripic.com>") or ""


async def _cfg_app_base_url() -> str:
    return await get_config("app_base_url", env_key="APP_BASE_URL", default="https://cheripic.com") or ""


async def _cfg_min_age_hours() -> int:
    return await get_int("email_nudge_min_age_hours", env_key="EMAIL_NUDGE_MIN_AGE_HOURS", default=6)


async def _cfg_max_per_run() -> int:
    return await get_int("email_nudge_max_per_run", env_key="EMAIL_NUDGE_MAX_PER_RUN", default=200)


async def _cfg_support_email() -> str:
    # Falls back to just the mailbox part of the from-address (strip the
    # display name) so the footer contact line always has something valid.
    val = await get_config("email_support_email", default=None)
    if val:
        return val
    frm = await _cfg_from_address()
    if "<" in frm and ">" in frm:
        return frm.split("<", 1)[1].rstrip(">").strip()
    return frm


async def _cfg_logo_url() -> str:
    return await get_config("email_logo_url", default="") or ""


async def _cfg_unsubscribe_url() -> str:
    """Where the footer 'manage / unsubscribe' link goes. Defaults to /#/profile."""
    override = await get_config("email_unsubscribe_url", default=None)
    if override:
        return override
    app_base = await _cfg_app_base_url()
    return f"{app_base}/#/profile"

# Master switch for the in-process APScheduler wiring in main.py. Off by
# default so a first Railway deploy of this file cannot start sending mail
# before the operator has set RESEND_API_KEY and reviewed the eligibility
# query. Set ENABLE_DAILY_EMAIL_CRON=true on Railway to turn it on.
def scheduler_enabled() -> bool:
    return os.getenv("ENABLE_DAILY_EMAIL_CRON", "").strip().lower() in ("1", "true", "yes", "on")


# --- Postgres access -------------------------------------------------------
# The minimal supabase_client wrapper only speaks eq-filters, and we need
# `in`, `is.null`, and `lt` here. So this module talks PostgREST directly
# with the same service key.
_PG_BASE = f"{SUPABASE_URL}/rest/v1"
_PG_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


async def _pg_get(client: httpx.AsyncClient, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    resp = await client.get(f"{_PG_BASE}/{table}", params=params, headers=_PG_HEADERS, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


async def _pg_post(client: httpx.AsyncClient, table: str, payload: dict[str, Any]) -> None:
    resp = await client.post(f"{_PG_BASE}/{table}", json=payload, headers=_PG_HEADERS, timeout=30.0)
    if resp.status_code >= 300:
        logger.warning("insert into %s failed [%s]: %s", table, resp.status_code, resp.text[:400])


# --- Eligibility -----------------------------------------------------------
async def gather_candidates(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """
    Return one row per USER we should email, each carrying the notifications
    that will be summarized in that user's email. Structure:
        [
          {
            "user_id": ...,
            "email":    ...,
            "nick_name": ...,
            "notifications": [ {id, type, title, body, created_at}, ... ]
          },
          ...
        ]
    """
    min_age_hours = await _cfg_min_age_hours()
    max_per_run = await _cfg_max_per_run()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_age_hours)).isoformat()

    # 1. Pull unread nudgeable notifications, oldest first (so the summary
    #    email covers the earliest missed match first).
    type_list = ",".join(NUDGEABLE_TYPES)
    notifs = await _pg_get(
        client,
        "notifications",
        {
            "select": "id,user_id,type,title,body,created_at",
            "type": f"in.({type_list})",
            "read_at": "is.null",
            "created_at": f"lt.{cutoff}",
            "order": "created_at.asc",
            # Over-fetch so grouping-by-user still yields ~max_per_run users
            "limit": str(max_per_run * 4),
        },
    )
    if not notifs:
        return []

    # 2. Filter out notifications we've already emailed under this campaign.
    notif_ids = [n["id"] for n in notifs]
    already_sent_ids: set[str] = set()
    # PostgREST `in.()` filter needs comma-joined values — chunk to keep the
    # query string sane on very large backlogs.
    for i in range(0, len(notif_ids), 100):
        chunk = notif_ids[i : i + 100]
        sent_rows = await _pg_get(
            client,
            "sent_emails",
            {
                "select": "notification_id",
                "campaign": f"eq.{CAMPAIGN}",
                "notification_id": f"in.({','.join(chunk)})",
            },
        )
        for r in sent_rows:
            if r.get("notification_id"):
                already_sent_ids.add(r["notification_id"])

    fresh = [n for n in notifs if n["id"] not in already_sent_ids]
    if not fresh:
        return []

    # 3. Look up recipient emails from user_profiles for each unique user_id.
    unique_user_ids = list({n["user_id"] for n in fresh})
    email_by_user: dict[str, dict[str, Any]] = {}
    for i in range(0, len(unique_user_ids), 100):
        chunk = unique_user_ids[i : i + 100]
        prof_rows = await _pg_get(
            client,
            "user_profiles",
            {
                "select": "user_id,email,nick_name,full_name",
                "user_id": f"in.({','.join(chunk)})",
            },
        )
        for r in prof_rows:
            if r.get("email"):
                email_by_user[r["user_id"]] = r

    # 4. Group notifications by user, drop users we have no email for, cap the
    #    total number of users we'll email in this run.
    by_user: dict[str, dict[str, Any]] = {}
    for n in fresh:
        prof = email_by_user.get(n["user_id"])
        if not prof:
            continue
        bucket = by_user.setdefault(
            n["user_id"],
            {
                "user_id": n["user_id"],
                "email": prof["email"],
                "nick_name": prof.get("nick_name") or prof.get("full_name") or "there",
                "notifications": [],
            },
        )
        bucket["notifications"].append(n)

    return list(by_user.values())[:max_per_run]


# --- Email rendering + send -----------------------------------------------
def _summarize_lines(n_match: int, n_interest: int) -> tuple[str, str]:
    """
    Return (headline_short, headline_long) — generic wording that reads
    well for any count. Specific counts are still exposed to templates as
    {{matches_count}} / {{interests_count}} for admins who want a
    data-heavy variant, but the default headlines stay evergreen so one
    template covers "one new match" through "ten interests waiting"
    equally well.
    """
    short = "A new potential connection"
    long = "A new potential connection is waiting on CheriPic."
    return short, long


async def _load_template(slug: str) -> dict[str, str] | None:
    """
    Fetch one row from email_templates. Returns None if the row is missing
    or the DB call fails — callers fall back to the hardcoded default in
    that case so a broken template never blocks sends.
    """
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/email_templates",
                params={"select": "subject,html_body,text_body", "slug": f"eq.{slug}", "limit": "1"},
                headers=headers,
            )
            resp.raise_for_status()
            rows = resp.json() or []
        return rows[0] if rows else None
    except Exception as e:
        logger.warning("email_templates load failed for slug=%s: %r", slug, e)
        return None


def _apply_template_vars(text: str, ctx: dict[str, Any]) -> str:
    """Tiny {{key}} substitution engine. No conditionals, no escaping."""
    out = text
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", "" if v is None else str(v))
    return out


async def _build_render_context(
    name: str,
    notifs: list[dict[str, Any]],
    *,
    override_headline: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Shared substitution context for all match-nudge renders. Returns
    (headline_short, ctx) so the caller can also use headline_short in
    the subject line if the template references it.
    """
    n_match = sum(1 for n in notifs if n["type"] == "match_available")
    n_interest = sum(1 for n in notifs if n["type"] == "interest_received")

    default_headline_short, default_headline = _summarize_lines(n_match, n_interest)
    headline = override_headline or default_headline

    app_base = await _cfg_app_base_url()
    ctx = {
        "name": name,
        "headline": headline,
        "headline_short": default_headline_short,
        "matches_count": n_match,
        "interests_count": n_interest,
        "matches_plural": "" if n_match == 1 else "s",
        "interests_plural": "" if n_interest == 1 else "s",
        "app_url": app_base,
        "logo_url": await _cfg_logo_url(),
        "support_email": await _cfg_support_email(),
        "unsubscribe_url": await _cfg_unsubscribe_url(),
    }
    return default_headline_short, ctx


def _fallback_html(ctx: dict[str, Any]) -> str:
    """
    Used when email_templates row is missing. Matches the v2 design in
    migration 007 — deep-purple ground, logo on top, white card with a
    purple headline, "Clarity before connection." tagline.
    """
    logo_img = (
        f"<img src=\"{ctx['logo_url']}\" alt=\"CheriPic\" width=\"150\" style=\"display:block;max-width:150px;height:auto;margin:0 auto;\" />"
        if ctx.get("logo_url") else ""
    )
    return (
        f"<div style=\"background:#34205f; padding:36px 16px 28px; font-family:'Helvetica Neue',Arial,sans-serif;\">"
        f"<div style=\"max-width:560px; margin:0 auto; text-align:center;\">"
        # Logo
        f"<div style=\"margin-bottom:20px;\">{logo_img}"
        f"<div style=\"height:2px;width:56px;background:linear-gradient(90deg,#6c47ff,#8a60ff);border-radius:2px;margin:8px auto 0;\"></div>"
        f"</div>"
        # White card
        f"<div style=\"background:#ffffff;border-radius:22px;overflow:hidden;box-shadow:0 18px 60px rgba(10,10,15,0.35);text-align:left;\">"
        f"<div style=\"padding:36px 40px 8px;color:#0a0a0f;\">"
        f"<div style=\"font-size:16px;font-weight:800;color:#0a0a0f;\">Hey {ctx['name']} 👋</div>"
        f"<h1 style=\"margin:18px 0 20px;font-size:26px;line-height:1.25;font-weight:800;color:#6c47ff;\">{ctx['headline']}</h1>"
        f"<p style=\"margin:0 0 14px;font-size:15px;line-height:1.65;color:#3d3d47;\">Not just another profile — someone who may align with what matters to you.</p>"
        f"<p style=\"margin:0 0 24px;font-size:15px;line-height:1.65;color:#3d3d47;\">Take a look before this connection moves on.</p>"
        f"</div>"
        f"<div style=\"padding:4px 40px 20px;text-align:center;\">"
        f"<a href=\"{ctx['app_url']}/#/matching\" "
        f"style=\"display:inline-block;padding:16px 40px;font-size:16px;color:#fff;text-decoration:none;font-weight:700;border-radius:14px;background:linear-gradient(135deg,#6c47ff 0%,#8a60ff 100%);\">See My Connection →</a>"
        f"</div>"
        f"<div style=\"padding:22px 40px 30px;text-align:center;font-size:14px;color:#3d3d47;line-height:1.6;\">"
        f"With love,<br /><strong style=\"color:#6c47ff;font-size:15px;\">The CheriPic Team</strong>"
        f"<div style=\"margin-top:8px;font-style:italic;color:#8a8a94;font-size:13px;\">Clarity before connection.</div>"
        f"</div>"
        f"</div>"
        # Footer on purple ground
        f"<div style=\"margin-top:22px;font-size:12px;color:#c4b5fd;line-height:1.75;\">"
        f"Need help? <a href=\"mailto:{ctx['support_email']}\" style=\"color:#ffffff;text-decoration:none;font-weight:600;\">{ctx['support_email']}</a><br />"
        f"<a href=\"{ctx['unsubscribe_url']}\" style=\"color:#c4b5fd;\">Manage notification preferences</a>"
        f"</div>"
        f"</div>"
        f"</div>"
    )


def _fallback_text(ctx: dict[str, Any]) -> str:
    return (
        f"Hey {ctx['name']},\n\n"
        f"{ctx['headline']}\n\n"
        f"Open CheriPic: {ctx['app_url']}/#/matching\n\n"
        f"Manage notifications: {ctx['unsubscribe_url']}\n"
    )


async def render_email(name: str, notifs: list[dict[str, Any]]) -> tuple[str, str, str]:
    """
    Build (subject, html, text) by loading the 'daily_match_nudge' template
    from the DB and substituting variables. Falls back to a hardcoded
    minimal template if the row is missing or the DB call fails, so a
    broken template can NEVER block sends.
    """
    _, ctx = await _build_render_context(name, notifs)
    template = await _load_template(CAMPAIGN)

    if template:
        subject = _apply_template_vars(template["subject"], ctx)
        html = _apply_template_vars(template["html_body"], ctx)
        text = _apply_template_vars(template["text_body"] or _fallback_text(ctx), ctx)
    else:
        logger.warning("email_templates row missing for %s — using hardcoded fallback.", CAMPAIGN)
        subject = f"{ctx['headline_short']} on CheriPic"
        html = _fallback_html(ctx)
        text = _fallback_text(ctx)

    return subject, html, text


async def send_email(
    client: httpx.AsyncClient, to: str, subject: str, html: str, text: str
) -> tuple[bool, str | None]:
    """Fire one Resend send. Returns (ok, provider_message_id_or_None)."""
    api_key = await _cfg_resend_api_key()
    from_address = await _cfg_from_address()
    if not api_key:
        logger.error("Resend api key not configured (app_config.resend_api_key + RESEND_API_KEY env are both empty).")
        return False, None

    res = await client.post(
        RESEND_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_address,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=15.0,
    )
    if res.status_code >= 300:
        logger.warning("Resend %s for %s: %s", res.status_code, to, res.text[:200])
        return False, None
    try:
        return True, (res.json() or {}).get("id")
    except ValueError:
        return True, None


_FAKE_TEST_NOTIFS = [
    {"type": "match_available", "id": "test", "title": "", "body": ""},
    {"type": "match_available", "id": "test2", "title": "", "body": ""},
    {"type": "interest_received", "id": "test3", "title": "", "body": ""},
]


async def send_test_email(to_address: str) -> dict[str, Any]:
    """
    Send a single test email to `to_address` using the current SAVED DB
    template with fake sample data (2 matches + 1 interest). Bypasses
    eligibility and does NOT touch sent_emails / notifications, so it's
    safe to fire repeatedly and gives the admin an accurate preview of the
    live layout (logo, colours, button, unsubscribe link).
    """
    if not to_address or "@" not in to_address:
        return {"ok": False, "error": "invalid_address"}

    subject, html, text = await render_email("there (test)", _FAKE_TEST_NOTIFS)

    async with httpx.AsyncClient() as client:
        ok, provider_id = await send_email(client, to_address, subject, html, text)
    return {
        "ok": ok,
        "to": to_address,
        "subject": subject,
        "provider_id": provider_id,
        "error": None if ok else "resend_call_failed_see_logs",
    }


# Extra sample values injected only into the test-send context so admins
# can preview templates that carry variables no cron currently supplies
# (e.g. {{otp_code}} on auth_otp). Real sends of those templates fill
# these keys from their own call site (the auth flow for otp_code).
_TEST_EXTRA_VARS: dict[str, Any] = {
    "otp_code": "519247",
}


async def send_template_test_email(
    to_address: str,
    subject_template: str,
    html_template: str,
    text_template: str,
) -> dict[str, Any]:
    """
    Same as send_test_email but renders from the provided RAW templates
    instead of the saved DB row. Lets an admin test an unsaved draft from
    the Email Templates editor without committing it first.

    The render context is the standard cron context (name, headline, app
    URL, logo, etc.) PLUS every key in _TEST_EXTRA_VARS so templates that
    reference otp_code / future variables still preview correctly.
    """
    if not to_address or "@" not in to_address:
        return {"ok": False, "error": "invalid_address"}
    if not subject_template or not html_template:
        return {"ok": False, "error": "subject and html_body are required"}

    _, ctx = await _build_render_context("there (test)", _FAKE_TEST_NOTIFS)
    ctx = {**ctx, **_TEST_EXTRA_VARS}
    subject = _apply_template_vars(subject_template, ctx)
    html = _apply_template_vars(html_template, ctx)
    text = _apply_template_vars(text_template or _fallback_text(ctx), ctx)

    async with httpx.AsyncClient() as client:
        ok, provider_id = await send_email(client, to_address, subject, html, text)
    return {
        "ok": ok,
        "to": to_address,
        "subject": subject,
        "provider_id": provider_id,
        "error": None if ok else "resend_call_failed_see_logs",
    }


# --- Orchestration --------------------------------------------------------
async def run_once(dry_run: bool = False) -> dict[str, int]:
    """One full cron pass. Returns counts so the caller/scheduler can log."""
    api_key = await _cfg_resend_api_key()
    if not api_key and not dry_run:
        logger.error("Resend api key not configured — aborting cron run.")
        return {"sent": 0, "failed": 0, "skipped": 0, "eligible": 0}

    min_age_hours = await _cfg_min_age_hours()
    max_per_run = await _cfg_max_per_run()

    async with httpx.AsyncClient() as client:
        candidates = await gather_candidates(client)
        logger.info(
            "Eligible recipients: %d (min notification age %dh, cap %d/run)",
            len(candidates), min_age_hours, max_per_run,
        )
        if not candidates:
            return {"sent": 0, "failed": 0, "skipped": 0, "eligible": 0}

        sent = failed = 0
        for c in candidates:
            subject, html, text = await render_email(c["nick_name"], c["notifications"])
            if dry_run:
                logger.info("[dry-run] %-30s | %d notif → %s", c["email"], len(c["notifications"]), subject)
                sent += 1
                continue

            ok, provider_id = await send_email(client, c["email"], subject, html, text)
            if not ok:
                failed += 1
                continue

            sent += 1
            # Record ONE sent_emails row per notification covered. This is
            # what future runs check to skip already-covered notifications,
            # so the row count must match the notification count — not "1
            # per email".
            for n in c["notifications"]:
                await _pg_post(client, "sent_emails", {
                    "user_id": c["user_id"],
                    "email": c["email"],
                    "notification_id": n["id"],
                    "campaign": CAMPAIGN,
                    "subject": subject,
                    "provider": "resend",
                    "provider_id": provider_id,
                    "status": "sent",
                })

        result = {"sent": sent, "failed": failed, "skipped": 0, "eligible": len(candidates)}
        logger.info("Cron done: %s", result)
        return result


# --- Standalone entrypoint (Render cron / manual runs) -------------------
async def _cli(dry_run: bool) -> int:
    result = await run_once(dry_run=dry_run)
    return 0 if result["failed"] == 0 else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Log who would be emailed without sending.")
    args = parser.parse_args()
    sys.exit(asyncio.run(_cli(args.dry_run)))
