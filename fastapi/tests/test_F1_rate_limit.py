"""
F1 · X-SEC.3 rate limiting (slowapi wired in main.py).

TestClient without `with` (no lifespan). Buckets are reset before every
test by tests/conftest.py. Downstream calls are monkeypatched; no network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import pytest
from fastapi.testclient import TestClient

import auth
import main

A = "aaaaaaaa-0000-0000-0000-00000000000a"
B = "bbbbbbbb-0000-0000-0000-00000000000b"


@pytest.fixture
def client(monkeypatch):
    main.app.dependency_overrides[main.get_current_user_id] = lambda: A

    async def fake_delete(uid):
        return {"ok": True, "status": "completed", "deleted_at": "now"}

    monkeypatch.setattr(main, "delete_account", fake_delete)
    monkeypatch.setattr("auth.forget_user", lambda uid: None)
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    auth._cache.clear()


def test_account_delete_fourth_call_in_an_hour_is_429_with_generic_body(client):
    for _ in range(3):
        assert client.post("/account/delete").status_code == 200
    r = client.post("/account/delete")
    assert r.status_code == 429
    assert r.json() == {"detail": "too_many_requests"}  # no limit/bucket internals
    assert r.headers.get("retry-after") == "60"


def test_bucket_is_the_verified_member_not_the_ip(client):
    # Tokens verified by get_current_user_id land in auth._cache; the key
    # function only trusts that cache (never an unverified JWT sub).
    auth._cache["tok-a"] = (9e18, A)
    auth._cache["tok-b"] = (9e18, B)
    for _ in range(3):
        assert client.post("/account/delete", headers={"Authorization": "Bearer tok-a"}).status_code == 200
    assert client.post("/account/delete", headers={"Authorization": "Bearer tok-a"}).status_code == 429
    # Same IP, different verified member → own bucket.
    assert client.post("/account/delete", headers={"Authorization": "Bearer tok-b"}).status_code == 200


def test_key_function_falls_back_to_ip_for_unverified_tokens():
    class Req:
        headers = {"authorization": "Bearer forged.jwt.value"}
        client = type("C", (), {"host": "203.0.113.9"})()

    assert main.rate_limit_key(Req()) == "ip:203.0.113.9"
    auth._cache["good"] = (9e18, A)
    Req.headers = {"authorization": "Bearer good"}
    assert main.rate_limit_key(Req()) == f"user:{A}"
    auth._cache.clear()


def test_trust_proxy_uses_first_forwarded_hop(monkeypatch):
    class Req:
        headers = {"x-forwarded-for": "198.51.100.7, 10.0.0.1"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert main.rate_limit_key(Req()) == "ip:10.0.0.1"
    monkeypatch.setenv("RATE_LIMIT_TRUST_PROXY", "true")
    assert main.rate_limit_key(Req()) == "ip:198.51.100.7"


def test_withdraw_interest_31st_call_in_a_minute_is_429(client, monkeypatch):
    async def fake(request_id, caller):
        return {"ok": True, "status": "withdrawn", "receiver_id": B}

    monkeypatch.setattr(main, "withdraw_interest", fake)
    for _ in range(30):
        assert client.post("/matching/withdraw-interest", json={"request_id": "rrrrrrrr-1"}).status_code == 200
    assert client.post("/matching/withdraw-interest", json={"request_id": "rrrrrrrr-1"}).status_code == 429


def test_trigger_self_email_sixth_call_in_an_hour_is_429(client, monkeypatch):
    async def fake_send(**kw):
        return True

    monkeypatch.setattr(main, "send_transactional_email", fake_send)
    for _ in range(5):
        assert client.post("/emails/trigger-self", json={"template_slug": "welcome_free"}).status_code == 200
    assert client.post("/emails/trigger-self", json={"template_slug": "welcome_free"}).status_code == 429


def test_every_sensitive_endpoint_has_its_configured_limit():
    limits = {k: [str(l.limit) for l in v] for k, v in main.limiter._route_limits.items()}
    assert limits["main.chat"] == ["20 per 1 minute"]
    assert limits["main.account_delete"] == ["3 per 1 hour"]
    assert limits["main.verify_face"] == ["5 per 1 hour"]
    assert limits["main.matching_withdraw_interest"] == ["30 per 1 minute"]
    assert limits["main.trigger_self_email"] == ["5 per 1 hour"]
    for name in (
        "matching_notify_interest_received", "matching_notify_partner_proposal", "matching_notify_unmatch",
        "matching_notify_interest_responded", "matching_notify_partner_proposal_declined",
        "chat_notify_unread_message", "assessment_notify_completed",
    ):
        assert limits[f"main.{name}"] == ["30 per 1 minute"], name


def test_limits_are_env_configurable(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_CHAT", "7/minute")
    assert main._rl("CHAT", "20/minute") == "7/minute"
    monkeypatch.delenv("RATE_LIMIT_CHAT")
    assert main._rl("CHAT", "20/minute") == "20/minute"
