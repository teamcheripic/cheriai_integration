"""
Member-initiated state changes that must be authorised and race-safe on the
server (development handoff §13, "Critical transaction rules").

  • withdraw_interest  — 11.2B.1 · sender withdraws a still-pending interest.
  • delete_account     — 12.1.1 · permanent account deletion (App Store
                          5.1.1(v) / Google Play account-deletion policy).

Both run with the service key (bypassing RLS) only AFTER the caller's
Supabase JWT has been verified by auth.get_current_user_id, and both check
ownership themselves. Conditional PATCHes (`status=eq.pending`) make the
interest operations idempotent: if an accept and a withdraw race, exactly
one wins and the loser gets the authoritative state back.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from supabase_client import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger("cheripic.member_actions")

_REST = f"{SUPABASE_URL}/rest/v1"
_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


class MemberActionError(Exception):
    """`str(e)` is a stable machine code; `.state` carries the current row state."""

    def __init__(self, code: str, state: Optional[str] = None) -> None:
        super().__init__(code)
        self.state = state


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# 11.2B.1 · Withdraw interest
# --------------------------------------------------------------------------

async def withdraw_interest(request_id: str, caller_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{_REST}/match_requests",
            params={"select": "id,sender_id,receiver_id,status", "id": f"eq.{request_id}"},
            headers=_HEADERS,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            raise MemberActionError("request_not_found")
        row = rows[0]
        if row["sender_id"] != caller_id:
            raise MemberActionError("not_sender")
        if row["status"] != "pending":
            raise MemberActionError("not_pending", state=row["status"])

        # Conditional update — only a still-pending row changes.
        u = await client.patch(
            f"{_REST}/match_requests",
            params={"id": f"eq.{request_id}", "sender_id": f"eq.{caller_id}", "status": "eq.pending"},
            json={"status": "withdrawn", "responded_at": _now()},
            headers=_HEADERS,
        )
        if u.status_code >= 300:
            logger.error("[withdraw-interest] patch failed [%s]: %s", u.status_code, u.text[:200])
            raise MemberActionError("update_failed")
        if not u.json():
            # Lost the race (accepted/declined meanwhile) — report real state.
            again = await client.get(
                f"{_REST}/match_requests",
                params={"select": "status", "id": f"eq.{request_id}"},
                headers=_HEADERS,
            )
            state = (again.json() or [{}])[0].get("status")
            raise MemberActionError("not_pending", state=state)

        # The receiver's "interest received" inbox item no longer leads to an
        # actionable request; the app shows an unavailable state for it. We
        # don't delete it (no implied recall of notifications).
        logger.info("[withdraw-interest] request=%s by=%s", request_id, caller_id)
        return {"ok": True, "status": "withdrawn", "receiver_id": row["receiver_id"]}


# --------------------------------------------------------------------------
# 10C.1.1 · Other member after an official end
# --------------------------------------------------------------------------

async def pause_partner_after_official_end(caller_id: str, other_id: str) -> bool:
    """
    The initiator chose their own resume/pause outcome; the OTHER member
    defaults to paused until they choose (handoff §9). Only applied when a
    formerly-official, now-inactive match between the two really exists, so
    a caller can't pause arbitrary members. unmatchUser clears partnered_at
    but keeps both partner_accepted_by_* flags, which is what we check.
    Stores the state in the bio JSON (`intros_paused`,
    `official_end_choice_pending`) that the apps read.

    Idempotent per ended match: the match id is recorded as
    `official_end_match_id`, so a retried (or repeated) notify-unmatch can't
    re-pause a member who has since made their own choice.
    """
    import json

    async with httpx.AsyncClient(timeout=15.0) as client:
        pair = _pair_filter(caller_id, other_id)
        m = await client.get(
            f"{_REST}/matches",
            params={
                "select": "id",
                "or": f"({pair})",
                "partner_accepted_by_a": "eq.true",
                "partner_accepted_by_b": "eq.true",
                "is_active": "eq.false",
                "order": "created_at.desc",
                "limit": "1",
            },
            headers=_HEADERS,
        )
        if m.status_code >= 300 or not m.json():
            return False
        match_id = m.json()[0].get("id")
        p = await client.get(
            f"{_REST}/user_profiles", params={"select": "bio", "user_id": f"eq.{other_id}"}, headers=_HEADERS
        )
        rows = p.json() if p.status_code < 300 else []
        if not rows:
            return False
        try:
            bio = json.loads(rows[0].get("bio") or "{}")
            if not isinstance(bio, dict):
                bio = {"about_yourself": str(bio)}
        except (ValueError, TypeError):
            bio = {"about_yourself": rows[0].get("bio") or ""}
        if match_id and bio.get("official_end_match_id") == match_id:
            # Already applied for this ended relationship — respect whatever
            # the member chose since.
            return False
        bio["intros_paused"] = True
        bio["official_end_choice_pending"] = True
        if match_id:
            bio["official_end_match_id"] = match_id
        u = await client.patch(
            f"{_REST}/user_profiles",
            params={"user_id": f"eq.{other_id}"},
            json={"bio": json.dumps(bio)},
            headers=_HEADERS,
        )
        return u.status_code < 300


# --------------------------------------------------------------------------
# Pair checks used by the notify-* endpoints so they can't be used as a
# "notify anyone with anything" relay (handoff §13/§14).
# --------------------------------------------------------------------------

def _pair_filter(a: str, b: str) -> str:
    return f"and(user_a_id.eq.{a},user_b_id.eq.{b}),and(user_a_id.eq.{b},user_b_id.eq.{a})"


async def latest_match_between(a: str, b: str, *, active: Optional[bool] = None) -> Optional[dict[str, Any]]:
    """Most recent matches row for the pair (optionally filtered by is_active), or None."""
    params = {
        "select": "id,user_a_id,user_b_id,is_active,partnered_at,partner_accepted_by_a,partner_accepted_by_b",
        "or": f"({_pair_filter(a, b)})",
        "order": "created_at.desc",
        "limit": "1",
    }
    if active is not None:
        params["is_active"] = f"eq.{str(active).lower()}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{_REST}/matches", params=params, headers=_HEADERS)
    if r.status_code >= 300:
        return None
    rows = r.json() or []
    return rows[0] if rows else None


async def request_in_state(sender_id: str, receiver_id: str, status: str) -> bool:
    """True when a match_requests row sender→receiver currently has `status`."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{_REST}/match_requests",
            params={
                "select": "id",
                "sender_id": f"eq.{sender_id}",
                "receiver_id": f"eq.{receiver_id}",
                "status": f"eq.{status}",
                "limit": "1",
            },
            headers=_HEADERS,
        )
    return r.status_code < 300 and bool(r.json())


