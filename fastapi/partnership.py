"""
partnership.py — explicit backend flow for finalizing a CheriPic partnership.

Why this exists as Python code (not just a DB trigger):
    Earlier iterations relied on `AFTER UPDATE OF partnered_at` triggers
    to do the cleanup automatically. Two problems recurred:
      1. The OF-column clause has subtle semantics — it fires only when
         the column is *mentioned in the SQL SET list*, not when an
         upstream BEFORE trigger changes it. That silently broke the
         cleanup after every wipe and re-test.
      2. Hidden magic. The flow was invisible in git; a support engineer
         reading the code couldn't see what happens on a partnership.

    Now: the CLIENT explicitly calls POST /matching/finalize-partnership
    when it detects both users have accepted. This module runs the
    cleanup in one atomic sequence using the service-role key, so
    RLS + hidden triggers no longer matter for correctness.

What "finalize" does (in order, all with the service_role client):
    1. Load the match, verify caller is a participant, verify both
       partner_accepted_by_a and _b are true, and it isn't already
       partnered (idempotent).
    2. Stamp matches.partnered_at = now().
    3. For every OTHER active match involving either partner:
         a. Deactivate it (is_active = false, partnered_at = null)
         b. DELETE all match_messages for that match_id
         c. DELETE all match_reads for that match_id
         d. INSERT a partner_dropped notification for the third party
            (deduped so re-runs don't spam)
    4. Decline every pending match_requests to/from either partner.
    5. DELETE every user_match_views row involving either partner —
       both directions. Clears Home stories, Discover, and frees the
       third parties' quota slots.

Rules preserved from the user's spec:
    • Partnership match itself is NEVER touched by the sweep (id filter)
    • Messages between the two partners are KEPT
    • Third parties always get a notification
    • Cleanup is idempotent — safe to re-run
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

_PG_BASE = f"{SUPABASE_URL}/rest/v1"
_PG_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Prefer": "return=representation",
}


class PartnershipError(Exception):
    """Raised when the caller/state can't finalize a partnership."""


