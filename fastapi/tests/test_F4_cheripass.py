"""
F4 — CheriPass server-validated pass + vendor redemption (H11.14 / H13.8) and
anonymous sponsored-click tracking (H11.21). No network: PostgREST is mocked
with respx, the member tier with monkeypatch.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import base64
import json
import time

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import billing
import cheripass
from auth import get_current_user_id
from supabase_client import SUPABASE_URL

REST = f"{SUPABASE_URL}/rest/v1"
U = "11111111-1111-4111-8111-111111111111"
M = "22222222-2222-4222-8222-222222222222"
E = "33333333-3333-4333-8333-333333333333"
BANNER = "44444444-4444-4444-8444-444444444444"
VENDOR = "vendor-key-abc"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CHERIPASS_TOKEN_SECRET", "unit-test-secret")
    monkeypatch.setenv("CHERIPASS_VENDOR_KEYS", f"other-key, {VENDOR}")
    state = {"tier": "premium-lite", "merchant": {"id": M, "name": "Dining Partner", "title": "15% off", "is_active": True,
                                                  "membership_tier": "premium-lite"},
             "experience": {"id": E, "title": "Sunset cruise", "is_active": True, "membership_tier": None},
             "profile": {"user_id": U, "nick_name": "Sam", "is_active": True}, "claims": []}

    async def tier(uid):
        return state["tier"]

    monkeypatch.setattr(billing, "get_membership_tier", tier)
    app = FastAPI()
    app.include_router(cheripass.router)
    app.dependency_overrides[get_current_user_id] = lambda: U
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{REST}/merchants").mock(side_effect=lambda r: httpx.Response(200, json=[state["merchant"]] if state["merchant"] else []))
        mock.get(f"{REST}/experiences").mock(side_effect=lambda r: httpx.Response(200, json=[state["experience"]]))
        mock.get(f"{REST}/user_profiles").mock(side_effect=lambda r: httpx.Response(200, json=[state["profile"]]))

        def claims_get(request):
            q = request.url.params
            hits = [c for c in state["claims"]
                    if q.get("user_id") == f"eq.{c['user_id']}" and q.get("claimed_at") == f"eq.{c['claimed_at']}"]
            return httpx.Response(200, json=hits[:1])

        def claims_post(request):
            row = json.loads(request.content)
            row["id"] = f"claim-{len(state['claims'])}"
            state["claims"].append(row)
            return httpx.Response(201, json=[row])

        mock.get(f"{REST}/claimed_offers").mock(side_effect=claims_get)
        mock.post(f"{REST}/claimed_offers").mock(side_effect=claims_post)
        state["analytics"] = mock.post(f"{REST}/analytics_events").mock(return_value=httpx.Response(201, json=[]))
        yield state, TestClient(app)


def _token(client, kind="merchant", oid=M):
    return client.post("/cheripass/token", json={"offer_kind": kind, "offer_id": oid})


def _verify(client, token, key=VENDOR):
    return client.post("/cheripass/verify", json={"token": token}, headers={"X-Vendor-Key": key} if key else {})


# ------------------------------------------------------------------ token

def test_eligible_member_gets_short_lived_signed_token(env):
    state, client = env
    r = _token(client)
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "Sam" and body["tier_label"] == "Premium Lite" and body["offer_title"] == "Dining Partner"
    assert body["expires_in"] == 120
    payload = cheripass.decode_token(body["token"])
    assert payload["u"] == U and payload["o"] == M and payload["k"] == "m" and payload["t"] == "premium-lite"
    assert payload["e"] - payload["i"] == 120 and payload["n"]
    # No legal name / email / contact in the token.
    assert "@" not in base64.urlsafe_b64decode(body["token"].split(".")[0] + "==").decode()


def test_null_experience_tier_is_free_and_nonces_differ(env):
    state, client = env
    state["tier"] = "basic"
    a, b = _token(client, "experience", E).json(), _token(client, "experience", E).json()
    assert a["tier_label"] == "Free" and a["offer_title"] == "Sunset cruise"
    assert cheripass.decode_token(a["token"])["n"] != cheripass.decode_token(b["token"])["n"]


def test_ineligible_tier_is_403(env):
    state, client = env
    state["tier"] = "basic"
    r = _token(client)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "tier_ineligible"
    assert "Premium Lite" in r.json()["detail"]["message"]


def test_inactive_offer_is_410_not_an_upsell(env):
    state, client = env
    state["tier"] = "basic"  # even when the tier is also insufficient, withdrawn wins (never a payment cure)
    state["merchant"]["is_active"] = False
    r = _token(client)
    assert r.status_code == 410 and r.json()["detail"]["code"] == "offer_inactive"


def test_missing_offer_and_bad_id(env):
    state, client = env
    state["merchant"] = None
    assert _token(client).json()["detail"]["code"] == "offer_not_found"
    assert _token(client, oid="not-a-uuid").status_code == 400
    assert client.post("/cheripass/token", json={"offer_kind": "voucher", "offer_id": M}).status_code == 422


def test_suspended_account_is_refused(env):
    state, client = env
    state["profile"]["is_active"] = False
    r = _token(client)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_inactive"


def test_token_requires_auth():
    app = FastAPI()
    app.include_router(cheripass.router)
    assert TestClient(app).post("/cheripass/token", json={"offer_kind": "merchant", "offer_id": M}).status_code == 401


# ------------------------------------------------------------------ verify

def test_vendor_redeems_once_then_already_redeemed(env):
    state, client = env
    token = _token(client).json()["token"]
    first = _verify(client, token)
    assert first.status_code == 200 and first.json()["status"] == "redeemed"
    assert first.json()["tier_label"] == "Premium Lite" and first.json()["display_name"] == "Sam"
    assert len(state["claims"]) == 1
    claim = state["claims"][0]
    assert claim["user_id"] == U and claim["merchant_id"] == M and claim["is_used"] is True and claim["used_at"]
    second = _verify(client, token)
    assert second.status_code == 200 and second.json()["status"] == "already_redeemed"
    assert len(state["claims"]) == 1  # no second row


def test_new_token_for_same_offer_is_a_separate_redemption(env):
    state, client = env
    _verify(client, _token(client).json()["token"])
    time.sleep(0)  # different nonce → different key even within the same second
    assert _verify(client, _token(client).json()["token"]).json()["status"] == "redeemed"
    assert len(state["claims"]) == 2 and state["claims"][0]["claimed_at"] != state["claims"][1]["claimed_at"]


def test_tampered_token_rejected(env):
    state, client = env
    token = _token(client).json()["token"]
    body, sig = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["t"] = "premium"
    forged = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    for bad in (f"{forged}.{sig}", f"{body}.{sig[:-2]}AA", "garbage-token-value", body):
        r = _verify(client, bad)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_token"
    assert state["claims"] == []


def test_token_signed_with_other_secret_rejected(env, monkeypatch):
    state, client = env
    token = _token(client).json()["token"]
    monkeypatch.setenv("CHERIPASS_TOKEN_SECRET", "rotated-secret")
    assert _verify(client, token).json()["detail"]["code"] == "invalid_token"


def test_expired_token_rejected(env):
    state, client = env
    token, _ = cheripass.issue_token(uid=U, offer_kind="merchant", offer_id=M, tier="premium-lite", now=time.time() - 121)
    r = _verify(client, token)
    assert r.status_code == 410 and r.json()["detail"]["code"] == "token_expired"
    assert state["claims"] == []


def test_verify_rechecks_tier_and_offer_now(env):
    state, client = env
    token = _token(client).json()["token"]
    state["tier"] = "basic"  # membership lapsed after the pass was shown
    assert _verify(client, token).json()["detail"]["code"] == "tier_ineligible"
    state["tier"] = "premium-lite"
    state["merchant"]["is_active"] = False  # partner withdrew the offer
    assert _verify(client, token).json()["detail"]["code"] == "offer_inactive"
    assert state["claims"] == []


def test_vendor_key_required(env, monkeypatch):
    state, client = env
    token = _token(client).json()["token"]
    assert _verify(client, token, key=None).json()["detail"]["code"] == "vendor_unauthorized"
    assert _verify(client, token, key="wrong").status_code == 401
    monkeypatch.setenv("CHERIPASS_VENDOR_KEYS", "")
    assert _verify(client, token).status_code == 503
    assert state["claims"] == []


def test_fallback_secret_is_derived_not_the_service_key(monkeypatch):
    monkeypatch.delenv("CHERIPASS_TOKEN_SECRET", raising=False)
    s = cheripass._secret()
    assert s and s != cheripass.SUPABASE_KEY.encode()
    token, _ = cheripass.issue_token(uid=U, offer_kind="experience", offer_id=E, tier="basic")
    assert cheripass.decode_token(token)["k"] == "e"


# ------------------------------------------------------------------ sponsored click

def test_sponsored_click_stores_only_banner_and_kind(env):
    state, client = env
    r = client.post("/analytics/sponsored-click", json={"banner_id": BANNER, "kind": "whatsapp"})
    assert r.status_code == 202 and r.json() == {"recorded": True}
    row = json.loads(state["analytics"].calls.last.request.content)
    assert row == {"user_id": None, "event_type": "sponsored_click", "metadata": {"banner_id": BANNER, "kind": "whatsapp"}}


def test_sponsored_click_validation_and_failure_is_swallowed(env):
    state, client = env
    assert client.post("/analytics/sponsored-click", json={"banner_id": BANNER, "kind": "sms"}).status_code == 422
    assert client.post("/analytics/sponsored-click", json={"banner_id": "x", "kind": "web"}).status_code == 400
    state["analytics"].mock(return_value=httpx.Response(401, json={"message": "rls"}))
    r = client.post("/analytics/sponsored-click", json={"banner_id": BANNER, "kind": "web"})
    assert r.status_code == 202 and r.json() == {"recorded": False}
