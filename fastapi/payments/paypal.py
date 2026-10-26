"""
PayPal provider — PLACEHOLDER (not implemented yet).

Env (backend only, never in the DB):
  PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET   OAuth client credentials (required)
  PAYPAL_WEBHOOK_ID                        webhook id used by
                                           /v1/notifications/verify-webhook-signature
  PAYPAL_ENV                               "sandbox" | "live" (suggested)

What to implement (see payments/base.py for the interface and
payments/stripe_provider.py for a complete example):
  create_checkout  POST /v1/billing/subscriptions with plan_id=price.price_ref
                   (a P-… plan id), custom_id=<user id>, application_context
                   return_url/cancel_url → CheckoutResult(mode="redirect",
                   url=<links[rel=approve].href>). SDK mode: return
                   {"subscription_id", "client_id"} for the PayPal JS/native SDK.
  create_portal    PayPal has no customer portal: return
                   https://www.paypal.com/myaccount/autopay/ (or cancel via
                   POST /v1/billing/subscriptions/{id}/cancel from an endpoint).
  verify_and_parse_webhook
                   verify with verify-webhook-signature (PAYPAL_WEBHOOK_ID);
                   BILLING.SUBSCRIPTION.ACTIVATED/UPDATED → subscription_active,
                   BILLING.SUBSCRIPTION.CANCELLED/EXPIRED → subscription_cancelled
                   (only when access ends), PAYMENT.SALE.COMPLETED →
                   payment_succeeded, BILLING.SUBSCRIPTION.PAYMENT.FAILED →
                   payment_failed. user_id from resource.custom_id, tier from
                   plan_id via payment_provider_prices.price_ref.

Until then: is_configured() is True once the secret env vars exist, but
implemented=False keeps it out of active selection/failover and every
operation raises ProviderNotImplemented.
"""

from __future__ import annotations

from payments._placeholder import PlaceholderProvider


class PayPalProvider(PlaceholderProvider):
    id = "paypal"
    display_name = "PayPal"
    secret_env = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET")
    webhook_secret_env = ("PAYPAL_WEBHOOK_ID",)
