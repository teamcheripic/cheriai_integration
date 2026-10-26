"""
Checkout.com provider — PLACEHOLDER (not implemented yet).

Env (backend only, never in the DB):
  CHECKOUT_COM_SECRET_KEY      secret API key (required)
  CHECKOUT_COM_WEBHOOK_SECRET  webhook signature key (Cko-Signature)

What to implement (see payments/base.py for the interface and
payments/stripe_provider.py for a complete example):
  create_checkout  POST /payment-links (or Hosted Payments Page) with amount
                   from price.amount/currency, reference=<user id>, and
                   recurring/"payment_type": "Recurring" (store the instrument
                   for renewals) → redirect to _links.redirect.href. SDK mode:
                   payment session for Flow ({"payment_session_id", "payment_session_secret"}).
  create_portal    no hosted portal: link to a CheriPic billing page.
  verify_and_parse_webhook
                   HMAC-SHA256(body, CHECKOUT_COM_WEBHOOK_SECRET) == Cko-Signature;
                   payment_captured → payment_succeeded (+ subscription_active
                   for the first/renewal charge), payment_declined →
                   payment_failed. Renewals must be scheduled by us (no native
                   subscriptions).

Until then: is_configured() is True once the secret env vars exist, but
implemented=False keeps it out of active selection/failover and every
operation raises ProviderNotImplemented.
"""

from __future__ import annotations

from payments._placeholder import PlaceholderProvider


class CheckoutComProvider(PlaceholderProvider):
    id = "checkout_com"
    display_name = "Checkout.com"
    secret_env = ("CHECKOUT_COM_SECRET_KEY",)
    webhook_secret_env = ("CHECKOUT_COM_WEBHOOK_SECRET",)
