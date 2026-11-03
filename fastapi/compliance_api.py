"""
compliance_api.py — compliance-checklist endpoints (COMPLIANCE_CONTRACT.md).

Member
  POST /ai/report                 {chat_message_id, category, note?}
                                  → {ok, report_id, status, stored}
  GET  /me/export                 → JSON file (attachment) of the caller's data
Admin
  POST /admin/moderation/notify   {action_id} → {sent, template, skipped?}

Errors use {"detail": {"code", "message"}}. Rate limits use the shared
`rate_limit()` dependency. Every table these read/write comes from migrations
056–058 (NOT APPLIED); a missing table/column falls back as documented on
each route and never 500s.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, NoReturn, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import compliance
from auth import get_current_admin_id, get_current_user_id
from ratelimit import rate_limit
from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger("cheripic.compliance_api")

router = APIRouter()

_REST = f"{SUPABASE_URL}/rest/v1"
_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _err(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


# =============================================================================
# POST /ai/report
# =============================================================================
class AIReportBody(BaseModel):
    chat_message_id: str = Field(min_length=1, max_length=64)
    category: str = "harmful_ai_response"
    note: Optional[str] = Field(default=None, max_length=1000)


@router.post("/ai/report", tags=["Safety"], dependencies=[Depends(rate_limit("ai_report"))])
async def ai_report(body: AIReportBody, user_id: str = Depends(get_current_user_id)):
    """Report one CheriAI reply. Not standing-gated (reporting stays open)."""
    category = (body.category or "").strip()
    if category not in compliance.REPORT_REASONS:
        _err(422, "invalid_category", "Choose a reason from the list.")

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{_REST}/chat_history",
            params={"select": "id,user_id,role,content,conversation_id,created_at",
                    "id": f"eq.{body.chat_message_id}", "limit": "1"},
            headers=_HEADERS,
        )
        if r.status_code >= 300:
            if r.status_code in (400, 404):
                _err(404, "message_not_found", "We couldn't find that reply.")
            _err(502, "upstream_error", "Please try again.")
        rows = r.json() or []
        # Not found and "someone else's" look the same — no existence leak.
        if not rows or rows[0].get("user_id") != user_id:
            _err(404, "message_not_found", "We couldn't find that reply.")
        msg = rows[0]
        if msg.get("role") != "assistant":
            _err(422, "not_ai_response", "Only CheriAI replies can be reported here.")

        report = {
            "reporter_id": user_id,
            "reported_id": None,
            "category": category,
            "reason": compliance.REPORT_REASONS[category],
            "description": (body.note or "").strip() or None,
            "status": "open",
            "target_type": "ai_response",
            "target_id": str(msg.get("id")),
            "content_snapshot": {
                "content": msg.get("content"),
                "conversation_id": msg.get("conversation_id"),
                "created_at": msg.get("created_at"),
            },
            "priority": compliance.report_priority(category),
        }
        ins = await client.post(
            f"{_REST}/user_reports", json=report, headers={**_HEADERS, "Prefer": "return=representation"}
        )
    if ins.status_code < 300:
        data = ins.json() if ins.content else []
        rid = (data[0] or {}).get("id") if isinstance(data, list) and data else None
        logger.info("[ai-report] stored category=%s", category)
        return {"ok": True, "report_id": rid, "status": "open", "stored": True}
    # Pre-057: no target_type/content_snapshot columns and reported_id is NOT
    # NULL, so the row can't be stored yet. Log (without the content) and
    # acknowledge so the member still sees the confirmation.
    if compliance.is_missing(ins) or ins.status_code == 400:
        logger.warning("[ai-report] user_reports not ready for AI reports (057 not applied) [%s]; %s",
                       ins.status_code, json.dumps({"category": category, "target_id": report["target_id"]}))
        return {"ok": True, "report_id": None, "status": "open", "stored": False}
    logger.error("[ai-report] insert failed [%s]", ins.status_code)
    _err(502, "report_failed", "We couldn't send your report. Please try again.")


# =============================================================================
# GET /me/export
# =============================================================================
_KYC_IMAGE_COLUMNS = ("kyc_face_url", "kyc_id_url")

# (key in export, table, filter column, select)
_EXPORT_SOURCES: list[tuple[str, str, str, str]] = [
    ("consents", "member_consents", "user_id", "*"),
    ("memberships", "user_memberships", "user_id", "*"),
    ("purchase_agreements", "purchase_agreements", "user_id", "*"),
    ("interests_sent", "match_requests", "sender_id", "*"),
    ("interests_received", "match_requests", "receiver_id", "*"),
    ("messages_sent", "match_messages", "sender_id", "id,match_id,content,created_at"),
    ("legacy_messages_sent", "messages", "sender_id", "id,chat_id,receiver_id,text,created_at"),
    ("cheriai_history", "chat_history", "user_id", "id,conversation_id,role,content,stage,created_at"),
    ("cheriai_memory", "cheri_ai_user_memory", "user_id", "summary,insights,updated_at"),
    ("dob_change_requests", "dob_change_requests", "user_id", "id,current_dob,requested_dob,reason,status,created_at,reviewed_at,review_note"),
    ("appeals", "appeals", "user_id", "id,kind,message,status,created_at,decided_at,decision_note"),
]
_REPORT_COLUMNS = "id,category,reason,description,status,created_at,target_type,target_id"
_REPORT_COLUMNS_LEGACY = "id,category,reason,description,status,created_at"


async def _rows(client: httpx.AsyncClient, table: str, params: dict[str, str]) -> Optional[list[dict[str, Any]]]:
    """Rows, or None when the table/column doesn't exist (or the read failed)."""
    try:
        r = await client.get(f"{_REST}/{table}", params={**params, "limit": "10000"}, headers=_HEADERS)
    except Exception as e:
        logger.warning("[me-export] %s read failed: %s", table, type(e).__name__)
        return None
    if r.status_code >= 300:
        if not compliance.is_missing(r):
            logger.warning("[me-export] %s read failed [%s]", table, r.status_code)
        return None
    data = r.json()
    return data if isinstance(data, list) else []


