"""
CheriPass — server-validated pass display and vendor redemption
(handoff 10B2.2, §11 "Redemption decision required", §13 "Pass display/redemption").

  POST /cheripass/token   member (Bearer JWT) asks for a pass for ONE offer.
                          The server re-reads the member's tier and the offer
                          (active + required tier) right now and returns a
                          short-lived HMAC-signed token the app renders as a QR.
  POST /cheripass/verify  partner (X-Vendor-Key) submits a scanned token. The
                          server checks the signature + expiry, re-checks the
                          offer and the member's tier NOW, and records the
                          redemption once in `claimed_offers`.
  POST /analytics/sponsored-click
                          anonymous sponsored-card click (10B2.4): only the
                          banner id and destination kind are stored.

A screenshot of the pass is not proof: the token expires after
TOKEN_TTL_SECONDS and each token redeems at most once.

Token format (URL-safe, no padding):
    base64url(compact JSON payload) "." base64url(HMAC-SHA256(secret, payload_b64))
    payload = {"v":1,"u":uid,"k":"m"|"e","o":offer_id,"t":tier,"i":iat,"e":exp,"n":nonce}

Secrets: CHERIPASS_TOKEN_SECRET; when unset a key is HMAC-derived from the
service key (never logged). Vendor keys: CHERIPASS_VENDOR_KEYS (comma list).
Which partners receive a key, and how they scan (staff web page, partner app),
is a product/ops decision — this module only provides the verification API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ratelimit import rate_limit
from pydantic import BaseModel, Field

import billing
from auth import get_current_user_id
from supabase_client import SUPABASE_KEY, supabase

logger = logging.getLogger("cheripic.cheripass")

router = APIRouter()

TOKEN_TTL_SECONDS = 120
TOKEN_VERSION = 1
TIER_LEVEL = {"basic": 0, "premium-lite": 1, "premium": 2}
TIER_LABEL = {"basic": "Free", "premium-lite": "Premium Lite", "premium": "Premium"}
_KIND_CODE = {"merchant": "m", "experience": "e"}
_CODE_KIND = {v: k for k, v in _KIND_CODE.items()}
_OFFER_TABLE = {"merchant": "merchants", "experience": "experiences"}
_OFFER_COLUMN = {"merchant": "merchant_id", "experience": "experience_id"}


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

def _fail(status: int, code: str, message: str) -> HTTPException:
    """`detail` is always {code, message}: a stable machine code plus member-safe copy."""
    return HTTPException(status_code=status, detail={"code": code, "message": message})


# --------------------------------------------------------------------------
# signing
# --------------------------------------------------------------------------

def _secret() -> bytes:
    configured = os.getenv("CHERIPASS_TOKEN_SECRET") or ""
    if configured:
        return configured.encode()
    # Derived, never the raw service key itself, so a leaked derived key
    # can't be used against PostgREST.
    return hmac.new((SUPABASE_KEY or "").encode(), b"cheripass-token-v1", hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload_b64: str) -> str:
    return _b64e(hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).digest())


def issue_token(*, uid: str, offer_kind: str, offer_id: str, tier: str, now: Optional[float] = None) -> tuple[str, dict]:
    iat = int(now if now is not None else time.time())
    payload = {
        "v": TOKEN_VERSION,
        "u": uid,
        "k": _KIND_CODE[offer_kind],
        "o": offer_id,
        "t": tier,
        "i": iat,
        "e": iat + TOKEN_TTL_SECONDS,
        "n": _b64e(secrets.token_bytes(12)),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}", payload


def decode_token(token: str) -> dict:
    """Verify the signature and shape; does NOT check expiry (the caller does)."""
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 1024:
        raise _fail(400, "invalid_token", "This pass code isn't valid.")
    body, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(body)):
        raise _fail(400, "invalid_token", "This pass code isn't valid.")
    try:
        payload = json.loads(_b64d(body))
        assert payload.get("v") == TOKEN_VERSION
        assert payload.get("k") in _CODE_KIND and payload.get("t") in TIER_LEVEL
        uuid.UUID(str(payload["u"]))
        uuid.UUID(str(payload["o"]))
        int(payload["i"]), int(payload["e"])
        assert isinstance(payload.get("n"), str) and payload["n"]
    except Exception:
        raise _fail(400, "invalid_token", "This pass code isn't valid.")
    return payload


# --------------------------------------------------------------------------
# data reads (service key, after auth)
# --------------------------------------------------------------------------

def _uuid_or_400(value: str, code: str = "invalid_offer") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception:
        raise _fail(400, code, "That offer couldn't be found.")


async def _load_offer(kind: str, offer_id: str) -> dict:
    cols = "id,name,title,is_active,membership_tier" if kind == "merchant" else "id,title,is_active,membership_tier"
    rows = await supabase.select(_OFFER_TABLE[kind], columns=cols, eq={"id": offer_id}, limit=1)
    if not rows:
        raise _fail(404, "offer_not_found", "That offer couldn't be found.")
    row = rows[0]
    tier = row.get("membership_tier")
    # Same normalisation as the app (cheripassOffers.asTier): unknown/null → Free.
    row["_tier"] = tier if tier in TIER_LEVEL else "basic"
    row["_title"] = (row.get("name") or row.get("title") or "").strip() if kind == "merchant" else (row.get("title") or "").strip()
    return row


async def _load_member(uid: str) -> dict:
    rows = await supabase.select("user_profiles", columns="user_id,nick_name,is_active", eq={"user_id": uid}, limit=1)
    return rows[0] if rows else {}


def _check_offer_and_tier(offer: dict, member: dict, tier: str) -> None:
    if not offer.get("is_active"):
        raise _fail(410, "offer_inactive", "This offer is no longer available. Partners can change or end offers at any time.")
    if not member or member.get("is_active") is False:
        raise _fail(403, "account_inactive", "This membership isn't active.")
    if TIER_LEVEL[tier] < TIER_LEVEL[offer["_tier"]]:
        raise _fail(403, "tier_ineligible", f"This offer requires {TIER_LABEL[offer['_tier']]} membership.")


# --------------------------------------------------------------------------
# POST /cheripass/token
# --------------------------------------------------------------------------

class TokenRequest(BaseModel):
    offer_kind: Literal["merchant", "experience"]
    offer_id: str = Field(min_length=1, max_length=64)


@router.post("/cheripass/token", dependencies=[Depends(rate_limit("cheripass_token"))])
async def cheripass_token(req: TokenRequest, user_id: str = Depends(get_current_user_id)) -> dict:
    offer_id = _uuid_or_400(req.offer_id)
    offer, member, tier = await asyncio.gather(
        _load_offer(req.offer_kind, offer_id),
        _load_member(user_id),
        billing.get_membership_tier(user_id),
    )
    _check_offer_and_tier(offer, member, tier)
    token, payload = issue_token(uid=user_id, offer_kind=req.offer_kind, offer_id=offer_id, tier=tier)
    return {
        "token": token,
        "expires_at": datetime.fromtimestamp(payload["e"], tz=timezone.utc).isoformat(),
        "expires_in": TOKEN_TTL_SECONDS,
        "display_name": (member.get("nick_name") or "").strip(),
        "tier": tier,
        "tier_label": TIER_LABEL[tier],
        "offer_kind": req.offer_kind,
        "offer_id": offer_id,
        "offer_title": offer["_title"],
    }


# --------------------------------------------------------------------------
# POST /cheripass/verify  (partner)
# --------------------------------------------------------------------------

class VerifyRequest(BaseModel):
    token: str = Field(min_length=10, max_length=1024)


def _vendor_keys() -> list[str]:
    return [k.strip() for k in (os.getenv("CHERIPASS_VENDOR_KEYS") or "").split(",") if k.strip()]


def require_vendor(x_vendor_key: str = Header(default="")) -> None:
    keys = _vendor_keys()
    if not keys:
        raise _fail(503, "vendor_verification_unavailable", "Partner verification isn't configured yet.")
    if not x_vendor_key or not any(hmac.compare_digest(x_vendor_key.encode(), k.encode()) for k in keys):
        raise _fail(401, "vendor_unauthorized", "Partner key not recognised.")


def redemption_key(payload: dict) -> str:
    """
    Deterministic per-token timestamp stored as claimed_offers.claimed_at, so a
    second verify of the same token finds the first redemption without a new
    column: issue time + nonce-derived microseconds.
    """
    micros = int.from_bytes(hashlib.sha256(payload["n"].encode()).digest()[:4], "big") % 1_000_000
    ts = datetime.fromtimestamp(int(payload["i"]), tz=timezone.utc) + timedelta(microseconds=micros)
    return ts.isoformat()


# Same-process guard against two concurrent verifies of one token. Cross-process
# races need a unique index (see the report: redemption nonce column + index).
_redeem_lock = asyncio.Lock()


async def _find_redemption(uid: str, kind: str, offer_id: str, key: str) -> Optional[dict]:
    rows = await supabase.select(
        "claimed_offers",
        columns="id,is_used,used_at,claimed_at",
        eq={"user_id": uid, _OFFER_COLUMN[kind]: offer_id, "claimed_at": key},
        limit=1,
    )
    return rows[0] if rows else None


@router.post("/cheripass/verify", dependencies=[Depends(rate_limit("cheripass_verify")), Depends(require_vendor)])
async def cheripass_verify(req: VerifyRequest) -> dict:
    payload = decode_token(req.token)
    uid, kind, offer_id = str(payload["u"]), _CODE_KIND[payload["k"]], str(payload["o"])
    key = redemption_key(payload)

    async with _redeem_lock:
        existing = await _find_redemption(uid, kind, offer_id, key)
        if existing and existing.get("is_used"):
            return {"status": "already_redeemed", "redeemed_at": existing.get("used_at"), "offer_kind": kind, "offer_id": offer_id}

        if int(payload["e"]) <= int(time.time()):
            raise _fail(410, "token_expired", "This pass has expired. Ask the member to reopen their CheriPass.")

        offer, member, tier = await asyncio.gather(_load_offer(kind, offer_id), _load_member(uid), billing.get_membership_tier(uid))
        _check_offer_and_tier(offer, member, tier)

        now = datetime.now(timezone.utc).isoformat()
        if existing:
            await supabase.update("claimed_offers", payload={"is_used": True, "used_at": now}, eq={"id": existing["id"]})
        else:
            await supabase.insert(
                "claimed_offers",
                {"user_id": uid, _OFFER_COLUMN[kind]: offer_id, "claimed_at": key, "is_used": True, "used_at": now},
            )

    logger.info("[cheripass] redeemed kind=%s offer=%s", kind, offer_id)
    return {
        "status": "redeemed",
        "redeemed_at": now,
        "display_name": (member.get("nick_name") or "").strip(),
        "tier": tier,
        "tier_label": TIER_LABEL[tier],
        "offer_kind": kind,
        "offer_id": offer_id,
        "offer_title": offer["_title"],
    }


# --------------------------------------------------------------------------
# POST /analytics/sponsored-click  (10B2.4, anonymous)
# --------------------------------------------------------------------------

class SponsoredClick(BaseModel):
    banner_id: str = Field(min_length=1, max_length=64)
    kind: Literal["web", "whatsapp"]


@router.post("/analytics/sponsored-click", status_code=202, dependencies=[Depends(rate_limit("sponsored_click"))])
async def sponsored_click(req: SponsoredClick) -> dict:
    """
    No auth and no identity on purpose: the row holds only the banner id and
    destination kind (user_id NULL — analytics_events.user_id is nullable).
    Failures are swallowed; tracking must never affect navigation.
    """
    banner_id = _uuid_or_400(req.banner_id, code="invalid_banner")
    try:
        await supabase.insert(
            "analytics_events",
            {"user_id": None, "event_type": "sponsored_click", "metadata": {"banner_id": banner_id, "kind": req.kind}},
        )
    except (RuntimeError, httpx.HTTPError) as e:
        logger.info("[analytics] sponsored_click insert failed: %s", type(e).__name__)
        return {"recorded": False}
    return {"recorded": True}


__all__ = ["router", "issue_token", "decode_token", "redemption_key", "TOKEN_TTL_SECONDS"]
