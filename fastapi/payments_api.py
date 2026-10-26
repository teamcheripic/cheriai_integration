"""
Payment-provider endpoints (documents/08_PAYMENT_PROVIDERS.md).

Member
  GET  /billing/config               active provider + how to pay (never cached by clients)
  POST /billing/checkout             { tier, platform, prefer?, success_url?, cancel_url? }
  POST /billing/portal               { return_url? } → member's OWN provider
Provider
  POST /billing/webhook/{provider}   signature-verified per provider
Admin
  GET  /admin/payments/providers
  PUT  /admin/payments/providers/{id}
  POST /admin/payments/active        { provider, reason }

Contract errors use one envelope:
  {"detail": {"code": "<code>", "message": "<member-safe text>", "errors"?: [...]}}

Rate limits use the shared `rate_limit()` dependency (not @limiter.limit —
that breaks body parsing with postponed annotations).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, NoReturn, Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth import get_current_admin_id, get_current_user_id
from payments import registry
from payments.base import (
    CHECKOUT_MODES,
    PAID_TIERS,
    PLATFORMS,
    PURCHASABLE_TIERS,
    BillingUser,
    NoSubscription,
    Provider,
    ProviderError,
    ProviderSettings,
    WebhookVerificationError,
    is_purchasable,
)
from payments.urls import default_return_urls, safe_return_url
from ratelimit import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()

KNOWN_TIERS = ("basic", "premium-lite", "premium")

MSG_UNAVAILABLE = "Payments are temporarily unavailable. Your membership isn't affected."
MSG_NOT_PURCHASABLE = "This plan isn't available to buy yet."
MSG_NO_SUBSCRIPTION = "You don't have a paid subscription to manage."


def _err(status: int, code: str, message: str, errors: Optional[List[str]] = None) -> NoReturn:
    detail: Dict[str, Any] = {"code": code, "message": message}
    if errors:
        detail["errors"] = errors
    raise HTTPException(status_code=status, detail=detail)


def _unavailable() -> NoReturn:
    _err(503, "provider_unavailable", MSG_UNAVAILABLE)


def _https(url: Optional[str]) -> bool:
    return isinstance(url, str) and url.lower().startswith("https://")


def _ready_for_sale(p: Provider) -> bool:
    return all(p.price_ready(t) for t in PURCHASABLE_TIERS)


def _tiers_block() -> Dict[str, Dict[str, bool]]:
    return {t: {"purchasable": is_purchasable(t)} for t in KNOWN_TIERS}


# =============================================================================
# Member endpoints
# =============================================================================
@router.get("/billing/config", tags=["Billing"], dependencies=[Depends(rate_limit("billing_config"))])
async def billing_config(user_id: str = Depends(get_current_user_id)):
    res = await registry.resolve_active()
    p = res.provider
    if p is None or not _ready_for_sale(p):
        if p is not None:
            logger.warning("[payments] active provider %s has no price for a purchasable tier", p.id)
        return {
            "provider": None,
            "display_name": None,
            "mode": None,
            "publishable_key": None,
            "available": False,
            "message": MSG_UNAVAILABLE,
            "failover": False,
            "tiers": _tiers_block(),
        }
    return {
        "provider": p.id,
        "display_name": p.settings.display_name,
        "mode": p.settings.checkout_mode,  # hosted_redirect | payment_link | sdk
        "publishable_key": p.settings.publishable_key,
        "available": True,
        "message": None,
        "failover": res.failover,
        "tiers": _tiers_block(),
    }


class CheckoutBody(BaseModel):
    tier: str
    platform: str = "web"
    prefer: Optional[str] = None  # "redirect" → hosted page even for sdk-mode providers
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


@router.post("/billing/checkout", tags=["Billing"], dependencies=[Depends(rate_limit("billing_checkout"))])
async def billing_checkout(body: CheckoutBody, user_id: str = Depends(get_current_user_id)):
    if body.tier not in KNOWN_TIERS:
        _err(422, "invalid_tier", "Unknown plan.")
    if not is_purchasable(body.tier):
        _err(409, "tier_not_purchasable", MSG_NOT_PURCHASABLE)
    platform = body.platform if body.platform in PLATFORMS else "web"
    prefer = "redirect" if body.prefer == "redirect" else None

    res = await registry.resolve_active()
    p = res.provider
    if p is None:
        _unavailable()

    d_success, d_cancel = default_return_urls(platform)
    success_url = safe_return_url(body.success_url, "/#/profile?checkout=success") if body.success_url else d_success
    cancel_url = safe_return_url(body.cancel_url, "/#/pricing") if body.cancel_url else d_cancel

    try:
        result = await p.create_checkout(
            BillingUser(id=user_id), body.tier, platform,
            success_url=success_url, cancel_url=cancel_url, prefer=prefer,
        )
    except ProviderError as e:
        logger.warning("[payments] checkout via %s unavailable: %s (%s)", p.id, type(e).__name__, e)
        _unavailable()
    except Exception as e:
        logger.error("[payments] checkout via %s failed: %r", p.id, e, exc_info=True)
        _unavailable()

    if result.mode == "redirect":
        if not _https(result.url):
            logger.error("[payments] %s returned a non-https checkout url", p.id)
            _unavailable()
    elif prefer == "redirect" or result.mode != "sdk":
        _unavailable()
    return result.to_response(p.id)


class PortalBody(BaseModel):
    return_url: Optional[str] = None


async def open_portal(user_id: str, return_url: Optional[str]) -> str:
    """Member's OWN provider portal URL. Raises NoSubscription /
    ProviderError. Used by /billing/portal and the legacy
    /billing/portal-session."""
    m = await registry.get_member_billing(user_id)
    if m is None or not m.customer_id:
        raise NoSubscription("no customer on file")
    p = await registry.get_provider(m.payment_provider)
    if p is None:
        raise ProviderError(f"unknown provider {m.payment_provider}")
    return await p.create_portal(BillingUser(id=user_id), m, return_url=safe_return_url(return_url))


@router.post("/billing/portal", tags=["Billing"], dependencies=[Depends(rate_limit("billing_checkout"))])
async def billing_portal(body: Optional[PortalBody] = None, user_id: str = Depends(get_current_user_id)):
    try:
        url = await open_portal(user_id, body.return_url if body else None)
    except NoSubscription:
        _err(404, "no_subscription", MSG_NO_SUBSCRIPTION)
    except ProviderError as e:
        logger.warning("[payments] portal unavailable: %s (%s)", type(e).__name__, e)
        _unavailable()
    except Exception as e:
        logger.error("[payments] portal failed: %r", e, exc_info=True)
        _unavailable()
    if not _https(url):
        _unavailable()
    return {"url": url}


async def legacy_checkout_session(user_id: str, price_id: str, success_url: str, cancel_url: str) -> str:
    """/billing/create-checkout-session (contract unchanged: Stripe price id
    in, redirect url out). Stripe active → the original Checkout Session
    path; another provider active → the tier behind that price via the
    active provider's hosted page."""
    import billing

    res = await registry.resolve_active()
    p = res.provider
    if p is None or p.id == "stripe":
        return await billing.create_checkout_session(
            user_id=user_id, price_id=price_id, success_url=success_url, cancel_url=cancel_url
        )
    await billing.refresh_price_to_tier_from_db()
    tier = billing._tier_for_price(price_id)
    if not tier or not is_purchasable(tier):
        raise ValueError("price_id is not a purchasable plan")
    result = await p.create_checkout(
        BillingUser(id=user_id), tier, "web", success_url=success_url, cancel_url=cancel_url, prefer="redirect"
    )
    if result.mode != "redirect" or not _https(result.url):
        raise ProviderError("active provider has no hosted checkout")
    return result.url or ""


