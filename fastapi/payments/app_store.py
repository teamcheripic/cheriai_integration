"""
App Store / Google Play (RevenueCat) provider — PLACEHOLDER (not implemented yet).

Env (backend only, never in the DB):
  REVENUECAT_SECRET        RevenueCat secret API key (required)
  REVENUECAT_WEBHOOK_AUTH  value RevenueCat sends in the Authorization header

What to implement (see payments/base.py for the interface and
payments/stripe_provider.py for a complete example):
  create_checkout  in-app purchase happens in the app: return
                   CheckoutResult(mode="sdk", params={"offering": config.offering,
                   "product_id": price.price_ref, "app_user_id": <user id>}).
                   Never redirect (store rules).
  create_portal    platform subscription settings:
                   https://apps.apple.com/account/subscriptions (iOS) or
                   https://play.google.com/store/account/subscriptions (Android).
  verify_and_parse_webhook
                   compare Authorization to REVENUECAT_WEBHOOK_AUTH;
                   INITIAL_PURCHASE/RENEWAL/PRODUCT_CHANGE/UNCANCELLATION →
                   subscription_active, EXPIRATION → subscription_cancelled,
                   CANCELLATION → subscription_active with
                   cancel_at_period_end=True, BILLING_ISSUE → payment_failed.
                   user_id = event.app_user_id, tier from product_id.

Until then: is_configured() is True once the secret env vars exist, but
implemented=False keeps it out of active selection/failover and every
operation raises ProviderNotImplemented.
"""

from __future__ import annotations

from payments._placeholder import PlaceholderProvider


class AppStoreProvider(PlaceholderProvider):
    id = "app_store"
    display_name = "App Store / Google Play (RevenueCat)"
    secret_env = ("REVENUECAT_SECRET",)
    webhook_secret_env = ("REVENUECAT_WEBHOOK_AUTH",)