@router.get("/me/export", tags=["Account"], dependencies=[Depends(rate_limit("me_export"))])
async def me_export(user_id: str = Depends(get_current_user_id)):
    """The caller's own data as a downloadable JSON file. Not standing-gated
    (a terminated member can still export). KYC images are never included."""
    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "unavailable": [],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        prof = await _rows(client, "user_profiles", {"select": "*", "user_id": f"eq.{user_id}"})
        profile = dict(prof[0]) if prof else None
        if profile:
            for col in _KYC_IMAGE_COLUMNS:
                profile.pop(col, None)
        out["profile"] = profile
        for key, table, col, select in _EXPORT_SOURCES:
            rows = await _rows(client, table, {"select": select, col: f"eq.{user_id}"})
            if rows is None:
                out["unavailable"].append(key)
                rows = []
            out[key] = rows
        reports = await _rows(client, "user_reports", {"select": _REPORT_COLUMNS, "reporter_id": f"eq.{user_id}"})
        if reports is None:
            reports = await _rows(client, "user_reports",
                                  {"select": _REPORT_COLUMNS_LEGACY, "reporter_id": f"eq.{user_id}"})
        if reports is None:
            out["unavailable"].append("reports_filed")
        out["reports_filed"] = reports or []
    logger.info("[me-export] export generated (%d sections unavailable)", len(out["unavailable"]))
    return JSONResponse(
        content=json.loads(json.dumps(out, default=str)),
        headers={
            "Content-Disposition": 'attachment; filename="cheripic-data-export.json"',
            "Cache-Control": "no-store",
        },
    )


# =============================================================================
# POST /admin/moderation/notify
# =============================================================================
MODERATION_TEMPLATES = {
    "warning": "account_warning",
    "restriction": "account_restricted",
    "suspension": "account_suspended",
    "termination": "account_terminated",
    "reinstatement": "account_reinstated",
}


class ModerationNotifyBody(BaseModel):
    action_id: str = Field(min_length=1, max_length=64)


@router.post("/admin/moderation/notify", tags=["Admin"], dependencies=[Depends(rate_limit("admin_moderation"))])
async def admin_moderation_notify(body: ModerationNotifyBody, admin_id: str = Depends(get_current_admin_id)):
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{_REST}/moderation_actions",
            params={"select": "id,subject_user_id,action,reason,expires_at",
                    "id": f"eq.{body.action_id}", "limit": "1"},
            headers=_HEADERS,
        )
    if r.status_code >= 300:
        if compliance.is_missing(r):
            _err(503, "moderation_tables_missing",
                 "Moderation emails need migration 057_moderation.sql to be applied.")
        if r.status_code == 400:
            _err(404, "action_not_found", "Unknown moderation action.")
        _err(502, "upstream_error", "Please try again.")
    rows = r.json() or []
    if not rows:
        _err(404, "action_not_found", "Unknown moderation action.")
    action = rows[0]
    slug = MODERATION_TEMPLATES.get(action.get("action") or "")
    if not slug or not action.get("subject_user_id"):
        return {"sent": False, "template": None, "skipped": "no_template_for_action"}

    import transactional_email as te

    try:
        template = await te._load_template(slug)
    except Exception:
        template = None
    if not template:
        logger.info("[moderation-notify] template %s missing; skipped", slug)
        return {"sent": False, "template": slug, "skipped": "template_missing"}

    try:
        app_base = await te._cfg_app_base_url()
    except Exception:
        app_base = "https://cheripic.com"
    sent = await te.send_transactional_email(
        user_id=action["subject_user_id"],
        template_slug=slug,
        extra_vars={
            "reason": action.get("reason") or "",
            "until": action.get("expires_at") or "",
            "appeal_url": f"{(app_base or 'https://cheripic.com').rstrip('/')}/#/contact",
            "appeal_instructions": "If you think this decision is wrong, open CheriPic and go to "
                                   "Settings > Request a decision review, or reply to this email.",
        },
        dedup_window_hours=0,
        essential=True,
    )
    logger.info("[moderation-notify] %s sent=%s (action %s)", slug, sent, action.get("id"))
    return {"sent": bool(sent), "template": slug}