# =============================================================================
# Webhooks
# =============================================================================
@router.post("/billing/webhook/{provider}", tags=["Billing"])
async def billing_webhook(provider: str, request: Request):
    if registry.provider_class(provider) is None:
        _err(404, "unknown_provider", "Unknown payment provider.")
    p = await registry.get_provider(provider)
    if p is None:
        _err(404, "unknown_provider", "Unknown payment provider.")
    if not p.implemented:
        _err(501, "provider_not_implemented", "This provider isn't supported yet.")
    body = await request.body()
    try:
        return await p.handle_webhook(request.headers, body)
    except WebhookVerificationError as e:
        logger.warning("[payments] %s webhook rejected: %s", provider, e)
        _err(400, "invalid_signature", "Invalid signature.")
    except Exception as e:
        # Idempotency claim already released → non-2xx makes the provider retry.
        logger.error("[payments] %s webhook error: %r", provider, e, exc_info=True)
        _err(500, "webhook_processing_failed", "Webhook processing failed.")


# =============================================================================
# Admin
# =============================================================================
_PG = registry.PG_BASE
_HEADERS = registry._HEADERS
_SECRET_RE = re.compile(r"^(sk|rk)_|^whsec_", re.IGNORECASE)
_actor_cache: Dict[str, tuple] = {}


