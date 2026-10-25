"""
Audit E — FastAPI endpoint contracts (status mapping, authorisation of the
notify-* relays, neutral copy, admin gating, KYC URL allowlist, webhook).

get_current_user_id is overridden; downstream calls are monkeypatched or
respx-mocked. TestClient is used WITHOUT a `with` block so the lifespan
(price-map refresh + APScheduler) never starts.
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

import auth
import main
from member_actions import MemberActionError
from partnership import PartnershipError

A = "aaaaaaaa-0000-0000-0000-00000000000a"
B = "bbbbbbbb-0000-0000-0000-00000000000b"
REST = f"{main.SUPABASE_URL}/rest/v1"


@pytest.fixture
def client():
    main.app.dependency_overrides[main.get_current_user_id] = lambda: A
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


@pytest.fixture
def sent(monkeypatch):
    """Capture send_transactional_email calls made by main.py."""
    calls = []

    async def fake_send(**kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(main, "send_transactional_email", fake_send)
    return calls


# --------------------------------------------------------------------------
# /matching/withdraw-interest status mapping
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "code,state,status",
    [("request_not_found", None, 404), ("not_sender", None, 403), ("not_pending", "accepted", 409), ("update_failed", None, 500)],
)
def test_withdraw_interest_status_mapping(client, monkeypatch, code, state, status):
    async def fake(request_id, caller):
        assert caller == A  # caller comes from the verified JWT
        raise MemberActionError(code, state=state)

    monkeypatch.setattr(main, "withdraw_interest", fake)
    r = client.post("/matching/withdraw-interest", json={"request_id": "rrrrrrrr-1"})
    assert r.status_code == status
    assert r.json()["detail"] == {"code": code, "state": state}


def test_withdraw_interest_success_and_validation(client, monkeypatch):
    async def fake(request_id, caller):
        return {"ok": True, "status": "withdrawn", "receiver_id": B}

    monkeypatch.setattr(main, "withdraw_interest", fake)
    assert client.post("/matching/withdraw-interest", json={"request_id": "rrrrrrrr-1"}).json()["status"] == "withdrawn"
    assert client.post("/matching/withdraw-interest", json={"request_id": "x"}).status_code == 422


def test_withdraw_interest_requires_auth():
    r = TestClient(main.app).post("/matching/withdraw-interest", json={"request_id": "rrrrrrrr-1"})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# /account/delete
# --------------------------------------------------------------------------

def test_account_delete_success_forgets_cached_token(client, monkeypatch):
    auth._cache["tok-of-a"] = (9e18, A)
    auth._cache["tok-of-b"] = (9e18, B)

    async def fake(uid):
        assert uid == A  # never from the body
        return {"ok": True, "status": "completed", "deleted_at": "now"}

    monkeypatch.setattr(main, "delete_account", fake)
    r = client.post("/account/delete", json={"user_id": B})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert "tok-of-a" not in auth._cache and "tok-of-b" in auth._cache
    auth._cache.clear()


@pytest.mark.parametrize(
    "code,status",
    [("billing_cancel_failed", 502), ("storage_list_failed", 500), ("auth_delete_failed", 500), ("delete_failed_notifications", 500)],
)
def test_account_delete_failure_mapping(client, monkeypatch, code, status):
    async def fake(uid):
        raise MemberActionError(code)

    monkeypatch.setattr(main, "delete_account", fake)
    r = client.post("/account/delete", json={})
    assert r.status_code == status
    assert r.json()["detail"] == {"code": code, "retryable": True}


def test_account_delete_requires_auth():
    assert TestClient(main.app).post("/account/delete", json={}).status_code == 401


# --------------------------------------------------------------------------
# /matching/notify-unmatch
# --------------------------------------------------------------------------

def _unmatch_env(monkeypatch, *, ended, duplicate=False):
    paused = []

    async def latest(a, b, active=None):
        return ended

    async def recent(*a, **k):
        return duplicate

    async def pause(caller, other):
        paused.append((caller, other))
        return True

    monkeypatch.setattr(main, "latest_match_between", latest)
    monkeypatch.setattr(main, "recent_notification_exists", recent)
    monkeypatch.setattr(main, "pause_partner_after_official_end", pause)
    return paused


@respx.mock
def test_notify_unmatch_official_neutral_copy_and_pause(client, monkeypatch, sent):
    paused = _unmatch_env(
        monkeypatch, ended={"id": "m1", "is_active": False, "partner_accepted_by_a": True, "partner_accepted_by_b": True}
    )
    respx.get(f"{REST}/public_profiles").mock(
        return_value=httpx.Response(200, json=[{"user_id": A, "nick_name": "Ann"}, {"user_id": B, "nick_name": "Bo"}])
    )
    ins = respx.post(f"{REST}/notifications").mock(return_value=httpx.Response(201))
    r = client.post("/matching/notify-unmatch", json={"other_user_id": B, "was_partnered": True})
    assert r.json() == {"ok": True}
    rows = json.loads(ins.calls.last.request.content)
    other = next(n for n in rows if n["user_id"] == B)
    # Neutral: no reason, no initiator name, and not told they're "open".
    assert "Ann" not in other["title"] + other["body"]
    assert "paused" in other["body"].lower()
    assert paused == [(A, B)]
    assert [c["template_slug"] for c in sent] == ["relationship_ended"]
    assert sent[0]["user_id"] == B


@respx.mock
def test_notify_unmatch_ordinary_no_pause_no_email(client, monkeypatch, sent):
    paused = _unmatch_env(monkeypatch, ended={"id": "m1", "is_active": False})
    respx.get(f"{REST}/public_profiles").mock(return_value=httpx.Response(200, json=[]))
    ins = respx.post(f"{REST}/notifications").mock(return_value=httpx.Response(201))
    assert client.post("/matching/notify-unmatch", json={"other_user_id": B, "was_partnered": False}).json() == {"ok": True}
    rows = json.loads(ins.calls.last.request.content)
    assert len(rows) == 1 and rows[0]["user_id"] == B
    assert paused == [] and sent == []


@respx.mock
def test_notify_unmatch_client_claims_official_but_server_disagrees(client, monkeypatch, sent):
    # was_partnered is client-supplied; without both flags it's an ordinary end.
    paused = _unmatch_env(
        monkeypatch, ended={"id": "m1", "is_active": False, "partner_accepted_by_a": True, "partner_accepted_by_b": False}
    )
    respx.get(f"{REST}/public_profiles").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{REST}/notifications").mock(return_value=httpx.Response(201))
    client.post("/matching/notify-unmatch", json={"other_user_id": B, "was_partnered": True})
    assert paused == [] and sent == []


@respx.mock
def test_notify_unmatch_without_ended_match_is_refused(client, monkeypatch, sent):
    paused = _unmatch_env(monkeypatch, ended=None)
    ins = respx.post(f"{REST}/notifications")
    r = client.post("/matching/notify-unmatch", json={"other_user_id": B, "was_partnered": True})
    assert r.json()["skipped"] == "no_ended_match"
    assert not ins.called and paused == [] and sent == []


@respx.mock
def test_notify_unmatch_retry_is_idempotent(client, monkeypatch, sent):
    _unmatch_env(
        monkeypatch,
        ended={"id": "m1", "is_active": False, "partner_accepted_by_a": True, "partner_accepted_by_b": True},
        duplicate=True,
    )
    ins = respx.post(f"{REST}/notifications")
    assert client.post("/matching/notify-unmatch", json={"other_user_id": B, "was_partnered": True}).json()["skipped"] == "duplicate"
    assert not ins.called and sent == []


# --------------------------------------------------------------------------
# Other notify relays must be bound to real pair state
# --------------------------------------------------------------------------

@respx.mock
def test_notify_interest_responded_requires_matching_request(client, monkeypatch, sent):
    async def no(*a, **k):
        return False

    async def none(*a, **k):
        return None

    monkeypatch.setattr(main, "request_in_state", no)
    monkeypatch.setattr(main, "latest_match_between", none)
    ins = respx.post(f"{REST}/notifications")
    for action in ("accept", "decline"):
        r = client.post("/matching/notify-interest-responded", json={"sender_user_id": B, "action": action})
        assert r.json()["skipped"] == "no_matching_request"
    assert not ins.called and sent == []


@respx.mock
def test_notify_interest_responded_verified_decline_sends(client, monkeypatch, sent):
    seen = []

    async def yes(sender, receiver, status):
        seen.append((sender, receiver, status))
        return True

    monkeypatch.setattr(main, "request_in_state", yes)
    respx.get(f"{REST}/public_profiles").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{REST}/notifications").mock(return_value=httpx.Response(201))
    client.post("/matching/notify-interest-responded", json={"sender_user_id": B, "action": "decline"})
    assert seen == [(B, A, "declined")]  # caller must be the RECEIVER
    assert sent[0]["template_slug"] == "interest_declined" and sent[0]["user_id"] == B


def test_notify_proposal_declined_requires_proposal(client, monkeypatch, sent):
    async def latest(a, b, active=None):
        return {"user_a_id": A, "user_b_id": B, "partner_accepted_by_a": True, "partner_accepted_by_b": False}

    monkeypatch.setattr(main, "latest_match_between", latest)
    # proposer B never set their flag → refused
    r = client.post("/matching/notify-partner-proposal-declined", json={"proposer_user_id": B})
    assert r.json()["skipped"] == "no_proposal" and sent == []


@respx.mock
def test_chat_unread_requires_active_pair_and_uses_generic_preview(client, monkeypatch):
    import push_notifications

    pushes = []

    async def fake_push(**kw):
        pushes.append(kw)
        return True

    monkeypatch.setattr(push_notifications, "send_push_notification", fake_push)

    async def none(*a, **k):
        return None

    monkeypatch.setattr(main, "latest_match_between", none)
    assert client.post("/chat/notify-unread-message", json={"receiver_user_id": B}).json()["skipped"] == "no_active_connection"
    assert pushes == []

    async def active(a, b, active=None):
        assert active is True
        return {"id": "m1", "is_active": True}

    monkeypatch.setattr(main, "latest_match_between", active)
    client.post("/chat/notify-unread-message", json={"receiver_user_id": B})
    assert len(pushes) == 1
    assert pushes[0]["user_id"] == B and pushes[0]["related_user_id"] == A
    assert "{{" not in pushes[0]["title"] and "Ann" not in pushes[0]["title"]


@respx.mock
def test_notify_partner_proposal_requires_live_proposal(client, sent):
    respx.get(f"{REST}/matches").mock(
        return_value=httpx.Response(
            200,
            json=[{"user_a_id": A, "user_b_id": B, "is_active": True, "partnered_at": None,
                   "partner_accepted_by_a": False, "partner_accepted_by_b": False}],
        )
    )
    ins = respx.post(f"{REST}/notifications")
    r = client.post("/matching/notify-partner-proposal", json={"match_id": "m1"})
    assert r.json()["skipped"] == "no_pending_proposal"
    assert not ins.called and sent == []


# --------------------------------------------------------------------------
# /emails/trigger-self whitelist
# --------------------------------------------------------------------------

def test_trigger_self_whitelist(client, sent):
    assert client.post("/emails/trigger-self", json={"template_slug": "verify_approved"}).status_code == 400
    r = client.post(
        "/emails/trigger-self",
        json={"template_slug": "welcome_free", "extra_vars": {"name": "<script>"}, "dedup_window_hours": 0},
    )
    assert r.status_code == 200
    assert sent == [{"user_id": A, "template_slug": "welcome_free", "extra_vars": None, "dedup_window_hours": 1}]


# --------------------------------------------------------------------------
# /matching/finalize-partnership mapping
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "code,status",
    [("match_not_found", 404), ("not_a_participant", 403), ("both_must_accept_first", 409),
     ("match_inactive", 409), ("already_official_elsewhere", 409), ("state_changed", 409), ("PATCH matches failed [500]: secret detail", 502)],
)
def test_finalize_status_mapping(client, monkeypatch, code, status):
    async def fake(match_id, uid):
        raise PartnershipError(code)

    monkeypatch.setattr(main, "finalize_partnership", fake)
    r = client.post("/matching/finalize-partnership", json={"match_id": "mmmmmmmm-1"})
    assert r.status_code == status
    assert "secret detail" not in r.text


# --------------------------------------------------------------------------
# Admin endpoints are gated (no override for get_current_admin_id)
# --------------------------------------------------------------------------

ADMIN_POSTS = [
    "/admin/run-daily-email-cron", "/admin/send-test-email", "/admin/send-test-template",
    "/admin/wipe-relationships", "/admin/trigger-email", "/admin/trigger-push",
    "/admin/simulate-auth-email?to_address=a@b.c", "/admin/invalidate-app-config-cache",
    "/admin/wipe-dedup-queue", "/memory/admin-reseed",
]


@pytest.mark.parametrize("path", ADMIN_POSTS)
def test_admin_endpoints_reject_missing_token(client, path):
    # Even with a member identity overridden, admin routes use their own
    # dependency and refuse a request without a bearer token.
    assert client.post(path, json={}).status_code == 401


def test_every_admin_route_depends_on_admin_guard():
    from fastapi.routing import APIRoute

    def deps(route):
        out, stack = [], list(route.dependant.dependencies)
        while stack:
            d = stack.pop()
            out.append(d.call)
            stack.extend(d.dependencies)
        return out

    admin_routes = [r for r in main.app.routes if isinstance(r, APIRoute) and ("/admin" in r.path)]
    assert admin_routes
    for r in admin_routes:
        assert main.get_current_admin_id in deps(r), r.path


@respx.mock
def test_admin_endpoint_rejects_non_admin_token():
    respx.get(f"{auth.SUPABASE_URL}/auth/v1/user").mock(
        return_value=httpx.Response(200, json={"id": A, "app_metadata": {}})
    )
    r = TestClient(main.app).post("/admin/wipe-relationships", headers={"Authorization": "Bearer member.jwt"})
    assert r.status_code == 403


# --------------------------------------------------------------------------
# /kyc/verify-face URL allowlist (SSRF / other members' images)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://evil.example.com/a.jpg",
        f"{main.SUPABASE_URL}/storage/v1/object/public/avatars/{B}/kyc-face-1.jpg",
        f"{main.SUPABASE_URL}/storage/v1/object/public/avatars/{A}/../{B}/kyc-face-1.jpg",
    ],
)
def test_verify_face_rejects_foreign_urls(client, monkeypatch, url):
    called = []

    async def fake_compare(**kw):
        called.append(kw)
        raise AssertionError("must not download")

    monkeypatch.setattr(main, "compare_faces", fake_compare)
    own = f"{main.SUPABASE_URL}/storage/v1/object/public/avatars/{A}/kyc-id-1.jpg"
    r = client.post("/kyc/verify-face", json={"user_id": A, "face_image_url": url, "id_image_url": own})
    assert r.status_code == 400 and called == []


def test_verify_face_accepts_own_folder(client, monkeypatch):
    from face_verification import FaceMatchResult

    async def fake_compare(**kw):
        return FaceMatchResult(similarity=80.0, distance=0.2, model="m", detector_backend="d", matched=True, threshold=70.0)

    async def fake_update(*a, **k):
        return []

    monkeypatch.setattr(main, "compare_faces", fake_compare)
    monkeypatch.setattr(main.supabase, "update", fake_update)
    base = f"{main.SUPABASE_URL}/storage/v1/object/public/avatars/{A}"
    r = client.post(
        "/kyc/verify-face",
        json={"user_id": A, "face_image_url": f"{base}/kyc-face-1.jpg", "id_image_url": f"{base}/kyc-id-UAE-1.jpg"},
    )
    assert r.status_code == 200
    # Never auto-approves: admin review remains the only approval path.
    assert r.json()["verification_final_status"] == "not_verified"
    assert r.json()["verification_status"] == "pending_review"


# --------------------------------------------------------------------------
# /billing/webhook
# --------------------------------------------------------------------------

def test_webhook_signature_error_is_400(monkeypatch):
    import billing

    async def fake(payload, sig):
        raise billing.WebhookSignatureError("bad sig")

    monkeypatch.setattr(billing, "handle_event", fake)
    r = TestClient(main.app).post("/billing/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_signature"


def test_webhook_processing_error_is_500_without_detail(monkeypatch):
    import billing

    async def fake(payload, sig):
        raise RuntimeError("Supabase insert failed: secret-ish text")

    monkeypatch.setattr(billing, "handle_event", fake)
    r = TestClient(main.app).post("/billing/webhook", content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 500 and "secret-ish" not in r.text


@respx.mock
def test_notify_interest_received_requires_pending_row(client, sent):
    pending = respx.get(f"{REST}/match_requests").mock(return_value=httpx.Response(200, json=[]))
    ins = respx.post(f"{REST}/notifications")
    r = client.post("/matching/notify-interest-received", json={"receiver_user_id": B})
    assert r.json()["skipped"] == "no_pending_row"
    q = pending.calls.last.request.url.params
    assert q["sender_id"] == f"eq.{A}" and q["receiver_id"] == f"eq.{B}" and q["status"] == "eq.pending"
    assert not ins.called and sent == []


@respx.mock
def test_notify_interest_received_dedups_unread_and_emails_receiver(client, sent):
    respx.get(f"{REST}/match_requests").mock(return_value=httpx.Response(200, json=[{"id": "r1"}]))
    respx.get(f"{REST}/notifications").mock(return_value=httpx.Response(200, json=[{"id": "n1"}]))  # unread exists
    ins = respx.post(f"{REST}/notifications")
    respx.get(f"{REST}/public_profiles").mock(return_value=httpx.Response(200, json=[]))
    client.post("/matching/notify-interest-received", json={"receiver_user_id": B})
    assert not ins.called
    assert sent[0]["template_slug"] == "interest_received" and sent[0]["user_id"] == B
    assert sent[0]["related_user_id"] == A and sent[0]["dedup_window_hours"] == 2
