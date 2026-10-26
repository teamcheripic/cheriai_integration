"""
F6 · Switchable payment-gateway layer (documents/08_PAYMENT_PROVIDERS.md).

payments/ registry + providers and payments_api.py endpoints. Supabase is
respx-mocked, Stripe is a fake object; no network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import json
import types
from typing import Optional

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app_config
import auth
import billing
import payments_api
import ratelimit
from payments import registry
from payments.base import (
    EVENT_SUBSCRIPTION_ACTIVE,
    CheckoutResult,
    NormalizedEvent,
    Provider,
    ProviderNotImplemented,
)
from payments.stripe_provider import StripeProvider

PG = registry.PG_BASE
AUTH_ADMIN = f"{registry.SUPABASE_URL}/auth/v1/admin/users"
U = "uuuuuuuu-0000-0000-0000-00000000000u"
ADMIN = "aaaaaaaa-0000-0000-0000-00000000000a"
LINK = "https://buy.stripe.com/test_lite"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
class FakeGateway(Provider):
    """An implemented non-Stripe provider (stands in for a future PayPal)."""

    id = "paypal"
    display_name = "PayPal"
    secret_env = ("FAKE_GATEWAY_SECRET",)
    implemented = True
    portal_calls: list = []

    async def create_checkout(self, user, tier, platform, *, success_url, cancel_url, prefer=None):
        if self.settings.checkout_mode == "sdk" and prefer != "redirect":
            return CheckoutResult(mode="sdk", params={"order_id": "O-1", "client_id": "pub_x", "tier": tier})
        if self.settings.checkout_mode == "sdk":
            return CheckoutResult(mode="sdk", params={})  # no hosted page → API must 503
        return CheckoutResult(mode="redirect", url=f"https://paypal.example/approve?u={user.id}")

    async def create_portal(self, user, membership, *, return_url):
        FakeGateway.portal_calls.append(membership.customer_id)
        return "https://paypal.example/autopay"

    async def verify_and_parse_webhook(self, headers, body):
        data = json.loads(body)
        if headers.get("x-fake-sig") != "ok":
            from payments.base import WebhookVerificationError
            raise WebhookVerificationError("bad sig")
        return NormalizedEvent(
            provider="paypal", event_id=data["id"], kind=EVENT_SUBSCRIPTION_ACTIVE, user_id=U,
            tier="premium-lite", customer_id="PAYER-1", subscription_id="I-SUB1", status="active", created=100,
        )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    registry.invalidate_cache()
    registry._cache = None
    app_config.invalidate_config_cache()
    app_config._cache = {}
    ratelimit.limiter.reset()
    monkeypatch.setitem(registry.PROVIDER_CLASSES, "paypal", FakeGateway)
    FakeGateway.portal_calls = []
    for k in ("STRIPE_SECRET_KEY", "FAKE_GATEWAY_SECRET", "PAYMENTS_AUTO_FAILOVER", "STRIPE_CHECKOUT_MODE",
              "STRIPE_PUBLISHABLE_KEY", "PADDLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("APP_BASE_URL", "https://cheripic.com")
    yield
    registry.invalidate_cache()
    registry._cache = None


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(payments_api.router)
    app.dependency_overrides[auth.get_current_user_id] = lambda: U
    app.dependency_overrides[auth.get_current_admin_id] = lambda: ADMIN
    return TestClient(app)


def prow(pid, *, enabled=False, active=False, mode="hosted_redirect", prio=1, pk=None):
    return {"id": pid, "display_name": pid.title(), "enabled": enabled, "is_active": active, "checkout_mode": mode,
            "publishable_key": pk, "config": {"currency": "usd"}, "priority": prio,
            "updated_at": "2026-10-01T00:00:00+00:00", "updated_by": None}


def price(pid, tier="premium-lite", *, ref=None, link=None, amount=59):
    return {"provider_id": pid, "tier": tier, "price_ref": ref, "payment_link": link, "currency": "usd",
            "amount": amount, "active": True}


STRIPE_ACTIVE = [prow("stripe", enabled=True, active=True, mode="payment_link", prio=1),
                 prow("paypal", enabled=False, prio=2), prow("paddle", prio=3)]
STRIPE_PRICES = [price("stripe", link=LINK, ref="price_lite_1")]


def mock_db(providers=None, prices=None, *, failover=False, member=None, tables_missing=False, tier_config=None):
    routes = types.SimpleNamespace()
    if tables_missing:
        routes.providers = respx.get(f"{PG}/payment_providers").mock(
            return_value=httpx.Response(404, json={"code": "PGRST205", "message": "Could not find the table"}))
        respx.get(f"{PG}/payment_provider_prices").mock(return_value=httpx.Response(404, json={"code": "PGRST205"}))
    else:
        routes.providers = respx.get(f"{PG}/payment_providers").mock(
            return_value=httpx.Response(200, json=providers if providers is not None else STRIPE_ACTIVE))
        respx.get(f"{PG}/payment_provider_prices").mock(
            return_value=httpx.Response(200, json=prices if prices is not None else STRIPE_PRICES))
    routes.app_config = respx.get(f"{PG}/app_config").mock(return_value=httpx.Response(
        200, json=[{"key": "payments_auto_failover", "value": "true" if failover else "false"}]))
    routes.tier_config = respx.get(f"{PG}/tier_config").mock(return_value=httpx.Response(200, json=tier_config or []))
    routes.members = respx.get(f"{PG}/user_memberships").mock(
        return_value=httpx.Response(200, json=[member] if member else []))
    return routes


def detail(r):
    return r.json()["detail"]


# ---------------------------------------------------------------------------
# GET /billing/config
# ---------------------------------------------------------------------------
@respx.mock
def test_config_returns_active_provider(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mock_db(providers=[prow("stripe", enabled=True, active=True, mode="payment_link", pk="pk_test_1")])
    r = client.get("/billing/config")
    assert r.status_code == 200
    b = r.json()
    assert b["provider"] == "stripe" and b["display_name"] == "Stripe" and b["mode"] == "payment_link"
    assert b["publishable_key"] == "pk_test_1" and b["available"] is True and b["message"] is None
    assert b["tiers"]["premium-lite"] == {"purchasable": True}
    assert b["tiers"]["premium"] == {"purchasable": False}


@respx.mock
def test_config_fails_over_when_active_unconfigured(client, monkeypatch):
    monkeypatch.setenv("FAKE_GATEWAY_SECRET", "s")  # stripe secret missing
    mock_db(providers=[prow("stripe", enabled=True, active=True, mode="payment_link", prio=1),
                       prow("paypal", enabled=True, prio=2)],
            prices=STRIPE_PRICES + [price("paypal", ref="P-PLAN")], failover=True)
    b = client.get("/billing/config").json()
    assert b["provider"] == "paypal" and b["available"] is True and b["failover"] is True


@respx.mock
def test_config_fails_over_when_active_disabled(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("FAKE_GATEWAY_SECRET", "s")
    mock_db(providers=[prow("paypal", enabled=False, active=True, prio=1),
                       prow("stripe", enabled=True, mode="payment_link", prio=2)], failover=True)
    b = client.get("/billing/config").json()
    assert b["provider"] == "stripe" and b["failover"] is True


@respx.mock
def test_config_no_failover_when_flag_off(client, monkeypatch):
    monkeypatch.setenv("FAKE_GATEWAY_SECRET", "s")
    mock_db(providers=[prow("stripe", enabled=True, active=True, mode="payment_link"),
                       prow("paypal", enabled=True, prio=2)],
            prices=STRIPE_PRICES + [price("paypal", ref="P-PLAN")], failover=False)
    b = client.get("/billing/config").json()
    assert b["available"] is False and b["provider"] is None


@respx.mock
def test_config_nothing_available_returns_message(client):
    mock_db(failover=True)  # stripe has no secret; others disabled / not implemented
    b = client.get("/billing/config").json()
    assert b["available"] is False
    assert b["message"] == "Payments are temporarily unavailable. Your membership isn't affected."
    assert b["tiers"]["premium-lite"]["purchasable"] is True


@respx.mock
def test_config_is_cached_and_invalidatable(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    routes = mock_db()
    client.get("/billing/config")
    client.get("/billing/config")
    assert routes.providers.call_count == 1
    registry.invalidate_cache()
    client.get("/billing/config")
    assert routes.providers.call_count == 2


# ---------------------------------------------------------------------------
# POST /billing/checkout
# ---------------------------------------------------------------------------
@respx.mock
def test_checkout_payment_link_redirect(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mock_db()
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "ios"})
    assert r.status_code == 200
    assert r.json() == {"provider": "stripe", "mode": "redirect", "url": f"{LINK}?client_reference_id={U}"}


@respx.mock
def test_checkout_session_mode_uses_billing(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    seen = {}

    async def fake_session(user_id, price_id, success_url, cancel_url):
        seen.update(user_id=user_id, price_id=price_id, success_url=success_url, cancel_url=cancel_url)
        return "https://checkout.stripe.com/c/pay/cs_1"

    monkeypatch.setattr(billing, "create_checkout_session", fake_session)
    mock_db(providers=[prow("stripe", enabled=True, active=True, mode="hosted_redirect")])
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "web",
                                                "success_url": "https://evil.example/x"})
    assert r.json() == {"provider": "stripe", "mode": "redirect", "url": "https://checkout.stripe.com/c/pay/cs_1"}
    assert seen["user_id"] == U and seen["price_id"] == "price_lite_1"
    assert seen["success_url"] == "https://cheripic.com/#/profile?checkout=success"  # foreign URL replaced
    assert seen["cancel_url"] == "https://cheripic.com/#/pricing"


def _fake_stripe(monkeypatch):
    calls = {}

    def eph(**kw):
        calls["eph"] = kw
        return {"secret": "ek_test_1"}

    def sub_create(**kw):
        calls["sub"] = kw
        return {"id": "sub_new", "latest_invoice": {"confirmation_secret": {"client_secret": "pi_secret_1"}}}

    fake = types.SimpleNamespace(EphemeralKey=types.SimpleNamespace(create=eph),
                                 Subscription=types.SimpleNamespace(create=sub_create))
    monkeypatch.setattr(billing, "_get_stripe", lambda: fake)

    async def cust(uid):
        return "cus_1"

    monkeypatch.setattr(billing, "_find_or_create_customer", cust)
    return calls


@respx.mock
def test_checkout_sdk_mode_params_shape(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    calls = _fake_stripe(monkeypatch)
    mock_db(providers=[prow("stripe", enabled=True, active=True, mode="sdk", pk="pk_test_1")])
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "android"})
    b = r.json()
    assert b["provider"] == "stripe" and b["mode"] == "sdk" and "url" not in b
    p = b["params"]
    assert p["client_secret"] == "pi_secret_1" and p["ephemeral_key"] == "ek_test_1"
    assert p["customer"] == "cus_1" and p["publishable_key"] == "pk_test_1"
    assert calls["sub"]["items"] == [{"price": "price_lite_1"}]
    assert calls["sub"]["metadata"] == {"cheripic_user_id": U}


@respx.mock
def test_checkout_sdk_prefer_redirect_returns_hosted_url(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")

    async def fake_session(**kw):
        return "https://checkout.stripe.com/c/pay/cs_2"

    monkeypatch.setattr(billing, "create_checkout_session", fake_session)
    mock_db(providers=[prow("stripe", enabled=True, active=True, mode="sdk")])
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "ios", "prefer": "redirect"})
    assert r.json() == {"provider": "stripe", "mode": "redirect", "url": "https://checkout.stripe.com/c/pay/cs_2"}


@respx.mock
def test_checkout_sdk_prefer_redirect_without_hosted_page_is_503(client, monkeypatch):
    monkeypatch.setenv("FAKE_GATEWAY_SECRET", "s")
    mock_db(providers=[prow("paypal", enabled=True, active=True, mode="sdk")],
            prices=[price("paypal", ref="P-PLAN")])
    sdk = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "ios"}).json()
    assert sdk == {"provider": "paypal", "mode": "sdk",
                   "params": {"order_id": "O-1", "client_id": "pub_x", "tier": "premium-lite"}}
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "ios", "prefer": "redirect"})
    assert r.status_code == 503 and detail(r)["code"] == "provider_unavailable"


@respx.mock
def test_checkout_premium_is_409(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mock_db()
    r = client.post("/billing/checkout", json={"tier": "premium", "platform": "web"})
    assert r.status_code == 409
    assert detail(r)["code"] == "tier_not_purchasable" and detail(r)["message"]


@respx.mock
def test_checkout_unavailable_is_503(client):
    mock_db()  # no STRIPE_SECRET_KEY, no failover
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "web"})
    assert r.status_code == 503
    assert detail(r) == {"code": "provider_unavailable",
                         "message": "Payments are temporarily unavailable. Your membership isn't affected."}


@respx.mock
def test_checkout_placeholder_not_implemented_is_503(client, monkeypatch):
    monkeypatch.setenv("PADDLE_API_KEY", "k")
    from payments.paddle import PaddleProvider
    from payments.base import ProviderSettings
    import asyncio

    p = PaddleProvider(ProviderSettings(id="paddle", display_name="Paddle", enabled=True))
    assert p.is_configured() is True and p.implemented is False
    with pytest.raises(ProviderNotImplemented):
        asyncio.run(p.create_checkout(None, "premium-lite", "web", success_url="", cancel_url=""))
    # An active-but-unimplemented provider is never selected.
    mock_db(providers=[prow("paddle", enabled=True, active=True)], prices=[price("paddle", ref="pri_1")])
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "web"})
    assert r.status_code == 503 and detail(r)["code"] == "provider_unavailable"


# ---------------------------------------------------------------------------
# POST /billing/portal
# ---------------------------------------------------------------------------
@respx.mock
def test_portal_uses_members_own_provider_after_switch(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("FAKE_GATEWAY_SECRET", "s")
    seen = {}

    async def fake_portal(user_id, return_url):
        seen.update(user_id=user_id, return_url=return_url)
        return "https://billing.stripe.com/p/session/1"

    monkeypatch.setattr(billing, "create_portal_session", fake_portal)
    mock_db(providers=[prow("paypal", enabled=True, active=True, prio=2),
                       prow("stripe", enabled=True, mode="payment_link", prio=1)],
            prices=STRIPE_PRICES + [price("paypal", ref="P-PLAN")],
            member={"tier": "premium-lite", "payment_provider": "stripe", "provider_customer_id": "cus_9",
                    "stripe_customer_id": "cus_9", "provider_subscription_id": "sub_9",
                    "stripe_subscription_id": "sub_9"})
    assert client.get("/billing/config").json()["provider"] == "paypal"  # active switched
    r = client.post("/billing/portal", json={})
    assert r.status_code == 200 and r.json() == {"url": "https://billing.stripe.com/p/session/1"}
    assert seen["user_id"] == U and seen["return_url"] == "https://cheripic.com/#/profile"
    assert FakeGateway.portal_calls == []


@respx.mock
def test_portal_routes_to_non_stripe_provider(client, monkeypatch):
    mock_db(providers=STRIPE_ACTIVE,
            member={"tier": "premium-lite", "payment_provider": "paypal", "provider_customer_id": "PAYER-1",
                    "stripe_customer_id": None, "provider_subscription_id": "I-1", "stripe_subscription_id": None})
    r = client.post("/billing/portal", json={})
    assert r.json() == {"url": "https://paypal.example/autopay"} and FakeGateway.portal_calls == ["PAYER-1"]


@respx.mock
def test_portal_no_subscription_is_404_with_code(client):
    mock_db(member=None)
    r = client.post("/billing/portal", json={})
    assert r.status_code == 404 and detail(r)["code"] == "no_subscription" and detail(r)["message"]


@respx.mock
def test_portal_pre_054_columns_missing_falls_back_to_stripe(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")

    async def fake_portal(user_id, return_url):
        return "https://billing.stripe.com/p/session/2"

    monkeypatch.setattr(billing, "create_portal_session", fake_portal)
    mock_db()

    def members(request):
        if "payment_provider" in request.url.params["select"]:
            return httpx.Response(400, json={"code": "42703", "message": "column does not exist"})
        return httpx.Response(200, json=[{"tier": "premium-lite", "stripe_customer_id": "cus_1"}])

    respx.get(f"{PG}/user_memberships").mock(side_effect=members)
    r = client.post("/billing/portal", json={})
    assert r.json() == {"url": "https://billing.stripe.com/p/session/2"}


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
@respx.mock
def test_webhook_stripe_route_delegates_to_billing(client, monkeypatch):
    seen = {}

    async def fake_handle(payload, signature):
        seen.update(payload=payload, signature=signature)
        return {"event_id": "evt_1", "handled": True}

    monkeypatch.setattr(billing, "handle_event", fake_handle)
    mock_db()
    r = client.post("/billing/webhook/stripe", content=b'{"x":1}', headers={"Stripe-Signature": "t=1,v1=abc"})
    assert r.status_code == 200 and r.json()["event_id"] == "evt_1"
    assert seen == {"payload": b'{"x":1}', "signature": "t=1,v1=abc"}


@respx.mock
def test_webhook_stripe_bad_signature_is_400(client, monkeypatch):
    async def bad(payload, signature):
        raise billing.WebhookSignatureError("nope")

    monkeypatch.setattr(billing, "handle_event", bad)
    mock_db()
    r = client.post("/billing/webhook/stripe", content=b"{}")
    assert r.status_code == 400 and detail(r)["code"] == "invalid_signature"


def test_legacy_webhook_alias_still_routes_to_stripe(monkeypatch):
    import main

    async def fake_handle(payload, signature):
        return {"event_id": "evt_legacy", "handled": True}

    monkeypatch.setattr(billing, "handle_event", fake_handle)
    r = TestClient(main.app).post("/billing/webhook", content=b"{}", headers={"stripe-signature": "s"})
    assert r.status_code == 200 and r.json()["event_id"] == "evt_legacy"
    # and the new per-provider route is mounted on the real app
    assert any(getattr(rt, "path", "") == "/billing/webhook/{provider}" for rt in main.app.routes)


@respx.mock
def test_webhook_unknown_and_placeholder_providers(client):
    mock_db()
    r = client.post("/billing/webhook/bitcoin", content=b"{}")
    assert r.status_code == 404 and detail(r)["code"] == "unknown_provider"
    r = client.post("/billing/webhook/paddle", content=b"{}")
    assert r.status_code == 501 and detail(r)["code"] == "provider_not_implemented"


@respx.mock
def test_webhook_other_provider_normalised_into_memberships(client, monkeypatch):
    import transactional_email

    emails = []

    async def fake_email(**kw):
        emails.append(kw["template_slug"])
        return True

    monkeypatch.setattr(transactional_email, "send_transactional_email", fake_email)
    billing._provider_cols_missing_at = None
    mock_db()
    claim = respx.post(f"{PG}/stripe_events").mock(return_value=httpx.Response(201, json=[]))
    respx.patch(f"{PG}/stripe_events").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{PG}/stripe_events").mock(return_value=httpx.Response(200, json=[]))
    respx.patch(f"{PG}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    ins = respx.post(f"{PG}/user_memberships").mock(return_value=httpx.Response(201, json=[]))
    r = client.post("/billing/webhook/paypal", content=b'{"id": "WH-1"}', headers={"x-fake-sig": "ok"})
    assert r.status_code == 200 and r.json()["handled"] is True
    assert json.loads(claim.calls.last.request.content)["event_id"] == "paypal:WH-1"
    row = json.loads(ins.calls.last.request.content)
    assert row["tier"] == "premium-lite" and row["payment_provider"] == "paypal"
    assert row["provider_subscription_id"] == "I-SUB1" and "stripe_subscription_id" not in row
    assert emails == ["welcome_premium_lite"]
    bad = client.post("/billing/webhook/paypal", content=b'{"id": "WH-2"}', headers={"x-fake-sig": "no"})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# billing.py provider columns
# ---------------------------------------------------------------------------
@respx.mock
async def test_stripe_upsert_sets_provider_columns():
    billing._provider_cols_missing_at = None
    respx.patch(f"{PG}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    ins = respx.post(f"{PG}/user_memberships").mock(return_value=httpx.Response(201, json=[]))
    await billing._upsert_active_membership(U, "premium-lite", stripe_customer_id="cus_1", stripe_subscription_id="sub_1")
    row = json.loads(ins.calls.last.request.content)
    assert row["payment_provider"] == "stripe"
    assert row["provider_customer_id"] == "cus_1" and row["provider_subscription_id"] == "sub_1"
    assert row["stripe_subscription_id"] == "sub_1"


@respx.mock
async def test_stripe_upsert_retries_without_provider_columns_pre_054():
    billing._provider_cols_missing_at = None
    respx.patch(f"{PG}/user_memberships").mock(return_value=httpx.Response(200, json=[]))

    def ins(request):
        body = json.loads(request.content)
        if "payment_provider" in body:
            return httpx.Response(400, json={"code": "PGRST204", "message": "Could not find the 'payment_provider' column"})
        return httpx.Response(201, json=[])

    route = respx.post(f"{PG}/user_memberships").mock(side_effect=ins)
    await billing._upsert_active_membership(U, "premium-lite", stripe_subscription_id="sub_1")
    assert route.call_count == 2
    assert "payment_provider" not in json.loads(route.calls.last.request.content)
    await billing._upsert_active_membership(U, "premium-lite", stripe_subscription_id="sub_1")
    assert route.call_count == 3  # memo: no doomed first attempt
    billing._provider_cols_missing_at = None


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@respx.mock
def test_admin_list_shape(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    mock_db(failover=True)
    respx.get(f"{PG}/payment_provider_events").mock(return_value=httpx.Response(200, json=[
        {"id": "e1", "from_provider": "paypal", "to_provider": "stripe", "changed_by": ADMIN,
         "reason": "restored", "created_at": "2026-10-20T00:00:00+00:00"}]))
    respx.get(f"{AUTH_ADMIN}/{ADMIN}").mock(return_value=httpx.Response(200, json={"email": "ops@cheripic.com"}))

    def members(request):
        return httpx.Response(200, json=[{"payment_provider": "stripe"}, {"payment_provider": "stripe"},
                                         {"payment_provider": "paypal"}])

    respx.get(f"{PG}/user_memberships").mock(side_effect=members)
    b = client.get("/admin/payments/providers").json()
    assert b["subscriber_counts"] == {"stripe": 2, "paypal": 1}
    assert b["payments_auto_failover"] is True
    assert b["events"][0]["actor_email"] == "ops@cheripic.com"
    assert {"from_provider", "to_provider", "reason", "created_at", "actor_email"} <= set(b["events"][0])
    stripe = next(p for p in b["providers"] if p["id"] == "stripe")
    for k in ("id", "display_name", "enabled", "is_active", "checkout_mode", "publishable_key", "config",
              "priority", "secret_configured", "updated_at", "updated_by", "prices"):
        assert k in stripe
    assert stripe["secret_configured"] is True
    assert stripe["prices"] == [{"tier": "premium-lite", "price_ref": "price_lite_1", "payment_link": LINK,
                                 "currency": "usd", "amount": 59.0, "active": True}]
    assert "sk_test_x" not in json.dumps(b)


@respx.mock
def test_admin_put_rejects_secret_publishable_key(client):
    mock_db()
    r = client.put("/admin/payments/providers/stripe", json={"publishable_key": "sk_live_oops"})
    assert r.status_code == 422 and detail(r)["code"] == "secret_not_allowed" and detail(r)["errors"]
    r = client.put("/admin/payments/providers/stripe", json={"publishable_key": "rk_live_oops"})
    assert r.status_code == 422


@respx.mock
def test_admin_put_updates_settings_and_prices(client):
    mock_db()
    patch = respx.patch(f"{PG}/payment_providers").mock(return_value=httpx.Response(204))
    up = respx.post(f"{PG}/payment_provider_prices").mock(return_value=httpx.Response(201))
    r = client.put("/admin/payments/providers/paypal", json={
        "enabled": True, "checkout_mode": "hosted_redirect", "publishable_key": None, "config": {"currency": "usd"},
        "priority": 2, "prices": [
            {"tier": "premium-lite", "price_ref": "P-LITE", "payment_link": None, "amount": 59, "currency": "usd", "active": True},
            {"tier": "premium", "price_ref": None, "payment_link": None, "amount": None, "currency": None, "active": False}]})
    assert r.status_code == 200 and "provider" in r.json()
    body = json.loads(patch.calls.last.request.content)
    assert body["enabled"] is True and body["publishable_key"] is None and body["updated_by"] == ADMIN
    rows = json.loads(up.calls.last.request.content)
    assert rows[0]["price_ref"] == "P-LITE" and rows[1]["currency"] == "usd"
    assert up.calls.last.request.url.params["on_conflict"] == "provider_id,tier"


@respx.mock
def test_admin_put_cannot_disable_active_and_validates(client):
    mock_db()
    r = client.put("/admin/payments/providers/stripe", json={"enabled": False})
    assert r.status_code == 409 and detail(r)["code"] == "cannot_disable_active"
    r = client.put("/admin/payments/providers/paypal", json={"checkout_mode": "magic",
                                                             "prices": [{"tier": "premium-lite", "payment_link": "http://x"}]})
    assert r.status_code == 422 and detail(r)["code"] == "invalid_settings" and len(detail(r)["errors"]) == 2
    assert client.put("/admin/payments/providers/nope", json={}).status_code == 404


@respx.mock
def test_admin_switch_refusals(client, monkeypatch):
    mock_db(providers=STRIPE_ACTIVE + [prow("razorpay", enabled=True, prio=4)])
    r = client.post("/admin/payments/active", json={"provider": "paypal", "reason": "stripe restricted"})
    assert r.status_code == 409
    d = detail(r)
    assert d["code"] == "provider_disabled" and d["message"]
    assert any("disabled" in e for e in d["errors"]) and any("secret" in e.lower() for e in d["errors"])
    assert any("price" in e.lower() for e in d["errors"])
    r = client.post("/admin/payments/active", json={"provider": "razorpay", "reason": "try"})
    assert detail(r)["code"] == "provider_not_implemented"
    r = client.post("/admin/payments/active", json={"provider": "paypal", "reason": ""})
    assert r.status_code == 422 and detail(r)["code"] == "reason_required"
    assert client.post("/admin/payments/active", json={"provider": "nope", "reason": "abc"}).status_code == 404


@respx.mock
def test_admin_switch_success_audits_and_invalidates(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("FAKE_GATEWAY_SECRET", "s")
    routes = mock_db(providers=[prow("stripe", enabled=True, active=True, mode="payment_link"),
                                prow("paypal", enabled=True, prio=2)],
                     prices=STRIPE_PRICES + [price("paypal", ref="P-PLAN")])
    rpc = respx.post(f"{PG}/rpc/set_active_payment_provider").mock(
        return_value=httpx.Response(200, json={"changed": True, "from": "stripe", "to": "paypal"}))
    client.get("/billing/config")
    before = routes.providers.call_count
    r = client.post("/admin/payments/active", json={"provider": "paypal", "reason": "stripe restricted"})
    assert r.status_code == 200 and r.json()["changed"] is True and r.json()["to"] == "paypal"
    assert json.loads(rpc.calls.last.request.content) == {"p_provider": "paypal", "p_admin": ADMIN,
                                                          "p_reason": "stripe restricted"}
    assert registry._cache_at == 0.0  # invalidated
    client.get("/billing/config")
    assert routes.providers.call_count > before + 1  # refetched after the switch


@respx.mock
def test_admin_switch_without_rpc_flips_and_writes_audit_row(client, monkeypatch):
    monkeypatch.setenv("FAKE_GATEWAY_SECRET", "s")
    mock_db(providers=[prow("stripe", enabled=True, active=True, mode="payment_link"),
                       prow("paypal", enabled=True, prio=2)],
            prices=STRIPE_PRICES + [price("paypal", ref="P-PLAN")])
    respx.post(f"{PG}/rpc/set_active_payment_provider").mock(
        return_value=httpx.Response(404, json={"code": "PGRST202"}))
    flips = respx.patch(f"{PG}/payment_providers").mock(return_value=httpx.Response(204))
    audit = respx.post(f"{PG}/payment_provider_events").mock(return_value=httpx.Response(201))
    r = client.post("/admin/payments/active", json={"provider": "paypal", "reason": "stripe restricted"})
    assert r.status_code == 200
    assert [json.loads(c.request.content)["is_active"] for c in flips.calls] == [False, True]
    assert json.loads(audit.calls.last.request.content) == {
        "from_provider": "stripe", "to_provider": "paypal", "changed_by": ADMIN, "reason": "stripe restricted"}


# ---------------------------------------------------------------------------
# Tables missing (054 not applied) → Stripe env + tier_config
# ---------------------------------------------------------------------------
@respx.mock
def test_tables_missing_falls_back_to_stripe_env(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_env")
    mock_db(tables_missing=True, tier_config=[
        {"tier": "premium-lite", "stripe_price_id": "price_REPLACE_PREMIUM_LITE", "stripe_payment_link": LINK,
         "monthly_price_usd": 59},
        {"tier": "premium", "stripe_price_id": "price_REPLACE_PREMIUM",
         "stripe_payment_link": "https://buy.stripe.com/prem", "monthly_price_usd": 149}])
    b = client.get("/billing/config").json()
    assert b["provider"] == "stripe" and b["mode"] == "payment_link" and b["available"] is True
    assert b["publishable_key"] == "pk_env"
    r = client.post("/billing/checkout", json={"tier": "premium-lite", "platform": "web"})
    assert r.json()["url"] == f"{LINK}?client_reference_id={U}"
    # admin view still renders, writes are refused
    a = client.get("/admin/payments/providers").json()
    assert a["source"] == "fallback" and [p["id"] for p in a["providers"]][0] == "stripe"
    w = client.post("/admin/payments/active", json={"provider": "paypal", "reason": "abc"})
    assert w.status_code == 503 and detail(w)["code"] == "payments_tables_missing"


# ---------------------------------------------------------------------------
# GET /billing/me exposes the member's provider
# ---------------------------------------------------------------------------
@respx.mock
def test_billing_me_includes_payment_provider(monkeypatch):
    import main

    async def a(v):
        return v

    monkeypatch.setattr(billing, "get_membership_tier", lambda uid: a("premium-lite"))
    monkeypatch.setattr(billing, "get_quota_period",
                        lambda uid: a({"period_start": "2026-10-01", "period_end": None, "source": "calendar_month"}))
    monkeypatch.setattr(billing, "get_period_usage", lambda uid, p: a(1))
    monkeypatch.setattr(main, "get_monthly_limits", lambda: a({"basic": 10, "premium-lite": 50, "premium": 100}))
    mock_db(member={"tier": "premium-lite", "payment_provider": "stripe", "provider_customer_id": "cus_1",
                    "stripe_customer_id": "cus_1", "provider_subscription_id": "sub_1", "stripe_subscription_id": "sub_1"})
    main.app.dependency_overrides[main.get_current_user_id] = lambda: U
    try:
        r = TestClient(main.app).get(f"/billing/me/{U}")
    finally:
        main.app.dependency_overrides.clear()
    b = r.json()
    assert b["payment_provider"] == "stripe" and b["payment_provider_display_name"] == "Stripe"
    assert b["tier"] == "premium-lite"