def _provider_out(p: Provider, counts: Dict[str, int]) -> Dict[str, Any]:
    s = p.settings
    return {
        "id": s.id,
        "display_name": s.display_name,
        "enabled": s.enabled,
        "is_active": s.is_active,
        "checkout_mode": s.checkout_mode,
        "publishable_key": s.publishable_key,
        "config": s.config or {},
        "priority": s.priority,
        "secret_configured": p.is_configured(),
        "webhook_secret_configured": p.webhook_configured(),
        "implemented": p.implemented,
        "unusable_reason": registry.unusable_reason(p),
        "updated_at": s.updated_at,
        "updated_by": s.updated_by,
        "subscriber_count": counts.get(s.id, 0),
        "prices": [
            {k: v for k, v in pr.to_dict().items()}
            for pr in sorted(s.prices.values(), key=lambda x: PAID_TIERS.index(x.tier) if x.tier in PAID_TIERS else 99)
        ],
    }


async def _actor_email(client: httpx.AsyncClient, uid: Optional[str]) -> Optional[str]:
    if not uid:
        return None
    hit = _actor_cache.get(uid)
    if hit and time.monotonic() - hit[1] < 600:
        return hit[0]
    email = None
    try:
        r = await client.get(f"{registry.SUPABASE_URL}/auth/v1/admin/users/{uid}", headers=_HEADERS)
        if r.status_code < 300:
            email = (r.json() or {}).get("email")
    except Exception:
        email = None
    _actor_cache[uid] = (email, time.monotonic())
    return email