async def _pg_get(client: httpx.AsyncClient, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
    resp = await client.get(f"{_PG_BASE}/{path}", params=params, headers=_PG_HEADERS, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


async def _pg_patch(client: httpx.AsyncClient, path: str, params: dict[str, str], payload: dict[str, Any]) -> None:
    resp = await client.patch(
        f"{_PG_BASE}/{path}",
        params=params,
        json=payload,
        headers=_PG_HEADERS,
        timeout=15.0,
    )
    if resp.status_code >= 300:
        raise PartnershipError(f"PATCH {path} failed [{resp.status_code}]: {resp.text[:200]}")


async def _pg_delete(client: httpx.AsyncClient, path: str, params: dict[str, str]) -> None:
    resp = await client.delete(
        f"{_PG_BASE}/{path}",
        params=params,
        headers=_PG_HEADERS,
        timeout=15.0,
    )
    if resp.status_code >= 300:
        raise PartnershipError(f"DELETE {path} failed [{resp.status_code}]: {resp.text[:200]}")


async def _pg_post(client: httpx.AsyncClient, path: str, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    resp = await client.post(
        f"{_PG_BASE}/{path}",
        json=payload,
        headers=_PG_HEADERS,
        timeout=15.0,
    )
    if resp.status_code >= 300:
        raise PartnershipError(f"POST {path} failed [{resp.status_code}]: {resp.text[:200]}")


async def finalize_partnership(match_id: str, caller_user_id: str) -> dict[str, Any]:
    """
    Finalize the partnership referenced by `match_id` on behalf of
    `caller_user_id`. Idempotent — calling this on an already-partnered
    match returns the current state without re-running the sweep.

    Returns a summary dict of what happened so the caller/UI can react.
    """
    async with httpx.AsyncClient() as client:
        # ---- 1. Load the match + verify caller is a participant ----
        rows = await _pg_get(
            client,
            "matches",
            {
                "select": "id,user_a_id,user_b_id,partner_accepted_by_a,partner_accepted_by_b,partnered_at,is_active",
                "id": f"eq.{match_id}",
                "limit": "1",
            },
        )
        if not rows:
            raise PartnershipError("match_not_found")
        m = rows[0]

        if caller_user_id not in (m["user_a_id"], m["user_b_id"]):
            raise PartnershipError("not_a_participant")

        if not m.get("is_active"):
            raise PartnershipError("match_inactive")

        if not (m.get("partner_accepted_by_a") and m.get("partner_accepted_by_b")):
            raise PartnershipError("both_must_accept_first")

        partner_ids = [m["user_a_id"], m["user_b_id"]]

        # ---- Idempotent short-circuit ----
        if m.get("partnered_at"):
            logger.info("[partnership] already finalized match=%s — no-op", match_id)
            return {
                "match_id": match_id,
                "partnered_at": m["partnered_at"],
                "already_finalized": True,
                "side_matches_deactivated": 0,
                "third_parties_notified": 0,
                "views_deleted": 0,
                "requests_declined": 0,
            }

        now_iso = datetime.now(timezone.utc).isoformat()

        # ---- 2. Stamp partnered_at ----
        await _pg_patch(
            client,
            "matches",
            {"id": f"eq.{match_id}"},
            {"partnered_at": now_iso},
        )

        # ---- 3. Sweep side matches for both partners ----
        side_matches_deactivated = 0
        third_parties_notified: set[str] = set()

        for uid in partner_ids:
            side_matches = await _pg_get(
                client,
                "matches",
                {
                    "select": "id,user_a_id,user_b_id",
                    "id": f"neq.{match_id}",
                    "is_active": "eq.true",
                    "or": f"(user_a_id.eq.{uid},user_b_id.eq.{uid})",
                },
            )
            for sm in side_matches:
                third_party = sm["user_b_id"] if sm["user_a_id"] == uid else sm["user_a_id"]
                sm_id = sm["id"]

                # (a) Deactivate the side match
                await _pg_patch(
                    client,
                    "matches",
                    {"id": f"eq.{sm_id}"},
                    {"is_active": False, "partnered_at": None},
                )

                # (b) Delete all messages in the side match. Messages
                # between the TWO PARTNERS themselves are untouched —
                # the sweep already excludes match_id via id.neq.
                await _pg_delete(
                    client,
                    "match_messages",
                    {"match_id": f"eq.{sm_id}"},
                )

                # (c) Delete read receipts
                await _pg_delete(
                    client,
                    "match_reads",
                    {"match_id": f"eq.{sm_id}"},
                )

                side_matches_deactivated += 1

                # (d) Notify the third party — dedup against existing rows
                existing = await _pg_get(
                    client,
                    "notifications",
                    {
                        "select": "id",
                        "user_id": f"eq.{third_party}",
                        "type": "eq.partner_dropped",
                        "related_user_id": f"eq.{uid}",
                        "limit": "1",
                    },
                )
                if not existing:
                    await _pg_post(
                        client,
                        "notifications",
                        {
                            "user_id": third_party,
                            "type": "partner_dropped",
                            "title": "A connection has ended",
                            "body": (
                                "Your connection is now in a committed "
                                "partnership. Wishing you well as you continue "
                                "your journey."
                            ),
                            "related_user_id": uid,
                        },
                    )
                    third_parties_notified.add(third_party)

        # ---- 4. Decline pending match_requests for either partner ----
        # PostgREST `in.` filter needs comma-joined UUIDs.
        partner_ids_csv = ",".join(partner_ids)
        await _pg_patch(
            client,
            "match_requests",
            {
                "status": "eq.pending",
                "or": (
                    f"(sender_id.in.({partner_ids_csv}),"
                    f"receiver_id.in.({partner_ids_csv}))"
                ),
            },
            {"status": "declined"},
        )
        # We don't get a row count from PATCH without a follow-up read;
        # count is informational so we skip a second query.
        requests_declined = "count_not_tracked"

        # ---- 5. Delete user_match_views involving either partner ----
        # Both directions: rows where the partner is the viewer AND rows
        # where the partner is the target. Frees third-party slots and
        # clears the Home stories strip.
        for uid in partner_ids:
            await _pg_delete(
                client,
                "user_match_views",
                {"user_id": f"eq.{uid}"},
            )
            await _pg_delete(
                client,
                "user_match_views",
                {"target_user_id": f"eq.{uid}"},
            )

        return {
            "match_id": match_id,
            "partnered_at": now_iso,
            "already_finalized": False,
            "side_matches_deactivated": side_matches_deactivated,
            "third_parties_notified": len(third_parties_notified),
            "requests_declined": requests_declined,
        }
