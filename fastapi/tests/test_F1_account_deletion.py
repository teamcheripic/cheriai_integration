"""
F1 · H13.1 / H13.9e deletion record + account_deleted confirmation.
Supabase/Resend respx-mocked, Stripe faked; no network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import hashlib
import json
import logging
import sys
import types

import httpx
import pytest
import respx

import cron_daily_email as cde
import member_actions as ma
import transactional_email as te
from member_actions import MemberActionError

REST = ma._REST
BASE = ma.SUPABASE_URL
A = "aaaaaaaa-0000-0000-0000-00000000000a"
HASH_A = hashlib.sha256(A.encode()).hexdigest()


@pytest.fixture
def billing_ok(monkeypatch):
    cancelled = []
    fake_stripe = types.SimpleNamespace(Subscription=types.SimpleNamespace(cancel=cancelled.append))
    fake_billing = types.ModuleType("billing")
    fake_billing._get_stripe = lambda: fake_stripe
    monkeypatch.setitem(sys.modules, "billing", fake_billing)
    return cancelled


@pytest.fixture
def email_cfg(monkeypatch):
    async def cfg(*a, **k):
        return "x"

    async def key():
        return "re_test"

    async def tpl(slug):
        assert slug == "account_deleted"
        return {"subject": "Your CheriPic account has been deleted", "html_body": "<p>Hi {{name}}</p>",
                "text_body": "Hi {{name}}"}

    monkeypatch.setattr(cde, "_cfg_resend_api_key", key)
    monkeypatch.setattr(cde, "_cfg_from_address", cfg)
    for name in ("_cfg_app_base_url", "_cfg_logo_url", "_cfg_support_email", "_cfg_unsubscribe_url"):
        monkeypatch.setattr(te, name, cfg)
    monkeypatch.setattr(te, "_load_template", tpl)


def _mock_world(order, *, profile=True, sub_rows=(), record_status=201):
    def rec(label, resp):
        def _se(request):
            order.append(label(request) if callable(label) else label)
            return resp
        return _se

    respx.get(f"{REST}/user_memberships").mock(
        side_effect=rec("memberships_lookup", httpx.Response(200, json=list(sub_rows)))
    )
    respx.get(f"{REST}/user_profiles").mock(side_effect=rec(
        "profile_lookup",
        httpx.Response(200, json=[{"email": "m@example.test", "nick_name": "Mia"}] if profile else []),
    ))
    respx.get(f"{BASE}/auth/v1/admin/users/{A}").mock(
        side_effect=rec("auth_user_lookup", httpx.Response(200, json={"id": A, "email": "auth@example.test"}))
    )
    respx.get(f"{REST}/matches").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{REST}/cheri_ai_assessment_sessions").mock(return_value=httpx.Response(200, json=[]))
    respx.delete(url__startswith=f"{REST}/").mock(
        side_effect=rec(lambda r: "del:" + r.url.path.rsplit("/", 1)[-1], httpx.Response(204))
    )
    respx.post(f"{BASE}/storage/v1/object/list/avatars").mock(return_value=httpx.Response(200, json=[]))
    # Private buckets from migration 048 don't exist yet in this scenario.
    for _b in ("media", "kyc"):
        respx.post(f"{BASE}/storage/v1/object/list/{_b}").mock(
            return_value=httpx.Response(400, json={"error": "Bucket not found", "message": "Bucket not found"})
        )
    resend = respx.post(cde.RESEND_ENDPOINT).mock(side_effect=rec("confirmation_email", httpx.Response(200, json={"id": "re"})))
    record = respx.post(f"{REST}/account_deletions").mock(
        side_effect=rec("deletion_record", httpx.Response(record_status, json={"code": "42P01"}))
    )
    auth = respx.delete(f"{BASE}/auth/v1/admin/users/{A}").mock(side_effect=rec("auth_delete", httpx.Response(200, json={})))
    return resend, record, auth


@respx.mock
async def test_confirmation_sent_before_login_removed_and_record_written(billing_ok, email_cfg):
    order: list[str] = []
    resend, record, auth = _mock_world(order, sub_rows=[{"stripe_subscription_id": "sub_1"}])
    out = await ma.delete_account(A)
    assert out["ok"] is True
    assert order.index("profile_lookup") < order.index("del:user_profiles")  # address captured first
    assert order.index("confirmation_email") < order.index("auth_delete")
    assert order[-1] == "deletion_record"
    sent = json.loads(resend.calls.last.request.content)
    assert sent["to"] == ["m@example.test"] and "headers" not in sent
    row = json.loads(record.calls.last.request.content)
    assert row["user_id_hash"] == HASH_A and A not in json.dumps(row)  # hash only, never the id
    assert row["status"] == "completed" and row["completed_at"] and row["requested_at"]
    assert row["stripe_cancelled"] is True and row["confirmation_sent"] is True and row["error"] is None
    assert row["retained_tables"] == ["user_reports", "user_account_status", "stripe_events", "sent_emails", "sent_push"]


@respx.mock
async def test_failure_is_recorded_and_still_raises(billing_ok, email_cfg):
    order: list[str] = []
    _, record, auth = _mock_world(order)
    respx.post(f"{BASE}/storage/v1/object/list/avatars").mock(return_value=httpx.Response(500))
    with pytest.raises(MemberActionError) as e:
        await ma.delete_account(A)
    assert str(e.value) == "storage_list_failed"
    assert not auth.called and "confirmation_email" not in order  # no "deleted" mail on failure
    row = json.loads(record.calls.last.request.content)
    assert row["status"] == "failed" and row["error"] == "storage_list_failed" and "completed_at" not in row


@respx.mock
async def test_missing_table_falls_back_to_structured_log(billing_ok, email_cfg, caplog):
    order: list[str] = []
    _mock_world(order, record_status=404)
    caplog.set_level(logging.INFO, logger="cheripic.member_actions")
    assert (await ma.delete_account(A))["ok"] is True
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[account-deletion-record]")]
    assert lines, "structured fallback line expected"
    data = json.loads(lines[-1].split(" ", 1)[1])
    assert data["user_id_hash"] == HASH_A and data["status"] == "completed"
    assert A not in lines[-1]


@respx.mock
async def test_missing_template_is_soft(billing_ok, email_cfg, monkeypatch):
    async def no_tpl(slug):
        return None

    monkeypatch.setattr(te, "_load_template", no_tpl)
    order: list[str] = []
    resend, record, auth = _mock_world(order)
    assert (await ma.delete_account(A))["ok"] is True
    assert auth.called and not resend.called
    assert json.loads(record.calls.last.request.content)["confirmation_sent"] is False


@respx.mock
async def test_retry_after_profile_gone_uses_auth_email(billing_ok, email_cfg):
    order: list[str] = []
    resend, _, _ = _mock_world(order, profile=False)
    assert (await ma.delete_account(A))["ok"] is True
    assert json.loads(resend.calls.last.request.content)["to"] == ["auth@example.test"]


@respx.mock
async def test_email_provider_failure_does_not_block_deletion(billing_ok, email_cfg):
    order: list[str] = []
    resend, record, auth = _mock_world(order)
    resend.mock(side_effect=httpx.ConnectTimeout("resend down"))
    assert (await ma.delete_account(A))["ok"] is True
    assert auth.called
    assert json.loads(record.calls.last.request.content)["confirmation_sent"] is False


def test_migration_051_defines_private_table_and_type_value():
    sql = open(os.path.join(os.path.dirname(__file__), "..", "migrations",
                            "051_account_deletions_and_notification_types.sql")).read()
    assert sql.startswith("-- NOT APPLIED — needs approval")
    for col in ("user_id_hash", "requested_at", "completed_at", "status", "retained_tables",
                "stripe_cancelled", "error"):
        assert col in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql and "REVOKE ALL ON public.account_deletions FROM anon, authenticated" in sql
    assert "CREATE POLICY" not in sql  # no client access
    assert "'report_submitted'" in sql
