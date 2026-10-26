"""
R1 · PWA / admin-panel backward compatibility (regression audit,
documents/audit/REGRESSION_PWA_BACKEND.md).

Every request here is the EXACT body / query shape the live web app
(src/services/*, src/pages/*) or the admin panel (admin_panel/src/*) sends
today, replayed against the current backend. The responses are checked for
the fields those callers read. No network: auth dependencies are
overridden, downstream helpers are monkeypatched, Supabase REST calls are
respx-mocked. TestClient without `with` so the lifespan never starts.
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

import billing
import main
import push_notifications

A = "aaaaaaaa-0000-4000-8000-00000000000a"   # the signed-in PWA member
B = "bbbbbbbb-0000-4000-8000-00000000000b"   # the other member
ADMIN = "dddddddd-0000-4000-8000-00000000000d"
MATCH = "mmmmmmmm-0000-4000-8000-00000000000m"
REST = f"{main.SUPABASE_URL}/rest/v1"
PERIOD = {"period_start": "2026-10-01", "period_end": None, "source": "calendar_month"}


async def _a(value):
    return value


@pytest.fixture
def member():
    main.app.dependency_overrides[main.get_current_user_id] = lambda: A
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


@pytest.fixture
def admin():
    main.app.dependency_overrides[main.get_current_admin_id] = lambda: ADMIN
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_send(**kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(main, "send_transactional_email", fake_send)
    return calls


# ---------------------------------------------------------------------------
# CheriAI chat — src/services/cheriAIService.ts
# ---------------------------------------------------------------------------

@pytest.fixture
def chat(monkeypatch, member):
    state = {"saved": [], "used": 3, "reserve": 4}
    monkeypatch.setattr(main, "classify_canned", lambda m: None)
    monkeypatch.setattr(main, "fetch_user_profile", lambda uid: _a({"user_id": uid, "nick_name": "Nick"}))
    monkeypatch.setattr(main, "load_memory", lambda uid: _a({"summary": "", "insights": []}))
    monkeypatch.setattr(main, "load_recent_messages", lambda uid: _a([]))
    monkeypatch.setattr(main, "get_monthly_limits", lambda: _a({"basic": 10, "premium-lite": 50, "premium": float("inf")}))
    monkeypatch.setattr(billing, "get_membership_tier", lambda uid: _a("basic"))
    monkeypatch.setattr(billing, "get_quota_period", lambda uid: _a(PERIOD))
    monkeypatch.setattr(billing, "get_period_usage", lambda uid, p: _a(state["used"]))
    monkeypatch.setattr(billing, "try_reserve_message", lambda uid, p, lim: _a(state["reserve"]))
    monkeypatch.setattr(billing, "release_message", lambda uid, p: _a(None))
    monkeypatch.setattr(billing, "increment_period_usage", lambda uid, p: _a(state["used"] + 1))

    async def llm(prompt, user=None, json_mode=False, **k):
        return '{"reply": "Hi from Cheri", "follow_up": "Want to talk more?"}', {"model": "m", "total_tokens": 3}

    async def save(*args):
        state["saved"].append(args)

    monkeypatch.setattr(main, "send_to_llm_with_usage", llm)
    monkeypatch.setattr(main, "save_conversation_message", save)
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(main, "update_memory_async", noop)
    monkeypatch.setattr(main, "_record_ai_usage", noop)
    return state, member


def test_chat_accepts_pwa_payload_with_legacy_user_id_and_null_conversation(chat):
    state, client = chat
    # cheriAIService.sendMessage → {user_id, message, stage, context, conversation_id}
    # (context undefined is dropped by JSON.stringify; conversation_id may be null).
    r = client.post("/chat", json={"user_id": B, "message": "How do I open up?", "stage": "general",
                                   "conversation_id": None})
    assert r.status_code == 200
    body = r.json()
    # Fields CheriAIChat.tsx reads.
    assert body["reply"] == "Hi from Cheri" and body["follow_up"] == "Want to talk more?"
    assert body["conversation_id"].startswith(f"conv_{A}_")  # body user_id ignored, JWT wins
    assert body["tier"] == "basic" and body["daily_used"] == 4 and body["daily_limit"] == 10
    assert all(row[0] == A for row in state["saved"])


def test_chat_keeps_existing_conversation_id(chat):
    state, client = chat
    r = client.post("/chat", json={"user_id": A, "message": "again", "stage": "general",
                                   "conversation_id": "conv_x_1"})
    assert r.status_code == 200 and r.json()["conversation_id"] == "conv_x_1"


def test_chat_over_long_message_is_422_with_readable_string_detail(chat):
    _, client = chat
    r = client.post("/chat", json={"user_id": A, "message": "x" * 2001, "stage": "general"})
    assert r.status_code == 422
    # cheriAIService renders `detail` verbatim when it's a string.
    assert isinstance(r.json()["detail"], str) and "2,000" in r.json()["detail"]
    assert client.post("/chat", json={"message": "x" * 2000}).status_code == 200


def test_chat_quota_429_keeps_the_structured_detail_the_pwa_parses(chat):
    state, client = chat
    state["used"] = 10
    r = client.post("/chat", json={"user_id": A, "message": "hello?", "stage": "general"})
    assert r.status_code == 429
    d = r.json()["detail"]
    # CheriDailyLimitError(d.message, d.tier, d.daily_used, d.daily_limit)
    assert isinstance(d, dict) and d["message"] and d["tier"] == "basic"
    assert d["daily_used"] == 10 and d["daily_limit"] == 10


def test_cheri_thread_and_conversation_reads(member, monkeypatch):
    rows = [{"role": "assistant", "content": "b", "created_at": "2", "conversation_id": "c1", "stage": "general"},
            {"role": "user", "content": "a", "created_at": "1", "conversation_id": "c1", "stage": "general"}]

    async def select(table, **kw):
        assert kw["eq"]["user_id"] == A
        return list(rows)

    monkeypatch.setattr(main.supabase, "select", select)
    r = member.get(f"/cheri-thread/{A}?limit=50")
    assert r.status_code == 200 and [m["content"] for m in r.json()["messages"]] == ["a", "b"]
    r = member.get(f"/conversations/{A}/c1")
    assert r.status_code == 200 and len(r.json()["messages"]) == 2
    r = member.get(f"/conversations/{A}")
    assert r.status_code == 200 and r.json()["conversations"][0]["conversation_id"] == "c1"
    # Another member's thread stays private.
    assert member.get(f"/cheri-thread/{B}?limit=50").status_code == 403


def test_health_is_open_and_says_ok():
    r = TestClient(main.app).get("/health", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Self-service email + memory seed — Registration.tsx / Profile.tsx
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["welcome_free", "verification_submitted"])
def test_trigger_self_accepts_every_pwa_template(member, sent, slug):
    r = member.post("/emails/trigger-self", json={"template_slug": slug})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert sent[-1]["user_id"] == A and sent[-1]["template_slug"] == slug


def test_memory_seed_with_pwa_body(member, monkeypatch):
    monkeypatch.setattr(main, "seed_memory_from_profile", lambda uid: _a({"summary": "s", "insights": [1]}))
    r = member.post("/memory/seed", json={"user_id": A})
    assert r.status_code == 200 and r.json() == {"ok": True, "summary": "s", "insights_count": 1}


# ---------------------------------------------------------------------------
# Assessment completion notify — cheriAIAssessmentService.ts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,eligible", [(86, True), (100, True), (58, False), (-12, False)])
def test_assessment_notify_completed_pwa_body(member, sent, score, eligible):
    # The PWA computes 100 - 14 x flags with no floor, so negative scores happen.
    r = member.post("/assessment/notify-completed", json={"score": score, "eligible": eligible})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert sent[-1]["template_slug"] == "assessment_completed"


# ---------------------------------------------------------------------------
# Matching notify relays — matchingService.ts (called AFTER the DB change)
# ---------------------------------------------------------------------------

@respx.mock
def test_notify_interest_received_after_pending_insert(member, sent):
    respx.get(f"{REST}/match_requests").mock(return_value=httpx.Response(200, json=[{"id": "r1"}]))
    respx.get(f"{REST}/notifications").mock(return_value=httpx.Response(200, json=[]))
    ins = respx.post(f"{REST}/notifications").mock(return_value=httpx.Response(201))
    respx.get(f"{REST}/public_profiles").mock(return_value=httpx.Response(200, json=[{"nick_name": "Ann"}]))
    # sendMatchRequestSimple sends an explicit Content-Type header too.
    r = member.post("/matching/notify-interest-received", json={"receiver_user_id": B},
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and r.json() == {"ok": True}  # PWA retries unless r.ok
    assert ins.called and sent[-1]["user_id"] == B


@pytest.mark.parametrize("action,state", [("accept", "accepted"), ("decline", "declined")])
@respx.mock
def test_notify_interest_responded_after_rpc_or_update(member, sent, monkeypatch, action, state):
    async def in_state(sender, receiver, status):
        return sender == B and receiver == A and status == state

    monkeypatch.setattr(main, "request_in_state", in_state)
    monkeypatch.setattr(main, "latest_match_between", lambda *a, **k: _a(None))
    respx.get(f"{REST}/public_profiles").mock(return_value=httpx.Response(200, json=[{"nick_name": "Ann"}]))
    respx.post(f"{REST}/notifications").mock(return_value=httpx.Response(201))
    r = member.post("/matching/notify-interest-responded", json={"sender_user_id": B, "action": action})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert sent[-1]["user_id"] == B


@respx.mock
def test_notify_partner_proposal_after_flag_update(member, sent):
    # acceptPartnership has already set partner_accepted_by_a=true.
    respx.get(f"{REST}/matches").mock(return_value=httpx.Response(200, json=[{
        "user_a_id": A, "user_b_id": B, "is_active": True, "partnered_at": None,
        "partner_accepted_by_a": True, "partner_accepted_by_b": False}]))
    respx.get(f"{REST}/notifications").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{REST}/notifications").mock(return_value=httpx.Response(201))
    respx.get(f"{REST}/public_profiles").mock(return_value=httpx.Response(200, json=[{"user_id": A, "nick_name": "Ann"}]))
    r = member.post("/matching/notify-partner-proposal", json={"match_id": MATCH})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json().get("inserted") is True
    assert sent[-1]["template_slug"] == "partner_proposal_received"


def test_finalize_partnership_pwa_body(member, monkeypatch):
    async def fin(match_id, user_id):
        assert (match_id, user_id) == (MATCH, A)
        return {"ok": True, "partnered_at": "now"}

    monkeypatch.setattr(main, "finalize_partnership", fin)
    r = member.post("/matching/finalize-partnership", json={"match_id": MATCH})
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.parametrize("was_partnered", [False, True])
@respx.mock
def test_notify_unmatch_after_deactivation(member, sent, monkeypatch, was_partnered):
    # unmatchUser set is_active=false + partnered_at=null, kept both flags.
    ended = {"id": MATCH, "user_a_id": A, "user_b_id": B, "is_active": False, "partnered_at": None,
             "partner_accepted_by_a": was_partnered, "partner_accepted_by_b": was_partnered}

    async def latest(a, b, active=None):
        assert active is False
        return ended

    monkeypatch.setattr(main, "latest_match_between", latest)
    monkeypatch.setattr(main, "recent_notification_exists", lambda *a, **k: _a(False))
    monkeypatch.setattr(main, "pause_partner_after_official_end", lambda a, b: _a(True))
    respx.get(f"{REST}/public_profiles").mock(return_value=httpx.Response(200, json=[
        {"user_id": A, "nick_name": "Ann"}, {"user_id": B, "nick_name": "Ben"}]))
    ins = respx.post(f"{REST}/notifications").mock(return_value=httpx.Response(201))
    r = member.post("/matching/notify-unmatch", json={"other_user_id": B, "was_partnered": was_partnered})
    assert r.status_code == 200 and r.json() == {"ok": True} and ins.called
    assert bool(sent) is was_partnered  # official end also emails the other member


@respx.mock
def test_notify_partner_proposal_declined_after_unmatch(member, sent, monkeypatch):
    m = {"user_a_id": B, "user_b_id": A, "is_active": False, "partnered_at": None,
         "partner_accepted_by_a": True, "partner_accepted_by_b": False}
    monkeypatch.setattr(main, "latest_match_between", lambda *a, **k: _a(m))
    respx.get(f"{REST}/public_profiles").mock(return_value=httpx.Response(200, json=[{"user_id": A, "nick_name": "Ann"}]))
    r = member.post("/matching/notify-partner-proposal-declined", json={"proposer_user_id": B})
    assert r.status_code == 200 and sent[-1]["template_slug"] == "partner_proposal_declined"


def test_notify_unread_message_for_active_pair(member, monkeypatch):
    pushes = []

    async def push(**kw):
        pushes.append(kw)
        return True

    monkeypatch.setattr(main, "latest_match_between", lambda *a, **k: _a({"id": MATCH, "is_active": True}))
    monkeypatch.setattr(push_notifications, "send_push_notification", push)
    r = member.post("/chat/notify-unread-message", json={"receiver_user_id": B})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert pushes and pushes[0]["user_id"] == B


# ---------------------------------------------------------------------------
# KYC image URLs written by the PWA (Profile.uploadToAvatarBucket)
# ---------------------------------------------------------------------------

def test_pwa_public_avatar_kyc_urls_are_accepted(monkeypatch):
    from face_verification import is_own_storage_image_url

    base = "https://test.supabase.co"
    monkeypatch.setenv("SUPABASE_URL", base)
    for folder in ("kyc-id", "kyc-face"):
        url = f"{base}/storage/v1/object/public/avatars/{A}/{folder}-1729850000000.jpg"
        assert is_own_storage_image_url(url, A)
        assert not is_own_storage_image_url(url, B)


# ---------------------------------------------------------------------------
# Admin panel — admin_panel/src/pages/*
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug,extra", [
    ("verify_approved", None),                                   # KycReviewPage approve
    ("verify_needs_attention", {"rejection_reason": "blurry"}),  # KycReviewPage reject
    ("account_suspended", None),                                 # UsersPage toggleSuspend
    ("account_reactivated", None),
    ("verify_revoked", {"rejection_reason": "mismatch"}),        # UsersPage revoke
])
def test_admin_trigger_email_shapes(admin, sent, slug, extra):
    body = {"user_id": B, "template_slug": slug}
    if extra is not None:
        body["extra_vars"] = extra
    r = admin.post("/admin/trigger-email", json=body)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert sent[-1]["template_slug"] == slug and sent[-1]["extra_vars"] == extra


def test_admin_trigger_push_targeted(admin, monkeypatch):
    monkeypatch.setattr(push_notifications, "ONESIGNAL_APP_ID", "app")
    monkeypatch.setattr(push_notifications, "ONESIGNAL_REST_API_KEY", "key")
    monkeypatch.setattr(push_notifications, "send_push_notification", lambda **kw: _a(True))
    r = admin.post("/admin/trigger-push", json={"user_id": B, "title": "T", "body": "B", "url": "/#/home"})
    assert r.status_code == 200 and r.json() == {"ok": True, "mode": "targeted", "user_id": B}


@respx.mock
def test_admin_wipe_dedup_queue(admin):
    respx.delete(f"{REST}/sent_emails").mock(return_value=httpx.Response(204, headers={"content-range": "*/3"}))
    respx.delete(f"{REST}/sent_push").mock(return_value=httpx.Response(204, headers={"content-range": "*/1"}))
    r = admin.post("/admin/wipe-dedup-queue", json={"scope": "recent", "hours": 24})
    assert r.status_code == 200 and r.json() == {"ok": True, "deleted": {"sent_emails": 3, "sent_push": 1}}


@respx.mock
def test_admin_wipe_relationships_returns_the_counts_systempage_reads(admin):
    respx.route(method="DELETE", url__startswith=REST).mock(return_value=httpx.Response(200, json=[]))
    r = admin.post("/admin/wipe-relationships")
    assert r.status_code == 200
    body = r.json()
    for k in ("ok", "matches", "match_requests", "user_match_views", "match_messages",
              "match_reads", "notifications", "wiped_at"):
        assert k in body


def test_admin_misc_endpoints(admin, monkeypatch):
    monkeypatch.setattr(main, "seed_memory_from_profile", lambda uid: _a({"summary": "s", "insights": []}))
    r = admin.post("/memory/admin-reseed", json={"user_id": B})
    assert r.status_code == 200 and r.json()["ok"] is True

    assert admin.post("/admin/invalidate-app-config-cache").json() == {"ok": True}

    monkeypatch.setattr(main, "send_test_email", lambda to: _a({"ok": True, "to": to}))
    assert admin.post("/admin/send-test-email", json={"to_address": "qa@cheripic.com"}).json()["ok"] is True

    async def tpl(**kw):
        return {"ok": True, "to": kw["to_address"]}

    monkeypatch.setattr(main, "send_template_test_email", tpl)
    r = admin.post("/admin/send-test-template", json={"to_address": "qa@cheripic.com", "subject": "S",
                                                     "html_body": "<p>x</p>", "text_body": "x"})
    assert r.status_code == 200 and r.json()["ok"] is True

    monkeypatch.setattr(main, "handle_send_email", lambda p: _a({"ok": True, "action": "magiclink", "slug": "s",
                                                                   "to": p["user"]["email"]}))
    r = admin.post("/admin/simulate-auth-email?to_address=qa%40cheripic.com&action=magiclink")
    assert r.status_code == 200 and r.json()["to"] == "qa@cheripic.com"

    monkeypatch.setattr(main, "run_daily_email_cron", lambda dry_run=False: _a({"dry_run": dry_run, "eligible": 0}))
    r = admin.post("/admin/run-daily-email-cron?dry_run=true")
    assert r.status_code == 200 and r.json()["dry_run"] is True


def test_admin_endpoints_still_reject_members(member):
    # A member JWT (no app_metadata.is_admin) must not reach admin routes.
    r = member.post("/admin/trigger-email", json={"user_id": B, "template_slug": "verify_approved"})
    assert r.status_code in (401, 403, 503)
