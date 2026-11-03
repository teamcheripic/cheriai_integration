"""
G · expiry-aware tiers, refund/dispute webhooks, reconcile default, account
deletion additions (legal hold, evidence preservation, OneSignal, Stripe
customer), partnership evidence preservation and the KYC retention job.
Supabase/OneSignal respx-mocked, Stripe faked. No network.
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
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import billing
import compliance
import kyc_retention
import member_actions as ma
from member_actions import MemberActionError

U = "uuuuuuuu-0000-0000-0000-00000000000u"
REST = ma._REST
BASE = ma.SUPABASE_URL


async def _a(v):
    return v


def _iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# =============================================================================
# Expiry-aware tier reads
# =============================================================================

def test_membership_expired_grace():
    assert billing.MEMBERSHIP_GRACE_DAYS == 3
    assert billing.membership_expired({"current_period_end": _iso(-4)}) is True
    assert billing.membership_expired({"current_period_end": _iso(-2)}) is False
    assert billing.membership_expired({"current_period_end": None}) is False
    assert billing.membership_expired({}) is False


@pytest.mark.parametrize("end,tier", [(_iso(-5), "basic"), (_iso(-1), "premium-lite"), (None, "premium-lite")])
async def test_get_membership_tier_is_expiry_aware(monkeypatch, end, tier):
    async def select(table, **k):
        assert "current_period_end" in k["columns"]
        return [{"tier": "premium-lite", "current_period_end": end}]

    monkeypatch.setattr(billing.supabase, "select", select)
    assert await billing.get_membership_tier(U) == tier


async def test_current_membership_snapshot_is_expiry_aware(monkeypatch):
    async def select(table, **k):
        return [{"tier": "premium", "stripe_subscription_id": "sub_1", "current_period_end": _iso(-10)}]

    monkeypatch.setattr(billing.supabase, "select", select)
    assert (await billing._get_current_membership(U))["tier"] == "basic"


def test_reconcile_on_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_STRIPE_RECONCILE", raising=False)
    assert billing.reconcile_enabled() is True
    for off in ("false", "0", "no", "off"):
        monkeypatch.setenv("ENABLE_STRIPE_RECONCILE", off)
        assert billing.reconcile_enabled() is False


# =============================================================================
# charge.refunded / charge.dispute.created
# =============================================================================

@pytest.fixture
def revoke_env(monkeypatch):
    st = {"upserts": [], "cancelled": [], "charges": {}}
    fake = types.SimpleNamespace(
        Subscription=types.SimpleNamespace(cancel=lambda sid: st["cancelled"].append(sid)),
        Charge=types.SimpleNamespace(retrieve=lambda cid: st["charges"][cid]),
    )
    monkeypatch.setattr(billing, "_get_stripe", lambda: fake)

    async def select(table, **k):
        assert table == "user_memberships" and k["eq"]["stripe_customer_id"] == "cus_1"
        return [{"user_id": U, "tier": "premium-lite", "stripe_subscription_id": "sub_1", "stripe_customer_id": "cus_1"}]

    async def upsert(**kw):
        st["upserts"].append(kw)

    monkeypatch.setattr(billing.supabase, "select", select)
    monkeypatch.setattr(billing, "_upsert_active_membership", upsert)
    return st


async def test_full_refund_revokes_paid_access(revoke_env):
    ok = await billing._handle_charge_refunded({"id": "ch_1", "customer": "cus_1", "amount": 5900,
                                                "amount_refunded": 5900, "refunded": True})
    assert ok is True
    assert revoke_env["cancelled"] == ["sub_1"]
    up = revoke_env["upserts"][0]
    assert up["user_id"] == U and up["tier"] == "basic" and up["last_invoice_status"] == "refunded"


async def test_partial_refund_keeps_access(revoke_env):
    await billing._handle_charge_refunded({"id": "ch_1", "customer": "cus_1", "amount": 5900,
                                           "amount_refunded": 1000, "refunded": False})
    assert revoke_env["upserts"] == [] and revoke_env["cancelled"] == []


async def test_dispute_revokes_and_flags(revoke_env, caplog):
    revoke_env["charges"]["ch_9"] = {"id": "ch_9", "customer": "cus_1", "metadata": {}}
    ok = await billing._handle_dispute_created({"id": "dp_1", "charge": "ch_9", "reason": "fraudulent"})
    assert ok is True
    assert revoke_env["upserts"][0]["last_invoice_status"] == "disputed"
    assert "ADMIN FLAG" in caplog.text and "dp_1" in caplog.text


async def test_handle_event_dispatches_refund_and_dispute(monkeypatch):
    seen = []
    for etype, obj in (("charge.refunded", {"id": "ch_1"}), ("charge.dispute.created", {"id": "dp_1"})):
        event = {"id": f"evt_{etype}", "type": etype, "created": 1, "data": {"object": obj}}
        fake = types.SimpleNamespace(
            error=types.SimpleNamespace(SignatureVerificationError=Exception),
            Webhook=types.SimpleNamespace(construct_event=lambda payload, sig_header, secret, e=event: e),
        )
        monkeypatch.setattr(billing, "_get_stripe", lambda f=fake: f)
        monkeypatch.setattr(billing, "get_webhook_secret", lambda: "whsec")
        monkeypatch.setattr(billing, "refresh_price_to_tier_from_db", lambda: _a(None))
        monkeypatch.setattr(billing, "_record_event_or_skip", lambda eid, et: _a(True))
        monkeypatch.setattr(billing.supabase, "update", lambda *a, **k: _a([]))
        monkeypatch.setattr(billing, "_handle_charge_refunded", lambda o: seen.append(("refund", o["id"])) or _a(True))
        monkeypatch.setattr(billing, "_handle_dispute_created", lambda o: seen.append(("dispute", o["id"])) or _a(True))
        out = await billing.handle_event(b"{}", "sig")
        assert out["handled"] is True
    assert seen == [("refund", "ch_1"), ("dispute", "dp_1")]


async def test_checkout_session_carries_agreement_metadata(monkeypatch):
    seen = {}
    fake = types.SimpleNamespace(checkout=types.SimpleNamespace(Session=types.SimpleNamespace(
        create=lambda **kw: seen.update(kw) or types.SimpleNamespace(url="https://checkout.stripe.com/x"))))
    monkeypatch.setattr(billing, "_get_stripe", lambda: fake)
    monkeypatch.setattr(billing, "_find_or_create_customer", lambda uid: _a("cus_1"))
    await billing.create_checkout_session(U, "price_1", "https://s", "https://c",
                                          extra_metadata={"terms_version": "2026-11-03"})
    assert seen["metadata"] == {"terms_version": "2026-11-03", "cheripic_user_id": U}
    assert seen["subscription_data"]["metadata"] == seen["metadata"]


# =============================================================================
# Account deletion additions
# =============================================================================

@pytest.fixture
def stripe_fake(monkeypatch):
    st = {"subs": [], "customers": []}
    fake_stripe = types.SimpleNamespace(
        Subscription=types.SimpleNamespace(cancel=lambda s: st["subs"].append(s)),
        Customer=types.SimpleNamespace(delete=lambda c: st["customers"].append(c)),
    )
    fake_billing = types.ModuleType("billing")
    fake_billing._get_stripe = lambda: fake_stripe
    monkeypatch.setitem(sys.modules, "billing", fake_billing)
    monkeypatch.setattr(ma, "_send_deletion_confirmation", lambda r: _a(False))
    return st


def _deletion_world(*, matches_a=(), messages=()):
    respx.get(f"{REST}/user_memberships").mock(return_value=httpx.Response(
        200, json=[{"stripe_subscription_id": "sub_1", "stripe_customer_id": "cus_1"}]))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/auth/v1/admin/users/{U}").mock(return_value=httpx.Response(200, json={}))

    def matches(request):
        if request.url.params.get("user_a_id"):
            return httpx.Response(200, json=[{"id": m} for m in matches_a])
        return httpx.Response(200, json=[])

    respx.get(f"{REST}/matches").mock(side_effect=matches)
    respx.get(f"{REST}/match_messages").mock(return_value=httpx.Response(200, json=list(messages)))
    respx.get(f"{REST}/cheri_ai_assessment_sessions").mock(return_value=httpx.Response(200, json=[]))
    deletes = respx.delete(url__startswith=f"{REST}/").mock(return_value=httpx.Response(204))
    for b in ("avatars", "media", "kyc"):
        respx.post(f"{BASE}/storage/v1/object/list/{b}").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{REST}/account_deletions").mock(return_value=httpx.Response(201))
    preserved = respx.post(f"{REST}/preserved_content").mock(return_value=httpx.Response(201))
    auth = respx.delete(f"{BASE}/auth/v1/admin/users/{U}").mock(return_value=httpx.Response(200, json={}))
    return deletes, preserved, auth


@respx.mock
async def test_legal_hold_blocks_deletion(stripe_fake, monkeypatch):
    monkeypatch.setattr(compliance, "has_legal_hold", lambda uid: _a(True))
    record = respx.post(f"{REST}/account_deletions").mock(return_value=httpx.Response(201))
    with pytest.raises(MemberActionError) as e:
        await ma.delete_account(U)
    assert str(e.value) == "legal_hold"
    assert stripe_fake["subs"] == []  # nothing touched, not even billing
    assert json.loads(record.calls.last.request.content)["error"] == "legal_hold"


def test_legal_hold_endpoint_is_409(monkeypatch):
    import main

    async def held(uid):
        raise MemberActionError("legal_hold")

    monkeypatch.setattr(main, "delete_account", held)
    main.app.dependency_overrides[main.get_current_user_id] = lambda: U
    try:
        r = TestClient(main.app).post("/account/delete")
    finally:
        main.app.dependency_overrides.clear()
    assert r.status_code == 409 and r.json()["detail"]["code"] == "legal_hold"
    assert "support" in r.json()["detail"]["message"].lower()


@respx.mock
async def test_deletion_preserves_messages_when_report_open_and_deletes_new_tables(stripe_fake, monkeypatch):
    monkeypatch.setattr(compliance, "has_open_report", lambda uid: _a(True))
    msgs = [{"id": "mm1", "match_id": "m1", "sender_id": U, "content": "evidence", "created_at": "t"}]
    deletes, preserved, auth = _deletion_world(matches_a=["m1"], messages=msgs)
    out = await ma.delete_account(U)
    assert out["ok"] is True and auth.called
    first = json.loads(preserved.calls[0].request.content)
    assert first[0]["source_table"] == "match_messages" and first[0]["payload"]["content"] == "evidence"
    deleted_tables = {c.request.url.path.rsplit("/", 1)[-1] for c in deletes.calls}
    assert {"member_consents", "dob_change_requests", "appeals"} <= deleted_tables
    assert not {"purchase_agreements", "user_reports", "moderation_actions", "preserved_content"} & deleted_tables
    # Stripe: subscription cancelled, then the customer deleted.
    assert stripe_fake["subs"] == ["sub_1"] and stripe_fake["customers"] == ["cus_1"]


@respx.mock
async def test_deletion_without_open_report_preserves_nothing(stripe_fake):
    deletes, preserved, auth = _deletion_world(matches_a=["m1"])
    await ma.delete_account(U)
    assert not preserved.called


@respx.mock
async def test_onesignal_user_deleted_when_configured(stripe_fake, monkeypatch):
    monkeypatch.setenv("ONESIGNAL_APP_ID", "app-1")
    monkeypatch.setenv("ONESIGNAL_REST_API_KEY", "os-key")
    os_route = respx.delete(f"https://api.onesignal.com/apps/app-1/users/by/external_id/{U}").mock(
        return_value=httpx.Response(200, json={}))
    _deletion_world()
    await ma.delete_account(U)
    assert os_route.called
    assert os_route.calls.last.request.headers["authorization"] == "Key os-key"


@respx.mock
async def test_onesignal_skipped_when_not_configured(stripe_fake, monkeypatch):
    monkeypatch.setenv("ONESIGNAL_APP_ID", "")
    os_route = respx.delete(url__startswith="https://api.onesignal.com/").mock(return_value=httpx.Response(200))
    _deletion_world()
    await ma.delete_account(U)
    assert not os_route.called


@respx.mock
async def test_stripe_customer_delete_failure_does_not_block(stripe_fake, monkeypatch):
    def boom(c):
        raise RuntimeError("stripe down")

    sys.modules["billing"]._get_stripe().Customer.delete = boom
    _, _, auth = _deletion_world()
    assert (await ma.delete_account(U))["ok"] is True and auth.called


# =============================================================================
# Partnership sweep evidence preservation
# =============================================================================

def test_partnership_side_match_messages_preserved_before_delete():
    # Structural check (the full finalize sweep needs ~20 mocked calls): the
    # preservation call sits immediately before the side-match message
    # delete, with both members of the side match as participants. The
    # helper's behaviour is covered in test_G_compliance_helpers.py.
    import partnership

    src = open(partnership.__file__).read()
    i = src.index("compliance.preserve_match_messages(")
    j = src.index('"match_messages",', i)
    assert 0 < j - i < 400
    assert "[sm_id], [uid, third_party]" in src[i:j]


# =============================================================================
# KYC retention
# =============================================================================

def test_kyc_object_for_only_accepts_own_kyc_files():
    base = os.environ["SUPABASE_URL"]
    assert kyc_retention.kyc_object_for(f"storage://kyc/{U}/id.jpg", U) == ("kyc", f"{U}/id.jpg")
    assert kyc_retention.kyc_object_for(f"{base}/storage/v1/object/public/avatars/{U}/kyc-face.jpg", U) == \
        ("avatars", f"{U}/kyc-face.jpg")
    assert kyc_retention.kyc_object_for(f"{base}/storage/v1/object/public/avatars/{U}/profile.jpg", U) is None
    assert kyc_retention.kyc_object_for("storage://kyc/someone-else/id.jpg", U) is None
    assert kyc_retention.kyc_object_for("https://evil.example/kyc-x.jpg", U) is None


def test_kyc_retention_on_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_KYC_RETENTION", raising=False)
    assert kyc_retention.retention_enabled() is True
    monkeypatch.setenv("ENABLE_KYC_RETENTION", "false")
    assert kyc_retention.retention_enabled() is False

    class Sched:
        jobs = []

        def add_job(self, fn, trigger, **kw):
            self.jobs.append(kw["id"])

    assert kyc_retention.schedule(Sched()) is False
    monkeypatch.delenv("ENABLE_KYC_RETENTION")
    s = Sched()
    assert kyc_retention.schedule(s) is True and s.jobs == ["kyc_retention"]


@respx.mock
async def test_kyc_retention_before_056_does_nothing(monkeypatch):
    monkeypatch.setattr(kyc_retention, "_retention_days", lambda: _a(90))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(
        400, json={"code": "42703", "message": "column user_profiles.kyc_reviewed_at does not exist"}))
    patch = respx.patch(f"{REST}/user_profiles").mock(return_value=httpx.Response(204))
    out = await kyc_retention.run_kyc_retention()
    assert out == {"candidates": 0, "purged": 0, "held": 0, "errors": 0} and not patch.called


@respx.mock
async def test_kyc_retention_purges_and_skips_legal_holds(monkeypatch):
    H = "hhhhhhhh-0000-0000-0000-00000000000h"
    monkeypatch.setattr(kyc_retention, "_retention_days", lambda: _a(30))
    monkeypatch.setattr(compliance, "has_legal_hold", lambda uid: _a(uid == H))
    base = os.environ["SUPABASE_URL"]
    cand = respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[
        {"user_id": U, "kyc_face_url": f"storage://kyc/{U}/face.jpg",
         "kyc_id_url": f"{base}/storage/v1/object/public/avatars/{U}/kyc-id.jpg"},
        {"user_id": H, "kyc_face_url": f"storage://kyc/{H}/face.jpg", "kyc_id_url": None},
    ]))
    respx.post(f"{BASE}/storage/v1/object/list/kyc").mock(return_value=httpx.Response(200, json=[{"name": "extra.jpg"}]))
    kyc_rm = respx.delete(f"{BASE}/storage/v1/object/kyc").mock(return_value=httpx.Response(200, json=[]))
    av_rm = respx.delete(f"{BASE}/storage/v1/object/avatars").mock(return_value=httpx.Response(200, json=[]))
    patch = respx.patch(f"{REST}/user_profiles").mock(return_value=httpx.Response(204))
    out = await kyc_retention.run_kyc_retention()
    assert out == {"candidates": 2, "purged": 1, "held": 1, "errors": 0}
    params = cand.calls.last.request.url.params
    assert params["verification_final_status"] == "in.(verified,rejected)" and params["kyc_reviewed_at"].startswith("lt.")
    assert sorted(json.loads(kyc_rm.calls.last.request.content)["prefixes"]) == [f"{U}/extra.jpg", f"{U}/face.jpg"]
    assert json.loads(av_rm.calls.last.request.content)["prefixes"] == [f"{U}/kyc-id.jpg"]
    assert patch.call_count == 1 and patch.calls.last.request.url.params["user_id"] == f"eq.{U}"
    assert json.loads(patch.calls.last.request.content) == {"kyc_face_url": None, "kyc_id_url": None}


@respx.mock
async def test_kyc_retention_keeps_urls_when_storage_delete_fails(monkeypatch):
    monkeypatch.setattr(kyc_retention, "_retention_days", lambda: _a(90))
    respx.get(f"{REST}/user_profiles").mock(return_value=httpx.Response(200, json=[
        {"user_id": U, "kyc_face_url": f"storage://kyc/{U}/face.jpg", "kyc_id_url": None}]))
    respx.post(f"{BASE}/storage/v1/object/list/kyc").mock(return_value=httpx.Response(200, json=[]))
    respx.delete(f"{BASE}/storage/v1/object/kyc").mock(return_value=httpx.Response(500))
    patch = respx.patch(f"{REST}/user_profiles").mock(return_value=httpx.Response(204))
    out = await kyc_retention.run_kyc_retention()
    assert out["errors"] == 1 and out["purged"] == 0 and not patch.called
