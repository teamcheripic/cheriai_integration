"""
qa_login.py — admin-controlled "test login" (development mode) for QA.

While an admin has it switched on (Admin → Wipe & Cleanup → Test login), the
email addresses on the admin's list sign in with the fixed code 123456 and
no email is sent. Every other address keeps the normal emailed code.

Settings (public.app_config, written from the admin panel):
  test_login_enabled  'true' / 'false'
  test_login_until    ISO timestamp — the mode switches itself off after it
  test_login_emails   one pattern per line (or comma separated):
                        jane@example.com      one address
                        name+*@gmail.com      plus-addresses of one inbox
                        *@yourcompany.com     a whole domain you own

Safety rules (enforced here, so a raw App Config edit can't loosen them):
  • off unless enabled AND test_login_until is in the future;
  • a bare "*" and whole public mail domains (*@gmail.com, …) are ignored,
    so a real member's address can never be opened with 123456;
  • admin accounts never sign in this way (app_metadata.is_admin on the
    login, or a legacy admin_users row);
  • both endpoints are rate limited, and every test sign-in is logged.

Flow (mobile app, AuthScreens):
  POST /auth/test-login/check {email}        → {test: true, code} or
       {test: false}; when true the app skips the emailed code and shows
       "Test mode: enter <code>". The code is never in the app itself.
  POST /auth/test-login {email, code}        → {token_hash}; the app finishes
       with supabase.auth.verifyOtp({token_hash, type: 'email'}), so the
       session is a normal Supabase session. A new address gets a login
       (email confirmed) and then the usual onboarding.
"""

from __future__ import annotations

import hmac
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app_config import get_bool, get_config
from ratelimit import rate_limit
from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)
router = APIRouter()

TEST_CODE = "123456"
_REST = f"{SUPABASE_URL}/rest/v1"
_AUTH = f"{SUPABASE_URL}/auth/v1"
_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

# Whole-domain wildcards on these are refused: they'd cover real members.
PUBLIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "outlook.com", "hotmail.com",
    "hotmail.co.uk", "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "gmx.de", "yandex.com", "mail.com", "zoho.com", "rediffmail.com",
})
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")


class QaLoginCheck(BaseModel):
    email: str = Field(..., max_length=254)


class QaLoginRequest(BaseModel):
    email: str = Field(..., max_length=254)
    code: str = Field(..., max_length=12)


def _norm(email: str) -> Optional[str]:
    v = (email or "").strip().lower()
    return v if _EMAIL_RE.match(v) else None


def parse_patterns(raw: Optional[str]) -> list[str]:
    """Usable patterns from the admin's list; unsafe or malformed ones dropped."""
    out: list[str] = []
    for item in re.split(r"[\s,;]+", (raw or "").strip().lower()):
        if not item or "@" not in item:
            continue
        local, _, domain = item.rpartition("@")
        if not domain or "*" in domain or "." not in domain:
            continue
        if local == "*":
            if domain in PUBLIC_DOMAINS:
                continue  # *@gmail.com would open every Gmail member
            out.append(item)
        elif local.endswith("+*") and "*" not in local[:-2] and len(local) > 2:
            out.append(item)  # name+*@domain: one person's plus-addresses
        elif "*" not in local and _EMAIL_RE.match(item):
            out.append(item)
    return out


def email_matches(email: str, patterns: list[str]) -> bool:
    local, _, domain = email.rpartition("@")
    for p in patterns:
        p_local, _, p_domain = p.rpartition("@")
        if p_domain != domain:
            continue
        if p_local == "*" or p_local == local:
            return True
        if p_local.endswith("+*") and local.startswith(p_local[:-1]) and len(local) > len(p_local) - 1:
            return True
    return False


