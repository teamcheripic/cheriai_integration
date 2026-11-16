"""
admin_wipe.py — "Wipe all members completely" (admin Wipe & Cleanup page).

Runs the tested account-deletion process (member_actions.delete_account) for
every member, without the confirmation email (notify=False). Per member that
removes: the login, profile, every activity row (introductions, interests,
connections, chats, CheriAI history, assessments, notifications, …), images
in the avatars / media / kyc buckets, the Stripe customer, the RevenueCat
customer and the OneSignal identity. Each deletion leaves its usual hashed
account_deletions record.

Never touched:
  • admin accounts (admin_users) — always protected;
  • protected_accounts (migration 067) — test / App Review demo logins;
  • members under a legal hold — skipped and reported;
  • experiences, banners, settings, policies, admin data, the audit log.

The run is recorded in job_runs as job "wipe_members" (start, finish,
counts), so the admin page shows progress and the outcome.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

REST = f"{SUPABASE_URL}/rest/v1"
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Accept": "application/json"}
PAGE = 1000


async def _ids(client: httpx.AsyncClient, table: str, column: str) -> set[str]:
    out: set[str] = set()
    offset = 0
    while True:
        r = await client.get(f"{REST}/{table}", params={"select": column, "limit": str(PAGE), "offset": str(offset)}, headers=HEADERS)
        if r.status_code >= 300:
            if r.status_code == 404 or "PGRST205" in r.text:
                return out  # table not deployed (e.g. protected_accounts before 067)
            r.raise_for_status()
        rows = r.json() or []
        out.update(str(row[column]) for row in rows if row.get(column))
        if len(rows) < PAGE:
            return out
        offset += PAGE


async def _auth_user_ids(client: httpx.AsyncClient) -> set[str]:
    """Every login (members without a profile row included)."""
    out: set[str] = set()
    page = 1
    while True:
        r = await client.get(f"{SUPABASE_URL}/auth/v1/admin/users", params={"page": str(page), "per_page": "1000"}, headers=HEADERS)
        r.raise_for_status()
        users = (r.json() or {}).get("users") or []
        out.update(str(u["id"]) for u in users if u.get("id"))
        if len(users) < 1000:
            return out
        page += 1


async def wipe_targets() -> dict[str, Any]:
    """Who a member wipe would delete, and who it keeps."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        members = (await _auth_user_ids(client)) | (await _ids(client, "user_profiles", "user_id"))
        admins = await _ids(client, "admin_users", "id")
        protected = await _ids(client, "protected_accounts", "user_id")
    targets = sorted(members - admins - protected)
    return {"targets": targets, "admins": len(members & admins), "protected": len(members & protected)}


async def wipe_members() -> dict[str, Any]:
    from member_actions import MemberActionError, delete_account

    plan = await wipe_targets()
    counts: dict[str, Any] = {"deleted": 0, "legal_hold": 0, "failed": 0, "kept_admins": plan["admins"], "kept_protected": plan["protected"]}
    for uid in plan["targets"]:
        try:
            await delete_account(uid, notify=False)
            counts["deleted"] += 1
        except MemberActionError as e:
            counts["legal_hold" if str(e) == "legal_hold" else "failed"] += 1
        except Exception as e:  # one member's failure never stops the run
            logger.error("[wipe-members] member failed: %s", type(e).__name__)
            counts["failed"] += 1
    logger.warning("[wipe-members] done %s", counts)
    return counts
