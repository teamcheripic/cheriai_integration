#!/usr/bin/env python3
"""
CheriPic AI Backend — FastAPI Server

Endpoints:
  POST /chat                                       Chat with CheriAI
  GET  /conversations/{user_id}                    List a user's conversations
  GET  /conversations/{user_id}/{conversation_id}  Fetch full conversation history
  GET  /health                                     Health check

Run:
  cd ai_integration/fastapi
  source ../venv/bin/activate
  uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import os
import logging
import math
from datetime import datetime
from typing import Any, Optional, List
from contextlib import asynccontextmanager
import asyncio

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from dotenv import load_dotenv

load_dotenv()

from auth import get_current_user_id, get_current_admin_id, require_self
from supabase_client import supabase, SUPABASE_URL, SUPABASE_KEY
from llm_client import send_to_llm
from cheriai_prompts import CheriAIPromptBuilder
from user_memory import (
    load_memory,
    load_recent_messages,
    update_memory_async,
    seed_memory_from_profile,
)
from face_verification import compare_faces, FaceServiceUnavailable
from tier_limits import get_monthly_limits, STRIPE_PRICE_TO_TIER, describe_limits
import billing
from cron_daily_email import (
    run_once as run_daily_email_cron,
    scheduler_enabled_async as daily_email_enabled_async,
    scheduler_crontab_async as daily_email_crontab_async,
    send_test_email,
    send_template_test_email,
)
from app_config import invalidate_config_cache
from auth_email_hook import (
    HookVerificationError,
    get_hook_secret,
    handle_send_email,
    parse_payload,
    verify_signature,
)
from canned_responses import classify as classify_canned, respond as respond_canned
from partnership import finalize_partnership, PartnershipError
from transactional_email import send_transactional_email

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))
FRONTEND_URLS = os.getenv(
    "FRONTEND_URLS",
    "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://localhost:5176,http://127.0.0.1:5173"
).split(",")


class ChatRequest(BaseModel):
    # NOTE: user_id is intentionally NOT read from the body anymore — it's
    # derived from the verified bearer token (see auth.get_current_user_id).
    # The field is kept optional for backward-compat with older clients that
    # still send it, but the value is ignored server-side.
    user_id: Optional[str] = None
    message: str
    stage: Optional[str] = "general"
    context: Optional[dict] = None
    conversation_id: Optional[str] = None

    @validator('message')
    def message_not_empty(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError("message cannot be empty")
        return v


class ConversationMessage(BaseModel):
    role: str
    content: str
    timestamp: str


class ChatResponse(BaseModel):
    reply: str
    # Optional second bubble Cheri sometimes sends after the main reply (a
    # gentle invitation, a check-in, or a quick add-on). Rendered as a
    # separate message in the chat with a small typing delay. None when
    # Cheri's answer stood on its own.
    follow_up: Optional[str] = None
    conversation_id: str
    stage: str
    timestamp: str
    # Membership-aware usage info so the UI can render "N messages left
    # this period" and upsell to upgrade when the user gets close to the cap.
    tier: str
    monthly_used: int
    monthly_limit: Optional[int] = None  # None = unlimited
    period_start: Optional[str] = None   # ISO date the quota window opened
    period_end: Optional[str] = None     # ISO8601, None on calendar-month fallback
    # Deprecated: same values as monthly_used / monthly_limit. Kept so the
    # current frontend keeps rendering during the migration — remove once
    # membership.ts reads the monthly_* fields.
    daily_used: int
    daily_limit: Optional[int] = None
    # NOTE: we intentionally do NOT echo the user profile (email, phone, KYC
    # URLs, bio, etc.) on every reply. The frontend already has that.


def _split_reply_and_followup(raw: str) -> tuple[str, Optional[str]]:
    """
    Parse Cheri's two-bubble reply. The /chat handler asks OpenAI for JSON via
    response_format=json_object, so the FIRST path tries json.loads — that's
    the deterministic one. We also keep a `---`-separator fallback for any
    legacy/non-JSON code path and a final "use the whole text" guard so we
    NEVER show the user an empty bubble.

    Returns (main_reply, follow_up_or_None). Drops follow_up if it's empty,
    a duplicate of the main reply, or absurdly long (>250 chars).
    """
    text = (raw or "").strip()
    if not text:
        return "", None

    # --- Path A: structured JSON (the primary path) -------------------------
    import json
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            main = str(obj.get("reply") or "").strip()
            follow = str(obj.get("follow_up") or "").strip()
            if main:
                if not follow or follow == main or len(follow) > 250:
                    return main, None
                return main, follow
    except (ValueError, TypeError):
        pass  # not JSON — fall through

    # --- Path B: `---` / `***` / `===` separator line -----------------------
    import re
    parts = re.split(r"\n[ \t]*[-*=]{3,}[ \t]*\n", text, maxsplit=1)
    if len(parts) == 2:
        main = parts[0].strip()
        follow = parts[1].strip()
        if main and follow and follow != main and len(follow) <= 250:
            return main, follow
        if main:
            return main, None

    # --- Path C: whole thing as one bubble (model ignored every hint) -----
    return text, None


class ConversationHistoryResponse(BaseModel):
    user_id: str
    conversation_id: str
    messages: List[ConversationMessage]
    stage: str
    created_at: str


async def fetch_user_profile(user_id: str) -> Optional[dict]:
    """
    Look up a user_profiles row by the auth UID stored in the `user_id` column.
    Returns None if not found — chat still proceeds with a generic prompt.
    """
    try:
        rows = await supabase.select(
            "user_profiles",
            eq={"user_id": user_id},
            limit=1,
        )
        return rows[0] if rows else None
    except Exception as e:
        logger.warning(f"Profile lookup failed for {user_id}: {e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("CheriPic AI Backend starting...")
    logger.info(f"Allowed origins: {FRONTEND_URLS}")
    # Pass the Stripe price → tier mapping to the billing module so the
    # webhook handler knows which tier to grant for each price_id.
    billing._register_price_to_tier_mapping(STRIPE_PRICE_TO_TIER)
    logger.info("Stripe price→tier map loaded (%d entries)", len(STRIPE_PRICE_TO_TIER))
    # Warm the limits cache and log what's actually in force, so a missing
    # table or a bad row in cheri_ai_tier_limits is obvious in the deploy log
    # rather than surfacing later as a surprising quota.
    try:
        logger.info("Cheri AI monthly limits: %s", describe_limits(await get_monthly_limits()))
    except Exception as e:
        logger.warning("Could not load tier limits at startup (%r); using env/defaults", e)

    # ---- In-process scheduler for the daily match-nudge email --------------
    # Railway hobby plans don't ship a first-class cron for single-container
    # apps. Since the FastAPI process is always-on anyway, APScheduler ticks
    # cheaply alongside it.
    #
    # Both the master on/off and the crontab live in public.app_config
    # (daily_email_cron_enabled / daily_email_cron_schedule). A supervisor
    # tick runs every 15 min and reconciles the actual APScheduler job
    # against those values — arming it, disarming it, or rewriting its
    # trigger as the admin changes them in the panel. No Railway redeploy
    # required.
    #
    # Why 15 min, not 60s: the daily cron fires ONCE per day. The admin
    # touches the schedule maybe once a month. 15 min drops the ambient
    # REST poll ~15× (96 req/day vs 1,440 req/day) with no meaningful
    # user-facing cost — admins who want an instant sanity check can hit
    # "Trigger the daily nudge cron now" in Email Campaigns.
    app.state.scheduler = None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        # UTC-anchored so the daily job doesn't drift on DST-observing hosts.
        scheduler = AsyncIOScheduler(timezone="UTC")

        async def _reconcile_daily_nudge() -> None:
            try:
                enabled = await daily_email_enabled_async()
                wanted = (await daily_email_crontab_async()) or "30 8 * * *"
                job = scheduler.get_job("daily_match_nudge")
                if not enabled:
                    if job:
                        scheduler.remove_job("daily_match_nudge")
                        logger.info("Daily email cron DISARMED (admin toggle off).")
                    return
                # enabled — decide add vs. reschedule vs. leave alone
                current = getattr(job, "_cheripic_cron", None) if job else None
                if job is None:
                    scheduler.add_job(
                        run_daily_email_cron,
                        CronTrigger.from_crontab(wanted, timezone="UTC"),
                        id="daily_match_nudge",
                        max_instances=1,
                        coalesce=True,
                        misfire_grace_time=3600,
                    )
                    scheduler.get_job("daily_match_nudge")._cheripic_cron = wanted
                    logger.info("Daily email cron ARMED (UTC crontab: '%s').", wanted)
                elif current != wanted:
                    scheduler.reschedule_job(
                        "daily_match_nudge",
                        trigger=CronTrigger.from_crontab(wanted, timezone="UTC"),
                    )
                    scheduler.get_job("daily_match_nudge")._cheripic_cron = wanted
                    logger.info("Daily email cron RESCHEDULED (UTC crontab: '%s').", wanted)
            except Exception as e:
                logger.error("Cron supervisor tick failed: %r", e, exc_info=True)

        # First reconciliation before the loop starts, so a healthy DB row
        # is honored immediately at boot instead of waiting up to 60s.
        await _reconcile_daily_nudge()

        scheduler.add_job(
            _reconcile_daily_nudge,
            IntervalTrigger(minutes=15),
            id="daily_nudge_supervisor",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info("Cron supervisor running (15-min reconcile against app_config).")
    except Exception as e:
        logger.error("Could not start email scheduler: %r", e, exc_info=True)

    yield
    if getattr(app.state, "scheduler", None):
        app.state.scheduler.shutdown(wait=False)
        logger.info("Daily email scheduler stopped.")
    logger.info("CheriPic AI Backend shutting down...")


app = FastAPI(
    title="CheriPic AI Backend",
    description="AI-powered relationship coach and matching support for CheriPic",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[url.strip() for url in FRONTEND_URLS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "CheriPic AI Backend",
        "status": "running",
        "version": "1.0.0"
    }


@app.get("/health", tags=["Health"])
async def health():
    return {
        "status": "ok",
        "service": "CheriPic AI Backend",
        "timestamp": datetime.now().isoformat()
    }


@app.post("/admin/run-daily-email-cron", tags=["Admin"])
async def run_daily_email_now(
    dry_run: bool = False,
    admin_user_id: str = Depends(get_current_admin_id),
):
    """
    Admin-only manual trigger for the match-nudge cron. Useful for verifying
    the eligibility query + Resend config from the admin panel without
    waiting for the scheduled 08:30 UTC run. `dry_run=true` returns who
    WOULD be emailed and sends nothing.
    """
    logger.info("[cron-manual] admin=%s dry_run=%s", admin_user_id, dry_run)
    return await run_daily_email_cron(dry_run=dry_run)


class SendTestEmailRequest(BaseModel):
    to_address: str

    @validator("to_address")
    def _valid_email(cls, v: str) -> str:
        v = (v or "").strip()
        if "@" not in v or len(v) < 3:
            raise ValueError("to_address must be a valid email address")
        return v


@app.post("/admin/send-test-email", tags=["Admin"])
async def admin_send_test_email(
    req: SendTestEmailRequest,
    admin_user_id: str = Depends(get_current_admin_id),
):
    """
    Fire a single canned test email through Resend. Does NOT touch
    sent_emails or notifications — safe to repeat. Used by the admin panel's
    Email Campaigns page.
    """
    logger.info("[test-email] admin=%s to=%s", admin_user_id, req.to_address)
    # Cache-bust so an api key that was just rotated in the admin panel is
    # picked up on the very next click, not after the 60s TTL.
    invalidate_config_cache()
    return await send_test_email(req.to_address)


class SendTestTemplateRequest(BaseModel):
    to_address: str
    subject: str
    html_body: str
    text_body: Optional[str] = ""

    @validator("to_address")
    def _valid_email(cls, v: str) -> str:
        v = (v or "").strip()
        if "@" not in v or len(v) < 3:
            raise ValueError("to_address must be a valid email address")
        return v

    @validator("subject", "html_body")
    def _not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("subject and html_body are required")
        return v


@app.post("/admin/send-test-template", tags=["Admin"])
async def admin_send_test_template(
    req: SendTestTemplateRequest,
    admin_user_id: str = Depends(get_current_admin_id),
):
    """
    Fire a test email rendered from the RAW subject/html/text in the body
    — bypasses the saved template row, so an admin can preview an unsaved
    draft from the Email Templates editor without committing it first.
    Does NOT touch sent_emails or notifications.
    """
    logger.info("[test-template] admin=%s to=%s", admin_user_id, req.to_address)
    invalidate_config_cache()
    return await send_template_test_email(
        to_address=req.to_address,
        subject_template=req.subject,
        html_template=req.html_body,
        text_template=req.text_body or "",
    )


class FinalizePartnershipRequest(BaseModel):
    match_id: str

    @validator("match_id")
    def _valid_uuid(cls, v: str) -> str:
        if not v or len(v) < 8:
            raise ValueError("match_id is required")
        return v


class SelfTriggerEmailRequest(BaseModel):
    template_slug: str
    extra_vars: Optional[dict] = None
    dedup_window_hours: int = 1


@app.post("/emails/trigger-self", tags=["Email"])
async def trigger_self_email(
    req: SelfTriggerEmailRequest,
    caller_user_id: str = Depends(get_current_user_id),
):
    """
    Fire a transactional email TO THE AUTHED USER themselves. For
    events driven by user action — welcome-on-registration,
    account-deletion-request, etc. The user_id is always the caller,
    which prevents spam vectors.

    template_slug must be one of the 18 in email_templates. extra_vars
    optional (most user-triggered emails don't need them).
    """
    logger.info(
        "[trigger-self-email] user=%s template=%s",
        caller_user_id, req.template_slug,
    )
    ok = await send_transactional_email(
        user_id=caller_user_id,
        template_slug=req.template_slug,
        extra_vars=req.extra_vars,
        dedup_window_hours=req.dedup_window_hours,
    )
    return {"ok": ok, "template": req.template_slug}


class AdminTriggerEmailRequest(BaseModel):
    user_id: str
    template_slug: str
    extra_vars: Optional[dict] = None
    dedup_window_hours: int = 1


@app.post("/admin/trigger-email", tags=["Admin"])
async def admin_trigger_email(
    req: AdminTriggerEmailRequest,
    admin_user_id: str = Depends(get_current_admin_id),
):
    """
    Admin-only: fire any of the 18 transactional templates for any
    user_id. Used by:
      • KYC approval/rejection UI       → verify_approved / verify_needs_attention
      • Admin verification revoke       → verify_revoked
      • Suspend / reactivate flows      → account_suspended / account_reactivated
      • Support email-change action     → email_changed (to new address)
      • Support account-deletion action → account_deleted
      • Stripe webhook is separate — that runs server-to-server (no
        admin JWT needed) via send_transactional_email directly.
    """
    logger.info(
        "[admin-trigger-email] admin=%s user=%s template=%s",
        admin_user_id, req.user_id, req.template_slug,
    )
    ok = await send_transactional_email(
        user_id=req.user_id,
        template_slug=req.template_slug,
        extra_vars=req.extra_vars,
        dedup_window_hours=req.dedup_window_hours,
    )
    return {"ok": ok, "template": req.template_slug, "user_id": req.user_id}


class NotifyInterestReceivedRequest(BaseModel):
    """Body for /matching/notify-interest-received.

    The sender_id is derived from the JWT — the request only names the
    receiver, so a malicious client can't spoof interest coming from
    someone else. The server verifies a pending match_requests row
    exists (sender=caller, receiver=req.receiver_user_id) before
    inserting the notification / sending the email — that prevents
    the endpoint from being used as a "notify anyone with anything"
    spam relay.
    """
    receiver_user_id: str


@app.post("/matching/notify-interest-received", tags=["Matching"])
async def matching_notify_interest_received(
    req: NotifyInterestReceivedRequest,
    caller_user_id: str = Depends(get_current_user_id),
):
    """
    Fire-and-forget from the frontend after `sendMatchRequestSimple`.
    Does two things:
      1. Insert a `notifications` row of type 'interest_received' for
         the receiver so the in-app bell shows a red dot immediately.
      2. Send the `interest_received` transactional email to the
         receiver (dedup 2h, so multiple senders in a short burst
         yield one email — reduces cost + avoids inbox spam).

    Never raises to the caller — a downstream failure here must NOT
    fail the interest send. All errors are logged and swallowed;
    response is {"ok": true} unconditionally so the client's
    fire-and-forget contract holds.
    """
    logger.info(
        "[interest-received] sender=%s receiver=%s",
        caller_user_id, req.receiver_user_id,
    )
    try:
        # Verify the match_requests row actually exists. Without this
        # gate, any authed user could POST arbitrary receiver_user_id
        # values and cause noise notifications / emails.
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/match_requests",
                params={
                    "select": "id",
                    "sender_id": f"eq.{caller_user_id}",
                    "receiver_id": f"eq.{req.receiver_user_id}",
                    "status": "eq.pending",
                    "limit": "1",
                },
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                },
            )
            if resp.status_code >= 300 or not resp.json():
                logger.warning(
                    "[interest-received] no pending row for sender=%s → receiver=%s (status=%s), skipping notify",
                    caller_user_id, req.receiver_user_id, resp.status_code,
                )
                return {"ok": True, "skipped": "no_pending_row"}

            # Insert the in-app notification (service key bypasses RLS).
            # Best-effort — a failure to insert doesn't block the email.
            notif_resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/notifications",
                json={
                    "user_id": req.receiver_user_id,
                    "type": "interest_received",
                    "title": "Someone is interested in you",
                    "body": "Open your requests to see who — and decide whether to accept.",
                    # related_user_id lets the partnership-finalize sweep
                    # (partnership.py step 7) identify and delete this
                    # notification when the SENDER later partners with
                    # someone else. Without this, third parties would
                    # still see "User A is interested in you" after A
                    # partnered up. Same convention as partner_dropped.
                    "related_user_id": caller_user_id,
                },
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
            )
            if notif_resp.status_code >= 300:
                logger.warning(
                    "[interest-received] notification insert failed [%s]: %s",
                    notif_resp.status_code, notif_resp.text[:200],
                )

        # Send the email. dedup=2h so 5 senders in 90 min → 1 email.
        # send_transactional_email never raises; it returns False on
        # any skip/failure and logs internally.
        await send_transactional_email(
            user_id=req.receiver_user_id,
            template_slug="interest_received",
            dedup_window_hours=2,
        )
    except Exception as e:
        # Log but never surface — the fire-and-forget contract is
        # "the interest send succeeded, this side-channel is bonus".
        logger.error("[interest-received] notify failed: %r", e, exc_info=True)

    return {"ok": True}


class NotifyPartnerProposalRequest(BaseModel):
    """Body for /matching/notify-partner-proposal.

    The sender_id is derived from the JWT. Server verifies caller is
    one of the participants in match_id before writing the
    notification to the OTHER participant — prevents anyone from
    forging "make it official" notifications for someone they aren't
    matched with.
    """
    match_id: str


@app.post("/matching/notify-partner-proposal", tags=["Matching"])
async def matching_notify_partner_proposal(
    req: NotifyPartnerProposalRequest,
    caller_user_id: str = Depends(get_current_user_id),
):
    """
    Fire-and-forget from acceptPartnership() when the caller is the
    FIRST side to tap "Make it Official". Notifies the OTHER
    participant so they see the proposal without needing to be
    actively viewing the match.

    Never raises — a downstream failure never blocks the acceptance
    that already succeeded client-side. Uses service key so RLS on
    notifications can't silently drop the cross-user insert (which is
    what was happening from the client's own session).
    """
    logger.info("[partner-proposal] match=%s caller=%s", req.match_id, caller_user_id)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Verify caller is a participant in the match.
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/matches",
                params={
                    "select": "user_a_id,user_b_id,is_active,partnered_at",
                    "id": f"eq.{req.match_id}",
                    "limit": "1",
                },
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                },
            )
            rows = resp.json() if resp.status_code < 300 else []
            if not rows:
                return {"ok": True, "skipped": "match_not_found"}
            m = rows[0]
            if caller_user_id not in (m.get("user_a_id"), m.get("user_b_id")):
                return {"ok": True, "skipped": "not_a_participant"}
            other_id = m["user_b_id"] if m["user_a_id"] == caller_user_id else m["user_a_id"]

            # Dedup — don't spam if the caller re-taps.
            existing = await client.get(
                f"{SUPABASE_URL}/rest/v1/notifications",
                params={
                    "select": "id",
                    "user_id": f"eq.{other_id}",
                    "type": "eq.partner_proposal",
                    "related_user_id": f"eq.{caller_user_id}",
                    "limit": "1",
                },
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                },
            )
            if existing.status_code < 300 and existing.json():
                return {"ok": True, "skipped": "duplicate"}

            await client.post(
                f"{SUPABASE_URL}/rest/v1/notifications",
                json={
                    "user_id": other_id,
                    "type": "partner_proposal",
                    "title": "Someone wants to make it official 💞",
                    "body": "Open your connection to review and accept.",
                    "related_user_id": caller_user_id,
                    "related_id": req.match_id,
                },
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
            )
    except Exception as e:
        logger.error("[partner-proposal] notify failed: %r", e, exc_info=True)
    return {"ok": True}


class NotifyUnmatchRequest(BaseModel):
    """Body for /matching/notify-unmatch. Caller is the user who
    initiated the unmatch (from JWT). Notifies the OTHER user (and
    optionally the caller too — see was_partnered)."""
    other_user_id: str
    was_partnered: bool = False


@app.post("/matching/notify-unmatch", tags=["Matching"])
async def matching_notify_unmatch(
    req: NotifyUnmatchRequest,
    caller_user_id: str = Depends(get_current_user_id),
):
    """
    Fire-and-forget from unmatchUser() after the DB updates land.
    Inserts one notification for the other user (always), plus a
    self-notification for the initiator when this was a partnership
    (so their own inbox reflects the ended relationship).

    Uses service key so cross-user inserts land regardless of RLS.
    Never raises to the caller — the unmatch itself already
    succeeded client-side.
    """
    logger.info(
        "[unmatch-notify] caller=%s other=%s partnered=%s",
        caller_user_id, req.other_user_id, req.was_partnered,
    )
    try:
        # Pull nick_names for personalized copy — same privacy rule
        # as unmatchUser (nick_name only, never full_name).
        async with httpx.AsyncClient(timeout=15.0) as client:
            profs = await client.get(
                f"{SUPABASE_URL}/rest/v1/public_profiles",
                params={
                    "select": "user_id,nick_name",
                    "user_id": f"in.({caller_user_id},{req.other_user_id})",
                },
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                },
            )
            name_by_id: dict[str, str] = {}
            if profs.status_code < 300:
                for p in profs.json():
                    name_by_id[p["user_id"]] = p.get("nick_name") or "Someone"
            caller_label = name_by_id.get(caller_user_id, "Someone")
            other_label = name_by_id.get(req.other_user_id, "Someone")

            notifs: list[dict[str, Any]] = [
                {
                    "user_id": req.other_user_id,
                    "type": "unmatched",
                    "title": f"You and {caller_label} ended your relationship",
                    "body": "You're now open to new opportunities when you're ready.",
                    "related_user_id": caller_user_id,
                },
            ]
            if req.was_partnered:
                notifs.append({
                    "user_id": caller_user_id,
                    "type": "unmatched",
                    "title": f"You and {other_label} ended your relationship",
                    "body": "You're now open to new opportunities when you're ready.",
                    "related_user_id": req.other_user_id,
                })

            await client.post(
                f"{SUPABASE_URL}/rest/v1/notifications",
                json=notifs,
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
            )
    except Exception as e:
        logger.error("[unmatch-notify] failed: %r", e, exc_info=True)
    return {"ok": True}


@app.post("/matching/finalize-partnership", tags=["Matching"])
async def matching_finalize_partnership(
    req: FinalizePartnershipRequest,
    user_id: str = Depends(get_current_user_id),
):
    """
    Explicit partnership finalizer — no hidden DB triggers, no OF-clause
    firing bugs. The client calls this after detecting both users have
    accepted (partner_accepted_by_a AND partner_accepted_by_b both true).

    Runs the full cleanup in one place (partnership.py). See that
    module's docstring for the exact rules.

    Auth: caller must be a participant in the match — enforced inside
    finalize_partnership() by comparing against both user_a_id/user_b_id.
    Idempotent — if the match is already partnered, returns immediately
    without re-running the sweep.
    """
    logger.info("[partnership] finalize match=%s by user=%s", req.match_id, user_id)
    try:
        return await finalize_partnership(req.match_id, user_id)
    except PartnershipError as e:
        code = str(e)
        status_code = 400
        if code == "match_not_found":
            status_code = 404
        elif code == "not_a_participant":
            status_code = 403
        elif code == "both_must_accept_first":
            status_code = 409  # conflict — not yet ready
        raise HTTPException(status_code=status_code, detail=code)
    except Exception as e:
        logger.error("[partnership] finalize failed: %r", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"finalize_failed: {e}")


@app.post("/auth/send-email-hook", tags=["Auth"])
async def supabase_send_email_hook(request: Request):
    """
    Supabase Auth "Send Email Hook" receiver.

    Configure the URL + shared secret in Supabase Dashboard →
    Authentication → Hooks → Send Email Hook. Every outbound auth email
    (OTP, magic link, signup confirmation, recovery, invite, etc.) POSTs
    here instead of being sent by Supabase directly — we render our
    admin-editable template from public.email_templates and deliver it
    through Resend.

    Returns 200 on successful delivery. Anything else tells Supabase the
    email failed so its auth flow surfaces the error to the caller.
    """
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}

    secret = await get_hook_secret()
    if not secret:
        logger.error("[auth-hook] send_email_hook_secret is not configured — refusing request")
        raise HTTPException(status_code=500, detail="hook secret not configured")

    try:
        verify_signature(headers, body, secret)
    except HookVerificationError as e:
        logger.warning("[auth-hook] signature verification failed: %s", e)
        raise HTTPException(status_code=401, detail=f"unauthorized: {e}")

    try:
        payload = parse_payload(body)
    except ValueError as e:
        logger.warning("[auth-hook] bad payload: %s", e)
        raise HTTPException(status_code=400, detail=str(e))

    try:
        result = await handle_send_email(payload)
    except Exception as e:
        # Surface as 5xx so Supabase's own retry/error path fires and
        # the user gets a clean auth error rather than a silent no-send.
        logger.error("[auth-hook] delivery failed: %r", e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"delivery failed: {e}")

    return result


@app.post("/admin/simulate-auth-email", tags=["Admin"])
async def admin_simulate_auth_email(
    to_address: str,
    action: str = "magiclink",
    admin_user_id: str = Depends(get_current_admin_id),
):
    """
    Fires the auth-email hook handler with a synthetic Supabase payload —
    NO real Supabase auth request needed. Lets an admin verify the OTP
    template renders + Resend delivers, end-to-end, from the admin panel
    without triggering a real login flow.
    """
    if "@" not in to_address:
        raise HTTPException(status_code=400, detail="valid to_address required")

    payload = {
        "user": {
            "id": "00000000-0000-0000-0000-000000000000",
            "email": to_address,
            "user_metadata": {"full_name": "Test User"},
        },
        "email_data": {
            "token": "519247",
            "token_hash": "test_token_hash",
            "redirect_to": "",
            "email_action_type": action,
            "site_url": "",
        },
    }
    logger.info("[auth-hook-sim] admin=%s action=%s to=%s", admin_user_id, action, to_address)
    invalidate_config_cache()
    return await handle_send_email(payload)


@app.post("/admin/invalidate-app-config-cache", tags=["Admin"])
async def admin_invalidate_app_config_cache(
    admin_user_id: str = Depends(get_current_admin_id),
):
    """
    Clear the in-process app_config cache so the next read hits the DB.
    Called by the admin panel right after it writes a config value via
    admin_set_app_config, so the change takes effect on the very next
    backend request instead of waiting up to 60s for the TTL.

    The write itself goes DIRECT from the browser to Supabase (through the
    admin's own JWT) because the RPC's is_admin() check depends on
    auth.uid() — routing the write via the service-role backend would
    bypass user identity and 42501.
    """
    logger.info("[app-config-cache] invalidated by admin=%s", admin_user_id)
    invalidate_config_cache()
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
async def chat(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
):
    # user_id comes from the VERIFIED token — never from the request body.
    logger.info(f"Chat | User: {user_id} | Stage: {req.stage} | Length: {len(req.message)}")

    # ---- Cheap intent router ------------------------------------------------
    # If the message is an obvious pleasantry (hi / thanks / how are you /
    # who are you / etc.) we short-circuit here with a canned Cheri-voice
    # reply. Zero OpenAI tokens, zero quota consumed, still saved to
    # chat_history so the conversation flows naturally, memory distillation
    # is deliberately SKIPPED because these turns carry no signal.
    canned_category = classify_canned(req.message)
    if canned_category:
        logger.info(f"[canned] category={canned_category} user={user_id}")
        user_data = await fetch_user_profile(user_id) or {}
        display_name = user_data.get("nick_name") or user_data.get("full_name")
        reply_text, follow_up = respond_canned(canned_category, name=display_name)
        conversation_id = req.conversation_id or f"conv_{user_id}_{int(datetime.now().timestamp())}"

        try:
            await save_conversation_message(user_id, conversation_id, "user", req.message, req.stage)
            await save_conversation_message(user_id, conversation_id, "assistant", reply_text, req.stage)
            if follow_up:
                await save_conversation_message(user_id, conversation_id, "assistant", follow_up, req.stage)
        except Exception as e:
            logger.error(f"[canned chat_history] insert FAILED for user {user_id}: {e}")

        # Tier / period is still surfaced so the frontend usage strip
        # renders the same numbers as it would after an LLM reply. Cheap
        # queries — no OpenAI involvement.
        tier = await billing.get_membership_tier(user_id)
        limits = await get_monthly_limits()
        limit = limits.get(tier, limits["basic"])
        period = await billing.get_quota_period(user_id)
        used = await billing.get_period_usage(user_id, period["period_start"])
        capped = None if math.isinf(limit) else int(limit)

        return ChatResponse(
            reply=reply_text,
            follow_up=follow_up,
            conversation_id=conversation_id,
            stage=req.stage,
            timestamp=datetime.now().isoformat(),
            tier=tier,
            monthly_used=used,
            monthly_limit=capped,
            period_start=period["period_start"],
            period_end=period["period_end"],
            daily_used=used,
            daily_limit=capped,
        )

    # ---- Membership rate limit (BEFORE the LLM call so we don't burn $$) ----
    tier = await billing.get_membership_tier(user_id)
    limits = await get_monthly_limits()
    limit = limits.get(tier, limits["basic"])
    period = await billing.get_quota_period(user_id)
    used = await billing.get_period_usage(user_id, period["period_start"])
    if math.isfinite(limit) and used >= limit:
        resets = (
            "when your plan renews"
            if period["source"] == "billing_cycle"
            else "at the start of next month (UTC)"
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "cheri_ai_monthly_limit_reached",
                "message": (
                    f"You've used all {int(limit)} of your Cheri messages for "
                    f"this period. Upgrade your plan for more — they reset "
                    f"{resets}."
                ),
                "tier": tier,
                "monthly_used": used,
                "monthly_limit": int(limit),
                "period_start": period["period_start"],
                "period_end": period["period_end"],
                # Deprecated aliases — kept so the current frontend keeps
                # rendering while it migrates to the monthly_* fields.
                "daily_used": used,
                "daily_limit": int(limit),
            },
        )

    try:
        # Profile is optional — if missing, prompt builder uses defaults
        user_data = await fetch_user_profile(user_id) or {"id": user_id, "nick_name": "Friend"}
        if user_data.get("nick_name"):
            logger.info(f"Profile loaded: {user_data.get('nick_name')}")

        # Load memory + recent messages first (in parallel), then offers (which
        # Memory + recent messages only — we intentionally do NOT call
        # fetch_offers anymore. Cheri should answer the question on its own
        # merits, not weave a CheriPic upsell into every reply.
        memory, recent = await asyncio.gather(
            load_memory(user_id),
            load_recent_messages(user_id),
        )
        logger.info(
            f"Context: memory={'yes' if memory['summary'] else 'no'}, "
            f"insights={len(memory['insights'])}, recent={len(recent)}"
        )

        prompt_builder = CheriAIPromptBuilder(
            user_data, req.stage, memory=memory, recent_messages=recent
        )
        full_prompt = prompt_builder.build(req.message, req.context)

        logger.info("Calling LLM...")
        # json_mode locks Cheri's reply into {"reply": ..., "follow_up": ...}
        # so the second bubble can't get glued onto the first.
        llm_reply = await send_to_llm(full_prompt, user=user_id, json_mode=True)
        logger.info(f"LLM reply received ({len(llm_reply)} chars)")

        # Split into main bubble + optional follow-up bubble. Cheri is now
        # asked for JSON output via OpenAI's json_mode; the parser falls
        # back to text splitting if anything ever drifts.
        reply_text, follow_up = _split_reply_and_followup(llm_reply)
        logger.info(
            "Parsed reply: main=%d chars, follow_up=%s",
            len(reply_text),
            f"'{follow_up[:60]}...' ({len(follow_up)} chars)" if follow_up else "<none>",
        )

        conversation_id = req.conversation_id or f"conv_{user_id}_{int(datetime.now().timestamp())}"

        try:
            await save_conversation_message(user_id, conversation_id, "user", req.message, req.stage)
            await save_conversation_message(user_id, conversation_id, "assistant", reply_text, req.stage)
            saved_count = 2
            if follow_up:
                await save_conversation_message(user_id, conversation_id, "assistant", follow_up, req.stage)
                saved_count = 3
            logger.info(f"Saved {saved_count} messages to chat_history for conversation {conversation_id}")
        except Exception as e:
            logger.error(
                f"[chat_history] insert FAILED for user {user_id}: {e}. "
                "Check: (a) RLS off on chat_history, "
                "(b) no FK constraint on user_id (drop chat_history_user_id_fkey)."
            )

        # Distill new insights into Cheri's memory AFTER the response is sent.
        # Was `asyncio.create_task(...)` which could be garbage-collected before
        # running AND silently swallowed any unhandled exceptions inside
        # update_memory_async — the exact "user's chat isn't updating my
        # memory" bug. FastAPI's BackgroundTasks holds a hard reference and
        # runs each task to completion after the HTTP response is flushed,
        # so we get reliable execution + any exception surfaces in the logs.
        combined_for_memory = f"{reply_text}\n{follow_up}" if follow_up else reply_text
        background_tasks.add_task(
            update_memory_async, user_id, req.message, combined_for_memory
        )
        logger.info(
            f"[memory] scheduled distill task for user={user_id} "
            f"(background_tasks queue = {len(background_tasks.tasks)})"
        )

        # Bump the period counter only AFTER a successful LLM call so failed
        # requests don't eat into the user's quota.
        new_used = await billing.increment_period_usage(user_id, period["period_start"])

        return ChatResponse(
            reply=reply_text,
            follow_up=follow_up,
            conversation_id=conversation_id,
            stage=req.stage,
            timestamp=datetime.now().isoformat(),
            tier=tier,
            monthly_used=new_used,
            monthly_limit=None if math.isinf(limit) else int(limit),
            period_start=period["period_start"],
            period_end=period["period_end"],
            daily_used=new_used,
            daily_limit=None if math.isinf(limit) else int(limit),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chat processing failed: {str(e)}"
        )


# ============================================================================
# Cheri memory — seed from user profile
# ============================================================================
# Called from the frontend right after Registration submit (and after any
# Snapshot Bio edit) so Cheri's user-memory row is populated BEFORE the very
# first chat. Without this, Cheri starts every user cold and has to
# re-discover who they are through conversation. Runs one LLM call to distill
# `user_profiles` + bio JSON into an initial `summary` + `insights` set.


class SeedMemoryRequest(BaseModel):
    user_id: str

    @validator('user_id')
    def not_empty(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError("must not be empty")
        return v


class SeedMemoryResponse(BaseModel):
    ok: bool
    summary: str
    insights_count: int


@app.post("/memory/seed", response_model=SeedMemoryResponse, tags=["Memory"])
async def seed_memory(
    req: SeedMemoryRequest,
    caller_user_id: str = Depends(get_current_user_id),
):
    """
    Distill the caller's profile + bio fields into an initial Cheri memory
    row so the first chat turn opens with real context.

    Guarded by require_self — a user can only seed THEIR OWN memory. This
    matches the pattern used by other user-scoped endpoints. Never raises
    (seed_memory_from_profile swallows errors and returns {}), so the
    frontend can call it fire-and-forget without breaking registration.
    """
    require_self(caller_user_id, req.user_id)
    result = await seed_memory_from_profile(req.user_id)
    if not result:
        # No profile row yet, or LLM/JSON parse failure. Signal to the
        # caller so it can retry later (e.g. after profile insert commits).
        return SeedMemoryResponse(ok=False, summary="", insights_count=0)
    return SeedMemoryResponse(
        ok=True,
        summary=result.get("summary", ""),
        insights_count=len(result.get("insights", []) or []),
    )


# Admin-only counterpart: seed the memory of ANY user. Same distillation
# path — we just skip the require_self check because the caller has proven
# they hold the app_metadata.is_admin claim on their JWT. Used from the
# admin panel's "Reseed from profile" button in the AI Memory viewer,
# typically to fix rows that were left empty by a first-time seed run
# with the AI backend down or OPENAI_API_KEY unset.
@app.post("/memory/admin-reseed", response_model=SeedMemoryResponse, tags=["Memory"])
async def admin_reseed_memory(
    req: SeedMemoryRequest,
    admin_user_id: str = Depends(get_current_admin_id),
):
    logger.info(f"[memory-admin-reseed] admin={admin_user_id} target={req.user_id}")
    result = await seed_memory_from_profile(req.user_id)
    if not result:
        return SeedMemoryResponse(ok=False, summary="", insights_count=0)
    return SeedMemoryResponse(
        ok=True,
        summary=result.get("summary", ""),
        insights_count=len(result.get("insights", []) or []),
    )


class VerifyFaceRequest(BaseModel):
    user_id: str
    face_image_url: str
    id_image_url: str
    threshold: Optional[float] = 70.0

    @validator('user_id', 'face_image_url', 'id_image_url')
    def not_empty(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError("must not be empty")
        return v


class VerifyFaceResponse(BaseModel):
    similarity: float
    threshold: float
    matched: bool
    verification_final_status: str
    verification_status: str
    model: str
    detector_backend: str


@app.post("/kyc/verify-face", response_model=VerifyFaceResponse, tags=["KYC"])
async def verify_face(req: VerifyFaceRequest, user_id: str = Depends(get_current_user_id)):
    """
    Compare a user's live selfie against the face on their ID-proof image
    and, if the similarity is >= threshold, mark them verified.

    The score (0-100) is always returned so the frontend can show a "try
    again" message on sub-threshold attempts without us having to encode
    pass/fail in HTTP status codes.
    """
    logger.info(
        "Face verify | user=%s threshold=%.1f", user_id, req.threshold
    )

    try:
        result = await compare_faces(
            face_image_url=req.face_image_url,
            id_image_url=req.id_image_url,
            threshold=req.threshold,
        )
    except FaceServiceUnavailable as e:
        # ML stack from requirements.face.txt isn't installed on this host
        # (common on cPanel shared / very small VPS tiers). Return a clean
        # 503 so the frontend can show a "verification temporarily disabled"
        # toast instead of an opaque 500.
        logger.warning("Face service unavailable on this host: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Face verification is temporarily unavailable on this server. "
                "Try again later."
            ),
        )
    except ValueError as e:
        # insightface couldn't detect a face in one of the images.
        logger.warning("Face detection failed for %s: %s", user_id, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Couldn't detect a face in one of the images. Make sure your "
                "face is clearly visible (no glare, good lighting) and the ID "
                "photo shows a clear face."
            ),
        )
    except Exception as e:
        logger.error("Face verify error for %s: %s", user_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Face verification failed: {e}",
        )

    # Admin-approval workflow:
    #   - Always record the auto face-match score (admin uses it as a hint).
    #   - Always move the user into 'pending_review' so they show up on the
    #     admin queue.
    #   - NEVER auto-flip verification_final_status — only the admin can do
    #     that (manual UPDATE in Supabase, see KYC_ADMIN_REVIEW.md).
    # The `matched` flag in the response just tells the frontend whether the
    # similarity crossed the hint threshold; it's informational only now.
    update_payload = {
        "kyc_face_match_score": result.similarity,
        "verification_status": "pending_review",
        "verification_submitted_at": datetime.now().isoformat(),
    }

    try:
        await supabase.update(
            "user_profiles",
            payload=update_payload,
            eq={"user_id": user_id},
        )
    except Exception as e:
        # Don't fail the whole call — the score still came back. Log loudly.
        logger.error("Failed to persist KYC result for %s: %s", user_id, e)

    # The frontend uses verification_final_status to render the green badge —
    # which only changes when an admin manually approves. The auto-match
    # never touches it, so we always report 'not_verified' here.
    return VerifyFaceResponse(
        similarity=result.similarity,
        threshold=result.threshold,
        matched=result.matched,
        verification_final_status="not_verified",
        verification_status="pending_review",
        model=result.model,
        detector_backend=result.detector_backend,
    )


class CreateCheckoutRequest(BaseModel):
    user_id: str
    price_id: str
    success_url: str
    cancel_url: str

    @validator('user_id', 'price_id', 'success_url', 'cancel_url')
    def _not_empty(cls, v):
        if not v or len(v.strip()) == 0:
            raise ValueError("must not be empty")
        return v


class CreateCheckoutResponse(BaseModel):
    url: str


@app.post("/billing/create-checkout-session", response_model=CreateCheckoutResponse, tags=["Billing"])
async def create_checkout_session(
    req: CreateCheckoutRequest, user_id: str = Depends(get_current_user_id)
):
    """Returns the Stripe-hosted checkout URL the frontend should redirect to."""
    try:
        url = await billing.create_checkout_session(
            user_id=user_id,
            price_id=req.price_id,
            success_url=req.success_url,
            cancel_url=req.cancel_url,
        )
        return CreateCheckoutResponse(url=url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Stripe checkout error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class PortalSessionRequest(BaseModel):
    user_id: str
    return_url: str


class PortalSessionResponse(BaseModel):
    url: str


@app.post(
    "/billing/portal-session",
    response_model=PortalSessionResponse,
    tags=["Billing"],
)
async def create_billing_portal_session(
    req: PortalSessionRequest, user_id: str = Depends(get_current_user_id)
):
    """
    Returns a Stripe Customer Portal URL for cancelling / changing payment
    method / viewing invoices. Only works once the user has at least one
    successful checkout (= has a stripe_customer_id on user_memberships).
    """
    try:
        url = await billing.create_portal_session(
            user_id=user_id,
            return_url=req.return_url,
        )
        return PortalSessionResponse(url=url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Stripe portal error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/billing/webhook", tags=["Billing"])
async def stripe_webhook(request: Request):
    """
    Stripe webhook receiver. Configure the endpoint URL in your Stripe
    dashboard and put the signing secret in .env as STRIPE_WEBHOOK_SECRET.
    """
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        summary = await billing.handle_event(payload, signature)
        return summary
    except Exception as e:
        logger.error("Stripe webhook error: %s", e, exc_info=True)
        # Return 400 so Stripe retries — but signature errors should NOT
        # retry indefinitely (Stripe stops after a few attempts).
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/billing/me/{user_id}", tags=["Billing"])
async def get_my_membership(user_id: str, authed: str = Depends(get_current_user_id)):
    """
    Returns the user's active membership tier + this period's Cheri usage.
    Wrapped in defensive try/except so an upstream Supabase outage (missing
    table, expired key, transient network) returns a safe default instead
    of a 500 — the frontend's Home + Profile both hit this on every load
    and a hard 500 here blanks them out.
    """
    require_self(user_id, authed)
    try:
        tier = await billing.get_membership_tier(user_id)
    except Exception as e:
        logger.warning("get_membership_tier failed for %s: %r", user_id, e)
        tier = "basic"

    try:
        period = await billing.get_quota_period(user_id)
        used = await billing.get_period_usage(user_id, period["period_start"])
    except Exception as e:
        logger.warning("period usage lookup failed for %s: %r", user_id, e)
        period = {"period_start": None, "period_end": None, "source": "calendar_month"}
        used = 0

    limits = await get_monthly_limits()
    limit = limits.get(tier, limits["basic"])
    capped = None if math.isinf(limit) else int(limit)
    return {
        "tier": tier,
        "cheri_ai": {
            "monthly_used": used,
            "monthly_limit": capped,
            "period_start": period["period_start"],
            "period_end": period["period_end"],
            "resets_on": period["source"],  # billing_cycle | calendar_month
            # Deprecated aliases — see ChatResponse.
            "daily_used": used,
            "daily_limit": capped,
        },
    }


@app.get("/cheri-thread/{user_id}", tags=["Conversations"])
async def get_cheri_thread(
    user_id: str, limit: int = 50, authed: str = Depends(get_current_user_id)
):
    """
    Returns the user's last N chat messages with Cheri, oldest first.
    Treats all conversations as one rolling thread (the UX is iMessage-style,
    not multi-thread). Each item: { role, content, created_at, conversation_id }.
    """
    require_self(user_id, authed)
    try:
        rows = await supabase.select(
            "chat_history",
            columns="role,content,created_at,conversation_id",
            eq={"user_id": user_id},
            order="created_at.desc",
            limit=limit,
        )
        rows = list(reversed(rows))  # chronological
        return {
            "user_id": user_id,
            "count": len(rows),
            "messages": rows,
        }
    except Exception as e:
        logger.error(f"Error loading Cheri thread: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load thread: {str(e)}",
        )


@app.get("/conversations/{user_id}/{conversation_id}", response_model=ConversationHistoryResponse, tags=["Conversations"])
async def get_conversation(
    user_id: str, conversation_id: str, authed: str = Depends(get_current_user_id)
):
    require_self(user_id, authed)
    try:
        data = await supabase.select(
            "chat_history",
            eq={"user_id": user_id, "conversation_id": conversation_id},
            order="created_at.asc",
        )

        messages = [
            ConversationMessage(
                role=msg.get("role", ""),
                content=msg.get("content", ""),
                timestamp=msg.get("created_at", "")
            )
            for msg in data
        ]
        stage = next((msg.get("stage") for msg in data if msg.get("stage")), "general")

        return ConversationHistoryResponse(
            user_id=user_id,
            conversation_id=conversation_id,
            messages=messages,
            stage=stage,
            created_at=datetime.now().isoformat()
        )

    except Exception as e:
        logger.error(f"Error retrieving conversation: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve conversation: {str(e)}"
        )


@app.get("/conversations/{user_id}", tags=["Conversations"])
async def list_user_conversations(user_id: str, authed: str = Depends(get_current_user_id)):
    require_self(user_id, authed)
    try:
        data = await supabase.select(
            "chat_history",
            columns="conversation_id,stage,created_at",
            eq={"user_id": user_id},
            order="created_at.desc",
        )

        conversations = {}
        for msg in data:
            conv_id = msg.get("conversation_id")
            if conv_id and conv_id not in conversations:
                conversations[conv_id] = {
                    "conversation_id": conv_id,
                    "stage": msg.get("stage", "general"),
                    "created_at": msg.get("created_at")
                }

        return {
            "user_id": user_id,
            "conversation_count": len(conversations),
            "conversations": list(conversations.values())
        }

    except Exception as e:
        logger.error(f"Error listing conversations: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list conversations: {str(e)}"
        )


async def save_conversation_message(user_id: str, conversation_id: str, role: str, content: str, stage: str):
    await supabase.insert("chat_history", {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "stage": stage,
        "created_at": datetime.now().isoformat(),
    })


if __name__ == '__main__':
    import uvicorn
    logger.info(f"Starting server at {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