async def _mode_active() -> bool:
    if not await get_bool("test_login_enabled", default=False):
        return False
    until = await get_config("test_login_until")
    if not until:
        return False
    try:
        ts = datetime.fromisoformat(until.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts > datetime.now(timezone.utc)


async def eligible(email: Optional[str]) -> bool:
    """True when `email` may sign in with the test code right now."""
    if not email or not await _mode_active():
        return False
    patterns = parse_patterns(await get_config("test_login_emails"))
    return email_matches(email, patterns)


def _hashed_token(body: dict[str, Any]) -> Optional[str]:
    props = body.get("properties") if isinstance(body.get("properties"), dict) else {}
    return body.get("hashed_token") or props.get("hashed_token")


def _user_id(body: dict[str, Any]) -> Optional[str]:
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    return body.get("id") or user.get("id")


async def _generate_link(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post(f"{_AUTH}/admin/generate_link", json={"type": "magiclink", "email": email}, headers=_HEADERS)


async def mint_token_hash(email: str) -> str:
    """A one-time sign-in token for `email`, creating the login if needed.
    Raises HTTPException(403) for admin accounts, 502 when Auth refuses."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await _generate_link(client, email)
        if r.status_code in (404, 422) or (r.status_code == 400 and "not found" in r.text.lower()):
            # New address: create a confirmed login, then the usual onboarding runs.
            c = await client.post(f"{_AUTH}/admin/users", json={"email": email, "email_confirm": True}, headers=_HEADERS)
            if c.status_code >= 300 and c.status_code != 422:  # 422: created meanwhile
                logger.error("[test-login] create user failed [%s]", c.status_code)
                raise HTTPException(status_code=502, detail={"code": "test_login_failed", "message": "Could not sign in. Try again."})
            r = await _generate_link(client, email)
        if r.status_code >= 300:
            logger.error("[test-login] generate_link failed [%s]: %s", r.status_code, r.text[:200])
            raise HTTPException(status_code=502, detail={"code": "test_login_failed", "message": "Could not sign in. Try again."})
        body = r.json() or {}
        token, uid = _hashed_token(body), _user_id(body)
        if not token or not uid:
            raise HTTPException(status_code=502, detail={"code": "test_login_failed", "message": "Could not sign in. Try again."})
        from admin_wipe import is_admin_login

        user = body.get("user") if isinstance(body.get("user"), dict) else body
        if is_admin_login(user):
            logger.warning("[test-login] refused for an admin account")
            raise HTTPException(status_code=403, detail={"code": "test_login_unavailable", "message": "Test login isn't available for this address."})
        a = await client.get(f"{_REST}/admin_users", params={"select": "id", "id": f"eq.{uid}", "limit": "1"}, headers=_HEADERS)
        if a.status_code >= 300 or (a.json() or []):
            # Admins never get a test session (and an unreadable answer counts as admin).
            logger.warning("[test-login] refused for an admin account")
            raise HTTPException(status_code=403, detail={"code": "test_login_unavailable", "message": "Test login isn't available for this address."})
        return token


@router.post("/auth/test-login/check", tags=["Auth"], dependencies=[Depends(rate_limit("test_login_check"))])
async def check_test_login(req: QaLoginCheck) -> dict[str, Any]:
    """Whether this address signs in with the test code (no email is sent).
    The code is returned only for a listed address while the mode is on (that
    address can sign in with it anyway), so it never has to be in the app."""
    if await eligible(_norm(req.email)):
        return {"test": True, "code": TEST_CODE}
    return {"test": False}


@router.post("/auth/test-login", tags=["Auth"], dependencies=[Depends(rate_limit("test_login"))])
async def start_test_login(req: QaLoginRequest) -> dict[str, str]:
    email = _norm(req.email)
    if not hmac.compare_digest((req.code or "").strip(), TEST_CODE) or not await eligible(email):
        raise HTTPException(status_code=403, detail={"code": "test_login_unavailable", "message": "Test login isn't available for this address."})
    token = await mint_token_hash(email or "")
    logger.warning("[test-login] signed in a test address @%s", (email or "").rpartition("@")[2])
    return {"token_hash": token}
