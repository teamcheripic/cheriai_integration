"""
G · compliance gates on the HTTP surface: standing, CheriAI consent + safety,
checkout gates (age, standing, country, agreement, duplicate/upgrade),
prompt minimisation, /ai/report, /me/export, /admin/moderation/notify.

compliance.py helpers are stubbed by tests/conftest.py ("nothing changes"
answers); each test overrides the one it exercises. Supabase is respx-mocked
where a route talks to it directly. No network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import billing
import compliance
import compliance_api
import main
import payments_api
from payments.base import CheckoutResult
from payments.registry import Resolution

U = "uuuuuuuu-0000-0000-0000-00000000000u"
B = "bbbbbbbb-0000-0000-0000-00000000000b"
ADMIN = "adadadad-0000-0000-0000-0000000000ad"
REST = compliance._REST
PERIOD = {"period_start": "2026-11-01", "period_end": None, "source": "calendar_month"}


async def _a(v):
    return v


def _set(monkeypatch, name, value):
    monkeypatch.setattr(compliance, name, lambda *a, **k: _a(value))


@pytest.fixture
def member():
    main.app.dependency_overrides[main.get_current_user_id] = lambda: U
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


@pytest.fixture
def admin():
    main.app.dependency_overrides[main.get_current_admin_id] = lambda: ADMIN
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


# =============================================================================
# Standing
# =============================================================================

def test_terminated_member_gets_account_terminated(member, monkeypatch):
    _set(monkeypatch, "member_standing", "terminated")
    r = member.post("/matching/withdraw-interest", json={"request_id": "11111111-1111-1111-1111-111111111111"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "account_terminated" and r.json()["detail"]["message"]


def test_terminated_member_can_still_delete_account(member, monkeypatch):
    _set(monkeypatch, "member_standing", "terminated")
    monkeypatch.setattr(main, "delete_account", lambda uid: _a({"ok": True, "status": "completed"}))
    monkeypatch.setattr("auth.forget_user", lambda uid: None)
    assert member.post("/account/delete").status_code == 200


def test_restricted_member_blocked_on_contact_routes_only(member, monkeypatch):
    _set(monkeypatch, "member_standing", "restricted")
    r = member.post("/chat/notify-unread-message", json={"receiver_user_id": B})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_restricted"
    r = member.post("/matching/notify-interest-received", json={"receiver_user_id": B})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_restricted"
    # A non-contact member route still works for a restricted member.
    monkeypatch.setattr(main, "withdraw_interest", lambda rid, uid: _a({"ok": True, "status": "withdrawn"}))
    r = member.post("/matching/withdraw-interest", json={"request_id": "11111111-1111-1111-1111-111111111111"})
    assert r.status_code == 200


def test_contact_and_member_dependencies_are_wired():
    expect = {
        "/chat/notify-unread-message": "get_contact_user_id",
        "/matching/notify-interest-received": "get_contact_user_id",
        "/matching/notify-interest-responded": "get_contact_user_id",
        "/connect/activate": "get_contact_user_id",
        "/chat": "get_member_user_id",
        "/memory/seed": "get_member_user_id",
        "/media/photos/{target_id}": "get_member_user_id",
        "/cheripass/token": "get_member_user_id",
    }
    seen = {}
    for r in main.app.routes:
        if getattr(r, "path", None) in expect:
            seen[r.path] = {d.call.__name__ for d in r.dependant.dependencies}
    for path, dep in expect.items():
        assert dep in seen[path], (path, seen.get(path))
    # Deletion / export / portal / AI report stay ungated.
    for r in main.app.routes:
        if getattr(r, "path", None) in ("/account/delete", "/me/export", "/billing/portal", "/ai/report"):
            names = {d.call.__name__ for d in r.dependant.dependencies}
            assert "get_member_user_id" not in names and "get_contact_user_id" not in names


async def test_require_good_standing_unit(monkeypatch):
    from fastapi import HTTPException

    from auth import require_good_standing

    _set(monkeypatch, "member_standing", "suspended")
    assert await require_good_standing(U) == "suspended"
    with pytest.raises(HTTPException) as e:
        await require_good_standing(U, contact=True)
    assert e.value.detail["code"] == "account_restricted"
    _set(monkeypatch, "member_standing", "warned")
    assert await require_good_standing(U, contact=True) == "warned"


# =============================================================================
# CheriAI: consent, crisis router, moderation
# =============================================================================

@pytest.fixture
def chat(monkeypatch, member):
    st = {"llm_calls": 0, "saved": [], "memory": 0, "events": [],
          "llm": ('{"reply": "Hello there", "follow_up": ""}', {"model": "gpt-4o-mini"})}
    monkeypatch.setattr(main, "classify_canned", lambda m: None)
    monkeypatch.setattr(main, "fetch_user_profile", lambda uid: _a({"user_id": uid, "nick_name": "N"}))
    monkeypatch.setattr(main, "load_memory", lambda uid: _a({"summary": "", "insights": []}))
    monkeypatch.setattr(main, "load_recent_messages", lambda uid: _a([]))
    monkeypatch.setattr(main, "get_monthly_limits", lambda: _a({"basic": 10, "premium-lite": 50, "premium": float("inf")}))
    monkeypatch.setattr(billing, "get_membership_tier", lambda uid: _a("basic"))
    monkeypatch.setattr(billing, "get_quota_period", lambda uid: _a(PERIOD))
    monkeypatch.setattr(billing, "get_period_usage", lambda uid, p: _a(2))
    monkeypatch.setattr(billing, "try_reserve_message", lambda uid, p, lim: _a(3))
    monkeypatch.setattr(billing, "release_message", lambda uid, p: _a(None))

    async def llm(prompt, user=None, json_mode=False, **k):
        st["llm_calls"] += 1
        return st["llm"]

    async def save(*args):
        st["saved"].append(args)
        return f"h{len(st['saved'])}"  # chat_history id of this row

    async def mem(*a, **k):
        st["memory"] += 1

    async def event(uid, stage, **k):
        st["events"].append((stage, k.get("action")))
        return True

    monkeypatch.setattr(main, "send_to_llm_with_usage", llm)
    monkeypatch.setattr(main, "save_conversation_message", save)
    monkeypatch.setattr(main, "update_memory_async", mem)
    monkeypatch.setattr(main, "_record_ai_usage", lambda row: _a(None))
    monkeypatch.setattr(compliance, "log_ai_safety_event", event)
    return st, member


def test_chat_without_consent_is_403_ai_consent_required(chat, monkeypatch):
    st, c = chat
    _set(monkeypatch, "has_ai_consent", False)
    r = c.post("/chat", json={"message": "hello there friend"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ai_consent_required"
    assert st["llm_calls"] == 0 and st["saved"] == []


def test_canned_reply_is_also_consent_gated(chat, monkeypatch):
    st, c = chat
    monkeypatch.setattr(main, "classify_canned", lambda m: "greeting")
    _set(monkeypatch, "has_ai_consent", False)
    assert c.post("/chat", json={"message": "hi"}).status_code == 403


def test_chat_consent_unknown_is_503_and_nothing_sent(chat, monkeypatch):
    st, c = chat
    _set(monkeypatch, "has_ai_consent", None)
    r = c.post("/chat", json={"message": "hello there friend"})
    assert r.status_code == 503 and isinstance(r.json()["detail"], str) and st["llm_calls"] == 0


def test_crisis_message_gets_fixed_reply_without_llm(chat, monkeypatch):
    st, c = chat
    # Even when moderation would error, the router applies.
    _set(monkeypatch, "moderate", {"flagged": False, "categories": [], "self_harm": False, "error": True})
    r = c.post("/chat", json={"message": "I want to kill myself"})
    assert r.status_code == 200
    assert r.json()["reply"] == compliance.CRISIS_REPLY and r.json()["follow_up"] is None
    assert st["llm_calls"] == 0 and st["memory"] == 0
    assert ("crisis", "crisis_reply") in st["events"]
    assert r.json()["daily_used"] == 2  # no quota consumed


def test_crisis_reply_does_not_wait_for_consent(chat, monkeypatch):
    st, c = chat
    _set(monkeypatch, "has_ai_consent", False)
    r = c.post("/chat", json={"message": "i want to end my life"})
    assert r.status_code == 200 and r.json()["reply"] == compliance.CRISIS_REPLY and st["llm_calls"] == 0


def test_flagged_input_gets_safe_reply_without_llm(chat, monkeypatch):
    st, c = chat
    _set(monkeypatch, "moderate", {"flagged": True, "categories": ["harassment"], "self_harm": False, "error": False})
    r = c.post("/chat", json={"message": "something abusive here"})
    assert r.json()["reply"] == compliance.SAFE_INPUT_REPLY and st["llm_calls"] == 0
    assert ("input", "refused") in st["events"]


def test_flagged_self_harm_input_gets_crisis_reply(chat, monkeypatch):
    st, c = chat
    _set(monkeypatch, "moderate", {"flagged": True, "categories": ["self-harm"], "self_harm": True, "error": False})
    r = c.post("/chat", json={"message": "an oblique message"})
    assert r.json()["reply"] == compliance.CRISIS_REPLY and st["llm_calls"] == 0


def test_flagged_output_is_replaced_and_not_distilled(chat, monkeypatch):
    st, c = chat
    calls = []

    async def moderate(text):
        calls.append(text)
        flagged = "Hello there" in text
        return {"flagged": flagged, "categories": ["violence"] if flagged else [], "self_harm": False, "error": False}

    monkeypatch.setattr(compliance, "moderate", moderate)
    r = c.post("/chat", json={"message": "tell me something nice"})
    assert r.status_code == 200
    assert r.json()["reply"] == compliance.SAFE_OUTPUT_FALLBACK and r.json()["follow_up"] is None
    assert st["llm_calls"] == 1 and st["memory"] == 0 and len(calls) == 2
    assert ("output", "replaced") in st["events"]


def test_clean_chat_unchanged(chat):
    st, c = chat
    r = c.post("/chat", json={"message": "tell me something nice"})
    assert r.json()["reply"] == "Hello there" and st["memory"] == 1


def test_chat_returns_assistant_and_follow_up_message_ids(chat):
    st, c = chat
    st["llm"] = ('{"reply": "Main reply", "follow_up": "A follow-up?"}', {"model": "gpt-4o-mini"})
    b = c.post("/chat", json={"message": "tell me something nice"}).json()
    # saved rows: user (h1), assistant (h2), follow-up (h3)
    assert b["assistant_message_id"] == "h2" and b["follow_up_message_id"] == "h3"
    assert st["saved"][1][2] == "assistant" and st["saved"][1][3] == "Main reply"


def test_chat_without_follow_up_has_null_follow_up_id(chat):
    st, c = chat
    b = c.post("/chat", json={"message": "tell me something nice"}).json()
    assert b["assistant_message_id"] == "h2" and b["follow_up_message_id"] is None


def test_static_replies_carry_assistant_message_id(chat, monkeypatch):
    st, c = chat
    monkeypatch.setattr(main, "classify_canned", lambda m: "greeting")
    b = c.post("/chat", json={"message": "hi"}).json()
    assert b["assistant_message_id"] == "h2"
    b = c.post("/chat", json={"message": "I want to kill myself"}).json()
    assert b["assistant_message_id"] is not None


def test_chat_history_write_failure_leaves_ids_null(chat, monkeypatch):
    st, c = chat

    async def boom(*a):
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "save_conversation_message", boom)
    b = c.post("/chat", json={"message": "tell me something nice"}).json()
    assert b["reply"] == "Hello there" and b["assistant_message_id"] is None


async def test_save_conversation_message_returns_row_id(monkeypatch):
    async def insert(table, payload):
        assert table == "chat_history" and payload["role"] == "assistant"
        return [{"id": 42, **payload}]

    monkeypatch.setattr(main.supabase, "insert", insert)
    assert await main.save_conversation_message(U, "c", "assistant", "x", "general") == "42"

    async def empty(table, payload):
        return []

    monkeypatch.setattr(main.supabase, "insert", empty)
    assert await main.save_conversation_message(U, "c", "user", "x", "general") is None


def test_cheri_thread_selects_and_returns_ids(member, monkeypatch):
    seen = {}

    async def select(table, columns="*", **k):
        seen["columns"] = columns
        return [{"id": "h2", "role": "assistant", "content": "b", "created_at": "t2", "conversation_id": "c"},
                {"id": "h1", "role": "user", "content": "a", "created_at": "t1", "conversation_id": "c"}]

    monkeypatch.setattr(main.supabase, "select", select)
    r = member.get(f"/cheri-thread/{U}?limit=50")
    assert "id" in seen["columns"].split(",")
    assert [m["id"] for m in r.json()["messages"]] == ["h1", "h2"]


def test_memory_seed_without_consent_is_quiet_skip(member, monkeypatch):
    _set(monkeypatch, "has_ai_consent", False)
    called = []
    monkeypatch.setattr(main, "seed_memory_from_profile", lambda uid: called.append(uid) or _a({}))
    r = member.post("/memory/seed", json={"user_id": U})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "summary": "", "insights_count": 0, "skipped": "no_consent"}
    assert called == []


def test_admin_reseed_respects_member_consent(admin, monkeypatch):
    _set(monkeypatch, "has_ai_consent", False)
    r = admin.post("/memory/admin-reseed", json={"user_id": U})
    assert r.json()["skipped"] == "no_consent"


async def test_memory_llm_paths_skip_without_consent(monkeypatch):
    import user_memory

    _set(monkeypatch, "has_ai_consent", False)
    calls = []

    async def llm(*a, **k):
        calls.append(1)
        return "{}", {}

    monkeypatch.setattr(user_memory, "send_to_llm_with_usage", llm)
    monkeypatch.setattr(user_memory, "load_memory", lambda uid: _a({"summary": "", "insights": [], "turn_count": 0}))
    assert await user_memory.seed_memory_from_profile(U) == {}
    await user_memory.update_memory_async(U, "a long enough message to distill for sure", "reply")
    assert calls == []


def test_prompt_drops_religion_and_compatibility_score():
    from cheriai_prompts import CheriAIPromptBuilder

    user = {"user_id": U, "nick_name": "Sam",
            "bio": json.dumps({"religion": "Zoroastrian", "profession": "Chef"})}
    b = CheriAIPromptBuilder(user, "general", memory={"summary": "", "insights": []}, recent_messages=[])
    prompt = b.build("hi", {"current_match": {"nick_name": "Alex", "compatibility_score": 87}})
    text = json.dumps(prompt)
    assert "Zoroastrian" not in text and "Religion" not in text
    assert "87%" not in text and "compatible" not in text
    assert "Chef" in text and "Alex" in text


def test_seed_prompt_drops_religion_and_full_dob():
    import user_memory

    p = user_memory._build_seed_prompt({"nick_name": "Sam", "date_of_birth": "1990-05-01"},
                                       {"religion": "Zoroastrian", "profession": "Chef"})
    assert "Zoroastrian" not in p and "1990-05-01" not in p and "Age:" in p and "Chef" in p


async def test_fetch_user_profile_selects_only_prompt_columns(monkeypatch):
    seen = {}

    async def select(table, columns="*", **k):
        seen["columns"] = columns
        return [{"user_id": U}]

    monkeypatch.setattr(main.supabase, "select", select)
    await main.fetch_user_profile(U)
    cols = seen["columns"].split(",")
    assert "*" not in cols and not any(c.startswith("kyc_") for c in cols)
    assert not {"email", "phone", "avatar_url"} & set(cols)


# =============================================================================
# Checkout gates
# =============================================================================

class FakeProvider:
    id = "stripe"

    def __init__(self):
        self.calls = []

    async def create_checkout(self, user, tier, platform, *, success_url, cancel_url, prefer=None):
        self.calls.append({"user": user, "tier": tier})
        return CheckoutResult(mode="redirect", url="https://checkout.stripe.com/c/pay/cs_test_abc123")


@pytest.fixture
def shop(monkeypatch, member):
    prov = FakeProvider()
    recorded = []
    monkeypatch.setattr(payments_api.registry, "resolve_active",
                        lambda: _a(Resolution(provider=prov, configured_active="stripe")))

    async def record(row):
        recorded.append(row)
        return "pa1"

    monkeypatch.setattr(compliance, "record_purchase_agreement", record)
    return prov, recorded, member


AGREEMENT = {"accepted": True, "terms_version": "2026-11-03", "subscription_terms_version": "2026-11-03"}


def test_checkout_with_agreement_records_and_passes_metadata(shop):
    prov, recorded, c = shop
    r = c.post("/billing/checkout", json={"tier": "premium-lite", "agreement": AGREEMENT},
               headers={"X-CheriPic-Client": "mobile"})
    assert r.status_code == 200 and r.json()["mode"] == "redirect"
    assert prov.calls[0]["user"].metadata == {"terms_version": "2026-11-03", "subscription_terms_version": "2026-11-03"}
    row = recorded[0]
    assert row["terms_version"] == "2026-11-03" and row["tier"] == "premium-lite"
    assert row["channel"] == "web" and row["provider"] == "stripe" and row["checkout_ref"] == "cs_test_abc123"
    assert row["currency"] == "USD" and row["billing_interval"] == "month"


def test_checkout_legacy_client_without_agreement_is_implicit_legacy(shop):
    prov, recorded, c = shop
    r = c.post("/billing/checkout", json={"tier": "premium-lite"})
    assert r.status_code == 200
    assert recorded[0]["terms_version"] == "implicit-legacy"
    assert prov.calls[0]["user"].metadata["terms_version"] == "implicit-legacy"


@pytest.mark.parametrize("body,headers", [
    ({"tier": "premium-lite"}, {"X-CheriPic-Client": "mobile"}),
    ({"tier": "premium-lite", "agreement": None}, {}),
    ({"tier": "premium-lite", "agreement": {**AGREEMENT, "accepted": False}}, {}),
    ({"tier": "premium-lite", "agreement": {"accepted": True}}, {}),
])
def test_checkout_agreement_required(shop, body, headers):
    prov, recorded, c = shop
    r = c.post("/billing/checkout", json=body, headers=headers)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "agreement_required"
    assert prov.calls == [] and recorded == []


def test_checkout_under_age(shop, monkeypatch):
    prov, _, c = shop
    _set(monkeypatch, "fetch_profile_basics", {"date_of_birth": "2010-01-01", "country_of_residence": None})
    _set(monkeypatch, "member_standing", "terminated")  # age is checked first
    r = c.post("/billing/checkout", json={"tier": "premium-lite", "agreement": AGREEMENT})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "under_minimum_age"
    assert "21" in r.json()["detail"]["message"] and prov.calls == []


@pytest.mark.parametrize("profile", [None, {"date_of_birth": None}])
def test_checkout_without_profile_or_dob_is_refused(shop, monkeypatch, profile):
    _, _, c = shop
    _set(monkeypatch, "fetch_profile_basics", profile)
    r = c.post("/billing/checkout", json={"tier": "premium-lite", "agreement": AGREEMENT})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "under_minimum_age"


def test_checkout_terminated(shop, monkeypatch):
    _, _, c = shop
    _set(monkeypatch, "member_standing", "terminated")
    r = c.post("/billing/checkout", json={"tier": "premium-lite", "agreement": AGREEMENT})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_terminated"


def test_checkout_restricted_member_may_still_buy(shop, monkeypatch):
    _, _, c = shop
    _set(monkeypatch, "member_standing", "restricted")
    assert c.post("/billing/checkout", json={"tier": "premium-lite", "agreement": AGREEMENT}).status_code == 200


def test_checkout_country_unavailable(shop, monkeypatch):
    _, _, c = shop
    seen = []

    async def allows(code, feature):
        seen.append(feature)
        return False

    monkeypatch.setattr(compliance, "country_allows", allows)
    r = c.post("/billing/checkout", json={"tier": "premium-lite", "agreement": AGREEMENT})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "country_unavailable"
    assert seen == ["subscriptions"]


@pytest.mark.parametrize("have", ["premium-lite", "premium"])
def test_checkout_already_subscribed(shop, monkeypatch, have):
    prov, recorded, c = shop
    _set(monkeypatch, "active_paid_membership", {"tier": have})
    r = c.post("/billing/checkout", json={"tier": "premium-lite", "agreement": AGREEMENT})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "already_subscribed"
    assert prov.calls == [] and recorded == []


def test_checkout_upgrade_goes_to_portal_never_second_subscription(shop, monkeypatch):
    prov, recorded, c = shop
    monkeypatch.setattr(payments_api, "is_purchasable", lambda t: True)
    _set(monkeypatch, "active_paid_membership", {"tier": "premium-lite", "payment_provider": "stripe"})
    monkeypatch.setattr(payments_api, "open_portal", lambda uid, url: _a("https://billing.stripe.com/p/session_1"))
    r = c.post("/billing/checkout", json={"tier": "premium", "agreement": AGREEMENT})
    assert r.status_code == 200
    assert r.json() == {"provider": "stripe", "mode": "redirect", "url": "https://billing.stripe.com/p/session_1",
                        "upgrade": "portal"}
    assert prov.calls == []
    assert recorded[0]["tier"] == "premium" and recorded[0]["checkout_ref"] == "upgrade_portal"


def test_legacy_checkout_session_gates_use_string_detail(member, monkeypatch):
    monkeypatch.setattr(billing, "refresh_price_to_tier_from_db", lambda: _a(None))
    _set(monkeypatch, "fetch_profile_basics", {"date_of_birth": "2010-01-01"})
    body = {"user_id": U, "price_id": "price_x", "success_url": "https://cheripic.com/a", "cancel_url": "https://cheripic.com/b"}
    r = member.post("/billing/create-checkout-session", json=body)
    assert r.status_code == 403 and r.json() == {"detail": "under_minimum_age"}
    _set(monkeypatch, "fetch_profile_basics", {"date_of_birth": "1990-01-01"})
    _set(monkeypatch, "active_paid_membership", {"tier": "premium-lite"})
    monkeypatch.setitem(billing.TIER_FROM_PRICE_CACHE, "price_x", "premium")
    r = member.post("/billing/create-checkout-session", json=body)
    assert r.status_code == 409 and r.json() == {"detail": "already_subscribed"}  # upgrade refused here


def test_legacy_checkout_session_passes_agreement_metadata(member, monkeypatch):
    monkeypatch.setattr(billing, "refresh_price_to_tier_from_db", lambda: _a(None))
    seen = {}

    async def legacy(**kw):
        seen.update(kw)
        return "https://checkout.stripe.com/c/pay/cs_x"

    monkeypatch.setattr(payments_api, "legacy_checkout_session", legacy)
    body = {"user_id": U, "price_id": "price_x", "success_url": "https://cheripic.com/a",
            "cancel_url": "https://cheripic.com/b", "agreement": AGREEMENT}
    r = member.post("/billing/create-checkout-session", json=body)
    assert r.status_code == 200 and seen["metadata"]["terms_version"] == "2026-11-03"


# =============================================================================
# Connect activation gates
# =============================================================================

@pytest.fixture
def connect(monkeypatch, member):
    state = {"profile": {"user_id": U, "bio": json.dumps({"about_yourself": "I love long walks and books.",
                                                          "languages": ["English"], "intent": "long_term",
                                                          "gallery_urls": ["https://x/p.jpg"]}),
                         "gender": "female", "verification_final_status": "verified",
                         "date_of_birth": "1990-01-01", "country_of_residence": "AE"}}
    import readiness

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{readiness._REST}/user_profiles").mock(side_effect=lambda r: httpx.Response(200, json=[state["profile"]]))
        mock.get(f"{readiness._REST}/cheri_ai_user_profiles").mock(return_value=httpx.Response(200, json=[{"total_score": 86}]))
        mock.get(f"{readiness._REST}/matches").mock(return_value=httpx.Response(200, json=[]))
        mock.patch(f"{readiness._REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{"user_id": U}]))
        yield state, member


def test_connect_activate_ok(connect):
    _, c = connect
    assert c.post("/connect/activate").status_code == 200


def test_connect_activate_under_age(connect):
    state, c = connect
    state["profile"]["date_of_birth"] = "2008-01-01"
    r = c.post("/connect/activate")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "under_minimum_age"


def test_connect_activate_country_unavailable(connect, monkeypatch):
    _, c = connect
    seen = []

    async def allows(code, feature):
        seen.append((code, feature))
        return False

    monkeypatch.setattr(compliance, "country_allows", allows)
    r = c.post("/connect/activate")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "country_unavailable"
    assert seen == [("AE", "connect")]


def test_connect_activate_restricted(connect, monkeypatch):
    _, c = connect
    _set(monkeypatch, "member_standing", "suspended")
    r = c.post("/connect/activate")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_restricted"


def test_connect_pause_allowed_while_restricted_but_resume_is_not(connect, monkeypatch):
    _, c = connect
    _set(monkeypatch, "member_standing", "restricted")
    assert c.post("/connect/introductions", json={"paused": True}).status_code == 200
    r = c.post("/connect/introductions", json={"paused": False})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_restricted"


# =============================================================================
# POST /ai/report
# =============================================================================

def _chat_row(**over):
    row = {"id": "c1", "user_id": U, "role": "assistant", "content": "a reply",
           "conversation_id": "conv_1", "created_at": "2026-11-03T10:00:00Z"}
    row.update(over)
    return row


@respx.mock
def test_ai_report_inserts_snapshot(member):
    respx.get(f"{REST}/chat_history").mock(return_value=httpx.Response(200, json=[_chat_row()]))
    ins = respx.post(f"{REST}/user_reports").mock(return_value=httpx.Response(201, json=[{"id": "r1"}]))
    r = member.post("/ai/report", json={"chat_message_id": "c1", "category": "harmful_ai_response", "note": "bad"})
    assert r.status_code == 200 and r.json() == {"ok": True, "report_id": "r1", "status": "open", "stored": True}
    body = json.loads(ins.calls.last.request.content)
    assert body["target_type"] == "ai_response" and body["target_id"] == "c1" and body["reporter_id"] == U
    assert body["reported_id"] is None and body["status"] == "open" and body["category"] == "harmful_ai_response"
    assert body["content_snapshot"] == {"content": "a reply", "conversation_id": "conv_1",
                                        "created_at": "2026-11-03T10:00:00Z"}


@respx.mock
def test_ai_report_rejects_other_members_and_user_rows(member):
    route = respx.get(f"{REST}/chat_history")
    route.mock(return_value=httpx.Response(200, json=[_chat_row(user_id=B)]))
    r = member.post("/ai/report", json={"chat_message_id": "c1", "category": "harmful_ai_response"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "message_not_found"
    route.mock(return_value=httpx.Response(200, json=[_chat_row(role="user")]))
    r = member.post("/ai/report", json={"chat_message_id": "c1", "category": "harmful_ai_response"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "not_ai_response"


def test_ai_report_invalid_category(member):
    r = member.post("/ai/report", json={"chat_message_id": "c1", "category": "nope"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_category"


@respx.mock
def test_ai_report_before_057_acknowledges_without_storing(member):
    respx.get(f"{REST}/chat_history").mock(return_value=httpx.Response(200, json=[_chat_row()]))
    respx.post(f"{REST}/user_reports").mock(
        return_value=httpx.Response(400, json={"code": "PGRST204", "message": "Could not find the 'target_type' column"}))
    r = member.post("/ai/report", json={"chat_message_id": "c1", "category": "inaccurate_ai_response"})
    assert r.status_code == 200 and r.json()["stored"] is False and r.json()["ok"] is True


# =============================================================================
# GET /me/export
# =============================================================================

@respx.mock
def test_me_export_excludes_kyc_images_and_lists_missing_tables(member, monkeypatch):
    _set(monkeypatch, "member_standing", "terminated")  # export stays available
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[
        {"user_id": U, "nick_name": "N", "kyc_face_url": "https://x/face.jpg", "kyc_id_url": "https://x/id.jpg"}]))
    respx.get(f"{REST}/chat_history").mock(return_value=httpx.Response(200, json=[{"id": "c1", "content": "hi"}]))
    respx.get(url__regex=rf"{REST}/(member_consents|purchase_agreements|dob_change_requests|appeals).*").mock(
        return_value=httpx.Response(404, json={"code": "PGRST205"}))
    respx.get(url__startswith=f"{REST}/").mock(return_value=httpx.Response(200, json=[]))
    r = member.get("/me/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
    data = r.json()
    assert "kyc_face_url" not in data["profile"] and "kyc_id_url" not in data["profile"]
    assert data["cheriai_history"] == [{"id": "c1", "content": "hi"}]
    assert {"consents", "purchase_agreements", "dob_change_requests", "appeals"} <= set(data["unavailable"])


def test_new_routes_have_rate_limits():
    from ratelimit import RATE_LIMITS

    assert RATE_LIMITS["ai_report"] == "10/hour" and RATE_LIMITS["me_export"] == "3/day"
    for r in main.app.routes:
        if getattr(r, "path", None) in ("/ai/report", "/me/export", "/admin/moderation/notify"):
            assert any(getattr(d.call, "__qualname__", "").startswith("rate_limit.")
                       for d in r.dependant.dependencies), r.path


# =============================================================================
# POST /admin/moderation/notify
# =============================================================================

@pytest.fixture
def te_stub(monkeypatch):
    import transactional_email as te

    sent = []
    templates = {"account_warning", "account_suspended"}

    async def load(slug):
        return {"subject": "s", "html_body": "b"} if slug in templates else None

    async def send(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(te, "_load_template", load)
    monkeypatch.setattr(te, "send_transactional_email", send)
    monkeypatch.setattr(te, "_cfg_app_base_url", lambda: _a("https://cheripic.com"))
    return sent


@respx.mock
def test_moderation_notify_sends_matching_template(admin, te_stub):
    respx.get(f"{REST}/moderation_actions").mock(return_value=httpx.Response(200, json=[
        {"id": "a1", "subject_user_id": U, "action": "warning", "reason": "Rude messages", "expires_at": None}]))
    r = admin.post("/admin/moderation/notify", json={"action_id": "a1"})
    assert r.json() == {"sent": True, "template": "account_warning"}
    kw = te_stub[0]
    assert kw["user_id"] == U and kw["template_slug"] == "account_warning"
    assert kw["extra_vars"]["reason"] == "Rude messages" and kw["extra_vars"]["appeal_url"].endswith("/#/contact")
    # A decision about the account is sent even when the member opted out of email.
    assert kw["essential"] is True


@respx.mock
def test_moderation_notify_skips_missing_template_and_unmapped_action(admin, te_stub):
    route = respx.get(f"{REST}/moderation_actions")
    route.mock(return_value=httpx.Response(200, json=[{"id": "a2", "subject_user_id": U, "action": "termination", "reason": "x"}]))
    r = admin.post("/admin/moderation/notify", json={"action_id": "a2"})
    assert r.json() == {"sent": False, "template": "account_terminated", "skipped": "template_missing"}
    route.mock(return_value=httpx.Response(200, json=[{"id": "a3", "subject_user_id": U, "action": "note", "reason": "x"}]))
    assert admin.post("/admin/moderation/notify", json={"action_id": "a3"}).json()["skipped"] == "no_template_for_action"
    assert te_stub == []


@respx.mock
def test_moderation_notify_before_057_and_not_found(admin, te_stub):
    route = respx.get(f"{REST}/moderation_actions")
    route.mock(return_value=httpx.Response(404, json={"code": "PGRST205"}))
    r = admin.post("/admin/moderation/notify", json={"action_id": "a1"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "moderation_tables_missing"
    route.mock(return_value=httpx.Response(200, json=[]))
    assert admin.post("/admin/moderation/notify", json={"action_id": "zz"}).status_code == 404


def test_moderation_notify_requires_admin():
    r = TestClient(main.app).post("/admin/moderation/notify", json={"action_id": "a1"})
    assert r.status_code == 401
