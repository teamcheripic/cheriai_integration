"""
F12 · Admin-controlled test login (qa_login.py) and the admin single-member
delete. Supabase is respx-mocked; no network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import qa_login

USER = "11111111-0000-0000-0000-000000000000"
ADMIN = "aaaaaaaa-0000-0000-0000-000000000000"


def future(hours=2):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@pytest.fixture
def settings(monkeypatch):
    cfg = {"test_login_enabled": "true", "test_login_until": future(), "test_login_emails": "qa+*@gmail.com\n*@cheripic.com, tester@example.org"}

    async def fake_get(key, env_key=None, default=None):
        return cfg.get(key) or default

    async def fake_bool(key, env_key=None, default=False):
        v = cfg.get(key)
        return default if v is None else v.lower() in ("1", "true", "yes", "on")

    monkeypatch.setattr(qa_login, "get_config", fake_get)
    monkeypatch.setattr(qa_login, "get_bool", fake_bool)
    return cfg


def test_patterns_drop_unsafe_entries():
    assert qa_login.parse_patterns("*\n*@gmail.com\n*@*.com\nbad\nqa+*@gmail.com\n*@cheripic.com\nJane@Example.org") == [
        "qa+*@gmail.com", "*@cheripic.com", "jane@example.org"]


@pytest.mark.parametrize("email,ok", [
    ("qa+1@gmail.com", True),
    ("qa+anything@gmail.com", True),
    ("qa@gmail.com", False),          # the plain inbox isn't a plus-address
    ("qab+1@gmail.com", False),
    ("someone@gmail.com", False),
    ("anyone@cheripic.com", True),
    ("tester@example.org", True),
    ("tester2@example.org", False),
])
async def test_only_listed_addresses_are_eligible(settings, email, ok):
    assert await qa_login.eligible(email) is ok


async def test_off_when_disabled_or_expired(settings):
    settings["test_login_enabled"] = "false"
    assert await qa_login.eligible("tester@example.org") is False
    settings["test_login_enabled"] = "true"
    settings["test_login_until"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert await qa_login.eligible("tester@example.org") is False
    settings["test_login_until"] = ""
    assert await qa_login.eligible("tester@example.org") is False


def client():
    import main

    return TestClient(main.app)


def mock_link(uid=USER, admin=False, missing_first=False):
    route = respx.post(f"{qa_login._AUTH}/admin/generate_link")
    ok = httpx.Response(200, json={"id": uid, "hashed_token": "hash-123"})
    if missing_first:
        route.side_effect = [httpx.Response(404, json={"msg": "User not found"}), ok]
    else:
        route.mock(return_value=ok)
    create = respx.post(f"{qa_login._AUTH}/admin/users").mock(return_value=httpx.Response(200, json={"id": uid}))
    respx.get(f"{qa_login._REST}/admin_users").mock(return_value=httpx.Response(200, json=[{"id": uid}] if admin else []))
    return route, create


@respx.mock
def test_check_and_sign_in(settings):
    route, create = mock_link()
    c = client()
    assert c.post("/auth/test-login/check", json={"email": "QA+7@gmail.com"}).json() == {"test": True, "code": "123456"}
    assert c.post("/auth/test-login/check", json={"email": "real@gmail.com"}).json() == {"test": False}
    r = c.post("/auth/test-login", json={"email": "qa+7@gmail.com", "code": "123456"})
    assert r.status_code == 200 and r.json() == {"token_hash": "hash-123"}
    assert route.calls.last.request.content and b'"magiclink"' in route.calls.last.request.content
    assert not create.called


@respx.mock
def test_wrong_code_or_unlisted_address_refused(settings):
    route, _ = mock_link()
    c = client()
    assert c.post("/auth/test-login", json={"email": "qa+7@gmail.com", "code": "654321"}).status_code == 403
    assert c.post("/auth/test-login", json={"email": "real@gmail.com", "code": "123456"}).status_code == 403
    assert not route.called


@respx.mock
def test_new_address_gets_a_confirmed_login(settings):
    _, create = mock_link(missing_first=True)
    r = client().post("/auth/test-login", json={"email": "new@cheripic.com", "code": "123456"})
    assert r.status_code == 200
    assert create.called and b'"email_confirm":true' in create.calls.last.request.content.replace(b" ", b"")


@respx.mock
def test_admin_accounts_never_sign_in_this_way(settings):
    mock_link(uid=ADMIN, admin=True)
    r = client().post("/auth/test-login", json={"email": "boss@cheripic.com", "code": "123456"})
    assert r.status_code == 403 and "token_hash" not in r.text
