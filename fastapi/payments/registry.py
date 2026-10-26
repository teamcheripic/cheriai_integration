"""
Provider registry: loads `payment_providers` + `payment_provider_prices`
with the service key (60 s cache, invalidated by the admin endpoints),
resolves the active provider with optional auto-failover, and falls back to
Stripe env/tier_config values while migration 054 isn't applied.

Resolution (resolve_active):
  1. The row with is_active=true, if usable (enabled + implemented + secret
     configured).
  2. Otherwise, when app_config.payments_auto_failover is true, the next
     usable provider by priority.
  3. Otherwise none → /billing/config says available:false.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

import httpx

from payments.base import (
    PURCHASABLE_TIERS,
    Membership,
    Provider,
    ProviderPrice,
    ProviderSettings,
)
from payments._placeholder import PlaceholderProvider
from payments.app_store import AppStoreProvider
from payments.checkout_com import CheckoutComProvider
from payments.paddle import PaddleProvider
from payments.paypal import PayPalProvider
from payments.razorpay import RazorpayProvider
from payments.stripe_provider import StripeProvider
from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

PG_BASE = f"{SUPABASE_URL}/rest/v1"
_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

CACHE_TTL_SECONDS = 60.0

PROVIDER_CLASSES: Dict[str, Type[Provider]] = {
    "stripe": StripeProvider,
    "paypal": PayPalProvider,
    "paddle": PaddleProvider,
    "razorpay": RazorpayProvider,
    "checkout_com": CheckoutComProvider,
    "app_store": AppStoreProvider,
}

# Default rows used when the tables don't exist yet (mirrors the 054 seed).
_FALLBACK_ROWS = [
    ("stripe", "Stripe", 1, "payment_link", "usd"),
    ("paypal", "PayPal", 2, "hosted_redirect", "usd"),
    ("paddle", "Paddle", 3, "hosted_redirect", "usd"),
    ("razorpay", "Razorpay", 4, "hosted_redirect", "inr"),
    ("checkout_com", "Checkout.com", 5, "hosted_redirect", "usd"),
    ("app_store", "App Store / Google Play (RevenueCat)", 6, "sdk", None),
]


class TablesMissing(Exception):
    """payment_providers / payment_provider_prices aren't deployed (054)."""


@dataclass
class Snapshot:
    providers: List[ProviderSettings]
    source: str  # "db" | "fallback"

    def get(self, provider_id: str) -> Optional[ProviderSettings]:
        for p in self.providers:
            if p.id == provider_id:
                return p
        return None


@dataclass
class Resolution:
    provider: Optional[Provider]
    configured_active: Optional[str]  # the row marked is_active (may be unusable)
    failover: bool = False
    source: str = "db"
    reasons: Dict[str, str] = field(default_factory=dict)


_cache: Optional[Snapshot] = None
_cache_at: float = 0.0
_lock = asyncio.Lock()


def invalidate_cache() -> None:
    global _cache_at
    _cache_at = 0.0


def _missing_table(resp: httpx.Response) -> bool:
    if resp.status_code == 404:
        return True
    try:
        code = (resp.json() or {}).get("code")
    except Exception:
        code = None
    return code in ("42P01", "PGRST205")


