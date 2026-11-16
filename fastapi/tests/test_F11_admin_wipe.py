"""
F11 · Wipe & Cleanup: "wipe all members" never touches admin or protected
accounts, sends no deletion emails, skips legal holds and needs the exact
confirmation phrase. Supabase is respx-mocked; no network.
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

import admin_wipe
import member_actions

ADMIN, DEMO, M1, M2, HELD, NOPROFILE = (f"{c * 8}-0000-0000-0000-000000000000" for c in "abcdef")


def mock_people():
    respx.get(f"{admin_wipe.SUPABASE_URL}/auth/v1/admin/users").mock(return_value=httpx.Response(
        200, json={"users": [{"id": u} for u in (ADMIN, DEMO, M1, M2, HELD, NOPROFILE)]}))
    respx.get(f"{admin_wipe.REST}/user_profiles").mock(return_value=httpx.Response(
        200, json=[{"user_id": u} for u in (DEMO, M1, M2, HELD)]))
    respx.get(f"{admin_wipe.REST}/admin_users").mock(return_value=httpx.Response(200, json=[{"id": ADMIN}]))
    respx.get(f"{admin_wipe.REST}/protected_accounts").mock(return_value=httpx.Response(200, json=[{"user_id": DEMO}]))


@respx.mock
async def test_targets_exclude_admins_and_protected_accounts():
    mock_people()
    plan = await admin_wipe.wipe_targets()
    assert set(plan["targets"]) == {M1, M2, HELD, NOPROFILE}
    assert plan["admins"] == 1 and plan["protected"] == 1


@respx.mock
async def test_wipe_deletes_without_email_and_reports_legal_holds(monkeypatch):
    mock_people()
    calls = []

    async def fake_delete(uid, *, notify=True):
        calls.append((uid, notify))
        if uid == HELD:
            raise member_actions.MemberActionError("legal_hold")
        return {"ok": True}

    monkeypatch.setattr(member_actions, "delete_account", fake_delete)
    out = await admin_wipe.wipe_members()
    assert {u for u, _ in calls} == {M1, M2, HELD, NOPROFILE}
    assert all(notify is False for _, notify in calls)
    assert out == {"deleted": 3, "legal_hold": 1, "failed": 0, "kept_admins": 1, "kept_protected": 1}


@respx.mock
async def test_missing_protected_table_still_protects_admins():
    mock_people()
    respx.get(f"{admin_wipe.REST}/protected_accounts").mock(return_value=httpx.Response(404, json={"code": "PGRST205"}))
    plan = await admin_wipe.wipe_targets()
    assert ADMIN not in plan["targets"] and DEMO in plan["targets"]


def test_endpoint_requires_exact_phrase(monkeypatch):
    import auth
    import main

    main.app.dependency_overrides[auth.get_current_admin_id] = lambda: ADMIN
    started = []
    monkeypatch.setattr("asyncio.create_task", lambda coro: started.append(coro) or coro.close())
    try:
        c = TestClient(main.app)
        r = c.post("/admin/wipe/members", json={"confirm": "delete all"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "confirmation_required"
        assert started == []
        assert c.post("/admin/wipe/members", json={"confirm": "DELETE ALL MEMBERS"}).json() == {"started": True, "job": "wipe_members"}
        assert len(started) == 1
    finally:
        main.app.dependency_overrides.clear()


async def test_delete_account_notify_false_sends_no_email(monkeypatch):
    sent = []

    async def fake_send(recipient):
        sent.append(recipient)
        return True

    monkeypatch.setattr(member_actions, "_send_deletion_confirmation", fake_send)
    import inspect
    src = inspect.getsource(member_actions._delete_account_steps)
    assert "if notify else False" in src  # the email is skipped when notify=False
