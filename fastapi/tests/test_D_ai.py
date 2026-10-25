"""
Audit D · X-AI / H10 — pure CheriAI backend logic, fully offline.

Covers canned-router classification (free pleasantries, never swallowing real
or safety-relevant messages), AI disclosure in canned replies and the system
prompt, prompt construction, the OpenAI client (mocked with respx — no real
LLM call, single attempt, malformed bodies never leak to members) and the
privacy-minimised usage-ledger row.
"""
import json

import httpx
import pytest
import respx

import dotenv

# llm_client calls load_dotenv() at import. Neutralise it so this module never
# pulls the real .env (API keys) into the process — that also leaked real
# SUPABASE_* values into later test modules (tests/test_auth.py).
_real_load_dotenv = dotenv.load_dotenv
dotenv.load_dotenv = lambda *a, **k: False
try:
    import llm_client
finally:
    dotenv.load_dotenv = _real_load_dotenv

import canned_responses as canned  # noqa: E402
from cheriai_prompts import CheriAIPromptBuilder  # noqa: E402

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


# --------------------------------------------------------------------------
# Canned router (quota-free pleasantries — project rule)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "msg,cat",
    [
        ("hi", "greeting"),
        ("Heyyy!!", "greeting"),
        ("thank you", "gratitude"),
        ("how are you?", "wellbeing"),
        ("are you a bot", "identity"),
        ("Are you human?", "identity"),
        ("ok", "affirmation"),
        ("❤️❤️", "emoji_only"),
    ],
)
def test_pleasantries_are_canned(msg, cat):
    assert canned.classify(msg) == cat


@pytest.mark.parametrize(
    "msg",
    [
        "help",            # possible distress signal → LLM
        "help me",
        "no",              # answer to Cheri's last question → LLM
        "nope",
        "hi how do I know if he still loves me",
        "I feel like I can't go on",
        "x" * 61,
        "",
        "   ",
    ],
)
def test_real_or_sensitive_messages_reach_the_llm(msg):
    assert canned.classify(msg) is None


def test_identity_replies_disclose_ai_and_not_therapist():
    for variant in canned._POOLS["identity"]:
        text = (variant["reply"] + " " + variant.get("follow_up", "")).lower()
        assert "an ai" in text
        assert "not a" in text and ("therapist" in text)


def test_respond_personalises_and_never_raises_on_unknown_category():
    reply, follow = canned.respond("farewell", name="Alex")
    assert "{name}" not in reply
    assert canned.respond("does-not-exist") == ("I'm here 💜", None)
    # A name containing braces must not break formatting.
    reply, _ = canned.respond("gratitude", name="{evil}")
    assert isinstance(reply, str)


# --------------------------------------------------------------------------
# Prompt builder
# --------------------------------------------------------------------------

def _builder(**kw):
    user = {"id": "u1", "nick_name": "Alex", "gender": "female", "country_of_residence": "AE",
            "bio": json.dumps({"intent": "Something serious"})}
    return CheriAIPromptBuilder(user, kw.pop("stage", "general"), **kw)


def test_system_prompt_discloses_ai_and_never_denies_it():
    system, _ = _builder().build("hello")
    assert "You are not an AI" not in system
    assert "you are CheriAI, an AI" in system
    assert "not a therapist" in system
    assert "emergency services" in system
    assert "Alex" in system and "{name}" not in system


def test_unknown_stage_falls_back_to_general_focus():
    system, _ = _builder(stage="weird").build("hello")
    assert "Stage: just be present." in system


def test_user_prompt_contains_message_memory_and_recent_turns_trimmed():
    memory = {"summary": "Recently out of a long relationship",
              "insights": [{"fact": f"fact {i}"} for i in range(12)]}
    recent = [{"role": "user", "content": "x" * 500}, {"role": "assistant", "content": "ok"}]
    _, user = _builder(memory=memory, recent_messages=recent).build("  How do I move on?  ")
    assert 'Alex just texted you: "How do I move on?"' in user
    assert "fact 11" in user and "fact 3" not in user        # last 8 insights only
    assert "x" * 201 not in user                              # recent lines capped at 200 chars
    assert "What Alex is looking for: Something serious" in user


