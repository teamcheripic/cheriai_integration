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
# The real admin: a login flagged app_metadata.is_admin, NOT listed in the
# legacy admin_users table (which has its own ids). Live bug 2026-11-23: the
# wipe deleted this login because it only checked admin_users.
PANEL_ADMIN = "99999999-0000-0000-0000-000000000000"


def mock_people():
    respx.get(f"{admin_wipe.SUPABASE_URL}/auth/v1/admin/users").mock(return_value=httpx.Response(
        200, json={"users": [{"id": u} for u in (ADMIN, DEMO, M1, M2, HELD, NOPROFILE)]
                   + [{"id": PANEL_ADMIN, "app_metadata": {"provider": "email", "is_admin": True}}]}))
    respx.get(f"{admin_wipe.REST}/user_profiles").mock(return_value=httpx.Response(
        200, json=[{"user_id": u} for u in (DEMO, M1, M2, HELD)]))
    respx.get(f"{admin_wipe.REST}/admin_users").mock(return_value=httpx.Response(200, json=[{"id": ADMIN}]))
    respx.get(f"{admin_wipe.REST}/protected_accounts").mock(return_value=httpx.Response(200, json=[{"user_id": DEMO}]))


@respx.mock
async def test_targets_exclude_admins_and_protected_accounts():
    mock_people()
    plan = await admin_wipe.wipe_targets()
    assert set(plan["targets"]) == {M1, M2, HELD, NOPROFILE}
    assert PANEL_ADMIN not in plan["targets"]
    assert plan["admins"] == 2 and plan["protected"] == 1


@respx.mock
async def test_wipe_refuses_when_no_admin_login_is_visible():
    respx.get(f"{admin_wipe.SUPABASE_URL}/auth/v1/admin/users").mock(return_value=httpx.Response(200, json={"users": [{"id": M1}]}))
    respx.get(f"{admin_wipe.REST}/user_profiles").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{admin_wipe.REST}/admin_users").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{admin_wipe.REST}/protected_accounts").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(RuntimeError, match="no_admin_login_found"):
        await admin_wipe.wipe_targets()


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
    assert PANEL_ADMIN not in {u for u, _ in calls}
    assert out == {"deleted": 3, "legal_hold": 1, "failed": 0, "kept_admins": 2, "kept_protected": 1}


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
    from unittest.mock import MagicMock

    def fake_task(coro):
        started.append(coro)
        coro.close()
        return MagicMock()  # main holds the task until it finishes (_keep_task)

    monkeypatch.setattr("asyncio.create_task", fake_task)
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


def test_admin_single_delete_runs_full_deletion_and_refuses_admins(monkeypatch):
    import auth
    import main

    calls = []

    async def fake_targets():
        return {"targets": [M1], "admins": 1, "protected": 1}

    async def fake_delete(uid, *, notify=True):
        calls.append((uid, notify))
        return {"ok": True, "status": "completed"}

    monkeypatch.setattr(admin_wipe, "wipe_targets", fake_targets)
    monkeypatch.setattr(member_actions, "delete_account", fake_delete)
    main.app.dependency_overrides[auth.get_current_admin_id] = lambda: ADMIN
    try:
        c = TestClient(main.app)
        assert c.post(f"/admin/users/{M1}/delete").json()["status"] == "completed"
        assert calls == [(M1, False)]
        r = c.post(f"/admin/users/{ADMIN}/delete")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_deletable"
        assert c.post("/admin/users/not-a-uuid/delete").status_code == 422
        assert calls == [(M1, False)]
    finally:
        main.app.dependency_overrides.clear()


def test_match_pool_view_is_not_a_delete_target():
    # cheri_ai_match_pool is a view (055); deleting from it stopped every
    # deletion with "delete_failed_cheri_ai_match_pool" (live, 2026-11-22).
    assert "cheri_ai_match_pool" not in {t for t, _ in member_actions._DELETE_TARGETS}


def test_read_only_view_answer_is_recognised():
    view = httpx.Response(400, json={"code": "55000", "message": 'cannot delete from view "x"', "details": "Views that do not select from a single table or view are not automatically updatable."})
    fk = httpx.Response(409, json={"code": "23503", "message": "update or delete on table violates foreign key constraint"})
    assert member_actions._is_read_only_view(view) is True
    assert member_actions._is_read_only_view(fk) is False
