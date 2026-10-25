"""
readiness.py — server-owned reflection scoring and Connect activation
(handoff H1.13, §5 10A3.1/10A3.2 "final submit calculates result", §13,
acceptance "Identity approval alone cannot activate an incomplete or
below-threshold Connect profile"; audit 00_core H1.13 / H15.A3, B H5.25/H5.28).

POST /assessment/submit      {session_id, answers:[{question_id, option_id}]}
    Recomputes the reflection score from the question bank (red flags come
    from cheri_ai_question_options.red_flag, never from the client), stores
    the attempt record on the session and the member's current score on
    cheri_ai_user_profiles with the service key. Any client-sent score is
    ignored. Returns {score, eligible, red_flags, profile_tier, ...}.

POST /connect/activate
    Recomputes readiness on the server (identity verified, profile complete,
    reflection score >= 70, not in an official pair) and only then sets
    bio.connect_active=true, relationship_focus='single', intros_paused=false.
    Otherwise 409 {code:'not_ready', missing:[...], profile_fields:[...]}.

POST /connect/introductions  {paused: bool}
    Pausing is always allowed. Turning introductions ON applies the same
    readiness checks (an official pair blocks it).

The formula mirrors mobile_app/src/services/cheriAIAssessmentService.ts
(`scoreAssessment` / `latestResponsePerQuestion`): one answer per question
(latest wins), score = max(0, 100 - 14 x red flags), eligible = score >= 70
(inclusive, documents/audit/DECISIONS.md). The profile rule mirrors
profileService.ts `profileStatus` (the three member questions are optional).

Migration 053 (NOT APPLIED) makes these columns/keys server-only for client
roles; until then the app falls back to its client path when these routes
404 (backend not deployed).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ratelimit import rate_limit
from pydantic import BaseModel, Field

from auth import get_current_user_id
from supabase_client import SUPABASE_KEY, SUPABASE_URL
from transactional_email import send_transactional_email

logger = logging.getLogger(__name__)

router = APIRouter()

_REST = f"{SUPABASE_URL}/rest/v1"
_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Prefer": "return=representation",
}

SCORING_VERSION = "redflag14-v1"
RED_FLAG_PENALTY = 14
ELIGIBILITY_THRESHOLD = 70  # inclusive
STORY_MIN_CHARS = 20
_BIO_WRITE_ATTEMPTS = 3


# ------------------------------------------------------------------ pure rules

def dedupe_answers(answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One answer per question; the latest wins. Order = order of the winning answers.

    Each item needs `question_id`; `created_at` (stored rows) decides "latest"
    when present, otherwise list position (request body: later entries win).
    """
    by_q: dict[str, tuple[Any, int, dict[str, Any]]] = {}
    for i, a in enumerate(answers):
        qid = a.get("question_id")
        if not qid:
            continue
        key = (a.get("created_at") or "", i)
        prev = by_q.get(qid)
        if prev is None or (prev[0], prev[1]) <= key:
            by_q[qid] = (key[0], key[1], a)
    return [v[2] for v in sorted(by_q.values(), key=lambda v: (v[0], v[1]))]


def score_from_red_flags(red_flags: int) -> int:
    return max(0, 100 - red_flags * RED_FLAG_PENALTY)


def is_eligible(score: Any) -> bool:
    return isinstance(score, (int, float)) and not isinstance(score, bool) and score >= ELIGIBILITY_THRESHOLD


def profile_tier_for(red_flags: int) -> str:
    if red_flags == 0:
        return "aligned"
    if red_flags == 1:
        return "conscious"
    if red_flags == 2:
        return "developing"
    if red_flags <= 4:
        return "cautionary"
    return "high_risk"


def parse_bio(raw: Any) -> dict[str, Any]:
    """Same as the app's parseBio: non-JSON text counts as an empty profile."""
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _bio_for_write(raw: Any) -> dict[str, Any]:
    """Like SQL public._bio_json: a plain-text legacy bio survives as about_yourself."""
    if isinstance(raw, dict):
        return dict(raw)
    if not raw or not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        v = json.loads(raw)
        return dict(v) if isinstance(v, dict) else {"about_yourself": raw}
    except ValueError:
        return {"about_yourself": raw}


