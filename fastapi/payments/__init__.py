"""
Switchable payment-gateway layer (documents/08_PAYMENT_PROVIDERS.md).

  base.py            Provider ABC + dataclasses + typed errors
  stripe_provider.py Stripe (wraps billing.py — the only live provider)
  paypal.py, paddle.py, razorpay.py, checkout_com.py, app_store.py
                     placeholders: implement the interface, report
                     is_configured() from env, raise ProviderNotImplemented
  registry.py        provider rows (60 s cache), active resolution +
                     auto-failover, fallback to Stripe env/tier_config while
                     migration 054 isn't applied
  normalized.py      shared membership writer for NormalizedEvent
  urls.py            CheriPic-only return-URL allow-list
"""