async def _get(client: httpx.AsyncClient, table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    resp = await client.get(f"{PG_BASE}/{table}", params=params, headers=_HEADERS)
    if resp.status_code >= 300:
        if _missing_table(resp):
            raise TablesMissing(table)
        resp.raise_for_status()
    return resp.json() or []


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _settings_from_rows(rows: List[Dict[str, Any]], price_rows: List[Dict[str, Any]]) -> List[ProviderSettings]:
    prices: Dict[str, Dict[str, ProviderPrice]] = {}
    for r in price_rows:
        pid, tier = r.get("provider_id"), r.get("tier")
        if not pid or not tier:
            continue
        prices.setdefault(pid, {})[tier] = ProviderPrice(
            tier=tier,
            price_ref=r.get("price_ref"),
            payment_link=r.get("payment_link"),
            currency=r.get("currency") or "usd",
            amount=_num(r.get("amount")),
            active=bool(r.get("active", True)),
        )
    out = []
    for r in rows:
        if not r.get("id"):
            continue
        out.append(
            ProviderSettings(
                id=r["id"],
                display_name=r.get("display_name") or r["id"],
                enabled=bool(r.get("enabled")),
                is_active=bool(r.get("is_active")),
                checkout_mode=r.get("checkout_mode") or "hosted_redirect",
                publishable_key=r.get("publishable_key"),
                config=r.get("config") or {},
                priority=int(r.get("priority") if r.get("priority") is not None else 100),
                prices=prices.get(r["id"], {}),
                updated_at=r.get("updated_at"),
                updated_by=r.get("updated_by"),
            )
        )
    out.sort(key=lambda s: (s.priority, s.id))
    return out


async def _fallback_snapshot(client: Optional[httpx.AsyncClient] = None) -> Snapshot:
    """Stripe-only view built from env + tier_config (pre-054 production)."""
    stripe_prices: Dict[str, ProviderPrice] = {}
    try:
        async def _load(c: httpx.AsyncClient) -> List[Dict[str, Any]]:
            r = await c.get(
                f"{PG_BASE}/tier_config",
                params={"select": "tier,stripe_price_id,stripe_payment_link,monthly_price_usd"},
                headers=_HEADERS,
            )
            r.raise_for_status()
            return r.json() or []

        if client is not None:
            rows = await _load(client)
        else:
            async with httpx.AsyncClient(timeout=10.0) as c:
                rows = await _load(c)
        for r in rows:
            tier = r.get("tier")
            if tier not in PURCHASABLE_TIERS:
                continue
            link = r.get("stripe_payment_link")
            stripe_prices[tier] = ProviderPrice(
                tier=tier,
                price_ref=r.get("stripe_price_id"),
                payment_link=link if isinstance(link, str) and link.startswith("https://") else None,
                currency="usd",
                amount=_num(r.get("monthly_price_usd")),
            )
    except Exception as e:
        logger.warning("[payments] tier_config read failed in fallback: %r", e)

    mode = (os.getenv("STRIPE_CHECKOUT_MODE") or "payment_link").strip()
    if mode not in ("payment_link", "hosted_redirect", "sdk"):
        mode = "payment_link"
    providers = []
    for pid, name, prio, default_mode, currency in _FALLBACK_ROWS:
        is_stripe = pid == "stripe"
        providers.append(
            ProviderSettings(
                id=pid,
                display_name=name,
                enabled=is_stripe,
                is_active=is_stripe,
                checkout_mode=mode if is_stripe else default_mode,
                publishable_key=(os.getenv("STRIPE_PUBLISHABLE_KEY") or None) if is_stripe else None,
                config={"currency": currency} if currency else {},
                priority=prio,
                prices=stripe_prices if is_stripe else {},
            )
        )
    return Snapshot(providers=providers, source="fallback")


async def _fetch() -> Snapshot:
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            rows = await _get(client, "payment_providers", {"select": "*", "order": "priority.asc"})
            price_rows = await _get(client, "payment_provider_prices", {"select": "*"})
        except TablesMissing:
            return await _fallback_snapshot(client)
    return Snapshot(providers=_settings_from_rows(rows, price_rows), source="db")


async def load_snapshot(force: bool = False) -> Snapshot:
    global _cache, _cache_at
    if not force and _cache is not None and time.monotonic() - _cache_at < CACHE_TTL_SECONDS:
        return _cache
    async with _lock:
        if not force and _cache is not None and time.monotonic() - _cache_at < CACHE_TTL_SECONDS:
            return _cache
        try:
            snap = await _fetch()
        except Exception as e:
            # DB blip: keep serving the last good snapshot; with none, use
            # the Stripe fallback so checkout keeps working.
            logger.warning("[payments] provider load failed: %r", e)
            snap = _cache if _cache is not None else await _fallback_snapshot()
        _cache, _cache_at = snap, time.monotonic()
        return snap


def build(settings: ProviderSettings) -> Provider:
    cls = PROVIDER_CLASSES.get(settings.id)
    if cls is None:
        # A row the code doesn't know yet: treat as an unimplemented placeholder.
        cls = type(f"Unknown_{settings.id}", (PlaceholderProvider,), {"id": settings.id, "display_name": settings.display_name})
    return cls(settings)


def provider_class(provider_id: str) -> Optional[Type[Provider]]:
    return PROVIDER_CLASSES.get(provider_id)


async def get_provider(provider_id: str) -> Optional[Provider]:
    snap = await load_snapshot()
    s = snap.get(provider_id)
    if s is None:
        cls = PROVIDER_CLASSES.get(provider_id)
        if cls is None:
            return None
        s = ProviderSettings(id=provider_id, display_name=cls.display_name or provider_id)
    return build(s)


def unusable_reason(p: Provider) -> Optional[str]:
    if not p.settings.enabled:
        return "disabled"
    if not p.implemented:
        return "not_implemented"
    if not p.is_configured():
        return "secret_missing"
    return None


async def auto_failover_enabled() -> bool:
    from app_config import get_bool

    try:
        return await get_bool("payments_auto_failover", default=False)
    except Exception:
        return False


async def resolve_active() -> Resolution:
    snap = await load_snapshot()
    providers = [build(s) for s in snap.providers]
    active = next((p for p in providers if p.settings.is_active), None)
    reasons: Dict[str, str] = {}
    if active is not None:
        why = unusable_reason(active)
        if why is None:
            return Resolution(provider=active, configured_active=active.id, source=snap.source)
        reasons[active.id] = why
    if await auto_failover_enabled():
        for p in providers:  # already sorted by priority
            if active is not None and p.id == active.id:
                continue
            why = unusable_reason(p)
            if why is None:
                logger.warning(
                    "[payments] failover: active=%s unusable (%s) → using %s",
                    active.id if active else None, reasons.get(active.id) if active else "none", p.id,
                )
                return Resolution(provider=p, configured_active=active.id if active else None,
                                  failover=True, source=snap.source, reasons=reasons)
    return Resolution(provider=None, configured_active=active.id if active else None,
                      source=snap.source, reasons=reasons)


# -----------------------------------------------------------------------------
# Member's own provider (portal / existing subscribers)
# -----------------------------------------------------------------------------
_MEMBER_COLS = "tier,payment_provider,provider_customer_id,provider_subscription_id,stripe_customer_id,stripe_subscription_id"
_MEMBER_COLS_LEGACY = "tier,stripe_customer_id,stripe_subscription_id"


async def get_member_billing(user_id: str) -> Optional[Membership]:
    """The member's active membership, provider-normalised; None when there
    is no active row. Pre-054 (no provider columns) → provider 'stripe'."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        rows = None
        for cols in (_MEMBER_COLS, _MEMBER_COLS_LEGACY):
            resp = await client.get(
                f"{PG_BASE}/user_memberships",
                params={"select": cols, "user_id": f"eq.{user_id}", "is_active": "eq.true", "limit": "1"},
                headers=_HEADERS,
            )
            if resp.status_code < 300:
                rows = resp.json() or []
                break
            if resp.status_code != 400:  # 400 = unknown column (42703) → legacy select
                resp.raise_for_status()
        if rows is None:
            resp.raise_for_status()
    if not rows:
        return None
    r = rows[0]
    provider = r.get("payment_provider") or "stripe"
    return Membership(
        user_id=user_id,
        tier=r.get("tier") or "basic",
        payment_provider=provider,
        customer_id=r.get("provider_customer_id") or (r.get("stripe_customer_id") if provider == "stripe" else None),
        subscription_id=r.get("provider_subscription_id") or (r.get("stripe_subscription_id") if provider == "stripe" else None),
    )