def missing_profile_fields(profile: dict[str, Any] | None) -> list[str]:
    """profileService.ts profileStatus: about >= 20 chars, >= 1 language, gender, intent, >= 1 photo."""
    if not profile:
        return ["about", "languages", "gender", "intent", "photo"]
    bio = parse_bio(profile.get("bio"))
    about = bio.get("about_yourself")
    langs = bio.get("languages")
    gallery = bio.get("gallery_urls")
    missing = []
    if not (isinstance(about, str) and len(about.strip()) >= STORY_MIN_CHARS):
        missing.append("about")
    if not (isinstance(langs, list) and len(langs) >= 1):
        missing.append("languages")
    if not profile.get("gender"):
        missing.append("gender")
    if not bio.get("intent"):
        missing.append("intent")
    if not (isinstance(gallery, list) and len(gallery) >= 1):
        missing.append("photo")
    return missing


# ------------------------------------------------------------------ REST helpers

async def _get(client: httpx.AsyncClient, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    r = await client.get(f"{_REST}/{table}", params=params, headers=_HEADERS)
    if r.status_code >= 300:
        logger.error("[readiness] read %s failed [%s]", table, r.status_code)
        raise HTTPException(status_code=502, detail={"code": "upstream_error", "message": "Please try again."})
    data = r.json()
    return data if isinstance(data, list) else []


def _in(ids: list[str]) -> str:
    return "in.(" + ",".join(f'"{i}"' for i in ids) + ")"


async def compute_readiness(client: httpx.AsyncClient, user_id: str) -> dict[str, Any]:
    profiles = await _get(client, "user_profiles", {
        "select": "user_id,bio,gender,verification_final_status",
        "user_id": f"eq.{user_id}", "limit": "1",
    })
    profile = profiles[0] if profiles else None
    scores = await _get(client, "cheri_ai_user_profiles", {
        "select": "total_score", "user_id": f"eq.{user_id}", "limit": "1",
    })
    score = scores[0].get("total_score") if scores else None
    official = await _get(client, "matches", {
        "select": "id",
        "is_active": "eq.true",
        "partnered_at": "not.is.null",
        "or": f"(user_a_id.eq.{user_id},user_b_id.eq.{user_id})",
        "limit": "1",
    })
    profile_fields = missing_profile_fields(profile)
    missing: list[str] = []
    if not profile or profile.get("verification_final_status") != "verified":
        missing.append("identity")
    if profile_fields:
        missing.append("profile")
    if not is_eligible(score):
        missing.append("reflection")
    if official:
        missing.append("official")
    return {"profile": profile, "score": score, "missing": missing, "profile_fields": profile_fields}


def _not_ready(r: dict[str, Any]) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": "not_ready",
        "missing": r["missing"],
        "profile_fields": r["profile_fields"],
    })


