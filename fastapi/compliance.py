"""
compliance.py — shared server-side checks for the Nov 2026 compliance
checklist (documents/audit/COMPLIANCE_CONTRACT.md, "Backend (FastAPI)").

Everything here reads tables / RPCs added by migrations 056–059, which are
NOT APPLIED yet. Each helper detects a missing table, column or RPC
(PostgREST 404, PGRST202/204/205, Postgres 42P01/42703/42883) and falls back
to today's behaviour:

  member_standing      RPC missing             → 'active'
  has_ai_consent       ai_consent_at missing   → True (treated as consented)
  country_allows       RPC / flags missing     → True
  min_member_age       app_config key missing  → 21
  has_legal_hold       legal_holds missing     → False
  preserve_rows        preserved_content gone  → structured log line only
  log_ai_safety_event  ai_safety_events gone   → structured log line only

Callers always go through the module (`compliance.member_standing(...)`) so
tests can monkeypatch one place. None of these helpers raise.

Privacy: nothing here logs message text, KYC data or payment data. The AI
moderation call only ever receives the CheriAI chat text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger("cheripic.compliance")

_REST = f"{SUPABASE_URL}/rest/v1"
_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

DEFAULT_MIN_MEMBER_AGE = 21
STANDING_VALUES = ("active", "warned", "restricted", "suspended", "terminated")
OPEN_REPORT_STATUSES = ("open", "investigating", "escalated", "pending")

_MISSING_CODES = ("PGRST202", "PGRST204", "PGRST205", "42P01", "42703", "42883")


def is_missing(resp: httpx.Response) -> bool:
    """True when PostgREST says the table / column / RPC doesn't exist yet."""
    if resp.status_code == 404:
        return True
    if resp.status_code < 400:
        return False
    try:
        body = resp.json()
        code = str((body or {}).get("code") or "") if isinstance(body, dict) else ""
    except ValueError:
        code = ""
    text = resp.text or ""
    return code in _MISSING_CODES or any(c in text for c in _MISSING_CODES)


def is_missing_error(err: Exception) -> bool:
    msg = str(err)
    return any(c in msg for c in _MISSING_CODES) or "[404]" in msg


# --------------------------------------------------------------------------
# Standing (057 member_standing)
# --------------------------------------------------------------------------

_STANDING_TTL = 30.0
_standing_cache: dict[str, tuple[float, str]] = {}


