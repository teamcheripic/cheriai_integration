"""
F1 · H14S.16 third parties are never told why a connection closed.
transactional_email forces neutral connection_lost copy regardless of what
the caller (partnership.py sweep) passes. Resend/Supabase respx-mocked.
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

import cron_daily_email as cde
import transactional_email as te

U = "11111111-2222-3333-4444-555555555555"
FORBIDDEN = ("official", "committed", "partnership", "someone else", "partnered")


@pytest.fixture
def env(monkeypatch):
    async def cfg(*a, **k):
        return "x"

    async def cfg_key():
        return "re_test"

    async def tpl(slug):
        return {"subject": "An update", "html_body": "<p>{{context_line}}</p>", "text_body": "{{context_line}}"}

    async def not_live():
        return False

    pushes = []

    async def fake_push(**kw):
        pushes.append(kw)
        return True

    import push_notifications

    monkeypatch.setattr(cde, "_cfg_resend_api_key", cfg_key)
    monkeypatch.setattr(cde, "_cfg_from_address", cfg)
    for name in ("_cfg_app_base_url", "_cfg_logo_url", "_cfg_support_email", "_cfg_unsubscribe_url"):
        monkeypatch.setattr(te, name, cfg)
    monkeypatch.setattr(te, "_load_template", tpl)
    monkeypatch.setattr(te, "_communication_live", not_live)
    monkeypatch.setattr(push_notifications, "send_push_notification", fake_push)
    return pushes


@respx.mock
@pytest.mark.parametrize("caller_line", [
    "A connection you were building with has chosen to make it official with someone else.",
    "A connection you were exploring has chosen to make it official with someone else.",
    "Your connection is now in a committed partnership.",
])
async def test_connection_lost_copy_is_neutral_whatever_the_caller_passes(env, caller_line):
    respx.get(f"{te._PG_BASE}/user_profiles").mock(
        return_value=httpx.Response(200, json=[{"email": "tp@example.test", "nick_name": "T"}])
    )
    respx.post(f"{te._PG_BASE}/sent_emails").mock(return_value=httpx.Response(201))
    resend = respx.post(cde.RESEND_ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "re"}))
    ok = await te.send_transactional_email(
        user_id=U, template_slug="connection_lost", extra_vars={"context_line": caller_line}
    )
    assert ok is True
    body = json.loads(resend.calls.last.request.content)
    assert body["text"] == te.NEUTRAL_CONNECTION_ENDED_LINE
    for field in ("subject", "html", "text"):
        assert not any(w in body[field].lower() for w in FORBIDDEN), field
    # companion push is generic too
    assert env and not any(w in (env[0]["title"] + env[0]["body"]).lower() for w in FORBIDDEN)


def test_neutral_line_and_registry_never_disclose_the_reason():
    assert not any(w in te.NEUTRAL_CONNECTION_ENDED_LINE.lower() for w in FORBIDDEN)
    for slug in ("connection_lost", "relationship_ended"):
        cfg = te.NOTIFICATION_REGISTRY[slug]
        assert not any(w in (cfg["title"] + cfg["body"]).lower() for w in FORBIDDEN)
        assert "{{" not in cfg["title"] + cfg["body"]  # no names on the lock screen
