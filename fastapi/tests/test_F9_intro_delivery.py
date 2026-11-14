"""
F9 · Server delivery of introductions (migration 063 + introduction_delivery).
Supabase is respx-mocked; push is stubbed. No network.
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

import introduction_delivery as d
import push_notifications

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def fake(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(push_notifications, "send_push_notification", fake)
    return sent


def test_wants_introductions():
    assert d.wants_introductions('{"connect_active": true}') is True
    assert d.wants_introductions({"connect_active": True, "intros_paused": True}) is False
    assert d.wants_introductions('{"connect_active": false}') is False
    assert d.wants_introductions("not json") is False


@respx.mock
async def test_delivers_notifies_and_pushes_only_members_who_got_someone(pushes):
    respx.get(f"{d.REST}/user_profiles").mock(return_value=httpx.Response(200, json=[
        {"user_id": A, "bio": '{"connect_active": true}'},
        {"user_id": B, "bio": '{"connect_active": true, "intros_paused": true}'},  # paused: not called
        {"user_id": C, "bio": '{"connect_active": true}'},
    ]))
    calls = []

    def deliver(request):
        uid = json.loads(request.content)["p_user"]
        calls.append(uid)
        return httpx.Response(200, json=2 if uid == A else 0)

    respx.post(f"{d.REST}/rpc/deliver_introductions").mock(side_effect=deliver)
    notes = respx.post(f"{d.REST}/notifications").mock(return_value=httpx.Response(201))
    out = await d.run_intro_delivery()
    assert calls == [A, C]
    assert out == {"members": 2, "delivered_members": 1, "introductions": 2, "errors": 0}
    row = json.loads(notes.calls.last.request.content)
    assert row["user_id"] == A and row["type"] == "match_available"
    # Generic copy: no names on the lock screen.
    assert [p["campaign"] for p in pushes] == ["match_available"] and pushes[0]["user_id"] == A
    assert pushes[0]["url"] == "/#/matching"


@respx.mock
async def test_missing_rpc_is_a_noop(pushes):
    respx.get(f"{d.REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{"user_id": A, "bio": '{"connect_active": true}'}]))
    respx.post(f"{d.REST}/rpc/deliver_introductions").mock(return_value=httpx.Response(404, json={"code": "PGRST202"}))
    out = await d.run_intro_delivery()
    assert out["delivered_members"] == 0 and pushes == []


def test_schedule_respects_flag(monkeypatch):
    class S:
        jobs = []

        def add_job(self, *a, **k):
            self.jobs.append(k["id"])

    monkeypatch.setenv("ENABLE_INTRO_DELIVERY", "false")
    assert d.schedule(S()) is False
    monkeypatch.setenv("ENABLE_INTRO_DELIVERY", "true")
    s = S()
    assert d.schedule(s) is True and s.jobs == ["intro_delivery"]
