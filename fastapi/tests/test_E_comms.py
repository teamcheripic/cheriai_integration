"""
Audit E — email (Resend), push (OneSignal), auth email hook and the daily
nudge cron. Request construction, dedupe, opt-out and error handling are
verified against respx mocks; actual delivery needs provider logs
(BLOCKED in the audit matrix).
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

import app_config
import auth_email_hook
import cron_daily_email as cron
import push_notifications as push
import transactional_email as te

REST = f"{te.SUPABASE_URL}/rest/v1"
RESEND = cron.RESEND_ENDPOINT
ONESIGNAL = push.ONESIGNAL_API_URL
U = "uuuuuuuu-0000-0000-0000-00000000000u"
O = "oooooooo-0000-0000-0000-00000000000o"


def _config_rows(**kv):
    base = {"resend_api_key": "re_test_key", "app_base_url": "https://cheripic.com", "communication_live": "false"}
    base.update(kv)
    return [{"key": k, "value": v} for k, v in base.items()]


@pytest.fixture(autouse=True)
def _fresh_config():
    app_config.invalidate_config_cache()
    yield
    app_config.invalidate_config_cache()


def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# --------------------------------------------------------------------------
# send_transactional_email
# --------------------------------------------------------------------------

def _mock_email_env(*, live="false", profile=None, last_sent_hours=None, template=True):
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows(communication_live=live)))
    respx.get(f"{REST}/user_profiles").mock(
        return_value=httpx.Response(
            200,
            json=[profile if profile is not None else {"email": "jane@example.com", "nick_name": "Jane",
                                                       "notify_email_enabled": True, "notify_push_enabled": True}],
        )
    )
    sent_rows = [] if last_sent_hours is None else [{"sent_at": _iso(last_sent_hours)}]
    dedup = respx.get(f"{REST}/sent_emails").mock(return_value=httpx.Response(200, json=sent_rows))
    respx.get(f"{REST}/email_templates").mock(
        return_value=httpx.Response(
            200,
            json=[{"subject": "Hi {{name}}", "html_body": "<p>{{other_name}}</p>", "text_body": "t"}] if template else [],
        )
    )
    audit = respx.post(f"{REST}/sent_emails").mock(return_value=httpx.Response(201))
    resend = respx.post(RESEND).mock(return_value=httpx.Response(200, json={"id": "re_1"}))
    return dedup, audit, resend


@respx.mock
async def test_email_sends_renders_and_audits():
    dedup, audit, resend = _mock_email_env()
    ok = await te.send_transactional_email(U, "interest_declined", extra_vars={"other_name": "Bo"}, related_user_id=O)
    assert ok is True
    body = json.loads(resend.calls.last.request.content)
    assert body["to"] == ["jane@example.com"] and body["subject"] == "Hi Jane" and body["html"] == "<p>Bo</p>"
    assert resend.calls.last.request.headers["authorization"] == "Bearer re_test_key"
    row = json.loads(audit.calls.last.request.content)
    assert row["status"] == "sent" and row["campaign"] == "interest_declined" and row["related_user_id"] == O
    assert not dedup.called  # testing mode (communication_live=false) skips dedup


@respx.mock
async def test_email_dedup_window_per_pair_when_live():
    dedup, audit, resend = _mock_email_env(live="true", last_sent_hours=0.5)
    assert await te.send_transactional_email(U, "interest_declined", dedup_window_hours=2, related_user_id=O) is False
    q = dedup.calls.last.request.url.params
    assert q["related_user_id"] == f"eq.{O}" and q["status"] == "eq.sent" and q["campaign"] == "eq.interest_declined"
    assert not resend.called


@respx.mock
async def test_email_dedup_expired_window_sends():
    _, _, resend = _mock_email_env(live="true", last_sent_hours=3)
    assert await te.send_transactional_email(U, "welcome_free", dedup_window_hours=2) is True
    assert resend.called


@respx.mock
async def test_email_dedup_unscoped_uses_null_related():
    dedup, _, _ = _mock_email_env(live="true")
    await te.send_transactional_email(U, "welcome_free", dedup_window_hours=1)
    assert dedup.calls.last.request.url.params["related_user_id"] == "is.null"


@respx.mock
async def test_email_opt_out_skips_email():
    _, audit, resend = _mock_email_env(
        profile={"email": "jane@example.com", "notify_email_enabled": False, "notify_push_enabled": False}
    )
    assert await te.send_transactional_email(U, "payment_successful") is False
    assert not resend.called and not audit.called


@respx.mock
async def test_email_provider_error_recorded_as_failed():
    _, audit, resend = _mock_email_env()
    resend.mock(return_value=httpx.Response(422, json={"message": "bad"}))
    assert await te.send_transactional_email(U, "welcome_free") is False
    assert json.loads(audit.calls.last.request.content)["status"] == "failed"


@respx.mock
async def test_email_provider_timeout_never_raises():
    _, audit, resend = _mock_email_env()
    resend.mock(side_effect=httpx.ReadTimeout("slow"))
    assert await te.send_transactional_email(U, "welcome_free") is False
    assert json.loads(audit.calls.last.request.content)["status"] == "failed"


@respx.mock
async def test_email_no_profile_skips():
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[]))
    resend = respx.post(RESEND)
    assert await te.send_transactional_email(U, "welcome_free") is False
    assert not resend.called


@respx.mock
async def test_email_companion_push_fires_with_generic_copy(monkeypatch):
    _mock_email_env(template=False)  # missing template must not block push
    pushes = []

    async def fake_push(**kw):
        pushes.append(kw)
        return True

    monkeypatch.setattr(push, "send_push_notification", fake_push)
    await te.send_transactional_email(
        U, "partner_proposal_received", extra_vars={"other_name": "Bo", "other_user_id": O}, related_user_id=O
    )
    assert len(pushes) == 1
    p = pushes[0]
    assert "Bo" not in p["title"] + p["body"]
    assert p["url"] == f"/#/chat/{O}" and p["related_user_id"] == O


def test_registry_push_copy_is_generic_for_relationship_events():
    """Handoff §14: generic lock-screen previews; never name the other member
    or disclose why (e.g. 'partnered elsewhere')."""
    relationship = [
        "interest_received", "interest_accepted", "interest_declined", "connection_lost",
        "partner_proposal_received", "partner_proposal_accepted", "partner_proposal_declined", "relationship_ended",
    ]
    for slug in relationship:
        cfg = te.NOTIFICATION_REGISTRY[slug]
        text = cfg["title"] + " " + cfg["body"]
        assert "{{other_name}}" not in text, slug
        assert "partnered" not in text.lower(), slug
        assert "ended your" not in text.lower(), slug


# --------------------------------------------------------------------------
# send_push_notification (OneSignal)
# --------------------------------------------------------------------------

@pytest.fixture
def onesignal_on(monkeypatch):
    monkeypatch.setattr(push, "ONESIGNAL_APP_ID", "app-123")
    monkeypatch.setattr(push, "ONESIGNAL_REST_API_KEY", "os-key")


async def test_push_without_env_is_noop():
    with respx.mock:  # any network call would raise (unmatched)
        assert await push.send_push_notification(U, "t", "b", campaign="x") is False


@respx.mock
async def test_push_payload_alias_deeplink_and_audit(onesignal_on):
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows()))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{"notify_push_enabled": True}]))
    os_route = respx.post(ONESIGNAL).mock(return_value=httpx.Response(200, json={"id": "notif-1"}))
    audit = respx.post(f"{REST}/sent_push").mock(return_value=httpx.Response(201))
    ok = await push.send_push_notification(
        U, "New message", "Open CheriPic", campaign="red_room_message_unread", url="/#/chat/abc", related_user_id=O
    )
    assert ok is True
    payload = json.loads(os_route.calls.last.request.content)
    assert payload["include_aliases"] == {"external_id": [U]}
    assert payload["target_channel"] == "push"
    assert payload["app_id"] == "app-123"
    assert payload["web_url"] == "https://cheripic.com/#/chat/abc"
    assert payload["data"] == {"campaign": "red_room_message_unread", "url": "https://cheripic.com/#/chat/abc"}
    assert os_route.calls.last.request.headers["authorization"] == "Key os-key"
    row = json.loads(audit.calls.last.request.content)
    assert row["status"] == "sent" and row["provider_id"] == "notif-1" and row["related_user_id"] == O


@respx.mock
async def test_push_opt_out_skips(onesignal_on):
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows()))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{"notify_push_enabled": False}]))
    os_route = respx.post(ONESIGNAL)
    assert await push.send_push_notification(U, "t", "b", campaign="x") is False
    assert not os_route.called


@respx.mock
async def test_push_dedup_when_live(onesignal_on):
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows(communication_live="true")))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{}]))
    respx.get(f"{REST}/sent_push").mock(return_value=httpx.Response(200, json=[{"sent_at": _iso(0.1)}]))
    os_route = respx.post(ONESIGNAL)
    assert await push.send_push_notification(U, "t", "b", campaign="x", dedup_window_hours=1, related_user_id=O) is False
    assert not os_route.called


@respx.mock
async def test_push_200_with_errors_is_failure(onesignal_on):
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows()))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{}]))
    respx.post(ONESIGNAL).mock(return_value=httpx.Response(200, json={"id": "", "errors": ["All included players are not subscribed"]}))
    audit = respx.post(f"{REST}/sent_push").mock(return_value=httpx.Response(201))
    assert await push.send_push_notification(U, "t", "b", campaign="x") is False
    assert json.loads(audit.calls.last.request.content)["status"] == "failed"


@respx.mock
async def test_push_network_error_never_raises(onesignal_on):
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows()))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[{}]))
    respx.post(ONESIGNAL).mock(side_effect=httpx.ConnectError("down"))
    audit = respx.post(f"{REST}/sent_push").mock(return_value=httpx.Response(201))
    assert await push.send_push_notification(U, "t", "b", campaign="x") is False
    assert json.loads(audit.calls.last.request.content)["status"] == "failed"


# --------------------------------------------------------------------------
# Auth "Send Email Hook" (OTP) — signature, replay window, rendering
# --------------------------------------------------------------------------

SECRET_RAW = b"supersecretkeybytes-1234567890"
SECRET = "v1,whsec_" + base64.b64encode(SECRET_RAW).decode()


def _sign(body: bytes, ts: int, hid="msg_1"):
    mac = base64.b64encode(hmac.new(SECRET_RAW, f"{hid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": hid, "webhook-timestamp": str(ts), "webhook-signature": f"v1,{mac}"}


def test_hook_signature_valid():
    body = b'{"a":1}'
    auth_email_hook.verify_signature(_sign(body, int(time.time())), body, SECRET)


@pytest.mark.parametrize("mutate", ["body", "old", "missing"])
def test_hook_signature_rejections(mutate):
    body = b'{"a":1}'
    ts = int(time.time()) - (600 if mutate == "old" else 0)
    headers = _sign(body, ts)
    if mutate == "missing":
        headers.pop("webhook-signature")
    with pytest.raises(auth_email_hook.HookVerificationError):
        auth_email_hook.verify_signature(headers, b'{"a":2}' if mutate == "body" else body, SECRET)


@respx.mock
async def test_hook_renders_otp_and_confirmation_link_on_supabase_host():
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows()))
    respx.get(f"{REST}/email_templates").mock(
        return_value=httpx.Response(200, json=[{"subject": "Code {{otp_code}}", "html_body": "{{confirmation_url}}", "text_body": ""}])
    )
    resend = respx.post(RESEND).mock(return_value=httpx.Response(200, json={"id": "re_9"}))
    out = await auth_email_hook.handle_send_email(
        {"user": {"email": "jane@example.com"},
         "email_data": {"token": "123456", "token_hash": "th", "email_action_type": "signup",
                        "redirect_to": "cheripic://auth?x=1&y=2"}}
    )
    assert out["ok"] is True and out["slug"] == "auth_otp"
    sent = json.loads(resend.calls.last.request.content)
    assert sent["subject"] == "Code 123456"
    link = sent["html"]
    assert link.startswith(f"{te.SUPABASE_URL}/auth/v1/verify?")
    assert "redirect_to=cheripic%3A%2F%2Fauth%3Fx%3D1%26y%3D2" in link


@respx.mock
async def test_hook_delivery_failure_raises():
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows()))
    respx.get(f"{REST}/email_templates").mock(
        return_value=httpx.Response(200, json=[{"subject": "s", "html_body": "h", "text_body": ""}])
    )
    respx.post(RESEND).mock(return_value=httpx.Response(500))
    with pytest.raises(RuntimeError):
        await auth_email_hook.handle_send_email({"user": {"email": "a@b.c"}, "email_data": {"token": "1"}})


def test_mask_email():
    assert cron.mask_email("jane.doe@example.com") == "j***@example.com"
    assert cron.mask_email(None) == "***"


# --------------------------------------------------------------------------
# Daily nudge cron
# --------------------------------------------------------------------------

@respx.mock
async def test_cron_respects_opt_out_and_skips_empty_and_audits_one_row():
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows()))
    respx.post(f"{REST}/rpc/list_users_needing_match_nudge").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"user_id": "u1", "unacted_matches": 2, "pending_incoming": 0, "candidates_waiting": 2},
                {"user_id": "u2", "unacted_matches": 3, "pending_incoming": 0, "candidates_waiting": 3},  # opted out
                {"user_id": "u3", "unacted_matches": 0, "pending_incoming": 4, "candidates_waiting": 0},  # nothing unread
            ],
        )
    )
    respx.get(f"{REST}/sent_emails").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REST}/user_profiles").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"user_id": "u1", "email": "u1@example.com", "nick_name": "One", "notify_email_enabled": True},
                {"user_id": "u2", "email": "u2@example.com", "nick_name": "Two", "notify_email_enabled": False},
                {"user_id": "u3", "email": "u3@example.com", "nick_name": "Three", "notify_email_enabled": True},
            ],
        )
    )
    respx.get(f"{REST}/email_templates").mock(return_value=httpx.Response(200, json=[]))  # fallback template
    resend = respx.post(RESEND).mock(return_value=httpx.Response(200, json={"id": "re_c"}))
    audit = respx.post(f"{REST}/sent_emails").mock(return_value=httpx.Response(201))

    result = await cron.run_once()

    assert result == {"sent": 1, "failed": 0, "skipped": 0, "eligible": 1}
    assert [json.loads(c.request.content)["to"] for c in resend.calls] == [["u1@example.com"]]
    assert audit.call_count == 1
    row = json.loads(audit.calls.last.request.content)
    assert row["notification_id"] is None and row["campaign"] == "daily_match_nudge" and row["user_id"] == "u1"


@respx.mock
async def test_cron_dedups_users_emailed_within_window():
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows()))
    respx.post(f"{REST}/rpc/list_users_needing_match_nudge").mock(
        return_value=httpx.Response(200, json=[{"user_id": "u1", "unacted_matches": 1}])
    )
    s = respx.get(f"{REST}/sent_emails").mock(return_value=httpx.Response(200, json=[{"user_id": "u1"}]))
    resend = respx.post(RESEND)
    assert (await cron.run_once())["eligible"] == 0
    assert s.calls.last.request.url.params["campaign"] == "eq.daily_match_nudge"
    assert not resend.called


@respx.mock
async def test_cron_aborts_without_resend_key():
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows(resend_api_key="")))
    rpc = respx.post(f"{REST}/rpc/list_users_needing_match_nudge")
    os.environ.pop("RESEND_API_KEY", None)
    assert (await cron.run_once())["sent"] == 0
    assert not rpc.called


@respx.mock
async def test_cron_rpc_failure_is_logged_not_raised():
    respx.get(f"{REST}/app_config").mock(return_value=httpx.Response(200, json=_config_rows()))
    respx.post(f"{REST}/rpc/list_users_needing_match_nudge").mock(return_value=httpx.Response(500, text="x"))
    assert (await cron.run_once())["eligible"] == 0