def _parse_ts(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def _standing_from_tables(client: httpx.AsyncClient, user_id: str) -> str:
    """Mirror of member_standing() for when the RPC answers NULL to the
    service role (it only answers for auth.uid() / is_admin())."""
    r = await client.get(
        f"{_REST}/user_account_status",
        params={"select": "status,status_until", "user_id": f"eq.{user_id}", "limit": "1"},
        headers=_HEADERS,
    )
    if r.status_code >= 300:
        return "active"  # pre-057 (no status column) or upstream blip
    rows = r.json() or []
    status = (rows[0].get("status") if rows else None) or "active"
    until = _parse_ts(rows[0].get("status_until")) if rows else None
    if status in ("restricted", "suspended") and until and until <= datetime.now(timezone.utc):
        status = "active"
    if status in ("active", "warned"):
        p = await client.get(
            f"{_REST}/user_profiles",
            params={"select": "is_active", "user_id": f"eq.{user_id}", "limit": "1"},
            headers=_HEADERS,
        )
        prow = (p.json() or [{}])[0] if p.status_code < 300 and p.json() else {}
        if prow.get("is_active") is False:
            status = "suspended"
    return status if status in STANDING_VALUES else "active"


async def member_standing(user_id: str) -> str:
    """Effective standing: active | warned | restricted | suspended | terminated.
    'active' whenever 057 isn't applied or the lookup fails."""
    now = time.monotonic()
    hit = _standing_cache.get(user_id)
    if hit and hit[0] > now:
        return hit[1]
    standing = "active"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(f"{_REST}/rpc/member_standing", json={"uid": user_id}, headers=_HEADERS)
            if r.status_code < 300:
                val = r.json() if r.content else None
                if isinstance(val, str) and val in STANDING_VALUES:
                    standing = val
                else:
                    standing = await _standing_from_tables(client, user_id)
            elif is_missing(r):
                standing = "active"  # 057 not applied → today's behaviour
            else:
                logger.warning("[standing] member_standing failed [%s]", r.status_code)
    except Exception as e:
        logger.warning("[standing] lookup failed (treating as active): %s", type(e).__name__)
    if len(_standing_cache) > 5000:
        _standing_cache.clear()
    _standing_cache[user_id] = (now + _STANDING_TTL, standing)
    return standing


def forget_standing(user_id: Optional[str] = None) -> None:
    if user_id is None:
        _standing_cache.clear()
    else:
        _standing_cache.pop(user_id, None)


# --------------------------------------------------------------------------
# Age + country (056 / 058)
# --------------------------------------------------------------------------

async def min_member_age() -> int:
    try:
        from app_config import get_int

        age = await get_int("min_member_age", env_key="MIN_MEMBER_AGE", default=DEFAULT_MIN_MEMBER_AGE)
        return age if 13 <= age <= 120 else DEFAULT_MIN_MEMBER_AGE
    except Exception:
        return DEFAULT_MIN_MEMBER_AGE


def age_from_dob(dob: Any, today: Optional[date] = None) -> Optional[int]:
    """Calendar age from an ISO date (or date); None when unparseable/missing."""
    if not dob:
        return None
    try:
        d = dob if isinstance(dob, date) else date.fromisoformat(str(dob)[:10])
    except (TypeError, ValueError):
        return None
    t = today or datetime.now(timezone.utc).date()
    return t.year - d.year - ((t.month, t.day) < (d.month, d.day))


async def country_allows(code: Optional[str], feature: str) -> bool:
    """058 country_allows(); unknown/NULL code, missing RPC or any error → True
    (legacy values like 'UAE' must never lock existing members out)."""
    if not code:
        return True
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                f"{_REST}/rpc/country_allows", json={"p_code": code, "p_feature": feature}, headers=_HEADERS
            )
        if r.status_code >= 300:
            if not is_missing(r):
                logger.warning("[country] country_allows failed [%s]; allowing", r.status_code)
            return True
        val = r.json() if r.content else None
        return val is not False
    except Exception as e:
        logger.warning("[country] lookup failed (allowing): %s", type(e).__name__)
        return True


async def fetch_profile_basics(user_id: str) -> Optional[dict[str, Any]]:
    """{user_id, date_of_birth, country_of_residence} or None (no row / error)."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{_REST}/user_profiles",
                params={"select": "user_id,date_of_birth,country_of_residence",
                        "user_id": f"eq.{user_id}", "limit": "1"},
                headers=_HEADERS,
            )
        if r.status_code >= 300:
            return None
        rows = r.json() or []
        return rows[0] if rows else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# CheriAI consent (056 ai_consent_at)
# --------------------------------------------------------------------------

async def has_ai_consent(user_id: str) -> Optional[bool]:
    """True = consented (or the column doesn't exist yet), False = not given
    or withdrawn, None = couldn't tell (upstream error)."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{_REST}/user_profiles",
                params={"select": "ai_consent_at", "user_id": f"eq.{user_id}", "limit": "1"},
                headers=_HEADERS,
            )
        if r.status_code >= 300:
            if is_missing(r) or r.status_code == 400 and "ai_consent_at" in (r.text or ""):
                return True  # 056 not applied → treat as consented (today's behaviour)
            logger.warning("[ai-consent] lookup failed [%s]", r.status_code)
            return None
        rows = r.json() or []
        return bool(rows and rows[0].get("ai_consent_at"))
    except Exception as e:
        logger.warning("[ai-consent] lookup failed: %s", type(e).__name__)
        return None


# --------------------------------------------------------------------------
# Legal holds, open reports, evidence preservation (057)
# --------------------------------------------------------------------------

async def has_legal_hold(user_id: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{_REST}/legal_holds",
                params={"select": "id", "user_id": f"eq.{user_id}", "released_at": "is.null", "limit": "1"},
                headers=_HEADERS,
            )
        if r.status_code >= 300:
            if not is_missing(r):
                logger.warning("[legal-hold] lookup failed [%s]", r.status_code)
            return False
        return bool(r.json())
    except Exception as e:
        logger.warning("[legal-hold] lookup failed: %s", type(e).__name__)
        return False