async def recent_notification_exists(user_id: str, notif_type: str, related_user_id: str, minutes: int) -> bool:
    """Idempotency guard for retried notify calls."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{_REST}/notifications",
            params={
                "select": "id",
                "user_id": f"eq.{user_id}",
                "type": f"eq.{notif_type}",
                "related_user_id": f"eq.{related_user_id}",
                "created_at": f"gte.{since}",
                "limit": "1",
            },
            headers=_HEADERS,
        )
    return r.status_code < 300 and bool(r.json())


# --------------------------------------------------------------------------
# 12.1.1 · Delete account
# --------------------------------------------------------------------------

# (table, column) pairs removed for the member. Order is FK-safe.
_DELETE_TARGETS: list[tuple[str, str]] = [
    ("match_reads", "user_id"),
    ("match_messages", "sender_id"),
    ("match_requests", "sender_id"),
    ("match_requests", "receiver_id"),
    ("user_match_views", "user_id"),
    ("user_match_views", "target_user_id"),
    ("notifications", "user_id"),
    ("notifications", "related_user_id"),
    ("messages", "sender_id"),
    ("messages", "receiver_id"),
    ("chat_history", "user_id"),
    ("cheri_ai_user_memory", "user_id"),
    ("cheri_ai_match_pool", "user_id"),
    ("cheri_ai_daily_usage", "user_id"),
    ("cheri_ai_usage_periods", "user_id"),
    ("cheri_ai_user_profiles", "user_id"),
    ("user_compatibility_answers", "user_id"),
    ("user_selected_questions", "user_id"),
    ("user_dealbreakers", "user_id"),
    ("user_interests", "user_id"),
    ("user_contacts", "user_id"),
    ("chat_users", "user_id"),
    ("claimed_offers", "user_id"),
    ("analytics_events", "user_id"),
    ("blocked_users", "blocker_id"),
    ("blocked_users", "blocked_id"),
    ("user_relationship_history", "user_a_id"),
    ("user_relationship_history", "user_b_id"),
    ("user_memberships", "user_id"),
    ("user_profiles", "user_id"),
]

# Kept on purpose (disclosed in the Privacy Policy): user_reports (safety
# moderation evidence), user_account_status (suspension record so a removed
# abuser can't silently return), stripe_events / sent_emails / sent_push
# (financial and delivery records). user_devices cascades from auth.users.
RETAINED_TABLES: list[str] = [
    "user_reports", "user_account_status", "stripe_events", "sent_emails", "sent_push",
]


def _stripe_sub_already_gone(err: Exception) -> bool:
    """A subscription that no longer exists / is already cancelled is fine."""
    code = getattr(err, "code", None)
    msg = str(err).lower()
    return code == "resource_missing" or "no such subscription" in msg or "canceled subscription" in msg


async def _cancel_stripe_subscription(client: httpx.AsyncClient, user_id: str) -> int:
    """
    Cancel direct (Stripe) billing BEFORE any data is removed. If Stripe
    can't be reached we stop here and the job stays retryable — deleting
    user_memberships first would lose the only pointer to a subscription
    that would keep charging the member (handoff 12.1 / §13).
    """
    r = await client.get(
        f"{_REST}/user_memberships",
        params={"select": "stripe_subscription_id", "user_id": f"eq.{user_id}"},
        headers=_HEADERS,
    )
    if r.status_code >= 300:
        logger.error("[delete-account] membership lookup failed [%s]", r.status_code)
        raise MemberActionError("billing_lookup_failed")
    subs = {row.get("stripe_subscription_id") for row in (r.json() or [])}
    subs.discard(None)
    subs.discard("")
    cancelled = 0
    for sub in sorted(subs):
        try:
            import billing  # local import: optional dependency on stripe

            stripe = billing._get_stripe()
            # stripe-python is synchronous — keep it off the event loop.
            await asyncio.to_thread(stripe.Subscription.cancel, sub)
            cancelled += 1
            logger.info("[delete-account] cancelled stripe sub %s", sub)
        except Exception as e:
            if _stripe_sub_already_gone(e):
                logger.info("[delete-account] stripe sub %s already gone", sub)
                continue
            logger.error("[delete-account] stripe cancel failed for %s: %r", sub, e)
            raise MemberActionError("billing_cancel_failed")
    return cancelled


_STORAGE_PAGE = 1000


# Buckets that can hold a member's files: public photos (legacy originals +
# blurred derivatives), private originals and identity documents (048).
_MEMBER_BUCKETS = ("avatars", "media", "kyc")


def _bucket_missing(resp: httpx.Response) -> bool:
    """Storage answers 400/404 'Bucket not found' for a bucket that 048 hasn't created yet."""
    return resp.status_code in (400, 404) and "not found" in resp.text.lower()


