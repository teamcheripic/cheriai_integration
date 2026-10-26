"""
Paddle provider — PLACEHOLDER (not implemented yet).

Env (backend only, never in the DB):
  PADDLE_API_KEY         Paddle Billing API key (required)
  PADDLE_WEBHOOK_SECRET  notification destination secret (Paddle-Signature)
  PADDLE_ENV             "sandbox" | "production" (suggested)

What to implement (see payments/base.py for the interface and
payments/stripe_provider.py for a complete example):
  create_checkout  POST /transactions with items=[{price_id: price.price_ref}],
                   custom_data={"cheripic_user_id": <user id>} →
                   redirect to checkout.url. SDK mode: return
                   {"transaction_id", "client_token": publishable_key} for Paddle.js.
  create_portal    POST /customers/{customer_id}/portal-sessions → urls.general.overview
  verify_and_parse_webhook
                   verify Paddle-Signature (ts + h1 HMAC-SHA256 of "ts:body");
                   subscription.created/updated/activated → subscription_active,
                   subscription.canceled → subscription_cancelled,
                   transaction.completed → payment_succeeded,
                   transaction.payment_failed → payment_failed.

Until then: is_configured() is True once the secret env vars exist, but
implemented=False keeps it out of active selection/failover and every
operation raises ProviderNotImplemented.
"""

from __future__ import annotations

from payments._placeholder import PlaceholderProvider


class PaddleProvider(PlaceholderProvider):
    id = "paddle"
    display_name = "Paddle"
    secret_env = ("PADDLE_API_KEY",)
    webhook_secret_env = ("PADDLE_WEBHOOK_SECRET",)