async def has_open_report(user_id: str) -> bool:
    """Open report filed by or about the member. An unreadable answer counts
    as True so the caller preserves evidence (harmless when nothing's open)."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{_REST}/user_reports",
                params={
                    "select": "id",
                    "or": f"(reporter_id.eq.{user_id},reported_id.eq.{user_id})",
                    "status": f"in.({','.join(OPEN_REPORT_STATUSES)})",
                    "limit": "1",
                },
                headers=_HEADERS,
            )
        if r.status_code >= 300:
            logger.warning("[open-report] lookup failed [%s]", r.status_code)
            return True
        return bool(r.json())
    except Exception as e:
        logger.warning("[open-report] lookup failed: %s", type(e).__name__)
        return True


async def needs_preservation(user_ids: Iterable[str]) -> bool:
    for uid in {u for u in user_ids if u}:
        if await has_legal_hold(uid) or await has_open_report(uid):
            return True
    return False


async def preserve_rows(
    source_table: str,
    rows: list[dict[str, Any]],
    *,
    subject_user_id: Optional[str],
    reason: str,
    report_id: Optional[str] = None,
) -> int:
    """Copy rows into preserved_content before a hard delete. Returns the
    number stored (0 when the table is missing — then only counts are logged,
    never the content)."""
    if not rows:
        return 0
    payload = [
        {
            "source_table": source_table,
            "source_id": str(r.get("id")) if r.get("id") is not None else None,
            "subject_user_id": subject_user_id,
            "report_id": report_id,
            "payload": r,
            "reason": reason,
        }
        for r in rows
    ]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_REST}/preserved_content", json=payload, headers={**_HEADERS, "Prefer": "return=minimal"}
            )
        if resp.status_code < 300:
            logger.info("[preserve] stored %d %s rows (%s)", len(rows), source_table, reason)
            return len(rows)
        if is_missing(resp):
            logger.warning("[preserve] preserved_content missing (057 not applied); %d %s rows NOT preserved",
                           len(rows), source_table)
        else:
            logger.error("[preserve] insert failed [%s]; %d %s rows NOT preserved",
                         resp.status_code, len(rows), source_table)
    except Exception as e:
        logger.error("[preserve] insert failed (%s); %d %s rows NOT preserved",
                     type(e).__name__, len(rows), source_table)
    return 0


async def preserve_match_messages(
    match_ids: list[str], participants: Iterable[str], *, reason: str, subject_user_id: Optional[str] = None
) -> int:
    """Before match_messages for `match_ids` are hard-deleted: copy them to
    preserved_content when any participant has an open report or an active
    legal hold. Never raises."""
    if not match_ids:
        return 0
    try:
        if not await needs_preservation(participants):
            return 0
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{_REST}/match_messages",
                params={"select": "id,match_id,sender_id,content,created_at",
                        "match_id": f"in.({','.join(match_ids)})"},
                headers=_HEADERS,
            )
        if r.status_code >= 300:
            logger.error("[preserve] could not read match_messages [%s]", r.status_code)
            return 0
        return await preserve_rows("match_messages", r.json() or [], subject_user_id=subject_user_id, reason=reason)
    except Exception as e:
        logger.error("[preserve] match_messages preservation failed: %s", type(e).__name__)
        return 0


# --------------------------------------------------------------------------
# CheriAI safety: crisis router + OpenAI moderation
# --------------------------------------------------------------------------

CRISIS_REPLY = (
    "I'm really glad you told me, and I'm sorry you're carrying this. I'm an AI and can't give "
    "emergency help, but you deserve support right now from a real person. If you might act on "
    "these thoughts or you're in danger, please call your local emergency number (911 in the US, "
    "112 in the EU, 999 in the UK) or go to the nearest emergency room. You can also reach a "
    "crisis line: in the US call or text 988, in the UK and Ireland call Samaritans on 116 123, "
    "or find a local line at findahelpline.com. If you can, reach out to someone you trust and "
    "let them know how you're feeling."
)

SAFE_INPUT_REPLY = (
    "I can't help with that one. I'm here for conversations about dating, relationships and "
    "getting to know yourself. Want to talk about something along those lines?"
)

SAFE_OUTPUT_FALLBACK = (
    "Sorry, I can't give a good answer to that. Could you try asking in a different way?"
)

_CRISIS_PATTERNS = [
    r"\bkill(ing)?\s+my\s*self\b",
    r"\bkms\b",
    r"\bsuicid(e|al)\b",
    r"\bend(ing)?\s+(it\s+all|my\s+life)\b",
    r"\btake\s+my\s+(own\s+)?life\b",
    r"\b(want(\s+to)?|wanna|going\s+to|gonna)\s+die\b",
    r"\bwish\s+i\s+(was|were)\s+dead\b",
    r"\bbetter\s+off\s+dead\b",
    r"\bno\s+reason\s+to\s+live\b",
    r"\bself[\s-]?harm(ing)?\b",
    r"\bhurt(ing)?\s+my\s*self\b",
    r"\bcut(ting)?\s+my\s*self\b",
    r"\boverdos(e|ing)\b",
]
_CRISIS_RE = re.compile("|".join(_CRISIS_PATTERNS), re.IGNORECASE)

_SELF_HARM_CATEGORIES = ("self-harm", "self-harm/intent", "self-harm/instructions")

MODERATION_MODEL = "omni-moderation-latest"
OPENAI_MODERATION_URL = "https://api.openai.com/v1/moderations"


def is_crisis(text: str) -> bool:
    return bool(_CRISIS_RE.search(re.sub(r"\s+", " ", text or "")))


async def moderate(text: str) -> dict[str, Any]:
    """OpenAI omni-moderation-latest. Returns {flagged, categories, self_harm,
    error}. Fails OPEN (flagged=False, error=True) with a log line when the
    call can't be made; the crisis router still applies regardless."""
    out: dict[str, Any] = {"flagged": False, "categories": [], "self_harm": False, "error": False}
    key = os.getenv("OPENAI_API_KEY") or ""
    if not key or not (text or "").strip():
        return out
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                OPENAI_MODERATION_URL,
                json={"model": MODERATION_MODEL, "input": text},
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
        if r.status_code >= 300:
            logger.warning("[moderation] call failed [%s]; failing open", r.status_code)
            out["error"] = True
            return out
        res = ((r.json() or {}).get("results") or [{}])[0]
        cats = [k for k, v in (res.get("categories") or {}).items() if v]
        out["flagged"] = bool(res.get("flagged"))
        out["categories"] = cats
        out["self_harm"] = any(c in _SELF_HARM_CATEGORIES for c in cats)
        return out
    except Exception as e:
        logger.warning("[moderation] call failed (%s); failing open", type(e).__name__)
        out["error"] = True
        return out