async def _delete_storage(client: httpx.AsyncClient, user_id: str) -> int:
    """
    Removes everything under `<bucket>/<user_id>/` in every member bucket
    (profile, gallery, blurred derivatives, KYC). A bucket that doesn't exist
    yet is skipped. Raises on a failed list/remove so the auth user is NOT
    deleted while identity documents are still stored — a retry finishes it.
    """
    total = 0
    for bucket in _MEMBER_BUCKETS:
        names: list[str] = []
        offset = 0
        missing = False
        while True:
            listed = await client.post(
                f"{SUPABASE_URL}/storage/v1/object/list/{bucket}",
                json={"prefix": f"{user_id}/", "limit": _STORAGE_PAGE, "offset": offset},
                headers=_HEADERS,
            )
            if bucket != "avatars" and _bucket_missing(listed):
                missing = True
                break
            if listed.status_code >= 300:
                logger.error("[delete-account] storage list %s failed [%s]: %s", bucket, listed.status_code, listed.text[:200])
                raise MemberActionError("storage_list_failed")
            batch = listed.json() or []
            names.extend(f"{user_id}/{o['name']}" for o in batch if o.get("name"))
            if len(batch) < _STORAGE_PAGE:
                break
            offset += _STORAGE_PAGE
        if missing:
            continue
        for i in range(0, len(names), _STORAGE_PAGE):
            rm = await client.request(
                "DELETE",
                f"{SUPABASE_URL}/storage/v1/object/{bucket}",
                json={"prefixes": names[i : i + _STORAGE_PAGE]},
                headers=_HEADERS,
            )
            if rm.status_code >= 300:
                logger.error("[delete-account] storage remove %s failed [%s]: %s", bucket, rm.status_code, rm.text[:200])
                raise MemberActionError("storage_delete_failed")
        total += len(names)
    return total


