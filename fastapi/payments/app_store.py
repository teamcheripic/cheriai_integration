"""
App Store / Google Play provider, through RevenueCat.

Store rules: Apple (App Review Guideline 3.1.1) and Google Play (Payments
policy) require their own in-app purchase for digital subscriptions bought
inside a native app. This provider is therefore the ONLY one a native client
(platform ios/android) is ever routed to (registry.resolve_for_platform), and
it never sells on the web.

Env (backend only, never in the DB):
  REVENUECAT_SECRET        RevenueCat secret API key (v1, "sk_…")
  REVENUECAT_WEBHOOK_AUTH  the exact Authorization value set on the
                           RevenueCat webhook (Project → Integrations →
                           Webhooks); compared in constant time.

DB settings (payment_providers row "app_store", migration 061):
  checkout_mode  "sdk"
  config         {
                   "offering": "default",               # RevenueCat offering id
                   "entitlement_tiers": {"premium_lite": "premium-lite",
                                         "premium": "premium"},
                   "products": {"premium-lite": {"ios": "<App Store product id>",
                                                 "android": "<Play subscription id>:<base plan id>"}},
                   "accept_sandbox": true               # App Review purchases are sandbox
                 }
  prices         price_ref = the RevenueCat ENTITLEMENT id for the tier.

Identity: the app calls Purchases.logIn(<Supabase user id>), so RevenueCat's
app_user_id IS our user id. Anonymous ids ($RCAnonymousID:…) never change a
membership.

Truth: every state change is re-read from RevenueCat's REST API
(GET /v1/subscribers/{id}) before it is applied — the webhook is only a
trigger, so a forged/replayed/out-of-order body can't grant or remove
access. The same sync runs right after a purchase/restore in the app
(POST /billing/app-store/sync) so the member doesn't wait for the webhook.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

import httpx

from payments.base import (
    EVENT_IGNORED,
    EVENT_PAYMENT_FAILED,
    EVENT_SUBSCRIPTION_ACTIVE,
    EVENT_SUBSCRIPTION_CANCELLED,
    BillingUser,
    CheckoutResult,
    Membership,
    NormalizedEvent,
    PAID_TIERS,
    Provider,
    ProviderUnavailable,
    WebhookVerificationError,
)

logger = logging.getLogger(__name__)

RC_API = "https://api.revenuecat.com/v1"
NATIVE_PLATFORMS = ("ios", "android")

APPLE_MANAGE_URL = "https://apps.apple.com/account/subscriptions"
GOOGLE_MANAGE_URL = "https://play.google.com/store/account/subscriptions"

DEFAULT_ENTITLEMENT_TIERS = {"premium_lite": "premium-lite", "premium": "premium"}
# Higher wins when a member somehow holds two entitlements.
TIER_RANK = {"basic": 0, "premium-lite": 1, "premium": 2}

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Webhook types that only need a state re-read (the API decides the tier).
_STATE_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "PRODUCT_CHANGE",
    "CANCELLATION",
    "UNCANCELLATION",
    "EXPIRATION",
    "SUBSCRIPTION_PAUSED",
    "SUBSCRIPTION_EXTENDED",
    "TEMPORARY_ENTITLEMENT_GRANT",
    "REFUND_REVERSED",
    "NON_RENEWING_PURCHASE",
}


def is_member_id(v: Any) -> bool:
    return isinstance(v, str) and bool(_UUID.match(v))


def _parse_ts(v: Any) -> Optional[datetime]:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


class AppStoreProvider(Provider):
    id = "app_store"
    display_name = "App Store / Google Play (RevenueCat)"
    secret_env = ("REVENUECAT_SECRET",)
    webhook_secret_env = ("REVENUECAT_WEBHOOK_AUTH",)
    implemented = True

    # -- settings helpers --------------------------------------------------
    @property
    def _config(self) -> Dict[str, Any]:
        return self.settings.config or {}

    def entitlement_tiers(self) -> Dict[str, str]:
        """entitlement id → tier: config first, then the price rows."""
        out = dict(DEFAULT_ENTITLEMENT_TIERS)
        raw = self._config.get("entitlement_tiers")
        if isinstance(raw, dict):
            out.update({str(k): str(v) for k, v in raw.items() if v in PAID_TIERS})
        for tier, p in self.settings.prices.items():
            if p.active and p.price_ref and tier in PAID_TIERS:
                out[p.price_ref] = tier
        return out

    def product_for(self, tier: str, platform: str) -> Optional[str]:
        products = self._config.get("products")
        if not isinstance(products, dict):
            return None
        per_tier = products.get(tier)
        if not isinstance(per_tier, dict):
            return None
        v = per_tier.get(platform)
        return v.strip() if isinstance(v, str) and v.strip() else None

    def price_ready(self, tier: str, mode: Optional[str] = None) -> bool:
        p = self.settings.price(tier)
        if p is None or not p.price_ref:
            return False
        return all(self.product_for(tier, pl) for pl in NATIVE_PLATFORMS)

    # -- operations --------------------------------------------------------
    async def create_checkout(
        self,
        user: BillingUser,
        tier: str,
        platform: str,
        *,
        success_url: str,
        cancel_url: str,
        prefer: Optional[str] = None,
    ) -> CheckoutResult:
        # Store purchases happen only inside the native app: no hosted page.
        if platform not in NATIVE_PLATFORMS or prefer == "redirect":
            raise ProviderUnavailable("app_store sells in the native apps only")
        price = self.settings.price(tier)
        product = self.product_for(tier, platform)
        if price is None or not price.price_ref or not product:
            raise ProviderUnavailable(f"app_store: no {platform} product for {tier}")
        return CheckoutResult(
            mode="sdk",
            params={
                "app_user_id": user.id,
                "offering": self._config.get("offering") or None,
                "product_id": product,
                "entitlement": price.price_ref,
                "tier": tier,
            },
        )

    async def create_portal(self, user: BillingUser, membership: Membership, *, return_url: str) -> str:
        """The store's own subscription page (RevenueCat's management_url
        points at the store that actually bills the member)."""
        try:
            sub = await self.fetch_subscriber(membership.user_id)
        except Exception as e:
            logger.warning("[app_store] subscriber lookup for portal failed: %r", e)
            sub = None
        url = (sub or {}).get("management_url")
        if isinstance(url, str) and url.startswith("https://"):
            return url
        store = _latest_store(sub or {})
        return GOOGLE_MANAGE_URL if store == "PLAY_STORE" else APPLE_MANAGE_URL

    # -- RevenueCat REST ---------------------------------------------------
    async def fetch_subscriber(self, app_user_id: str) -> Dict[str, Any]:
        secret = (os.getenv("REVENUECAT_SECRET") or "").strip()
        if not secret:
            raise ProviderUnavailable("REVENUECAT_SECRET missing")
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{RC_API}/subscribers/{quote(app_user_id, safe='')}",
                headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
            )
        if r.status_code >= 300:
            raise ProviderUnavailable(f"RevenueCat subscriber lookup failed ({r.status_code})")
        return (r.json() or {}).get("subscriber") or {}

    def state_from_subscriber(self, user_id: str, sub: Mapping[str, Any], *, now: Optional[datetime] = None) -> NormalizedEvent:
        """The member's current tier as a NormalizedEvent (active or
        cancelled), built only from RevenueCat's own record."""
        now = now or datetime.now(timezone.utc)
        accept_sandbox = self._config.get("accept_sandbox", True) is not False
        tiers = self.entitlement_tiers()
        subscriptions = sub.get("subscriptions") or {}

        best: Optional[Tuple[str, str, Dict[str, Any]]] = None  # (tier, product, sub row)
        for ent_id, ent in (sub.get("entitlements") or {}).items():
            tier = tiers.get(ent_id)
            if not tier or not isinstance(ent, dict):
                continue
            expires = _parse_ts(ent.get("expires_date"))
            grace = _parse_ts(ent.get("grace_period_expires_date"))
            # expires_date null = lifetime/non-expiring grant.
            if expires is not None and expires <= now and not (grace and grace > now):
                continue
            product = ent.get("product_identifier") or ""
            row = subscriptions.get(product) or {}
            if row.get("refunded_at"):
                continue
            if row.get("is_sandbox") and not accept_sandbox:
                continue
            if best is None or TIER_RANK[tier] > TIER_RANK[best[0]]:
                best = (tier, product, row)

        created = int(now.timestamp())
        event_id = f"sync:{user_id}:{time.time_ns()}"
        if best is None:
            return NormalizedEvent(
                provider=self.id, event_id=event_id, kind=EVENT_SUBSCRIPTION_CANCELLED,
                user_id=user_id, customer_id=user_id, subscription_id=None, created=created, status="expired",
            )
        tier, product, row = best
        billing_issue = bool(row.get("billing_issues_detected_at"))
        return NormalizedEvent(
            provider=self.id,
            event_id=event_id,
            kind=EVENT_SUBSCRIPTION_ACTIVE,
            user_id=user_id,
            tier=tier,
            customer_id=user_id,
            # Per member + product: unique per row, and a superseded product
            # (Lite → Premium) can never downgrade the new one.
            subscription_id=f"{user_id}:{product}",
            period_start=_parse_ts(row.get("purchase_date")),
            period_end=_parse_ts(row.get("expires_date")),
            status="past_due" if billing_issue else "active",
            created=created,
            cancel_at_period_end=bool(row.get("unsubscribe_detected_at")),
        )

    async def sync_member(self, user_id: str) -> Dict[str, Any]:
        """Re-read the member from RevenueCat and apply it. Returns the
        resulting tier. Never downgrades a member billed by ANOTHER provider
        (e.g. a Stripe web subscriber who opens the app)."""
        from payments.normalized import apply_normalized_event
        from payments.registry import get_member_billing

        if not is_member_id(user_id):
            return {"user_id": user_id, "applied": False, "reason": "not_a_member_id"}
        sub = await self.fetch_subscriber(user_id)
        ev = self.state_from_subscriber(user_id, sub)
        current = await get_member_billing(user_id)
        if ev.kind == EVENT_SUBSCRIPTION_CANCELLED:
            if current is None or current.tier == "basic" or current.payment_provider != self.id:
                return {"user_id": user_id, "tier": current.tier if current else "basic", "applied": False}
        elif current is not None and current.payment_provider != self.id and current.tier != "basic":
            # Paid elsewhere too: keep the higher tier, never two writers.
            if TIER_RANK.get(current.tier, 0) >= TIER_RANK[ev.tier or "basic"]:
                logger.warning("[app_store] %s also pays via %s; keeping that membership", user_id, current.payment_provider)
                return {"user_id": user_id, "tier": current.tier, "applied": False}
        await apply_normalized_event(ev)
        return {"user_id": user_id, "tier": ev.tier if ev.kind == EVENT_SUBSCRIPTION_ACTIVE else "basic", "applied": True}

    # -- webhooks ----------------------------------------------------------
    def _check_auth(self, headers: Mapping[str, str]) -> None:
        expected = (os.getenv("REVENUECAT_WEBHOOK_AUTH") or "").strip()
        got = ""
        for k, v in headers.items():
            if k.lower() == "authorization":
                got = (v or "").strip()
                break
        if not expected:
            raise WebhookVerificationError("REVENUECAT_WEBHOOK_AUTH not configured")
        candidates = {got, got[7:].strip() if got.lower().startswith("bearer ") else got}
        if not any(hmac.compare_digest(c.encode(), expected.encode()) for c in candidates if c):
            raise WebhookVerificationError("bad Authorization")

    @staticmethod
    def _parse_body(body: bytes) -> Dict[str, Any]:
        try:
            data = json.loads(body or b"{}")
        except ValueError as e:
            raise WebhookVerificationError("invalid JSON") from e
        ev = data.get("event") if isinstance(data, dict) else None
        if not isinstance(ev, dict):
            raise WebhookVerificationError("missing event")
        return ev

    @staticmethod
    def member_ids(ev: Mapping[str, Any]) -> List[str]:
        """Our user ids named by the event (app_user_id, original id, aliases)."""
        out: List[str] = []
        for v in [ev.get("app_user_id"), ev.get("original_app_user_id"), *(ev.get("aliases") or [])]:
            if is_member_id(v) and v not in out:
                out.append(v)
        return out

    async def verify_and_parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Optional[NormalizedEvent]:
        """Only BILLING_ISSUE is turned into an event here (payment_failed
        email). State changes go through handle_webhook → sync_member."""
        self._check_auth(headers)
        ev = self._parse_body(body)
        if ev.get("type") != "BILLING_ISSUE":
            return None
        ids = self.member_ids(ev)
        if not ids:
            return None
        product = ev.get("product_id") or ""
        return NormalizedEvent(
            provider=self.id,
            event_id=str(ev.get("id") or f"billing_issue:{ids[0]}:{ev.get('event_timestamp_ms')}"),
            kind=EVENT_PAYMENT_FAILED,
            user_id=ids[0],
            subscription_id=f"{ids[0]}:{product}",
            amount=ev.get("price") if isinstance(ev.get("price"), (int, float)) else None,
            currency=ev.get("currency"),
        )

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        from payments.normalized import apply_normalized_event

        self._check_auth(headers)
        ev = self._parse_body(body)
        etype = str(ev.get("type") or "")
        env = str(ev.get("environment") or "")
        if env == "SANDBOX" and self._config.get("accept_sandbox", True) is False:
            return {"provider": self.id, "handled": False, "status": "sandbox_ignored", "type": etype}

        if etype == "TEST":
            return {"provider": self.id, "handled": True, "status": "test_ok"}

        if etype == "BILLING_ISSUE":
            parsed = await self.verify_and_parse_webhook(headers, body)
            result: Dict[str, Any] = {"provider": self.id, "type": etype}
            if parsed is not None:
                result.update(await apply_normalized_event(parsed))
            # Grace period: the store keeps access; re-read so the row shows past_due.
            for uid in self.member_ids(ev)[:1]:
                result["sync"] = await self.sync_member(uid)
            return result

        if etype == "TRANSFER":
            ids = [u for u in [*(ev.get("transferred_from") or []), *(ev.get("transferred_to") or [])] if is_member_id(u)]
        elif etype in _STATE_EVENTS:
            ids = self.member_ids(ev)[:1]
        else:
            return {"provider": self.id, "handled": False, "status": EVENT_IGNORED, "type": etype}

        synced = [await self.sync_member(uid) for uid in ids]
        return {"provider": self.id, "handled": bool(synced), "type": etype, "sync": synced}


def _latest_store(sub: Mapping[str, Any]) -> Optional[str]:
    best: Tuple[Optional[datetime], Optional[str]] = (None, None)
    for row in (sub.get("subscriptions") or {}).values():
        if not isinstance(row, dict):
            continue
        ts = _parse_ts(row.get("purchase_date"))
        if ts and (best[0] is None or ts > best[0]):
            best = (ts, row.get("store"))
    return best[1]
