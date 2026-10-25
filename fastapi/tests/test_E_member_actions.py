"""
Audit E — member_actions.py (handoff §13 critical transaction rules).

Deterministic: every Supabase / Stripe call is mocked with respx or
monkeypatch; no network. Env is pinned BEFORE importing app modules so the
real backend .env (loaded by python-dotenv, which never overrides existing
vars) can't point the tests at the live project.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import json
import sys
import types

import httpx
import pytest
import respx

import member_actions as ma
from member_actions import MemberActionError

REST = ma._REST
BASE = ma.SUPABASE_URL
A = "aaaaaaaa-0000-0000-0000-00000000000a"
B = "bbbbbbbb-0000-0000-0000-00000000000b"
REQ = "rrrrrrrr-0000-0000-0000-00000000000r"


# --------------------------------------------------------------------------
# withdraw_interest — 11.2B.1
# --------------------------------------------------------------------------

@respx.mock
async def test_withdraw_not_found():
    respx.get(f"{REST}/match_requests").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(MemberActionError) as e:
        await ma.withdraw_interest(REQ, A)
    assert str(e.value) == "request_not_found"


@respx.mock
async def test_withdraw_not_sender():
    respx.get(f"{REST}/match_requests").mock(
        return_value=httpx.Response(200, json=[{"id": REQ, "sender_id": B, "receiver_id": A, "status": "pending"}])
    )
    patch = respx.patch(f"{REST}/match_requests")
    with pytest.raises(MemberActionError) as e:
        await ma.withdraw_interest(REQ, A)
    assert str(e.value) == "not_sender"
    assert not patch.called  # never writes for a non-owner


@respx.mock
async def test_withdraw_not_pending_returns_state():
    respx.get(f"{REST}/match_requests").mock(
        return_value=httpx.Response(200, json=[{"id": REQ, "sender_id": A, "receiver_id": B, "status": "accepted"}])
    )
    patch = respx.patch(f"{REST}/match_requests")
    with pytest.raises(MemberActionError) as e:
        await ma.withdraw_interest(REQ, A)
    assert str(e.value) == "not_pending"
    assert e.value.state == "accepted"
    assert not patch.called


@respx.mock
async def test_withdraw_race_lost_returns_authoritative_state():
    calls = {"n": 0}

    def get_side_effect(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[{"id": REQ, "sender_id": A, "receiver_id": B, "status": "pending"}])
        return httpx.Response(200, json=[{"status": "accepted"}])

    respx.get(f"{REST}/match_requests").mock(side_effect=get_side_effect)
    # Conditional PATCH matched zero rows (an accept won the race).
    patch = respx.patch(f"{REST}/match_requests").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(MemberActionError) as e:
        await ma.withdraw_interest(REQ, A)
    assert str(e.value) == "not_pending"
    assert e.value.state == "accepted"
    # The update is conditional on owner + still-pending.
    q = patch.calls.last.request.url.params
    assert q["status"] == "eq.pending" and q["sender_id"] == f"eq.{A}" and q["id"] == f"eq.{REQ}"


@respx.mock
async def test_withdraw_success():
    respx.get(f"{REST}/match_requests").mock(
        return_value=httpx.Response(200, json=[{"id": REQ, "sender_id": A, "receiver_id": B, "status": "pending"}])
    )
    patch = respx.patch(f"{REST}/match_requests").mock(
        return_value=httpx.Response(200, json=[{"id": REQ, "status": "withdrawn"}])
    )
    out = await ma.withdraw_interest(REQ, A)
    assert out == {"ok": True, "status": "withdrawn", "receiver_id": B}
    body = json.loads(patch.calls.last.request.content)
    assert body["status"] == "withdrawn" and body["responded_at"]


@respx.mock
async def test_withdraw_patch_error_is_update_failed():
    respx.get(f"{REST}/match_requests").mock(
        return_value=httpx.Response(200, json=[{"id": REQ, "sender_id": A, "receiver_id": B, "status": "pending"}])
    )
    respx.patch(f"{REST}/match_requests").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(MemberActionError) as e:
        await ma.withdraw_interest(REQ, A)
    assert str(e.value) == "update_failed"


# --------------------------------------------------------------------------
# pause_partner_after_official_end — §9 / §16 "official recipient end state"
# --------------------------------------------------------------------------

@respx.mock
async def test_pause_no_qualifying_match_no_write():
    m = respx.get(f"{REST}/matches").mock(return_value=httpx.Response(200, json=[]))
    prof = respx.get(f"{REST}/user_profiles")
    patch = respx.patch(f"{REST}/user_profiles")
    assert await ma.pause_partner_after_official_end(A, B) is False
    q = m.calls.last.request.url.params
    # Only a formerly-official (both flags), now-inactive pair qualifies,
    # and the pair filter covers both orderings of caller/other.
    assert q["partner_accepted_by_a"] == "eq.true"
    assert q["partner_accepted_by_b"] == "eq.true"
    assert q["is_active"] == "eq.false"
    assert A in q["or"] and B in q["or"]
    assert not prof.called and not patch.called


@respx.mock
async def test_pause_qualifying_patches_both_flags_and_preserves_bio():
    respx.get(f"{REST}/matches").mock(return_value=httpx.Response(200, json=[{"id": "m1"}]))
    respx.get(f"{REST}/user_profiles").mock(
        return_value=httpx.Response(200, json=[{"bio": json.dumps({"about_yourself": "hi", "intros_paused": False})}])
    )
    patch = respx.patch(f"{REST}/user_profiles").mock(return_value=httpx.Response(204))
    assert await ma.pause_partner_after_official_end(A, B) is True
    assert patch.calls.last.request.url.params["user_id"] == f"eq.{B}"  # the OTHER member
    bio = json.loads(json.loads(patch.calls.last.request.content)["bio"])
    assert bio["intros_paused"] is True
    assert bio["official_end_choice_pending"] is True
    assert bio["official_end_match_id"] == "m1"
    assert bio["about_yourself"] == "hi"


@respx.mock
@pytest.mark.parametrize("raw", ["not json at all", json.dumps(["a", "list"]), None])
async def test_pause_malformed_bio_handled(raw):
    respx.get(f"{REST}/matches").mock(return_value=httpx.Response(200, json=[{"id": "m1"}]))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{"bio": raw}]))
    patch = respx.patch(f"{REST}/user_profiles").mock(return_value=httpx.Response(204))
    assert await ma.pause_partner_after_official_end(A, B) is True
    bio = json.loads(json.loads(patch.calls.last.request.content)["bio"])
    assert bio["intros_paused"] is True and bio["official_end_choice_pending"] is True


@respx.mock
async def test_pause_is_idempotent_per_ended_match():
    """A retried/repeated notify must not re-pause a member who already chose."""
    respx.get(f"{REST}/matches").mock(return_value=httpx.Response(200, json=[{"id": "m1"}]))
    respx.get(f"{REST}/user_profiles").mock(
        return_value=httpx.Response(
            200, json=[{"bio": json.dumps({"intros_paused": False, "official_end_match_id": "m1"})}]
        )
    )
    patch = respx.patch(f"{REST}/user_profiles")
    assert await ma.pause_partner_after_official_end(A, B) is False
    assert not patch.called


@respx.mock
async def test_pause_profile_missing_no_write():
    respx.get(f"{REST}/matches").mock(return_value=httpx.Response(200, json=[{"id": "m1"}]))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[]))
    patch = respx.patch(f"{REST}/user_profiles")
    assert await ma.pause_partner_after_official_end(A, B) is False
    assert not patch.called


# --------------------------------------------------------------------------
# Pair-check helpers used by the notify-* endpoints
# --------------------------------------------------------------------------

@respx.mock
async def test_latest_match_between_filters():
    r = respx.get(f"{REST}/matches").mock(return_value=httpx.Response(200, json=[{"id": "m9"}]))
    assert (await ma.latest_match_between(A, B, active=False))["id"] == "m9"
    q = r.calls.last.request.url.params
    assert q["is_active"] == "eq.false" and q["order"] == "created_at.desc"
    respx.get(f"{REST}/matches").mock(return_value=httpx.Response(500))
    assert await ma.latest_match_between(A, B) is None  # fail closed


@respx.mock
async def test_request_in_state_and_recent_notification():
    respx.get(f"{REST}/match_requests").mock(return_value=httpx.Response(200, json=[{"id": "x"}]))
    assert await ma.request_in_state(A, B, "declined") is True
    n = respx.get(f"{REST}/notifications").mock(return_value=httpx.Response(200, json=[]))
    assert await ma.recent_notification_exists(B, "unmatched", A, minutes=10) is False
    assert n.calls.last.request.url.params["created_at"].startswith("gte.")


# --------------------------------------------------------------------------
# delete_account — 12.1.1 / §13 "Delete account"
# --------------------------------------------------------------------------

def _install_fake_billing(monkeypatch, cancel):
    fake_stripe = types.SimpleNamespace(Subscription=types.SimpleNamespace(cancel=cancel))
    fake_billing = types.ModuleType("billing")
    fake_billing._get_stripe = lambda: fake_stripe
    monkeypatch.setitem(sys.modules, "billing", fake_billing)


def _mock_happy_delete(order, *, sub_rows=None, storage_names=("kyc-face-1.jpg", "primary-1.jpg")):
    def rec(label, resp):
        def _se(request):
            order.append(label(request) if callable(label) else label)
            return resp() if callable(resp) else resp
        return _se

    respx.get(f"{REST}/user_memberships").mock(
        side_effect=rec("memberships_lookup", httpx.Response(200, json=sub_rows or []))
    )
    respx.get(f"{REST}/matches").mock(side_effect=rec("matches_lookup", lambda: httpx.Response(200, json=[{"id": "m1"}])))
    respx.get(f"{REST}/cheri_ai_assessment_sessions").mock(
        side_effect=rec("sessions_lookup", lambda: httpx.Response(200, json=[{"id": "s1"}]))
    )
    # Every REST DELETE; label by table (+ filter column) for ordering checks.
    respx.delete(url__startswith=f"{REST}/").mock(
        side_effect=rec(
            lambda r: "del:" + r.url.path.rsplit("/", 1)[-1] + ":" + ",".join(sorted(r.url.params.keys())),
            lambda: httpx.Response(404, json={"message": "not found"}),
        )
    )
    respx.post(f"{BASE}/storage/v1/object/list/avatars").mock(
        side_effect=rec("storage_list", lambda: httpx.Response(200, json=[{"name": n} for n in storage_names]))
    )
    respx.request("DELETE", f"{BASE}/storage/v1/object/avatars").mock(
        side_effect=rec("storage_remove", lambda: httpx.Response(200, json=[]))
    )
    # Private buckets from migration 048 don't exist yet in this scenario.
    for _b in ("media", "kyc"):
        respx.post(f"{BASE}/storage/v1/object/list/{_b}").mock(
            return_value=httpx.Response(400, json={"error": "Bucket not found", "message": "Bucket not found"})
        )
    return respx.delete(f"{BASE}/auth/v1/admin/users/{A}").mock(
        side_effect=rec("auth_delete", lambda: httpx.Response(200, json={}))
    )


@respx.mock
async def test_delete_account_order_tolerates_404_auth_last(monkeypatch):
    cancelled = []
    _install_fake_billing(monkeypatch, cancelled.append)
    order: list[str] = []
    auth = _mock_happy_delete(order, sub_rows=[{"stripe_subscription_id": "sub_1"}, {"stripe_subscription_id": "sub_1"}])

    out = await ma.delete_account(A)

    assert out["ok"] is True and out["status"] == "completed"
    assert cancelled == ["sub_1"]  # cancelled once even with duplicate rows
    assert order[0] == "memberships_lookup"  # billing handled before any data removal
    assert order[-1] == "auth_delete"  # login removed last → retryable
    assert order.index("storage_remove") < order.index("auth_delete")
    # profile row deleted after the dependent tables
    prof = order.index("del:user_profiles:user_id")
    assert prof > order.index("del:notifications:user_id")
    assert prof > order.index("del:match_requests:sender_id")
    assert auth.called
    # storage removal targets the member's own folder only
    removed = json.loads(respx.calls[[c.request.method == "DELETE" and c.request.url.path.endswith("/object/avatars") for c in respx.calls].index(True)].request.content)
    assert all(p.startswith(f"{A}/") for p in removed["prefixes"])


@respx.mock
async def test_delete_account_raises_on_table_failure_before_auth_delete(monkeypatch):
    _install_fake_billing(monkeypatch, lambda s: None)
    order: list[str] = []
    auth = _mock_happy_delete(order)

    def fail_notifications(request):
        if request.url.path.endswith("/notifications"):
            return httpx.Response(500, text="db down")
        return httpx.Response(204)

    respx.delete(url__startswith=f"{REST}/").mock(side_effect=fail_notifications)
    with pytest.raises(MemberActionError) as e:
        await ma.delete_account(A)
    assert str(e.value) == "delete_failed_notifications"
    assert not auth.called


@respx.mock
async def test_delete_account_stripe_failure_stops_before_any_deletion(monkeypatch):
    def boom(sub):
        raise RuntimeError("stripe unreachable")

    _install_fake_billing(monkeypatch, boom)
    order: list[str] = []
    auth = _mock_happy_delete(order, sub_rows=[{"stripe_subscription_id": "sub_1"}])
    with pytest.raises(MemberActionError) as e:
        await ma.delete_account(A)
    assert str(e.value) == "billing_cancel_failed"
    assert order == ["memberships_lookup"]  # nothing deleted, membership pointer kept
    assert not auth.called


@respx.mock
async def test_delete_account_stripe_already_cancelled_is_tolerated(monkeypatch):
    class Gone(Exception):
        code = "resource_missing"

    def gone(sub):
        raise Gone("No such subscription: sub_1")

    _install_fake_billing(monkeypatch, gone)
    order: list[str] = []
    _mock_happy_delete(order, sub_rows=[{"stripe_subscription_id": "sub_1"}])
    assert (await ma.delete_account(A))["ok"] is True
    assert order[-1] == "auth_delete"


@respx.mock
async def test_delete_account_storage_failure_keeps_login(monkeypatch):
    _install_fake_billing(monkeypatch, lambda s: None)
    order: list[str] = []
    auth = _mock_happy_delete(order)
    respx.post(f"{BASE}/storage/v1/object/list/avatars").mock(return_value=httpx.Response(500))
    with pytest.raises(MemberActionError) as e:
        await ma.delete_account(A)
    assert str(e.value) == "storage_list_failed"
    assert not auth.called  # KYC media must not outlive a "deleted" account


@respx.mock
async def test_delete_account_auth_delete_failure_raises(monkeypatch):
    _install_fake_billing(monkeypatch, lambda s: None)
    order: list[str] = []
    _mock_happy_delete(order)
    respx.delete(f"{BASE}/auth/v1/admin/users/{A}").mock(return_value=httpx.Response(500))
    with pytest.raises(MemberActionError) as e:
        await ma.delete_account(A)
    assert str(e.value) == "auth_delete_failed"


@respx.mock
async def test_delete_account_retry_after_partial_run_succeeds(monkeypatch):
    """Second attempt (rows already gone → 404/empty) still completes."""
    _install_fake_billing(monkeypatch, lambda s: None)
    order: list[str] = []
    _mock_happy_delete(order, storage_names=())
    respx.get(f"{REST}/matches").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REST}/cheri_ai_assessment_sessions").mock(return_value=httpx.Response(200, json=[]))
    respx.delete(f"{BASE}/auth/v1/admin/users/{A}").mock(return_value=httpx.Response(404))
    out = await ma.delete_account(A)
    assert out["ok"] is True


@respx.mock
async def test_delete_storage_empties_private_buckets_after_048():
    """Once 048 creates `kyc`/`media`, deletion must remove the member's files there too."""
    lists = {
        "avatars": [{"name": "primary-1.jpg"}],
        "media": [{"name": "gallery-1.jpg"}],
        "kyc": [{"name": "kyc-id-UAE-passport-1.jpg"}, {"name": "kyc-face-1.jpg"}],
    }
    removed = {}
    for b, objs in lists.items():
        respx.post(f"{BASE}/storage/v1/object/list/{b}").mock(return_value=httpx.Response(200, json=objs))

        def _rm(request, b=b):
            removed[b] = json.loads(request.content)["prefixes"]
            return httpx.Response(200, json=[])

        respx.request("DELETE", f"{BASE}/storage/v1/object/{b}").mock(side_effect=_rm)
    async with httpx.AsyncClient() as client:
        total = await ma._delete_storage(client, A)
    assert total == 4
    assert removed["kyc"] == [f"{A}/kyc-id-UAE-passport-1.jpg", f"{A}/kyc-face-1.jpg"]
    assert removed["media"] == [f"{A}/gallery-1.jpg"]


@respx.mock
async def test_delete_storage_private_bucket_list_error_blocks_deletion():
    respx.post(f"{BASE}/storage/v1/object/list/avatars").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/storage/v1/object/list/media").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/storage/v1/object/list/kyc").mock(return_value=httpx.Response(500, text="boom"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(MemberActionError) as e:
            await ma._delete_storage(client, A)
    assert str(e.value) == "storage_list_failed"