def user_id_hash(user_id: str) -> str:
    """SHA-256 of the user id — the deletion record never stores the id."""
    import hashlib

    return hashlib.sha256(user_id.encode()).hexdigest()


async def _deletion_recipient(client: httpx.AsyncClient, user_id: str) -> Optional[dict[str, str]]:
    """{email, name} captured BEFORE the profile is removed (falls back to
    the auth user, which still exists until the last step). Soft: None."""
    try:
        p = await client.get(
            f"{_REST}/user_profiles",
            params={"select": "email,nick_name,full_name", "user_id": f"eq.{user_id}", "limit": "1"},
            headers=_HEADERS,
        )
        rows = p.json() if p.status_code < 300 else []
        if rows and rows[0].get("email"):
            return {"email": rows[0]["email"], "name": rows[0].get("nick_name") or rows[0].get("full_name") or ""}
        a = await client.get(f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}", headers=_HEADERS)
        if a.status_code < 300 and (a.json() or {}).get("email"):
            return {"email": a.json()["email"], "name": ""}
    except Exception as e:
        logger.warning("[delete-account] recipient lookup failed: %s", type(e).__name__)
    return None


async def _send_deletion_confirmation(recipient: Optional[dict[str, str]]) -> bool:
    """H13.9e · account_deleted confirmation, sent before the login is
    removed. Never raises; a missing template is a soft failure."""
    if not recipient:
        return False
    try:
        from transactional_email import send_template_to_address

        return await send_template_to_address(recipient["email"], "account_deleted", name=recipient.get("name"))
    except Exception as e:
        logger.warning("[delete-account] confirmation email failed: %s", type(e).__name__)
        return False


async def _record_deletion(record: dict[str, Any]) -> bool:
    """
    H13.1 / H13.9e · write the deletion-job record to public.account_deletions
    (migration 051, NOT APPLIED). Until the table exists (PostgREST 404 /
    42P01 / PGRST205) the same fields go to a structured log line. Never
    raises — the record must not change the deletion outcome.
    """
    import json

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{_REST}/account_deletions", json=record, headers={**_HEADERS, "Prefer": "return=minimal"}
            )
        if r.status_code < 300:
            return True
        logger.warning("[delete-account] deletion record not stored [%s]", r.status_code)
    except Exception as e:
        logger.warning("[delete-account] deletion record not stored: %s", type(e).__name__)
    logger.info("[account-deletion-record] %s", json.dumps(record, sort_keys=True))
    return False