async def merge_bio(client: httpx.AsyncClient, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """bio || patch with optimistic concurrency (PATCH only if bio is unchanged), retried."""
    for _ in range(_BIO_WRITE_ATTEMPTS):
        rows = await _get(client, "user_profiles", {"select": "bio", "user_id": f"eq.{user_id}", "limit": "1"})
        if not rows:
            raise HTTPException(status_code=404, detail={"code": "profile_not_found"})
        raw = rows[0].get("bio")
        nxt = {**_bio_for_write(raw), **patch}
        params = {"user_id": f"eq.{user_id}"}
        params["bio"] = "is.null" if raw is None else f"eq.{raw}"
        r = await client.patch(f"{_REST}/user_profiles", params=params,
                               json={"bio": json.dumps(nxt)}, headers=_HEADERS)
        if r.status_code >= 300:
            logger.error("[readiness] bio write failed [%s]", r.status_code)
            raise HTTPException(status_code=502, detail={"code": "write_failed", "message": "Please try again."})
        if r.json():
            return nxt
    raise HTTPException(status_code=409, detail={"code": "concurrent_update", "message": "Please try again."})


# ------------------------------------------------------------------ models

class AnswerIn(BaseModel):
    question_id: str = Field(..., min_length=1, max_length=64)
    option_id: str = Field(..., min_length=1, max_length=64)


class AssessmentSubmit(BaseModel):
    """Any extra field (e.g. a client `score`) is ignored by pydantic."""
    session_id: str = Field(..., min_length=1, max_length=64)
    answers: list[AnswerIn] = Field(default_factory=list, max_length=60)


class IntroductionsBody(BaseModel):
    paused: bool


# ------------------------------------------------------------------ scoring

async def score_session(client: httpx.AsyncClient, user_id: str, req: AssessmentSubmit) -> dict[str, Any]:
    sessions = await _get(client, "cheri_ai_assessment_sessions", {
        "select": "id,user_id,is_completed", "id": f"eq.{req.session_id}", "limit": "1",
    })
    if not sessions:
        raise HTTPException(status_code=404, detail={"code": "session_not_found"})
    session = sessions[0]
    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail={"code": "not_owner"})
    if session.get("is_completed"):
        raise HTTPException(status_code=409, detail={"code": "already_completed"})

    if req.answers:
        raw = [{"question_id": a.question_id, "option_id": a.option_id} for a in req.answers]
    else:
        stored = await _get(client, "cheri_ai_assessment_responses", {
            "select": "question_id,selected_option_id,created_at",
            "session_id": f"eq.{req.session_id}", "order": "created_at.asc",
        })
        raw = [{"question_id": s["question_id"], "option_id": s["selected_option_id"],
                "created_at": s.get("created_at") or "", "answered_at": s.get("created_at")} for s in stored]
    answers = dedupe_answers(raw)
    if not answers:
        raise HTTPException(status_code=422, detail={"code": "incomplete", "answered": 0})

    option_ids = sorted({a["option_id"] for a in answers})
    question_ids = sorted({a["question_id"] for a in answers})
    options = {o["id"]: o for o in await _get(client, "cheri_ai_question_options", {
        "select": "id,question_id,option_key,option_text,red_flag", "id": _in(option_ids)})}
    questions = {q["id"]: q for q in await _get(client, "cheri_ai_laws_questions", {
        "select": "id,dimension_id,question_text", "id": _in(question_ids)})}
    for a in answers:
        o, q = options.get(a["option_id"]), questions.get(a["question_id"])
        if not o or not q or o.get("question_id") != a["question_id"]:
            raise HTTPException(status_code=422, detail={"code": "invalid_answer", "question_id": a["question_id"]})

    dims = {d["id"] for d in await _get(client, "cheri_ai_dimensions", {"select": "id"})}
    answered_dims = {questions[a["question_id"]].get("dimension_id") for a in answers}
    if dims and not dims.issubset(answered_dims):
        raise HTTPException(status_code=422, detail={
            "code": "incomplete", "answered": len(answers), "expected": len(dims)})

    red_flags = sum(1 for a in answers if options[a["option_id"]].get("red_flag") is True)
    score = score_from_red_flags(red_flags)
    eligible = is_eligible(score)
    tier = profile_tier_for(red_flags)
    now = datetime.now(timezone.utc).isoformat()
    qa_snapshot = {
        "scoring_version": SCORING_VERSION,
        "threshold": ELIGIBILITY_THRESHOLD,
        "score": score,
        "eligible": eligible,
        "completed_at": now,
        "scored_by": "server",
        "answers": [{
            "order": i + 1,
            "question_id": a["question_id"],
            "question_text": questions[a["question_id"]].get("question_text"),
            "dimension_id": questions[a["question_id"]].get("dimension_id"),
            "selected_option_id": a["option_id"],
            "selected_option_key": options[a["option_id"]].get("option_key"),
            "selected_option_text": options[a["option_id"]].get("option_text"),
            "is_red_flag": options[a["option_id"]].get("red_flag") is True,
            "answered_at": a.get("answered_at"),
        } for i, a in enumerate(answers)],
    }

    # Conditional claim: only an open attempt can be completed (a parallel submit loses).
    r = await client.patch(f"{_REST}/cheri_ai_assessment_sessions",
                           params={"id": f"eq.{req.session_id}", "user_id": f"eq.{user_id}",
                                   "is_completed": "not.is.true"},
                           json={"completed_at": now, "is_completed": True, "total_score": score,
                                 "completion_score": score, "red_flag_count": red_flags,
                                 "qa_snapshot": qa_snapshot},
                           headers=_HEADERS)
    if r.status_code >= 300:
        raise HTTPException(status_code=502, detail={"code": "write_failed", "message": "Please try again."})
    if not r.json():
        raise HTTPException(status_code=409, detail={"code": "already_completed"})

    prev = await _get(client, "cheri_ai_user_profiles", {
        "select": "assessment_count", "user_id": f"eq.{user_id}", "limit": "1"})
    prev_count = (prev[0].get("assessment_count") or 0) if prev else 0
    up = await client.post(f"{_REST}/cheri_ai_user_profiles", params={"on_conflict": "user_id"},
                           json={"user_id": user_id, "latest_assessment_id": req.session_id,
                                 "total_score": score, "red_flag_count": red_flags, "profile_tier": tier,
                                 "assessment_count": prev_count + 1, "last_assessed_at": now, "updated_at": now},
                           headers={**_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"})
    if up.status_code >= 300:
        logger.error("[assessment-submit] score write failed [%s]", up.status_code)
        raise HTTPException(status_code=502, detail={
            "code": "score_write_failed", "message": "Could not save your reflection result. Please try again."})

    return {"score": score, "eligible": eligible, "red_flags": red_flags, "profile_tier": tier,
            "scoring_version": SCORING_VERSION, "threshold": ELIGIBILITY_THRESHOLD}


async def _send_completed_email(user_id: str, score: int, eligible: bool) -> None:
    result_line = (
        "You're ready to meet connections aligned with your values — Discover is open."
        if eligible
        else "You scored below the readiness threshold. Spend some time with the reflections and retake when you're ready — your matches will be richer for it."
    )
    try:
        await send_transactional_email(user_id=user_id, template_slug="assessment_completed",
                                       extra_vars={"score": score, "result_line": result_line},
                                       dedup_window_hours=24)
    except Exception as e:  # email never fails the submit
        logger.warning("[assessment-submit] email failed: %r", e)


# ------------------------------------------------------------------ routes

@router.post("/assessment/submit", tags=["Assessment"], dependencies=[Depends(rate_limit("assessment_submit"))])
async def assessment_submit(req: AssessmentSubmit, caller_user_id: str = Depends(get_current_user_id)):
    async with httpx.AsyncClient(timeout=20.0) as client:
        result = await score_session(client, caller_user_id, req)
    await _send_completed_email(caller_user_id, result["score"], result["eligible"])
    return result


@router.post("/connect/activate", tags=["Connect"], dependencies=[Depends(rate_limit("connect_state"))])
async def connect_activate(caller_user_id: str = Depends(get_current_user_id)):
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await compute_readiness(client, caller_user_id)
        if r["missing"]:
            raise _not_ready(r)
        # Not official (checked above), so un-pausing is allowed.
        await merge_bio(client, caller_user_id,
                        {"connect_active": True, "relationship_focus": "single", "intros_paused": False})
    return {"ok": True, "connect_active": True, "intros_paused": False}


@router.post("/connect/introductions", tags=["Connect"], dependencies=[Depends(rate_limit("connect_state"))])
async def connect_introductions(body: IntroductionsBody, caller_user_id: str = Depends(get_current_user_id)):
    async with httpx.AsyncClient(timeout=20.0) as client:
        if not body.paused:
            r = await compute_readiness(client, caller_user_id)
            if r["missing"]:
                raise _not_ready(r)
        await merge_bio(client, caller_user_id,
                        {"intros_paused": body.paused, "official_end_choice_pending": False})
    return {"ok": True, "intros_paused": body.paused}