async def log_ai_safety_event(
    user_id: str,
    stage: str,
    *,
    action: str,
    categories: Optional[list[str]] = None,
    conversation_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> bool:
    """Insert an ai_safety_events row (056: id, user_id, conversation_id,
    stage in input/output/crisis, categories jsonb, action, details jsonb,
    created_at) with the service role. Never includes message text. Table
    missing (056 not applied) → skipped with a log line (hashed id only)."""
    from llm_client import hashed_openai_user

    row = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "stage": stage,
        "categories": categories or [],
        "action": action,
        "details": details or {},
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                f"{_REST}/ai_safety_events", json=row, headers={**_HEADERS, "Prefer": "return=minimal"}
            )
        if r.status_code < 300:
            return True
        if not is_missing(r):
            logger.warning("[ai-safety-event] insert failed [%s]", r.status_code)
    except Exception as e:
        logger.warning("[ai-safety-event] insert failed: %s", type(e).__name__)
    logger.warning("[ai-safety-event] %s", json.dumps(
        {"user": hashed_openai_user(user_id), "stage": stage, "action": action,
         "categories": categories or []}, sort_keys=True))
    return False


# --------------------------------------------------------------------------
# Report reason codes (057)
# --------------------------------------------------------------------------

REPORT_REASONS: dict[str, str] = {
    "underage": "Underage",
    "impersonation": "Fake profile",
    "harassment": "Abuse",
    "sexual_exploitation": "Sexual exploitation",
    "unwanted_sexual_content": "Unwanted sexual content",
    "scam": "Fraud or money requests",
    "threats": "Threats or violence",
    "privacy_violation": "Privacy violation",
    "spam": "Spam",
    "hate": "Hate",
    "self_harm": "Self-harm",
    "harmful_ai_response": "Harmful AI response",
    "inaccurate_ai_response": "Inaccurate AI response",
    "other": "Other",
}