async def delete_account(user_id: str) -> dict[str, Any]:
    """
    Idempotent and retryable: every step tolerates rows already being gone,
    and the auth user is removed last so a failure part-way leaves the
    member able to sign in and retry. Every attempt leaves a deletion record
    (hashed id, status, retained tables, billing outcome, error code).
    """
    record: dict[str, Any] = {
        "user_id_hash": user_id_hash(user_id),
        "requested_at": _now(),
        "status": "processing",
        "retained_tables": RETAINED_TABLES,
        "stripe_cancelled": False,
        "error": None,
    }
    try:
        result = await _delete_account_steps(user_id, record)
    except MemberActionError as e:
        record.update(status="failed", error=str(e))
        await _record_deletion(record)
        raise
    except Exception as e:
        record.update(status="failed", error=type(e).__name__)
        await _record_deletion(record)
        raise
    record.update(status="completed", completed_at=_now())
    await _record_deletion(record)
    return result


async def _delete_account_steps(user_id: str, record: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Billing first (nothing deleted if Stripe can't be reached).
        record["stripe_cancelled"] = (await _cancel_stripe_subscription(client, user_id)) > 0
        # Address for the confirmation, captured while the profile exists.
        recipient = await _deletion_recipient(client, user_id)

        # Close every pair the member is in first, so partners' Red Rooms
        # disappear and photo access ends immediately.
        for col in ("user_a_id", "user_b_id"):
            m = await client.get(
                f"{_REST}/matches", params={"select": "id", col: f"eq.{user_id}"}, headers=_HEADERS
            )
            if m.status_code >= 300:
                raise MemberActionError("delete_failed_matches")
            ids = [row["id"] for row in m.json()]
            if ids:
                inlist = f"in.({','.join(ids)})"
                await client.delete(f"{_REST}/match_reads", params={"match_id": inlist}, headers=_HEADERS)
                await client.delete(f"{_REST}/match_messages", params={"match_id": inlist}, headers=_HEADERS)
                d = await client.delete(f"{_REST}/matches", params={"id": inlist}, headers=_HEADERS)
                if d.status_code >= 300 and d.status_code != 404:
                    logger.error("[delete-account] matches delete failed [%s]: %s", d.status_code, d.text[:200])
                    raise MemberActionError("delete_failed_matches")
                try:
                    counts["matches"] = counts.get("matches", 0) + len(d.json())
                except Exception:
                    pass

        for table, col in _DELETE_TARGETS:
            d = await client.delete(f"{_REST}/{table}", params={col: f"eq.{user_id}"}, headers=_HEADERS)
            if d.status_code >= 300 and d.status_code != 404:
                logger.error("[delete-account] %s.%s failed [%s]: %s", table, col, d.status_code, d.text[:200])
                raise MemberActionError(f"delete_failed_{table}")
            try:
                counts[table] = counts.get(table, 0) + len(d.json())
            except Exception:
                pass

        sess = await client.get(
            f"{_REST}/cheri_ai_assessment_sessions",
            params={"select": "id", "user_id": f"eq.{user_id}"},
            headers=_HEADERS,
        )
        sids = [row["id"] for row in (sess.json() if sess.status_code < 300 else [])]
        if sids:
            await client.delete(
                f"{_REST}/cheri_ai_assessment_responses",
                params={"session_id": f"in.({','.join(sids)})"},
                headers=_HEADERS,
            )
            await client.delete(
                f"{_REST}/cheri_ai_assessment_sessions", params={"user_id": f"eq.{user_id}"}, headers=_HEADERS
            )

        counts["storage_objects"] = await _delete_storage(client, user_id)

        # Confirmation goes out while the login still exists (soft failure).
        record["confirmation_sent"] = await _send_deletion_confirmation(recipient)

        # Finally remove the login itself; this also revokes refresh tokens.
        a = await client.delete(f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}", headers=_HEADERS)
        if a.status_code >= 300 and a.status_code != 404:
            logger.error("[delete-account] auth delete failed [%s]: %s", a.status_code, a.text[:200])
            raise MemberActionError("auth_delete_failed")

    logger.info("[delete-account] user=%s done %r", user_id, counts)
    return {"ok": True, "status": "completed", "deleted_at": _now()}
