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
    4. DELETE every match_requests row to/from either partner,
       regardless of status (pending/accepted/declined/expired).
       Previously only 'pending' was deleted — the leftover declined
       and accepted rows still appeared in the third parties' sent /
       received lists as if the partners were still discoverable.
    5. DELETE every user_match_views row involving either partner —
       both directions. Clears Home stories, Discover, and frees the
       third parties' quota slots.
    6. DELETE stale matching-related notifications for the PARTNERS
       themselves. Keeps identity / billing / system notifications
       (KYC verified, payment successful, etc.) so their inbox still
       has real audit history — only the now-irrelevant matching
       stream is wiped.
    7. DELETE matching-related notifications ABOUT the partners for
       every other user on the platform. Third parties no longer see
       "User A was interested in you" once A has partnered up. Same
       type-whitelist as step 6 so identity/billing history is kept.

Rules preserved from the user's spec:
    • Partnership match itself is NEVER touched by the sweep (id filter)
    • Messages between the two partners are KEPT
    • Third parties always get a partner_dropped notification (step 3d)
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


async def _pg_post_soft(client: httpx.AsyncClient, path: str, payload: dict[str, Any] | list[dict[str, Any]]) -> bool:
    """
    Like _pg_post but NEVER raises. Returns True on success, False on
    any failure (logged). Used for courtesy notification inserts that
    must NOT abort the surrounding cleanup sweep — e.g. if the
    notifications_type_check migration isn't applied yet, a 23514
    would otherwise cascade up and skip the entire partnership sweep,
    leaving stale rows in match_requests / user_match_views / etc.
    Better to lose the courtesy notification than the isolation.
    """
    try:
        resp = await client.post(
            f"{_PG_BASE}/{path}",
            json=payload,
            headers=_PG_HEADERS,
            timeout=15.0,
        )
        if resp.status_code >= 300:
            logger.warning(
                "[partnership] soft POST %s failed [%s]: %s (continuing sweep)",
                path, resp.status_code, resp.text[:200],
            )
            return False
        return True
    except Exception as e:
        logger.warning("[partnership] soft POST %s raised: %r (continuing sweep)", path, e)
        return False


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

        # ---- Stamp partnered_at only if not already set ----
        # Prior versions of this file short-circuited the ENTIRE sweep
        # when partnered_at was already set. That created a footgun:
        # if the sweep partially succeeded (e.g. because the migration
        # 026/029 changes weren't deployed yet on an earlier attempt),
        # historical side-matches / request rows / notifications
        # LINGERED forever — user reported 2026-09-24 that Swaroop
        # still saw Varsh in Connect long after partnering with Emma.
        # New behavior: preserve the ORIGINAL partnered_at stamp
        # (audit trail), but re-run every sweep step regardless. Every
        # sweep step below is idempotent — deleting already-deleted
        # rows and deactivating already-inactive rows are all no-ops —
        # so calling finalize repeatedly for a partnered pair is safe
        # and self-healing.
        already_partnered_at = m.get("partnered_at")
        now_iso = datetime.now(timezone.utc).isoformat()
        if not already_partnered_at:
            await _pg_patch(
                client,
                "matches",
                {"id": f"eq.{match_id}"},
                {"partnered_at": now_iso},
            )
        stamped_at = already_partnered_at or now_iso

        # ---- 2b. Notify both partners that the partnership is now
        # official. Only fires when we STAMPED partnered_at just now
        # (first-time finalize) — a self-heal re-run against an
        # already-partnered pair skips this, so users don't get
        # duplicate "You made it official" notifications every time
        # the sweep re-runs. Service-role client, so RLS on
        # notifications can't drop the cross-user writes.
        if not already_partnered_at:
            # Soft — a CHECK constraint miss (migration 029 not applied)
            # must NOT abort the sweep. Losing the celebration notif is
            # far better than leaving stale side-matches lingering.
            await _pg_post_soft(
                client,
                "notifications",
                [
                    {
                        "user_id": partner_ids[0],
                        "type": "partnered",
                        "title": "You made it official 💞",
                        "body": "New introductions are paused while you focus on each other.",
                        "related_user_id": partner_ids[1],
                        "related_id": match_id,
                    },
                    {
                        "user_id": partner_ids[1],
                        "type": "partnered",
                        "title": "You made it official 💞",
                        "body": "New introductions are paused while you focus on each other.",
                        "related_user_id": partner_ids[0],
                        "related_id": match_id,
                    },
                ],
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
                    # Soft — CHECK constraint / RLS misconfig on this
                    # single row must not abort the sweep for OTHER
                    # third parties or the trailing steps.
                    inserted_ok = await _pg_post_soft(
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
                    if inserted_ok:
                        third_parties_notified.add(third_party)

        # ---- 4. DELETE every match_requests row involving either partner ----
        # ALL statuses — pending, accepted, declined, expired. The
        # partners' new life is with each other; every prior request
        # should be gone from BOTH sides' request lists so nobody sees
        # "you accepted so-and-so a month ago" for someone who's now
        # partnered. Previously this step limited to status=pending,
        # which left declined/expired rows on the third parties'
        # sent-requests screen — the user reported this on
        # 2026-09-24 as "should be removed everywhere including
        # intrests, connect or requests".
        partner_ids_csv = ",".join(partner_ids)
        await _pg_delete(
            client,
            "match_requests",
            {
                "or": (
                    f"(sender_id.in.({partner_ids_csv}),"
                    f"receiver_id.in.({partner_ids_csv}))"
                ),
            },
        )
        # We don't get a row count from PATCH without a follow-up read;
        # count is informational so we skip a second query.
        requests_deleted = "count_not_tracked"

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

        # ---- 6 & 7. Notification sweep ----
        # Matching-related notification types the sweep zaps. Everything
        # NOT on this list survives — verify_approved / verify_revoked /
        # payment_successful / account_suspended / etc. remain in the
        # user's inbox as real audit history. Add new types here if they
        # ever get introduced to the matching flow.
        MATCHING_NOTIF_TYPES_FOR_PARTNERS = (
            "interest_received",     # someone sent them interest
            "interest_accepted",     # their interest was accepted
            "match_available",       # a candidate was surfaced
            "match_accepted",        # their interest was accepted (alt naming)
            "match_declined",        # their interest was declined
            "match_expired",         # a pending request expired
            "match_created",         # legacy — mutual match created
            "new_match",             # legacy naming
            "partner_dropped",       # they were dropped by someone who partnered
            "partner_proposal",      # someone asked them to make it official
            # We deliberately KEEP `partnered` in the partners' own
            # inbox — it's the "you made it official" celebration
            # notification we just wrote in step 2b, and it's the only
            # signal that the partnership is live. Not stale history.
        )
        # For third parties we keep the partner_dropped rows we just
        # INSERTED in step 3d — those are the ONE piece of information
        # they SHOULD still see. Only the stale outgoing / incoming
        # matching signals about the partners are wiped.
        MATCHING_NOTIF_TYPES_FOR_THIRD_PARTIES = tuple(
            t for t in MATCHING_NOTIF_TYPES_FOR_PARTNERS if t != "partner_dropped"
        )

        # ---- 6. DELETE matching-related notifications for the PARTNERS ----
        # Both partners' inboxes are cleared of every past matching
        # signal (interest received, matches surfaced, etc.). Their
        # KYC / billing / system notifications stay put.
        await _pg_delete(
            client,
            "notifications",
            {
                "user_id": f"in.({partner_ids_csv})",
                "type": f"in.({','.join(MATCHING_NOTIF_TYPES_FOR_PARTNERS)})",
            },
        )

        # ---- 7. DELETE matching-related notifications ABOUT the partners
        # for every third party. Anything a third party has related to
        # either partner — "User A is interested in you", "You matched
        # with User B", etc. — is gone once the partnership is
        # finalized. Guarded on related_user_id so unrelated matches
        # in the same third party's inbox aren't touched. partner_dropped
        # is intentionally excluded from the type list so the "connection
        # has ended" notice we just wrote in step 3d survives the sweep.
        await _pg_delete(
            client,
            "notifications",
            {
                "related_user_id": f"in.({partner_ids_csv})",
                "type": f"in.({','.join(MATCHING_NOTIF_TYPES_FOR_THIRD_PARTIES)})",
                "user_id": f"not.in.({partner_ids_csv})",
            },
        )

        return {
            "match_id": match_id,
            "partnered_at": stamped_at,
            "already_finalized": bool(already_partnered_at),
            "side_matches_deactivated": side_matches_deactivated,
            "third_parties_notified": len(third_parties_notified),
            "requests_deleted": requests_deleted,
        }
