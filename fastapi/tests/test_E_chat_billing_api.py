"""
Audit E (area-D findings in main.py) — /chat quota/ledger/error handling,
input validation, production config guard, billing redirect allow-list,
generic 500s and the atomic quota reservation client. No network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import billing
import main

U = "uuuuuuuu-0000-0000-0000-00000000000u"
PERIOD = {"period_start": "2026-10-01", "period_end": None, "source": "calendar_month"}


@pytest.fixture
def chat_env(monkeypatch):
    state = {"saved": [], "ledger": [], "released": [], "incremented": [], "reserve": 7, "used": 3,
             "llm": ('{"reply": "Hello there", "follow_up": ""}', {"model": "gpt-4o-mini", "prompt_tokens": 10,
                                                                  "completion_tokens": 5, "total_tokens": 15})}

    async def a(value):
        return value

    monkeypatch.setattr(main, "classify_canned", lambda m: None)
    monkeypatch.setattr(main, "fetch_user_profile", lambda uid: a({"user_id": uid, "nick_name": "Secret Nick"}))
    monkeypatch.setattr(main, "load_memory", lambda uid: a({"summary": "", "insights": []}))
    monkeypatch.setattr(main, "load_recent_messages", lambda uid: a([]))
    monkeypatch.setattr(main, "get_monthly_limits", lambda: a({"basic": 10, "premium-lite": 50, "premium": float("inf")}))
    monkeypatch.setattr(billing, "get_membership_tier", lambda uid: a("basic"))
    monkeypatch.setattr(billing, "get_quota_period", lambda uid: a(PERIOD))
    monkeypatch.setattr(billing, "get_period_usage", lambda uid, p: a(state["used"]))
    monkeypatch.setattr(billing, "try_reserve_message", lambda uid, p, lim: a(state["reserve"]))

    async def release(uid, p):
        state["released"].append(uid)

    async def incr(uid, p):
        state["incremented"].append(uid)
        return 99

    monkeypatch.setattr(billing, "release_message", release)
    monkeypatch.setattr(billing, "increment_period_usage", incr)

    async def llm(prompt, user=None, json_mode=False, **k):
        if isinstance(state["llm"], Exception):
            raise state["llm"]
        return state["llm"]

    monkeypatch.setattr(main, "send_to_llm_with_usage", llm)

    async def save(*args):
        state["saved"].append(args)

    monkeypatch.setattr(main, "save_conversation_message", save)

    async def mem(*a, **k):
        return None

    monkeypatch.setattr(main, "update_memory_async", mem)

    async def ledger(row):
        state["ledger"].append(row)

    monkeypatch.setattr(main, "_record_ai_usage", ledger)
    main.app.dependency_overrides[main.get_current_user_id] = lambda: U
    yield state, TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_chat_success_uses_reservation_and_records_ledger_without_content(chat_env, caplog):
    state, client = chat_env
    caplog.set_level("INFO")
    r = client.post("/chat", json={"message": "How do I open up more?", "stage": "general"})
    assert r.status_code == 200
    assert r.json()["reply"] == "Hello there" and r.json()["monthly_used"] == 7
    assert state["incremented"] == []  # atomic reservation already counted it
    row = state["ledger"][0]
    assert row["purpose"] == "chat" and row["total_tokens"] == 15 and row["canned"] is False
    assert "How do I open up" not in str(row) and "Hello there" not in str(row)
    assert "Secret Nick" not in caplog.text  # nick_name no longer logged


def test_chat_limit_message_has_space_and_reset(chat_env):
    state, client = chat_env
    state["used"] = 10
    r = client.post("/chat", json={"message": "hi there friend"})
    assert r.status_code == 429
    msg = r.json()["detail"]["message"]
    assert msg == "You've used this period's CheriAI messages. Your allowance resets at the start of next month (UTC)."


def test_chat_reservation_refused_is_429(chat_env):
    state, client = chat_env
    state["reserve"] = False
    r = client.post("/chat", json={"message": "hi there friend"})
    assert r.status_code == 429
    assert "resets" in r.json()["detail"]["message"]


def test_chat_empty_json_reply_is_502_not_saved_and_released(chat_env):
    state, client = chat_env
    state["llm"] = ('{"reply": "", "follow_up": ""}', {"model": "gpt-4o-mini"})
    r = client.post("/chat", json={"message": "hi there friend"})
    assert r.status_code == 502
    assert state["saved"] == [] and state["released"] == [U]
    assert "{" not in r.text.replace('{"detail"', "")


def test_chat_internal_error_is_generic_500_and_released(chat_env):
    state, client = chat_env
    state["llm"] = RuntimeError("upstream said: sk-secret-ish detail")
    r = client.post("/chat", json={"message": "hi there friend"})
    assert r.status_code == 500 and "secret" not in r.text
    assert state["released"] == [U]


def test_chat_legacy_path_when_rpc_missing(chat_env):
    state, client = chat_env
    state["reserve"] = None
    r = client.post("/chat", json={"message": "hi there friend"})
    assert r.status_code == 200 and r.json()["monthly_used"] == 99 and state["incremented"] == [U]


def test_chat_canned_branch_records_canned_ledger_row(chat_env, monkeypatch):
    state, client = chat_env
    monkeypatch.setattr(main, "classify_canned", lambda m: "greeting")
    monkeypatch.setattr(main, "respond_canned", lambda c, name=None: ("Hi!", None))
    assert client.post("/chat", json={"message": "hi"}).status_code == 200
    assert state["ledger"][0]["canned"] is True and state["ledger"][0]["total_tokens"] == 0


def test_chat_input_validation(chat_env):
    state, client = chat_env
    assert client.post("/chat", json={"message": "x" * 2001}).status_code == 422
    r = client.post("/chat", json={"message": "hello friend", "stage": "<script>"})
    assert r.status_code == 200 and r.json()["stage"] == "general"
    assert client.post("/chat", json={"message": "hello friend", "stage": "matching"}).json()["stage"] == "matching"


def test_split_reply_never_returns_raw_json():
    assert main._split_reply_and_followup('{"reply": "", "follow_up": "x"}') == ("", None)
    assert main._split_reply_and_followup("plain text") == ("plain text", None)


def test_production_requires_openai_key(monkeypatch):
    import llm_client

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setattr(llm_client, "OPENAI_API_KEY", None)
    with pytest.raises(RuntimeError):
        main._assert_production_config()
    monkeypatch.setenv("ENVIRONMENT", "development")
    main._assert_production_config()


def test_safe_return_url(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://cheripic.com")
    assert main._safe_return_url("https://cheripic.com/profile") == "https://cheripic.com/profile"
    assert main._safe_return_url("cheripic://billing") == "cheripic://billing"
    assert main._safe_return_url("https://evil.example/phish") == "https://cheripic.com/#/profile"
    assert main._safe_return_url("https://user@cheripic.com/x") == "https://cheripic.com/#/profile"
    assert main._safe_return_url(None) == "https://cheripic.com/#/profile"


def test_portal_session_ignores_foreign_return_url(monkeypatch):
    seen = {}

    async def portal(user_id, return_url):
        seen["url"] = return_url
        return "https://billing.stripe.com/session/x"

    monkeypatch.setenv("APP_BASE_URL", "https://cheripic.com")
    monkeypatch.setattr(billing, "create_portal_session", portal)
    # The legacy endpoint now delegates to the payment layer (member's own
    # provider = Stripe here); keep the test offline.
    from payments import registry
    from payments.base import Membership, ProviderSettings
    from payments.stripe_provider import StripeProvider

    async def member(uid):
        return Membership(user_id=uid, tier="premium-lite", payment_provider="stripe", customer_id="cus_1")

    async def provider(pid):
        return StripeProvider(ProviderSettings(id="stripe", display_name="Stripe", enabled=True))

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(registry, "get_member_billing", member)
    monkeypatch.setattr(registry, "get_provider", provider)
    main.app.dependency_overrides[main.get_current_user_id] = lambda: U
    try:
        r = TestClient(main.app).post("/billing/portal-session", json={"user_id": U, "return_url": "https://evil.example"})
    finally:
        main.app.dependency_overrides.clear()
    assert r.status_code == 200 and seen["url"] == "https://cheripic.com/#/profile"


def test_conversation_endpoints_return_generic_500(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("relation chat_history: internal detail")

    monkeypatch.setattr(main.supabase, "select", boom)
    main.app.dependency_overrides[main.get_current_user_id] = lambda: U
    c = TestClient(main.app)
    try:
        for path in (f"/cheri-thread/{U}", f"/conversations/{U}", f"/conversations/{U}/c1"):
            r = c.get(path)
            assert r.status_code == 500 and "internal detail" not in r.text, path
    finally:
        main.app.dependency_overrides.clear()


@respx.mock
async def test_try_reserve_message_semantics():
    url = f"{main.SUPABASE_URL}/rest/v1/rpc/reserve_cheri_message"
    route = respx.post(url).mock(return_value=httpx.Response(200, json=5))
    assert await billing.try_reserve_message(U, "2026-10-01", 10) == 5
    assert route.calls.last.request.read() and b'"p_limit":10' in route.calls.last.request.content.replace(b" ", b"")
    route.mock(return_value=httpx.Response(200, content=b"null", headers={"content-type": "application/json"}))
    assert await billing.try_reserve_message(U, "2026-10-01", 10) is False
    route.mock(return_value=httpx.Response(404, json={}))
    assert await billing.try_reserve_message(U, "2026-10-01", 10) is None
