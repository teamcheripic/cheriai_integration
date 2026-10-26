"""
Stripe provider — wraps the existing billing.py logic (no duplication).

checkout_mode
  payment_link     redirect to the tier's Stripe Payment Link with
                   ?client_reference_id=<user id> (what the PWA/app did
                   before this layer existed)
  hosted_redirect  Stripe Checkout Session (billing.create_checkout_session)
  sdk              PaymentSheet params for @stripe/stripe-react-native:
                   customer + ephemeral key + an incomplete subscription's
                   client secret. `prefer="redirect"` falls back to a
                   Checkout Session (price_ref) or the Payment Link.

Webhooks delegate to billing.handle_event, which keeps idempotency
(stripe_events), out-of-order protection, emails and reconciliation; its
membership writes set payment_provider='stripe' + provider ids.

Env: STRIPE_SECRET_KEY (required), STRIPE_WEBHOOK_SECRET (webhooks),
STRIPE_PUBLISHABLE_KEY (fallback publishable key before 054).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote

import billing
from payments.base import (
    EVENT_IGNORED,
    EVENT_PAYMENT_FAILED,
    EVENT_PAYMENT_SUCCEEDED,
    EVENT_SUBSCRIPTION_ACTIVE,
    EVENT_SUBSCRIPTION_CANCELLED,
    BillingUser,
    CheckoutResult,
    Membership,
    NoSubscription,
    NormalizedEvent,
    Provider,
    ProviderUnavailable,
    WebhookVerificationError,
)

logger = logging.getLogger(__name__)

DEFAULT_EPHEMERAL_KEY_API_VERSION = "2024-06-20"


def _valid_price_ref(ref: Optional[str]) -> bool:
    return bool(ref) and ref.startswith("price_") and "REPLACE" not in ref  # type: ignore[union-attr]


def payment_link_url(link: str, user_id: str) -> str:
    sep = "&" if "?" in link else "?"
    return f"{link}{sep}client_reference_id={quote(user_id, safe='')}"


class StripeProvider(Provider):
    id = "stripe"
    display_name = "Stripe"
    secret_env = ("STRIPE_SECRET_KEY",)
    webhook_secret_env = ("STRIPE_WEBHOOK_SECRET",)
    implemented = True

    def price_ready(self, tier: str, mode: Optional[str] = None) -> bool:
        p = self.settings.price(tier)
        if p is None:
            return False
        mode = mode or self.settings.checkout_mode
        if mode == "payment_link":
            return bool(p.payment_link)
        return _valid_price_ref(p.price_ref)

    # -- checkout ------------------------------------------------------------
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
        mode = self.settings.checkout_mode
        if mode == "sdk" and prefer == "redirect":
            mode = "hosted_redirect" if self.price_ready(tier, "hosted_redirect") else "payment_link"
        if not self.price_ready(tier, mode):
            raise ProviderUnavailable(f"stripe has no {mode} price for {tier}")
        price = self.settings.price(tier)
        assert price is not None

        if mode == "payment_link":
            return CheckoutResult(mode="redirect", url=payment_link_url(price.payment_link or "", user.id))

        if not self.is_configured():
            raise ProviderUnavailable("STRIPE_SECRET_KEY missing")

        if mode == "hosted_redirect":
            try:
                url = await billing.create_checkout_session(
                    user_id=user.id,
                    price_id=price.price_ref or "",
                    success_url=success_url,
                    cancel_url=cancel_url,
                )
            except ValueError as e:
                raise ProviderUnavailable(str(e)) from e
            return CheckoutResult(mode="redirect", url=url)

        return CheckoutResult(mode="sdk", params=await self._payment_sheet_params(user, price.price_ref or ""))

    async def _payment_sheet_params(self, user: BillingUser, price_ref: str) -> Dict[str, Any]:
        stripe = billing._get_stripe()
        customer_id = await billing._find_or_create_customer(user.id)
        api_version = (self.settings.config or {}).get("ephemeral_key_api_version") or DEFAULT_EPHEMERAL_KEY_API_VERSION
        eph = billing._plain(
            await asyncio.to_thread(stripe.EphemeralKey.create, customer=customer_id, stripe_version=api_version)
        )
        sub = billing._plain(
            await asyncio.to_thread(
                stripe.Subscription.create,
                customer=customer_id,
                items=[{"price": price_ref}],
                payment_behavior="default_incomplete",
                payment_settings={"save_default_payment_method": "on_subscription"},
                metadata={"cheripic_user_id": user.id},
                expand=["latest_invoice.confirmation_secret"],
            )
        )
        inv = sub.get("latest_invoice") or {}
        # API >= 2025-03 (basil): invoice.confirmation_secret; older API
        # versions expose invoice.payment_intent.client_secret.
        secret = (inv.get("confirmation_secret") or {}).get("client_secret")
        if not secret and isinstance(inv.get("payment_intent"), dict):
            secret = inv["payment_intent"].get("client_secret")
        if not secret:
            raise ProviderUnavailable("stripe subscription has no client secret")
        return {
            "publishable_key": self.settings.publishable_key,
            "customer": customer_id,
            "ephemeral_key": eph.get("secret"),
            "client_secret": secret,
            "subscription_id": sub.get("id"),
            "intent_type": "payment",
            "merchant_display_name": (self.settings.config or {}).get("merchant_display_name") or "CheriPic",
        }

    # -- portal --------------------------------------------------------------
    async def create_portal(self, user: BillingUser, membership: Membership, *, return_url: str) -> str:
        if not self.is_configured():
            raise ProviderUnavailable("STRIPE_SECRET_KEY missing")
        try:
            # billing reads stripe_customer_id itself (unchanged behaviour).
            return await billing.create_portal_session(user_id=user.id, return_url=return_url)
        except ValueError as e:
            raise NoSubscription(str(e)) from e

    # -- webhooks ------------------------------------------------------------
    async def handle_webhook(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        sig = _header(headers, "stripe-signature")
        try:
            return await billing.handle_event(body, sig)
        except billing.WebhookSignatureError as e:
            raise WebhookVerificationError(str(e)) from e

    async def verify_and_parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> Optional[NormalizedEvent]:
        """Interface completeness / inspection only — live Stripe webhooks go
        through handle_webhook → billing.handle_event (richer handling)."""
        stripe = billing._get_stripe()
        try:
            event = stripe.Webhook.construct_event(
                payload=body, sig_header=_header(headers, "stripe-signature"), secret=billing.get_webhook_secret()
            )
        except (ValueError, stripe.error.SignatureVerificationError) as e:
            raise WebhookVerificationError(str(e)) from e
        event = billing._plain(event)
        et = event.get("type")
        obj = (event.get("data") or {}).get("object") or {}
        kind = EVENT_IGNORED
        if et in ("customer.subscription.created", "customer.subscription.updated"):
            kind = EVENT_SUBSCRIPTION_ACTIVE
        elif et == "customer.subscription.deleted":
            kind = EVENT_SUBSCRIPTION_CANCELLED
        elif et == "invoice.payment_succeeded":
            kind = EVENT_PAYMENT_SUCCEEDED
        elif et == "invoice.payment_failed":
            kind = EVENT_PAYMENT_FAILED
        if kind == EVENT_IGNORED:
            return None
        is_sub = kind in (EVENT_SUBSCRIPTION_ACTIVE, EVENT_SUBSCRIPTION_CANCELLED)
        items = ((obj.get("items") or {}).get("data") or []) if is_sub else []
        price_id = items[0]["price"]["id"] if items else None
        cps, cpe = billing._period_bounds(obj) if is_sub else (None, None)
        amount = obj.get("amount_paid") or obj.get("amount_due")
        return NormalizedEvent(
            provider="stripe",
            event_id=event["id"],
            kind=kind,
            user_id=billing._user_id_from_object(obj),
            tier=billing._tier_for_price(price_id),
            customer_id=obj.get("customer"),
            subscription_id=obj.get("id") if is_sub else obj.get("subscription"),
            period_start=cps,
            period_end=cpe,
            status=obj.get("status"),
            created=billing._as_int(event.get("created")),
            cancel_at_period_end=obj.get("cancel_at_period_end") if is_sub else None,
            amount=(amount / 100.0) if isinstance(amount, (int, float)) else None,
            currency=obj.get("currency"),
            invoice_url=obj.get("hosted_invoice_url"),
        )


def _header(headers: Mapping[str, str], name: str) -> str:
    try:
        v = headers.get(name)  # starlette Headers are case-insensitive
    except Exception:
        v = None
    if v is None:
        for k, val in dict(headers).items():
            if k.lower() == name:
                return val
    return v or ""