def test_offers_are_never_injected_into_the_prompt():
    _, user = _builder(offers=[{"title": "SECRET DEAL 50% off"}]).build("gift ideas?")
    assert "SECRET DEAL" not in user


def test_prompt_does_not_include_private_identifiers():
    _, user = CheriAIPromptBuilder(
        {"id": "u1", "nick_name": "Alex", "email": "a@example.com", "phone": "+971500000000"}, "general"
    ).build("hi there friend")
    assert "a@example.com" not in user and "+971500000000" not in user


# --------------------------------------------------------------------------
# LLM client (respx — no real network)
# --------------------------------------------------------------------------

@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(llm_client, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "OPENAI_MODEL", "gpt-4o-mini")


@respx.mock
async def test_send_returns_text_and_usage_with_single_attempt(with_key):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"message": {"content": '  {"reply": "Hey.", "follow_up": ""}  '}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940},
    }))
    text, usage = await llm_client.send_to_llm_with_usage(("sys JSON", "user"), user="u1", json_mode=True)
    assert text == '{"reply": "Hey.", "follow_up": ""}'
    assert usage == {"model": "gpt-4o-mini-2024-07-18", "prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940}
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["max_tokens"] == 180
    assert sent["messages"][0] == {"role": "system", "content": "sys JSON"}


@respx.mock
async def test_malformed_provider_body_raises_instead_of_leaking(with_key):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={"error": {"message": "internal detail"}}))
    with pytest.raises(llm_client.LLMResponseError):
        await llm_client.send_to_llm("hi")


@respx.mock
async def test_empty_content_raises(with_key):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]}))
    with pytest.raises(llm_client.LLMResponseError):
        await llm_client.send_to_llm("hi")


@respx.mock
async def test_http_error_is_not_retried(with_key):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(429, json={"error": "rate"}))
    with pytest.raises(httpx.HTTPStatusError):
        await llm_client.send_to_llm("hi")
    assert route.call_count == 1


async def test_mock_mode_without_key_is_offline_and_parseable(monkeypatch):
    monkeypatch.setattr(llm_client, "OPENAI_API_KEY", None)
    with respx.mock(assert_all_called=False) as router:
        text, usage = await llm_client.send_to_llm_with_usage("hello", json_mode=True)
        assert not router.calls
    assert json.loads(text)["reply"].startswith("[MOCK LLM]")
    assert usage["total_tokens"] == 0


# --------------------------------------------------------------------------
# Usage ledger (privacy-minimised) + cost estimate
# --------------------------------------------------------------------------

def test_cost_estimate_known_and_unknown_models():
    assert llm_client.estimate_cost_usd("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)
    assert llm_client.estimate_cost_usd("gpt-4o-mini-2024-07-18", 0, 1_000_000) == pytest.approx(0.60)
    assert llm_client.estimate_cost_usd("mystery-model", 10, 10) is None


def test_ledger_row_has_required_fields_and_no_content():
    row = llm_client.build_usage_ledger_row(
        user_id="u1", purpose="chat", tier="basic", model="gpt-4o-mini",
        usage={"prompt_tokens": 1000, "completion_tokens": 100},
    )
    assert set(row) == {"user_id", "purpose", "membership_tier", "model", "prompt_tokens",
                        "completion_tokens", "total_tokens", "cost_estimate_usd", "canned"}
    assert row["total_tokens"] == 1100
    assert row["cost_estimate_usd"] == pytest.approx((1000 * 0.15 + 100 * 0.60) / 1_000_000)
    assert not any(k in row for k in ("message", "reply", "prompt", "content"))


def test_ledger_separates_assessment_and_zeroes_canned_turns():
    a = llm_client.build_usage_ledger_row(user_id="u", purpose="assessment", tier="premium-lite",
                                         model="gpt-4o-mini", usage=None)
    assert a["purpose"] == "assessment" and a["total_tokens"] == 0
    c = llm_client.build_usage_ledger_row(user_id="u", purpose="chat", tier="basic",
                                         model="gpt-4o-mini", usage={"prompt_tokens": 5}, canned=True)
    assert c["cost_estimate_usd"] == 0.0 and c["model"] is None and c["canned"] is True
    with pytest.raises(ValueError):
        llm_client.build_usage_ledger_row(user_id="u", purpose="anything", tier="basic", model="m", usage={})
