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
from typing import Optional, List
from contextlib import asynccontextmanager
import asyncio

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from dotenv import load_dotenv

load_dotenv()

from auth import get_current_user_id, get_current_admin_id, require_self
from supabase_client import supabase
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
    yield
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


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
async def chat(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
):
    # user_id comes from the VERIFIED token — never from the request body.
    logger.info(f"Chat | User: {user_id} | Stage: {req.stage} | Length: {len(req.message)}")

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
