"""
F7 · Store billing (Apple IAP + Google Play Billing via RevenueCat).

payments/app_store.py + the platform routing in payments/registry.py and
payments_api.py. Supabase is respx-mocked; RevenueCat is respx-mocked or the
provider's fetch_subscriber is stubbed; no network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app_config
import auth
import payments_api
import ratelimit
from payments import app_store, registry
from payments.app_store import AppStoreProvider
from payments.base import (
    EVENT_SUBSCRIPTION_ACTIVE,
    EVENT_SUBSCRIPTION_CANCELLED,
    Membership,
    ProviderPrice,
    ProviderSettings,
    ProviderUnavailable,
    WebhookVerificationError,
)

PG = registry.PG_BASE
U = "11111111-2222-3333-4444-555555555555"
U2 = "66666666-7777-8888-9999-000000000000"
ADMIN = "aaaaaaaa-0000-0000-0000-00000000000a"
LINK = "https://buy.stripe.com/test_lite"
IOS_PRODUCT = "com.cheripic.app.premium_lite.monthly"
ANDROID_PRODUCT = "premium_lite:monthly"
STORE_CONFIG = {
    "offering": "default",
    "products": {"premium-lite": {"ios": IOS_PRODUCT, "android": ANDROID_PRODUCT}},
}
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    registry.invalidate_cache()
    registry._cache = None
    app_config.invalidate_config_cache()
    app_config._cache = {}
    ratelimit.limiter.reset()
    for k in ("STRIPE_SECRET_KEY", "PAYMENTS_AUTO_FAILOVER", "STRIPE_CHECKOUT_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("REVENUECAT_SECRET", "sk_rc_test")
    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH", "rc-hook-secret")
    monkeypatch.setenv("APP_BASE_URL", "https://cheripic.com")
    yield
    registry.invalidate_cache()
    registry._cache = None


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(payments_api.router)
    app.dependency_overrides[auth.get_current_user_id] = lambda: U
    app.dependency_overrides[auth.get_member_user_id] = lambda: U
    app.dependency_overrides[auth.get_current_admin_id] = lambda: ADMIN
    return TestClient(app)


def prow(pid, *, enabled=False, active=False, mode="hosted_redirect", prio=1, config=None):
    return {"id": pid, "display_name": pid.title(), "enabled": enabled, "is_active": active, "checkout_mode": mode,
            "publishable_key": None, "config": config if config is not None else {"currency": "usd"},
            "priority": prio, "updated_at": "2026-10-01T00:00:00+00:00", "updated_by": None}


def price(pid, tier="premium-lite", *, ref=None, link=None):
    return {"provider_id": pid, "tier": tier, "price_ref": ref, "payment_link": link, "currency": "usd",
            "amount": 59, "active": True}


def mock_db(*, store_enabled=True, store_active=False, store_config=None, member=None):
    providers = [
        prow("stripe", enabled=True, active=not store_active, mode="payment_link", prio=1),
        prow("app_store", enabled=store_enabled, active=store_active, mode="sdk", prio=6,
             config=store_config if store_config is not None else STORE_CONFIG),
    ]
    prices = [price("stripe", link=LINK, ref="price_lite_1"), price("app_store", ref="premium_lite")]
    respx.get(f"{PG}/payment_providers").mock(return_value=httpx.Response(200, json=providers))
    respx.get(f"{PG}/payment_provider_prices").mock(return_value=httpx.Response(200, json=prices))
    respx.get(f"{PG}/app_config").mock(return_value=httpx.Response(
        200, json=[{"key": "payments_auto_failover", "value": "true"}]))
    respx.get(f"{PG}/tier_config").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{PG}/user_memberships").mock(return_value=httpx.Response(200, json=[member] if member else []))


@pytest.fixture
def gates(monkeypatch):
    """Checkout gates pass; agreement recording is captured."""
    state = {"existing": None, "upgrade": False, "recorded": []}

    async def run(user_id, tier, *, agreement, agreement_required):
        return {"agreement": {"terms_version": "t1", "subscription_terms_version": "s1"},
                "existing": state["existing"], "upgrade": state["upgrade"]}

    async def record(user_id, tier, versions, *, provider, checkout_ref):
        state["recorded"].append(provider)
        return "pa1"

    monkeypatch.setattr(payments_api, "run_checkout_gates", run)
    monkeypatch.setattr(payments_api, "record_agreement", record)
    return state


def provider(config=None, prices=None):
    return AppStoreProvider(ProviderSettings(
        id="app_store", display_name="Store", enabled=True, checkout_mode="sdk",
        config=STORE_CONFIG if config is None else config,
        prices=prices if prices is not None else {"premium-lite": ProviderPrice(tier="premium-lite", price_ref="premium_lite")},
    ))


def subscriber(*, expires=None, grace=None, sandbox=False, refunded=False, unsubscribed=False, billing_issue=False,
               entitlement="premium_lite", product=IOS_PRODUCT, store="APP_STORE"):
    expires = expires if expires is not None else NOW + timedelta(days=20)
    return {
        "entitlements": {entitlement: {
            "expires_date": iso(expires) if expires else None,
            "grace_period_expires_date": iso(grace) if grace else None,
            "product_identifier": product,
        }},
        "subscriptions": {product: {
            "purchase_date": iso(NOW - timedelta(days=10)),
            "expires_date": iso(expires) if expires else None,
            "is_sandbox": sandbox,
            "refunded_at": iso(NOW) if refunded else None,
            "unsubscribe_detected_at": iso(NOW) if unsubscribed else None,
            "billing_issues_detected_at": iso(NOW) if billing_issue else None,
            "store": store,
        }},
        "management_url": "https://apps.apple.com/account/subscriptions",
    }


# ---------------------------------------------------------------------------
# Routing: native → store only; web → never the store
# ---------------------------------------------------------------------------
@respx.mock
def test_config_native_returns_store_provider(client):
    mock_db()
    for platform in ("ios", "android"):
        b = client.get(f"/billing/config?platform={platform}").json()
        assert b["provider"] == "app_store" and b["mode"] == "sdk" and b["available"] is True
        expected = IOS_PRODUCT if platform == "ios" else ANDROID_PRODUCT
        assert b["tiers"]["premium-lite"] == {"purchasable": True, "product_id": expected}
        assert "product_id" not in b["tiers"]["premium"]


@respx.mock
def test_config_web_never_returns_store_even_if_marked_active(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mock_db(store_active=True)
    b = client.get("/billing/config").json()
    assert b["provider"] == "stripe"


@respx.mock
def test_config_native_store_disabled_is_unavailable_not_stripe(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mock_db(store_enabled=False)
    b = client.get("/billing/config?platform=ios").json()
    assert b["available"] is False and b["provider"] is None


@respx.mock
def test_config_native_store_without_products_is_unavailable(client):
    mock_db(store_config={"offering": "default"})
    assert client.get("/billing/config?platform=android").json()["available"] is False


@respx.mock
def test_checkout_ios_returns_store_sdk_params(client, gates):
    mock_db()
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "ios"})
    assert r.status_code == 200
    b = r.json()
    assert b["provider"] == "app_store" and b["mode"] == "sdk"
    assert b["params"] == {"app_user_id": U, "offering": "default", "product_id": IOS_PRODUCT,
                           "entitlement": "premium_lite", "tier": "premium-lite"}
    assert gates["recorded"] == ["app_store"]


@respx.mock
def test_checkout_android_uses_play_product(client, gates):
    mock_db()
    b = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "android"}).json()
    assert b["params"]["product_id"] == ANDROID_PRODUCT


@respx.mock
def test_checkout_native_prefer_redirect_is_refused(client, gates):
    mock_db()
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "ios", "prefer": "redirect"})
    assert r.status_code == 503


@respx.mock
def test_checkout_web_goes_to_stripe(client, gates, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mock_db()
    b = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "web"}).json()
    assert b["provider"] == "stripe" and b["mode"] == "redirect"


@respx.mock
def test_native_upgrade_of_web_subscription_never_links_out(client, gates):
    mock_db()
    gates["existing"] = {"tier": "premium-lite", "payment_provider": "stripe"}
    gates["upgrade"] = True
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "ios"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "managed_elsewhere"
    assert "url" not in r.json()["detail"]


# ---------------------------------------------------------------------------
# Portal
# ---------------------------------------------------------------------------
@respx.mock
def test_portal_native_web_subscriber_gets_no_link(client):
    mock_db(member={"tier": "premium-lite", "payment_provider": "stripe", "provider_customer_id": "cus_1",
                    "provider_subscription_id": "sub_1", "stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1"})
    r = client.post("/billing/portal", json={}, headers={"X-CheriPic-Client": "mobile"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "managed_elsewhere"


@respx.mock
def test_portal_store_subscriber_gets_store_management_url(client):
    mock_db(member={"tier": "premium-lite", "payment_provider": "app_store", "provider_customer_id": U,
                    "provider_subscription_id": f"{U}:{ANDROID_PRODUCT}"})
    respx.get(f"{app_store.RC_API}/subscribers/{U}").mock(return_value=httpx.Response(200, json={"subscriber": {
        "management_url": "https://play.google.com/store/account/subscriptions?sku=premium_lite", "subscriptions": {}}}))
    r = client.post("/billing/portal", json={}, headers={"X-CheriPic-Client": "mobile"})
    assert r.status_code == 200 and r.json()["url"].startswith("https://play.google.com/")


def test_portal_falls_back_to_store_by_last_purchase():
    p = provider()

    async def boom(_):
        raise ProviderUnavailable("down")

    p.fetch_subscriber = boom
    url = asyncio.run(p.create_portal(None, Membership(user_id=U, payment_provider="app_store"), return_url=""))
    assert url == app_store.APPLE_MANAGE_URL


# ---------------------------------------------------------------------------
# Subscriber → state
# ---------------------------------------------------------------------------
def test_state_active_subscription():
    ev = provider().state_from_subscriber(U, subscriber(), now=NOW)
    assert ev.kind == EVENT_SUBSCRIPTION_ACTIVE and ev.tier == "premium-lite"
    assert ev.subscription_id == f"{U}:{IOS_PRODUCT}" and ev.customer_id == U
    assert ev.cancel_at_period_end is False and ev.status == "active"


def test_state_expired_is_cancelled():
    ev = provider().state_from_subscriber(U, subscriber(expires=NOW - timedelta(minutes=1)), now=NOW)
    assert ev.kind == EVENT_SUBSCRIPTION_CANCELLED


def test_state_grace_period_keeps_access_and_flags_past_due():
    ev = provider().state_from_subscriber(
        U, subscriber(expires=NOW - timedelta(hours=1), grace=NOW + timedelta(days=3), billing_issue=True), now=NOW)
    assert ev.kind == EVENT_SUBSCRIPTION_ACTIVE and ev.status == "past_due"


def test_state_refund_removes_access():
    assert provider().state_from_subscriber(U, subscriber(refunded=True), now=NOW).kind == EVENT_SUBSCRIPTION_CANCELLED


def test_state_sandbox_accepted_by_default_for_app_review():
    assert provider().state_from_subscriber(U, subscriber(sandbox=True), now=NOW).kind == EVENT_SUBSCRIPTION_ACTIVE
    strict = provider(config={**STORE_CONFIG, "accept_sandbox": False})
    assert strict.state_from_subscriber(U, subscriber(sandbox=True), now=NOW).kind == EVENT_SUBSCRIPTION_CANCELLED


def test_state_auto_renew_off_sets_cancel_at_period_end():
    assert provider().state_from_subscriber(U, subscriber(unsubscribed=True), now=NOW).cancel_at_period_end is True


def test_state_unknown_entitlement_is_ignored():
    ev = provider().state_from_subscriber(U, subscriber(entitlement="something_else"), now=NOW)
    assert ev.kind == EVENT_SUBSCRIPTION_CANCELLED


# ---------------------------------------------------------------------------
# sync_member
# ---------------------------------------------------------------------------
def _sync_env(monkeypatch, p, sub, current):
    applied = []

    async def fetch(_):
        return sub

    async def member(_):
        return current

    async def apply(ev):
        applied.append(ev)
        return {"handled": True}

    p.fetch_subscriber = fetch
    monkeypatch.setattr(registry, "get_member_billing", member)
    import payments.normalized as normalized
    monkeypatch.setattr(normalized, "apply_normalized_event", apply)
    return applied


def test_sync_applies_store_purchase(monkeypatch):
    p = provider()
    applied = _sync_env(monkeypatch, p, subscriber(), None)
    out = asyncio.run(p.sync_member(U))
    assert out["tier"] == "premium-lite" and out["applied"] is True and applied[0].kind == EVENT_SUBSCRIPTION_ACTIVE


def test_sync_never_downgrades_a_web_subscriber(monkeypatch):
    p = provider()
    applied = _sync_env(monkeypatch, p, {"entitlements": {}, "subscriptions": {}},
                        Membership(user_id=U, tier="premium-lite", payment_provider="stripe", customer_id="cus_1"))
    out = asyncio.run(p.sync_member(U))
    assert out == {"user_id": U, "tier": "premium-lite", "applied": False} and applied == []


def test_sync_downgrades_expired_store_subscriber(monkeypatch):
    p = provider()
    applied = _sync_env(monkeypatch, p, {"entitlements": {}, "subscriptions": {}},
                        Membership(user_id=U, tier="premium-lite", payment_provider="app_store", customer_id=U))
    assert asyncio.run(p.sync_member(U))["tier"] == "basic" and applied[0].kind == EVENT_SUBSCRIPTION_CANCELLED


def test_sync_ignores_anonymous_ids(monkeypatch):
    p = provider()
    applied = _sync_env(monkeypatch, p, subscriber(), None)
    assert asyncio.run(p.sync_member("$RCAnonymousID:abc"))["applied"] is False and applied == []


@respx.mock
def test_sync_endpoint(client, monkeypatch):
    mock_db()

    async def sync(self, uid):
        assert uid == U
        return {"tier": "premium-lite", "applied": True}

    monkeypatch.setattr(AppStoreProvider, "sync_member", sync)
    assert client.post("/billing/app-store/sync").json() == {"tier": "premium-lite", "applied": True}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
def hook(etype, **extra):
    return json.dumps({"api_version": "1.0", "event": {
        "id": "evt_1", "type": etype, "app_user_id": U, "original_app_user_id": U, "aliases": [U],
        "product_id": IOS_PRODUCT, "environment": "PRODUCTION", "event_timestamp_ms": 1, **extra}}).encode()


@respx.mock
def test_webhook_bad_authorization_is_400(client):
    mock_db()
    r = client.post("/billing/webhook/app_store", content=hook("INITIAL_PURCHASE"), headers={"Authorization": "nope"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_signature"


@respx.mock
def test_webhook_state_event_resyncs_member(client, monkeypatch):
    mock_db()
    calls = []

    async def sync(self, uid):
        calls.append(uid)
        return {"user_id": uid, "tier": "premium-lite", "applied": True}

    monkeypatch.setattr(AppStoreProvider, "sync_member", sync)
    for t in ("INITIAL_PURCHASE", "RENEWAL", "CANCELLATION", "EXPIRATION", "PRODUCT_CHANGE"):
        r = client.post("/billing/webhook/app_store", content=hook(t), headers={"Authorization": "Bearer rc-hook-secret"})
        assert r.status_code == 200 and r.json()["handled"] is True
    assert calls == [U] * 5


@respx.mock
def test_webhook_transfer_resyncs_both_members(client, monkeypatch):
    mock_db()
    calls = []

    async def sync(self, uid):
        calls.append(uid)
        return {"user_id": uid, "applied": True}

    monkeypatch.setattr(AppStoreProvider, "sync_member", sync)
    body = hook("TRANSFER", transferred_from=[U], transferred_to=[U2, "$RCAnonymousID:x"])
    assert client.post("/billing/webhook/app_store", content=body, headers={"Authorization": "rc-hook-secret"}).status_code == 200
    assert calls == [U, U2]


@respx.mock
def test_webhook_test_event_and_unknown_types(client, monkeypatch):
    mock_db()
    h = {"Authorization": "rc-hook-secret"}
    assert client.post("/billing/webhook/app_store", content=hook("TEST"), headers=h).json()["status"] == "test_ok"
    assert client.post("/billing/webhook/app_store", content=hook("INVOICE_ISSUANCE"), headers=h).json()["handled"] is False


def test_webhook_auth_missing_env_rejects(monkeypatch):
    monkeypatch.delenv("REVENUECAT_WEBHOOK_AUTH")
    with pytest.raises(WebhookVerificationError):
        provider()._check_auth({"Authorization": "anything"})


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
def test_store_provider_can_never_be_the_web_active_provider():
    codes = [c for c, _ in payments_api.switch_refusals(provider())]
    assert codes == ["store_provider_native_only"]
