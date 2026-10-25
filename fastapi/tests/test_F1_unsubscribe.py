"""
F1 · X-EMAIL.7 one-click unsubscribe for the daily nudge (RFC 8058).
Resend and Supabase are respx-mocked; no network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import cron_daily_email as cde
import main
import transactional_email as te

U = "11111111-2222-3333-4444-555555555555"
REST = cde._PG_BASE


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("EMAIL_UNSUBSCRIBE_SECRET", "unit-test-secret")
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://api.example.test")


def test_token_round_trip_and_tamper_rejection():
    tok = cde.make_unsubscribe_token(U)
    assert cde.verify_unsubscribe_token(tok) == U
    uid_part, sig_part = tok.split(".")
    other = cde._b64(b"99999999-2222-3333-4444-555555555555")
    assert cde.verify_unsubscribe_token(f"{other}.{sig_part}") is None  # swapped user
    flipped = sig_part[:-1] + ("A" if sig_part[-1] != "A" else "B")
    assert cde.verify_unsubscribe_token(f"{uid_part}.{flipped}") is None  # bad signature
    for junk in (None, "", "abc", "a.b.c", "x" * 600, f"{cde._b64(b'not-a-uuid')}.{sig_part}"):
        assert cde.verify_unsubscribe_token(junk) is None


def test_token_from_another_secret_is_rejected(monkeypatch):
    tok = cde.make_unsubscribe_token(U)
    monkeypatch.setenv("EMAIL_UNSUBSCRIBE_SECRET", "rotated")
    assert cde.verify_unsubscribe_token(tok) is None


def test_fallback_secret_is_derived_not_the_raw_service_key(monkeypatch):
    monkeypatch.delenv("EMAIL_UNSUBSCRIBE_SECRET")
    secret = cde._unsubscribe_secret()
    assert secret and secret != cde.SUPABASE_KEY.encode() and len(secret) == 32
    assert cde.verify_unsubscribe_token(cde.make_unsubscribe_token(U)) == U


def test_list_unsubscribe_headers_point_at_signed_endpoint():
    h = cde.list_unsubscribe_headers(U)
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    url = h["List-Unsubscribe"].strip("<>")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://api.example.test/emails/unsubscribe"
    assert cde.verify_unsubscribe_token(parse_qs(parts.query)["token"][0]) == U


@respx.mock
async def test_nudge_send_carries_headers(monkeypatch):
    async def cfg_key():
        return "re_test"

    async def candidates(client):
        return [{"user_id": U, "email": "m@example.test", "nick_name": "M",
                 "notifications": [{"type": "match_available"}]}]

    async def render(name, notifs):
        return "s", "<p>h</p>", "t"

    monkeypatch.setattr(cde, "_cfg_resend_api_key", cfg_key)
    monkeypatch.setattr(cde, "gather_candidates", candidates)
    monkeypatch.setattr(cde, "render_email", render)

    async def fixed(*a, **k):
        return 6

    monkeypatch.setattr(cde, "_cfg_min_age_hours", fixed)
    monkeypatch.setattr(cde, "_cfg_max_per_run", fixed)

    async def from_addr():
        return "CheriPic <noreply@cheripic.com>"

    monkeypatch.setattr(cde, "_cfg_from_address", from_addr)
    resend = respx.post(cde.RESEND_ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "re_1"}))
    respx.post(f"{REST}/sent_emails").mock(return_value=httpx.Response(201, json=[]))
    out = await cde.run_once()
    assert out["sent"] == 1
    body = json.loads(resend.calls.last.request.content)
    assert body["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert body["headers"]["List-Unsubscribe"].startswith("<https://api.example.test/emails/unsubscribe?token=")


@respx.mock
async def test_transactional_email_has_no_list_unsubscribe(monkeypatch):
    async def cfg_key():
        return "re_test"

    async def cfg(*a, **k):
        return "x"

    async def tpl(slug):
        return {"subject": "Hi", "html_body": "<p>x</p>", "text_body": "x"}

    async def not_live():
        return False

    monkeypatch.setattr(cde, "_cfg_resend_api_key", cfg_key)
    monkeypatch.setattr(cde, "_cfg_from_address", cfg)
    for name in ("_cfg_app_base_url", "_cfg_logo_url", "_cfg_support_email", "_cfg_unsubscribe_url"):
        monkeypatch.setattr(te, name, cfg)
    monkeypatch.setattr(te, "_load_template", tpl)
    monkeypatch.setattr(te, "_communication_live", not_live)
    respx.get(f"{te._PG_BASE}/user_profiles").mock(
        return_value=httpx.Response(200, json=[{"email": "m@example.test", "nick_name": "M"}])
    )
    respx.post(f"{te._PG_BASE}/sent_emails").mock(return_value=httpx.Response(201))
    resend = respx.post(cde.RESEND_ENDPOINT).mock(return_value=httpx.Response(200, json={"id": "re_2"}))
    assert await te.send_transactional_email(user_id=U, template_slug="payment_failed") is True
    assert "headers" not in json.loads(resend.calls.last.request.content)


@respx.mock
def test_post_unsubscribe_turns_email_preference_off():
    patch = respx.patch(f"{REST}/user_profiles").mock(return_value=httpx.Response(204))
    tok = cde.make_unsubscribe_token(U)
    r = TestClient(main.app).post(f"/emails/unsubscribe?token={tok}", data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and r.json() == {"ok": True, "unsubscribed": True}
    assert patch.calls.last.request.url.params["user_id"] == f"eq.{U}"
    assert json.loads(patch.calls.last.request.content) == {"notify_email_enabled": False}


@respx.mock
def test_post_unsubscribe_rejects_tampered_token_without_writing():
    patch = respx.patch(f"{REST}/user_profiles")
    tok = cde.make_unsubscribe_token(U)[:-2] + "xx"
    r = TestClient(main.app).post(f"/emails/unsubscribe?token={tok}")
    assert r.status_code == 400 and r.json() == {"detail": "invalid_token"}
    assert not patch.called


@respx.mock
def test_post_unsubscribe_db_failure_is_503():
    respx.patch(f"{REST}/user_profiles").mock(return_value=httpx.Response(500))
    r = TestClient(main.app).post(f"/emails/unsubscribe?token={cde.make_unsubscribe_token(U)}")
    assert r.status_code == 503


@respx.mock
def test_get_unsubscribe_only_shows_confirmation():
    patch = respx.patch(f"{REST}/user_profiles")
    tok = cde.make_unsubscribe_token(U)
    r = TestClient(main.app).get(f"/emails/unsubscribe?token={tok}")
    assert r.status_code == 200 and 'method="post"' in r.text
    assert not patch.called  # link scanners can't unsubscribe anyone
    assert TestClient(main.app).get("/emails/unsubscribe?token=bad").status_code == 400