async def _events(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    try:
        r = await client.get(
            f"{_PG}/payment_provider_events",
            params={"select": "id,from_provider,to_provider,changed_by,reason,created_at",
                    "order": "created_at.desc", "limit": "50"},
            headers=_HEADERS,
        )
        if r.status_code >= 300:
            return []
        rows = r.json() or []
    except Exception:
        return []
    out = []
    for e in rows:
        out.append({
            "id": e.get("id"),
            "from_provider": e.get("from_provider"),
            "to_provider": e.get("to_provider"),
            "reason": e.get("reason"),
            "created_at": e.get("created_at"),
            "changed_by": e.get("changed_by"),
            "actor_email": await _actor_email(client, e.get("changed_by")),
        })
    return out


async def _subscriber_counts(client: httpx.AsyncClient) -> Dict[str, int]:
    """Active paid memberships per provider (they stay on their provider)."""
    params = {"select": "payment_provider", "is_active": "eq.true", "tier": "neq.basic"}
    try:
        r = await client.get(f"{_PG}/user_memberships", params=params, headers=_HEADERS)
        if r.status_code == 400:  # pre-054: no payment_provider column → all Stripe
            r = await client.get(f"{_PG}/user_memberships",
                                 params={"select": "tier", "is_active": "eq.true", "tier": "neq.basic"},
                                 headers=_HEADERS)
        if r.status_code >= 300:
            return {}
        counts: Dict[str, int] = {}
        for row in r.json() or []:
            pid = row.get("payment_provider") or "stripe"
            counts[pid] = counts.get(pid, 0) + 1
        return counts
    except Exception:
        return {}


@router.get("/admin/payments/providers", tags=["Admin"], dependencies=[Depends(rate_limit("admin_payments"))])
async def admin_list_providers(admin_id: str = Depends(get_current_admin_id)):
    snap = await registry.load_snapshot(force=True)
    res = await registry.resolve_active()
    async with httpx.AsyncClient(timeout=10.0) as client:
        counts = await _subscriber_counts(client)
        events = await _events(client) if snap.source == "db" else []
    return {
        "providers": [_provider_out(registry.build(s), counts) for s in snap.providers],
        "events": events,
        "subscriber_counts": counts,
        "payments_auto_failover": await registry.auto_failover_enabled(),
        "resolved_provider": res.provider.id if res.provider else None,
        "failover": res.failover,
        "source": snap.source,  # "fallback" = migration 054 not applied (read-only)
    }


class PriceIn(BaseModel):
    tier: str
    price_ref: Optional[str] = None
    payment_link: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    active: bool = True


class ProviderUpdate(BaseModel):
    enabled: Optional[bool] = None
    checkout_mode: Optional[str] = None
    publishable_key: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    priority: Optional[int] = None
    display_name: Optional[str] = None
    prices: Optional[Union[List[PriceIn], Dict[str, Dict[str, Any]]]] = None


def _norm_prices(raw: Any) -> List[PriceIn]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [PriceIn(tier=t, **{k: v for k, v in (v or {}).items() if k != "tier"}) for t, v in raw.items()]
    return list(raw)


def _looks_secret(v: Any) -> bool:
    return isinstance(v, str) and bool(_SECRET_RE.match(v.strip()))


def _config_has_secret(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any(_config_has_secret(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_config_has_secret(v) for v in obj)
    return _looks_secret(obj)


def _tables_missing() -> NoReturn:
    _err(503, "payments_tables_missing",
         "Payment provider settings need migration 054_payment_providers.sql to be applied.")


@router.put("/admin/payments/providers/{provider_id}", tags=["Admin"], dependencies=[Depends(rate_limit("admin_payments"))])
async def admin_update_provider(provider_id: str, body: ProviderUpdate, admin_id: str = Depends(get_current_admin_id)):
    snap = await registry.load_snapshot(force=True)
    if snap.source != "db":
        _tables_missing()
    current = snap.get(provider_id)
    if current is None:
        _err(404, "provider_not_found", "Unknown payment provider.")

    fields = body.model_fields_set
    errors: List[str] = []
    secret = False
    patch: Dict[str, Any] = {}

    if "enabled" in fields and body.enabled is not None:
        if body.enabled is False and current.is_active:
            _err(409, "cannot_disable_active", "Make another provider active before disabling this one.",
                 ["This provider is active. Switch the active provider first."])
        patch["enabled"] = body.enabled
    if "checkout_mode" in fields and body.checkout_mode is not None:
        if body.checkout_mode not in CHECKOUT_MODES:
            errors.append(f"checkout_mode must be one of {', '.join(CHECKOUT_MODES)}.")
        patch["checkout_mode"] = body.checkout_mode
    if "publishable_key" in fields:
        key = (body.publishable_key or "").strip() or None
        if key and _looks_secret(key):
            secret = True
            errors.append("That looks like a SECRET key. Only publishable keys belong here; secrets go in the backend environment.")
        patch["publishable_key"] = key
    if "config" in fields and body.config is not None:
        if _config_has_secret(body.config):
            secret = True
            errors.append("Config contains a value that looks like a secret key.")
        patch["config"] = body.config
    if "priority" in fields and body.priority is not None:
        if not 0 <= body.priority <= 1000:
            errors.append("priority must be between 0 and 1000.")
        patch["priority"] = body.priority
    if "display_name" in fields and body.display_name:
        patch["display_name"] = body.display_name.strip()[:80]

    price_rows: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    for pr in _norm_prices(body.prices if "prices" in fields else None):
        if pr.tier not in PAID_TIERS:
            errors.append(f"Unknown tier '{pr.tier}'.")
            continue
        link = (pr.payment_link or "").strip() or None
        ref = (pr.price_ref or "").strip() or None
        cur = (pr.currency or "").strip().lower() or (current.config or {}).get("currency") or "usd"
        if link and not link.lower().startswith("https://"):
            errors.append(f"{pr.tier}: payment link must start with https://")
        if not re.fullmatch(r"[a-z]{3}", cur):
            errors.append(f"{pr.tier}: currency must be a 3-letter ISO code.")
        if pr.amount is not None and pr.amount < 0:
            errors.append(f"{pr.tier}: amount must be non-negative.")
        if _looks_secret(ref):
            secret = True
            errors.append(f"{pr.tier}: price reference looks like a secret key.")
        price_rows.append({
            "provider_id": provider_id, "tier": pr.tier, "price_ref": ref, "payment_link": link,
            "currency": cur, "amount": pr.amount, "active": pr.active, "updated_at": now,
        })

    if errors:
        if secret:
            _err(422, "secret_not_allowed", "Secret keys can't be stored in provider settings.", errors)
        _err(422, "invalid_settings", "Some provider settings are invalid.", errors)

    async with httpx.AsyncClient(timeout=10.0) as client:
        if patch:
            patch.update({"updated_at": now, "updated_by": admin_id})
            r = await client.patch(f"{_PG}/payment_providers", params={"id": f"eq.{provider_id}"},
                                   json=patch, headers={**_HEADERS, "Prefer": "return=minimal"})
            if r.status_code >= 300:
                logger.error("[payments] provider update failed [%s]: %s", r.status_code, r.text[:300])
                _err(502, "update_failed", "Could not save provider settings.")
        if price_rows:
            r = await client.post(
                f"{_PG}/payment_provider_prices", params={"on_conflict": "provider_id,tier"}, json=price_rows,
                headers={**_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            if r.status_code >= 300:
                logger.error("[payments] price upsert failed [%s]: %s", r.status_code, r.text[:300])
                _err(502, "update_failed", "Could not save provider prices.")

    registry.invalidate_cache()
    snap = await registry.load_snapshot(force=True)
    updated = snap.get(provider_id) or current
    async with httpx.AsyncClient(timeout=10.0) as client:
        counts = await _subscriber_counts(client)
    return {"provider": _provider_out(registry.build(updated), counts)}


class SwitchBody(BaseModel):
    provider: str
    reason: str = ""


def switch_refusals(p: Provider) -> List[tuple]:
    """[(code, message)] reasons `p` can't become active."""
    out = []
    if not p.settings.enabled:
        out.append(("provider_disabled", "The provider is disabled. Enable it first."))
    if not p.implemented:
        out.append(("provider_not_implemented", "This provider's integration isn't built yet."))
    if not p.is_configured():
        envs = ", ".join(p.secret_env) or "secret"
        out.append(("secret_missing", f"Its secret key ({envs}) is not configured in the backend environment."))
    missing = [t for t in PURCHASABLE_TIERS if not p.price_ready(t)]
    if missing:
        out.append(("prices_missing",
                    f"Missing {p.settings.checkout_mode} price for: {', '.join(missing)}."))
    return out


async def _switch(client: httpx.AsyncClient, target: str, previous: Optional[str], admin_id: str, reason: str) -> bool:
    r = await client.post(f"{_PG}/rpc/set_active_payment_provider",
                          json={"p_provider": target, "p_admin": admin_id, "p_reason": reason}, headers=_HEADERS)
    if r.status_code < 300:
        data = r.json() if r.content else {}
        return bool((data or {}).get("changed", True))
    code = None
    try:
        code = (r.json() or {}).get("code")
    except Exception:
        pass
    if not (r.status_code == 404 or code == "PGRST202"):
        logger.error("[payments] set_active_payment_provider failed [%s]: %s", r.status_code, r.text[:300])
        _err(502, "switch_failed", "Could not switch the active provider.")

    # RPC not deployed: two-step flip (partial unique index needs off → on).
    now = datetime.now(timezone.utc).isoformat()
    h = {**_HEADERS, "Prefer": "return=minimal"}
    r1 = await client.patch(f"{_PG}/payment_providers", params={"is_active": "eq.true"},
                            json={"is_active": False, "updated_at": now, "updated_by": admin_id}, headers=h)
    r2 = await client.patch(f"{_PG}/payment_providers", params={"id": f"eq.{target}"},
                            json={"is_active": True, "updated_at": now, "updated_by": admin_id}, headers=h)
    if r1.status_code >= 300 or r2.status_code >= 300:
        if previous:
            await client.patch(f"{_PG}/payment_providers", params={"id": f"eq.{previous}"},
                               json={"is_active": True}, headers=h)
        _err(502, "switch_failed", "Could not switch the active provider.")
    await client.post(f"{_PG}/payment_provider_events",
                      json={"from_provider": previous, "to_provider": target, "changed_by": admin_id, "reason": reason},
                      headers=h)
    return True


@router.post("/admin/payments/active", tags=["Admin"], dependencies=[Depends(rate_limit("admin_payments"))])
async def admin_set_active(body: SwitchBody, admin_id: str = Depends(get_current_admin_id)):
    reason = (body.reason or "").strip()
    if len(reason) < 3:
        _err(422, "reason_required", "Give a reason for the switch.", ["A reason (at least 3 characters) is required."])
    snap = await registry.load_snapshot(force=True)
    if snap.source != "db":
        _tables_missing()
    target = snap.get(body.provider)
    if target is None:
        _err(404, "provider_not_found", "Unknown payment provider.")
    previous = next((s.id for s in snap.providers if s.is_active), None)
    if previous == target.id:
        return {"changed": False, "active": target.id, "from": previous, "to": target.id}

    refusals = switch_refusals(registry.build(target))
    if refusals:
        _err(409, refusals[0][0], "This provider can't be made active yet.", [m for _, m in refusals])

    async with httpx.AsyncClient(timeout=10.0) as client:
        changed = await _switch(client, target.id, previous, admin_id, reason[:500])
    registry.invalidate_cache()
    logger.warning("[payments] active provider switched %s → %s by admin", previous, target.id)
    return {"changed": changed, "active": target.id, "from": previous, "to": target.id}


# Re-exported for main.py's legacy endpoints and tests.
__all__ = ["router", "open_portal", "legacy_checkout_session", "switch_refusals", "ProviderSettings"]
