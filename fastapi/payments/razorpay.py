"""
Razorpay provider — PLACEHOLDER (not implemented yet).

Env (backend only, never in the DB):
  RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET  API key pair (required); key id is
                                        also the publishable key for Checkout
  RAZORPAY_WEBHOOK_SECRET               X-Razorpay-Signature HMAC secret

What to implement (see payments/base.py for the interface and
payments/stripe_provider.py for a complete example):
  create_checkout  POST /v1/subscriptions with plan_id=price.price_ref,
                   notes={"cheripic_user_id": <user id>} → redirect to short_url,
                   or SDK mode params {"key": RAZORPAY_KEY_ID, "subscription_id"}.
  create_portal    no hosted portal: return the subscription short_url or a
                   CheriPic page that calls POST /v1/subscriptions/{id}/cancel.
  verify_and_parse_webhook
                   HMAC-SHA256(body, RAZORPAY_WEBHOOK_SECRET) == X-Razorpay-Signature;
                   subscription.activated/charged → subscription_active (+
                   payment_succeeded for charged), subscription.cancelled/completed
                   → subscription_cancelled, payment.failed → payment_failed.

Until then: is_configured() is True once the secret env vars exist, but
implemented=False keeps it out of active selection/failover and every
operation raises ProviderNotImplemented.
"""

from __future__ import annotations

from payments._placeholder import PlaceholderProvider


class RazorpayProvider(PlaceholderProvider):
    id = "razorpay"
    display_name = "Razorpay"
    secret_env = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET")
    webhook_secret_env = ("RAZORPAY_WEBHOOK_SECRET",)
