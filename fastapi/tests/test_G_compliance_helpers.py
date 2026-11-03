"""
G · compliance.py helpers against respx-mocked PostgREST / OpenAI — each new
table/column/RPC from migrations 056–058 and its missing-migration fallback.
No network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx

import compliance
import llm_client

pytestmark = pytest.mark.real_compliance

REST = compliance._REST
U = "uuuuuuuu-0000-0000-0000-00000000000u"


def _missing(code="PGRST202", status=404):
    return httpx.Response(status, json={"code": code, "message": "missing"})


# ------------------------------------------------------------------ standing

@respx.mock
async def test_standing_rpc_missing_is_active():
    respx.post(f"{REST}/rpc/member_standing").mock(return_value=_missing())
    assert await compliance.member_standing(U) == "active"


@respx.mock
async def test_standing_rpc_value_is_used_and_cached():
    route = respx.post(f"{REST}/rpc/member_standing").mock(return_value=httpx.Response(200, json="terminated"))
    assert await compliance.member_standing(U) == "terminated"
    assert await compliance.member_standing(U) == "terminated"
    assert route.call_count == 1  # 30 s cache


@respx.mock
async def test_standing_null_rpc_falls_back_to_tables():
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    respx.post(f"{REST}/rpc/member_standing").mock(return_value=httpx.Response(200, json=None))
    respx.get(f"{REST}/user_account_status").mock(
        return_value=httpx.Response(200, json=[{"status": "suspended", "status_until": future}]))
    assert await compliance.member_standing(U) == "suspended"


@respx.mock
async def test_standing_expired_suspension_is_active_and_legacy_toggle_is_suspended():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    respx.post(f"{REST}/rpc/member_standing").mock(return_value=httpx.Response(200, json=None))
    respx.get(f"{REST}/user_account_status").mock(
        return_value=httpx.Response(200, json=[{"status": "restricted", "status_until": past}]))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{"is_active": True}]))
    assert await compliance.member_standing(U) == "active"
    compliance.forget_standing()
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{"is_active": False}]))
    assert await compliance.member_standing(U) == "suspended"


@respx.mock
async def test_standing_upstream_error_is_active():
    respx.post(f"{REST}/rpc/member_standing").mock(side_effect=httpx.ConnectError("down"))
    assert await compliance.member_standing(U) == "active"


# ------------------------------------------------------------------ AI consent

@respx.mock
async def test_ai_consent_column_missing_counts_as_consented():
    respx.get(f"{REST}/user_profiles").mock(
        return_value=httpx.Response(400, json={"code": "42703", "message": "column user_profiles.ai_consent_at does not exist"}))
    assert await compliance.has_ai_consent(U) is True


@respx.mock
async def test_ai_consent_null_false_set_true_error_none():
    route = respx.get(f"{REST}/user_profiles")
    route.mock(return_value=httpx.Response(200, json=[{"ai_consent_at": None}]))
    assert await compliance.has_ai_consent(U) is False
    route.mock(return_value=httpx.Response(200, json=[{"ai_consent_at": "2026-11-03T10:00:00Z"}]))
    assert await compliance.has_ai_consent(U) is True
    route.mock(return_value=httpx.Response(500, json={"message": "boom"}))
    assert await compliance.has_ai_consent(U) is None


# ------------------------------------------------------------------ age / country

def test_age_from_dob():
    today = date(2026, 11, 3)
    assert compliance.age_from_dob("2005-11-03", today) == 21
    assert compliance.age_from_dob("2005-11-04", today) == 20
    assert compliance.age_from_dob(None, today) is None
    assert compliance.age_from_dob("not-a-date", today) is None


async def test_min_member_age_defaults_to_21(monkeypatch):
    import app_config

    async def get_int(key, env_key=None, default=0):
        return default

    monkeypatch.setattr(app_config, "get_int", get_int)
    assert await compliance.min_member_age() == 21

    async def bad(key, env_key=None, default=0):
        return 3

    monkeypatch.setattr(app_config, "get_int", bad)
    assert await compliance.min_member_age() == 21  # absurd values ignored


@respx.mock
async def test_country_allows_fallbacks_and_value():
    route = respx.post(f"{REST}/rpc/country_allows")
    route.mock(return_value=_missing())
    assert await compliance.country_allows("AE", "subscriptions") is True
    route.mock(return_value=httpx.Response(200, json=False))
    assert await compliance.country_allows("AE", "subscriptions") is False
    assert json.loads(route.calls.last.request.content) == {"p_code": "AE", "p_feature": "subscriptions"}
    route.mock(return_value=httpx.Response(200, json=None))  # unknown code → allowed
    assert await compliance.country_allows("UAE", "connect") is True
    assert await compliance.country_allows(None, "connect") is True


# ------------------------------------------------------------------ legal hold / reports / preservation

@respx.mock
async def test_legal_hold_missing_table_is_false_and_row_is_true():
    route = respx.get(f"{REST}/legal_holds")
    route.mock(return_value=_missing("PGRST205"))
    assert await compliance.has_legal_hold(U) is False
    route.mock(return_value=httpx.Response(200, json=[{"id": "h1"}]))
    assert await compliance.has_legal_hold(U) is True
    assert route.calls.last.request.url.params["released_at"] == "is.null"


@respx.mock
async def test_open_report_query_and_error_preserves():
    route = respx.get(f"{REST}/user_reports")
    route.mock(return_value=httpx.Response(200, json=[]))
    assert await compliance.has_open_report(U) is False
    params = route.calls.last.request.url.params
    assert params["or"] == f"(reporter_id.eq.{U},reported_id.eq.{U})"
    assert params["status"] == "in.(open,investigating,escalated,pending)"
    route.mock(return_value=httpx.Response(500))
    assert await compliance.has_open_report(U) is True


@respx.mock
async def test_preserve_rows_missing_table_logs_count_only(caplog):
    respx.post(f"{REST}/preserved_content").mock(return_value=_missing("42P01", 404))
    n = await compliance.preserve_rows("match_messages", [{"id": "m1", "content": "SECRET TEXT"}],
                                       subject_user_id=U, reason="test")
    assert n == 0
    assert "SECRET TEXT" not in caplog.text


@respx.mock
async def test_preserve_match_messages_only_when_needed():
    holds = respx.get(f"{REST}/legal_holds").mock(return_value=httpx.Response(200, json=[]))
    reports = respx.get(f"{REST}/user_reports").mock(return_value=httpx.Response(200, json=[]))
    msgs = respx.get(f"{REST}/match_messages").mock(
        return_value=httpx.Response(200, json=[{"id": "m1", "match_id": "x", "sender_id": U, "content": "hi"}]))
    stored = respx.post(f"{REST}/preserved_content").mock(return_value=httpx.Response(201))
    assert await compliance.preserve_match_messages(["x"], [U], reason="r") == 0
    assert not msgs.called and not stored.called
    reports.mock(return_value=httpx.Response(200, json=[{"id": "rep1"}]))
    assert await compliance.preserve_match_messages(["x"], [U], reason="r", subject_user_id=U) == 1
    body = json.loads(stored.calls.last.request.content)
    assert body[0]["source_table"] == "match_messages" and body[0]["source_id"] == "m1"
    assert body[0]["payload"]["content"] == "hi" and body[0]["subject_user_id"] == U
    assert holds.called


# ------------------------------------------------------------------ crisis + moderation

@pytest.mark.parametrize("text", ["I want to kill myself", "thinking about SUICIDE", "i wanna die",
                                  "I've been cutting myself", "no reason to live anymore", "kms"])
def test_crisis_router_matches(text):
    assert compliance.is_crisis(text)


@pytest.mark.parametrize("text", ["this date killed it", "I'm dying to meet him", "hello", "cut the cake"])
def test_crisis_router_ignores_ordinary_text(text):
    assert not compliance.is_crisis(text)


@respx.mock
async def test_moderation_flags_and_detects_self_harm():
    route = respx.post(compliance.OPENAI_MODERATION_URL).mock(return_value=httpx.Response(200, json={
        "results": [{"flagged": True, "categories": {"self-harm/intent": True, "violence": False}}]}))
    out = await compliance.moderate("text")
    assert out == {"flagged": True, "categories": ["self-harm/intent"], "self_harm": True, "error": False}
    assert json.loads(route.calls.last.request.content)["model"] == "omni-moderation-latest"


@respx.mock
async def test_moderation_fails_open_on_error():
    respx.post(compliance.OPENAI_MODERATION_URL).mock(return_value=httpx.Response(500))
    out = await compliance.moderate("text")
    assert out["flagged"] is False and out["error"] is True
    respx.post(compliance.OPENAI_MODERATION_URL).mock(side_effect=httpx.ConnectTimeout("x"))
    assert (await compliance.moderate("text"))["error"] is True


async def test_moderation_without_key_is_noop(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert (await compliance.moderate("text"))["flagged"] is False


@respx.mock
async def test_ai_safety_event_missing_table_logs_hash_not_id(caplog):
    respx.post(f"{REST}/ai_safety_events").mock(return_value=_missing("PGRST205"))
    assert await compliance.log_ai_safety_event(U, "crisis", action="crisis_reply") is False
    assert U not in caplog.text and llm_client.hashed_openai_user(U) in caplog.text


@respx.mock
async def test_ai_safety_event_row_matches_056_schema():
    route = respx.post(f"{REST}/ai_safety_events").mock(return_value=httpx.Response(201))
    ok = await compliance.log_ai_safety_event(U, "output", action="replaced", categories=["violence"],
                                              conversation_id="conv_1")
    assert ok is True
    assert json.loads(route.calls.last.request.content) == {
        "user_id": U, "conversation_id": "conv_1", "stage": "output", "categories": ["violence"],
        "action": "replaced", "details": {}}
    assert route.calls.last.request.headers["authorization"] == "Bearer service-test-key"


# ------------------------------------------------------------------ hashed OpenAI user

def test_hashed_user_uses_salt(monkeypatch):
    monkeypatch.setenv("CHERIAI_USER_HASH_SALT", "salt-a")
    a = llm_client.hashed_openai_user(U)
    assert a != U and len(a) == 64 and a == llm_client.hashed_openai_user(U)
    monkeypatch.setenv("CHERIAI_USER_HASH_SALT", "salt-b")
    assert llm_client.hashed_openai_user(U) != a
    monkeypatch.delenv("CHERIAI_USER_HASH_SALT")
    assert len(llm_client.hashed_openai_user(U)) == 64  # derived fallback salt


@respx.mock
async def test_llm_payload_carries_hashed_user_only(monkeypatch):
    monkeypatch.setattr(llm_client, "OPENAI_API_KEY", "sk-test")
    route = respx.post(f"{llm_client.OPENAI_BASE}/chat/completions").mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "hi"}}], "usage": {}}))
    await llm_client.send_to_llm("hello", user=U)
    sent = json.loads(route.calls.last.request.content)
    assert sent["user"] == llm_client.hashed_openai_user(U) and U not in json.dumps(sent)


# ------------------------------------------------------------------ purchases

@respx.mock
async def test_active_paid_membership_ignores_expired_rows():
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    route = respx.get(f"{REST}/user_memberships")
    route.mock(return_value=httpx.Response(200, json=[{"tier": "premium-lite", "current_period_end": old}]))
    assert await compliance.active_paid_membership(U) is None
    route.mock(return_value=httpx.Response(200, json=[{"tier": "premium-lite", "current_period_end": recent}]))
    assert (await compliance.active_paid_membership(U))["tier"] == "premium-lite"  # inside 3-day grace


@respx.mock
async def test_active_paid_membership_pre_054_retries_without_provider_column():
    route = respx.get(f"{REST}/user_memberships").mock(side_effect=[
        httpx.Response(400, json={"code": "42703", "message": "column payment_provider does not exist"}),
        httpx.Response(200, json=[{"tier": "premium"}]),
    ])
    assert (await compliance.active_paid_membership(U))["tier"] == "premium"
    assert "payment_provider" not in route.calls.last.request.url.params["select"]


@respx.mock
async def test_record_purchase_agreement_missing_table_and_success():
    route = respx.post(f"{REST}/purchase_agreements")
    route.mock(return_value=_missing("PGRST205"))
    assert await compliance.record_purchase_agreement({"user_id": U, "tier": "premium-lite"}) is None
    route.mock(return_value=httpx.Response(201, json=[{"id": "pa1"}]))
    assert await compliance.record_purchase_agreement({"user_id": U, "tier": "premium-lite"}) == "pa1"


@respx.mock
async def test_tier_price_falls_back_before_058():
    respx.get(f"{REST}/tier_config").mock(side_effect=[
        httpx.Response(400, json={"code": "42703", "message": "column currency does not exist"}),
        httpx.Response(200, json=[{"monthly_price_usd": 59}]),
    ])
    assert await compliance.tier_price("premium-lite") == {"price": 59, "currency": "USD", "billing_interval": "month"}
