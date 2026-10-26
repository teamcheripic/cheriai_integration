"""
Provider interface for the switchable payment-gateway layer
(documents/08_PAYMENT_PROVIDERS.md).

Every gateway implements `Provider`. The registry builds one instance per
request from its non-secret DB settings (`ProviderSettings`); secrets are
read from env vars by the provider itself and never leave the backend.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

# Purchasable rule (same as the apps' PURCHASABLE_TIERS): only Premium Lite
# is on sale today; Premium is "coming soon"; basic is free.
PURCHASABLE_TIERS: Tuple[str, ...] = ("premium-lite",)
PAID_TIERS: Tuple[str, ...] = ("premium-lite", "premium")
CHECKOUT_MODES: Tuple[str, ...] = ("hosted_redirect", "payment_link", "sdk")
PLATFORMS: Tuple[str, ...] = ("ios", "android", "web")


def is_purchasable(tier: str) -> bool:
    return tier in PURCHASABLE_TIERS


class ProviderError(Exception):
    """Base class for gateway errors surfaced to the API layer."""

    code = "provider_unavailable"


class ProviderNotImplemented(ProviderError):
    """The provider row exists but its integration hasn't been built yet."""

    code = "provider_not_implemented"


class ProviderUnavailable(ProviderError):
    """Configured provider can't take this payment right now (missing
    secret, missing price for the tier, upstream error…)."""

    code = "provider_unavailable"


class NoSubscription(ProviderError):
    """The member has no customer/subscription with their provider."""

    code = "no_subscription"


class WebhookVerificationError(ProviderError):
    """Signature/payload could not be verified (→ HTTP 400)."""

    code = "invalid_signature"


@dataclass
class ProviderPrice:
    tier: str
    price_ref: Optional[str] = None
    payment_link: Optional[str] = None
    currency: str = "usd"
    amount: Optional[float] = None
    active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "price_ref": self.price_ref,
            "payment_link": self.payment_link,
            "currency": self.currency,
            "amount": self.amount,
            "active": self.active,
        }


@dataclass
class ProviderSettings:
    """Non-secret settings from `payment_providers` (+ its prices)."""

    id: str
    display_name: str
    enabled: bool = False
    is_active: bool = False
    checkout_mode: str = "hosted_redirect"
    publishable_key: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    prices: Dict[str, ProviderPrice] = field(default_factory=dict)
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    def price(self, tier: str) -> Optional[ProviderPrice]:
        p = self.prices.get(tier)
        return p if p is not None and p.active else None


@dataclass
class BillingUser:
    id: str
    email: Optional[str] = None


@dataclass
class Membership:
    """The member's active user_memberships row, provider-normalised."""

    user_id: str
    tier: str = "basic"
    payment_provider: str = "stripe"
    customer_id: Optional[str] = None
    subscription_id: Optional[str] = None


@dataclass
class CheckoutResult:
    """`mode` is "redirect" (open `url`) or "sdk" (hand `params` to the
    provider's native SDK adapter in the app)."""

    mode: str
    url: Optional[str] = None
    params: Optional[Dict[str, Any]] = None

    def to_response(self, provider_id: str) -> Dict[str, Any]:
        if self.mode == "sdk":
            return {"provider": provider_id, "mode": "sdk", "params": self.params or {}}
        return {"provider": provider_id, "mode": "redirect", "url": self.url}


# NormalizedEvent.kind values
EVENT_SUBSCRIPTION_ACTIVE = "subscription_active"      # created / renewed / plan change
EVENT_SUBSCRIPTION_CANCELLED = "subscription_cancelled"  # access has ENDED
EVENT_PAYMENT_SUCCEEDED = "payment_succeeded"
EVENT_PAYMENT_FAILED = "payment_failed"
EVENT_IGNORED = "ignored"


@dataclass
class NormalizedEvent:
    provider: str
    event_id: str
    kind: str
    user_id: Optional[str] = None
    tier: Optional[str] = None
    customer_id: Optional[str] = None
    subscription_id: Optional[str] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    status: Optional[str] = None
    created: Optional[int] = None  # epoch seconds, for out-of-order protection
    cancel_at_period_end: Optional[bool] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    invoice_url: Optional[str] = None


def env_present(*names: str) -> bool:
    return all(bool((os.getenv(n) or "").strip()) for n in names)


class Provider(ABC):
    """One payment gateway.

    Subclasses set `id`, `secret_env` (env var names that must ALL be set for
    `is_configured()`), and `implemented`. A provider is only selectable as
    active (or as a failover target) when it is enabled, implemented and
    configured.
    """

    id: str = ""
    display_name: str = ""
    secret_env: Tuple[str, ...] = ()
    webhook_secret_env: Tuple[str, ...] = ()
    implemented: bool = False

    def __init__(self, settings: ProviderSettings) -> None:
        self.settings = settings

    # -- configuration -----------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.secret_env) and env_present(*self.secret_env)

    def webhook_configured(self) -> bool:
        return bool(self.webhook_secret_env) and env_present(*self.webhook_secret_env)

    def price_ready(self, tier: str, mode: Optional[str] = None) -> bool:
        """Whether this provider has what it needs to sell `tier` in `mode`
        (default: its configured checkout_mode)."""
        p = self.settings.price(tier)
        if p is None:
            return False
        mode = mode or self.settings.checkout_mode
        if mode == "payment_link":
            return bool(p.payment_link)
        return bool(p.price_ref)

    # -- operations --------------------------------------------------------
    @abstractmethod
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
        """Start a subscription checkout for `tier`. `prefer="redirect"`
        asks for a hosted page even when the provider runs in `sdk` mode
        (app build without that provider's native adapter)."""

    @abstractmethod
    async def create_portal(self, user: BillingUser, membership: Membership, *, return_url: str) -> str:
        """URL where the member manages/cancels their subscription."""

    @abstractmethod
    async def verify_and_parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Optional[NormalizedEvent]:
        """Verify the provider signature and normalise the event, or return
        None for event types we don't act on. Raise
        WebhookVerificationError on a bad signature."""

    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        """Verify + apply. Default: normalise then apply through the shared
        membership writer (idempotent, out-of-order safe, same emails)."""
        from payments.normalized import apply_normalized_event

        event = await self.verify_and_parse_webhook(headers, body)
        if event is None:
            return {"provider": self.id, "handled": False, "status": "ignored"}
        return await apply_normalized_event(event)