def report_priority(category: str) -> str:
    if category in ("underage", "sexual_exploitation", "threats", "self_harm"):
        return "urgent"
    if category in ("scam", "impersonation", "privacy_violation"):
        return "high"
    return "normal"


# --------------------------------------------------------------------------
# Purchases (058 purchase_agreements)
# --------------------------------------------------------------------------

TIER_RANK = {"basic": 0, "premium-lite": 1, "premium": 2}


async def active_paid_membership(user_id: str) -> Optional[dict[str, Any]]:
    """The member's active, unexpired paid membership row, or None.
    Expiry-aware (billing.MEMBERSHIP_GRACE_DAYS). Errors → None."""
    import billing

    cols = "tier,stripe_subscription_id,stripe_customer_id,current_period_end,payment_provider"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            params = {"select": cols, "user_id": f"eq.{user_id}", "is_active": "eq.true",
                      "tier": "neq.basic", "limit": "5"}
            r = await client.get(f"{_REST}/user_memberships", params=params, headers=_HEADERS)
            if r.status_code >= 300 and is_missing(r) or r.status_code == 400:
                params["select"] = cols.replace(",payment_provider", "")  # pre-054
                r = await client.get(f"{_REST}/user_memberships", params=params, headers=_HEADERS)
        if r.status_code >= 300:
            logger.warning("[purchase] membership lookup failed [%s]", r.status_code)
            return None
        for row in r.json() or []:
            if row.get("tier") in TIER_RANK and not billing.membership_expired(row):
                return row
    except Exception as e:
        logger.warning("[purchase] membership lookup failed: %s", type(e).__name__)
    return None


async def tier_price(tier: str) -> dict[str, Any]:
    """{price, currency, billing_interval} from tier_config (058 adds
    currency/billing_interval; before that USD / month)."""
    out: dict[str, Any] = {"price": None, "currency": "USD", "billing_interval": "month"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{_REST}/tier_config",
                params={"select": "monthly_price_usd,currency,billing_interval", "tier": f"eq.{tier}", "limit": "1"},
                headers=_HEADERS,
            )
            if r.status_code == 400 or is_missing(r):
                r = await client.get(
                    f"{_REST}/tier_config",
                    params={"select": "monthly_price_usd", "tier": f"eq.{tier}", "limit": "1"},
                    headers=_HEADERS,
                )
        if r.status_code < 300 and r.json():
            row = r.json()[0]
            out["price"] = row.get("monthly_price_usd")
            out["currency"] = row.get("currency") or "USD"
            out["billing_interval"] = row.get("billing_interval") or "month"
    except Exception:
        pass
    return out


async def record_purchase_agreement(row: dict[str, Any]) -> Optional[str]:
    """Insert a purchase_agreements row (service role). Returns its id, or
    None when the table is missing (058 not applied) / the insert failed —
    the purchase still proceeds and the agreement is logged instead."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                f"{_REST}/purchase_agreements", json=row,
                headers={**_HEADERS, "Prefer": "return=representation"},
            )
        if r.status_code < 300:
            data = r.json() if r.content else []
            return (data[0] or {}).get("id") if isinstance(data, list) and data else None
        if is_missing(r):
            logger.info("[purchase] purchase_agreements missing (058 not applied)")
        else:
            logger.warning("[purchase] agreement insert failed [%s]", r.status_code)
    except Exception as e:
        logger.warning("[purchase] agreement insert failed: %s", type(e).__name__)
    safe = {k: row.get(k) for k in ("tier", "terms_version", "subscription_terms_version", "channel", "provider")}
    logger.info("[purchase-agreement] %s", json.dumps(safe, sort_keys=True))
    return None
