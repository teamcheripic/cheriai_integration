"""
cron_daily_email.py — runs once a day on Render (see render.yaml `cron` service).

What it does:
    1. Fetches the users we want to email today (example: anyone who
       hasn't logged in for 3+ days)
    2. Sends each one a short email via Resend (https://resend.com)
    3. Exits cleanly so Render closes the container — no event loop, no
       webserver, just a script

Why this lives in the same repo as the FastAPI app:
    Shares the supabase_client + env config + Dockerfile, so we only have
    one image to build and one set of secrets to manage. The Dockerfile's
    default CMD runs uvicorn; the cron service overrides it with
    `python cron_daily_email.py` (see render.yaml).

Local test:
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

import httpx

from supabase_client import supabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("cron_daily_email")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_ADDRESS = os.getenv("RESEND_FROM", "CheriPic <noreply@arkxpert.com>")
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://arkxpert.com")
RESEND_ENDPOINT = "https://api.resend.com/emails"

# Tune this to whatever "needs a nudge" means for your product. Default:
# users who created an account 3+ days ago AND haven't sent a chat in 3+ days.
NUDGE_AFTER_DAYS = 3


async def fetch_recipients() -> list[dict]:
    """
    Return rows the cron should email today.
    Replace the query with whatever fits your campaign.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NUDGE_AFTER_DAYS)).isoformat()
    rows = await supabase.select(
        "user_profiles",
        columns="user_id, full_name, nick_name, email",
        # Adjust these filters to match your schema. Examples:
        #   .lt({"updated_at": cutoff})  → not active recently
        #   .eq({"is_active": True})
        eq={"is_active": True},
        # Cap per run so a one-time large list doesn't blow the cron budget.
        limit=200,
    )
    return [r for r in rows if r.get("email")]


async def send_email(client: httpx.AsyncClient, to: str, subject: str, html: str) -> bool:
    """Fire one Resend send. Returns True on 2xx, logs + False otherwise."""
    res = await client.post(
        RESEND_ENDPOINT,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"from": FROM_ADDRESS, "to": [to], "subject": subject, "html": html},
        timeout=15.0,
    )
    if res.status_code >= 300:
        logger.warning("Resend %s for %s: %s", res.status_code, to, res.text[:200])
        return False
    return True


def render_email(name: str) -> tuple[str, str]:
    """Build the subject + HTML body for one recipient."""
    subject = "Your CheriPic match might be one tap away ✨"
    html = f"""
    <div style="font-family: 'Jost', system-ui, sans-serif; max-width: 480px; margin: auto; color: #1a0420;">
      <h2 style="margin: 0 0 12px;">Hey {name},</h2>
      <p style="font-size: 15px; line-height: 1.55;">
        We saved a fresh match for you on CheriPic. The window doesn't stay
        open forever — new profiles cycle every few days.
      </p>
      <p style="margin: 24px 0;">
        <a href="{APP_BASE_URL}/matching"
           style="background: linear-gradient(135deg, #ec4899, #a855f7);
                  color: #fff; text-decoration: none; padding: 12px 22px;
                  border-radius: 12px; font-weight: 700; letter-spacing: 0.3px;">
          See who matches you →
        </a>
      </p>
      <p style="font-size: 12px; color: #6b7280; margin-top: 32px;">
        You're getting this because you signed up for CheriPic. You can
        unsubscribe from your profile.
      </p>
    </div>
    """
    return subject, html


async def main(dry_run: bool) -> int:
    if not RESEND_API_KEY and not dry_run:
        logger.error("RESEND_API_KEY is not set — aborting.")
        return 1

    recipients = await fetch_recipients()
    logger.info("Eligible recipients today: %d", len(recipients))
    if not recipients:
        return 0

    sent, failed = 0, 0
    async with httpx.AsyncClient() as client:
        for r in recipients:
            name = r.get("nick_name") or r.get("full_name") or "there"
            email = r["email"]
            subject, html = render_email(name)
            if dry_run:
                logger.info("[dry-run] %-30s — %s", email, subject)
                sent += 1
                continue
            ok = await send_email(client, email, subject, html)
            if ok:
                sent += 1
            else:
                failed += 1

    logger.info("Done. Sent=%d failed=%d", sent, failed)
    # Render marks a cron run as failed when the script exits non-zero, which
    # surfaces in the dashboard as a red dot — useful for alerting.
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Log who would be emailed without sending.")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.dry_run)))
