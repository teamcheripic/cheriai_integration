"""
official.py — official-request side effects that main.py doesn't own.

POST /matching/notify-official-withdrawn   (handoff §14 "Official request
withdrawn → live chat with request no longer actionable", audit B H14.7-c)

    Called fire-and-forget by the app after the caller withdrew their own
    pending "Make it official" request (11.3.2B). The recipient may still
    hold the `partner_proposal` inbox notice ("Someone wants to make it
    official"). Tapping it already lands on the live Red Room, which re-reads
    the request state and shows no accept action — but a stale, actionable-
    looking inbox entry contradicts the server state. This endpoint removes
    that stale notice for THIS pair only.

    Deliberately NO new push / inbox notice: the handoff treats an official
    request as private and low-pressure (no rejection notices, no nagging);
    the recipient sees the current state the next time they open the chat.

    Authorisation: the caller must be a participant of an ACTIVE, not-yet-
    official match in which the caller's own request flag is now clear —
    i.e. a withdrawal really happened (or there never was a request). Only
    notifications addressed to the other member, of type partner_proposal,
    related to the caller are deleted, so this can't be used to wipe
    anybody else's inbox. Idempotent.

    When migration 049 is applied the withdraw itself goes through the
    withdraw_official_request RPC, which records the 'withdrawn'
    official_request_events row atomically; on the legacy path (049 not
    applied) the event table doesn't exist and nothing is recorded.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request

from ratelimit import rate_limit
from pydantic import BaseModel

from auth import get_current_user_id
from supabase_client import SUPABASE_KEY, SUPABASE_URL

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


class OfficialWithdrawnRequest(BaseModel):
    match_id: str


async def clear_withdrawn_proposal_notice(match_id: str, caller_id: str) -> dict[str, Any]:
    """Delete the recipient's stale partner_proposal notice after a real withdrawal."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_REST}/matches",
            params={
                "select": "user_a_id,user_b_id,is_active,partnered_at,partner_accepted_by_a,partner_accepted_by_b",
                "id": f"eq.{match_id}",
                "limit": "1",
            },
            headers=_HEADERS,
        )
        rows = resp.json() if resp.status_code < 300 else []
        if not rows:
            return {"ok": True, "skipped": "match_not_found"}
        m = rows[0]
        if caller_id not in (m.get("user_a_id"), m.get("user_b_id")):
            return {"ok": True, "skipped": "not_a_participant"}
        caller_is_a = m.get("user_a_id") == caller_id
        caller_flag = m.get("partner_accepted_by_a") if caller_is_a else m.get("partner_accepted_by_b")
        if not m.get("is_active") or m.get("partnered_at") or caller_flag:
            # Ended, official, or the caller's request is still pending → the
            # notice is either handled elsewhere or still accurate.
            return {"ok": True, "skipped": "request_not_withdrawn"}
        other_id = m["user_b_id"] if caller_is_a else m["user_a_id"]
        d = await client.delete(
            f"{_REST}/notifications",
            params={
                "user_id": f"eq.{other_id}",
                "type": "eq.partner_proposal",
                "related_user_id": f"eq.{caller_id}",
            },
            headers=_HEADERS,
        )
        if d.status_code >= 300:
            logger.warning("[official-withdrawn] notice delete failed [%s]", d.status_code)
            return {"ok": True, "skipped": "delete_failed"}
        removed = d.json() if d.content else []
        return {"ok": True, "removed": len(removed) if isinstance(removed, list) else 0}


@router.post("/matching/notify-official-withdrawn", tags=["Matching"], dependencies=[Depends(rate_limit("notify"))])
async def matching_notify_official_withdrawn(
    req: OfficialWithdrawnRequest,
    caller_user_id: str = Depends(get_current_user_id),
):
    """Never raises: the withdrawal already succeeded; this is inbox hygiene."""
    try:
        return await clear_withdrawn_proposal_notice(req.match_id, caller_user_id)
    except Exception as e:
        logger.warning("[official-withdrawn] failed: %r", e)
        return {"ok": True, "skipped": "error"}
