"""
Audit E — Stripe webhook processing (signature, idempotency, retry
safety, membership emails) and the accept-official sweep guard.
Stripe and Supabase are mocked; no network.
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

import billing
import partnership
from face_verification import is_own_storage_image_url

REST = f"{billing.supabase._base}"
U = "uuuuuuuu-0000-0000-0000-00000000000u"


class _FakeSigError(Exception):
    pass


class _FakeStripe:
    """Minimal stand-in for the stripe module used by billing.handle_event."""

    class error:
        SignatureVerificationError = _FakeSigError

    def __init__(self, event):
        self._event = event
        outer = self

        class Webhook:
            @staticmethod
            def construct_event(payload, sig_header, secret):
                if sig_header != "good":
                    raise _FakeSigError("bad signature")
                return outer._event

        self.Webhook = Webhook


@pytest.fixture
def fake_stripe(monkeypatch):
    def install(event):
        fs = _FakeStripe(event)
        monkeypatch.setattr(billing, "_get_stripe", lambda: fs)
        monkeypatch.setattr(billing, "get_webhook_secret", lambda: "whsec_test")

        async def no_refresh():
            return None

        monkeypatch.setattr(billing, "refresh_price_to_tier_from_db", no_refresh)
        return fs

    return install


@pytest.fixture
def emails(monkeypatch):
    import transactional_email

    calls = []

    async def fake(**kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(transactional_email, "send_transactional_email", fake)
    return calls


SUB_EVENT = {
    "id": "evt_1",
    "type": "customer.subscription.updated",
    "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"cheripic_user_id": U},
        "items": {"data": [{"price": {"id": "price_premium"}}]},
        "current_period_start": 1760000000, "current_period_end": 1762600000, "cancel_at_period_end": False,
    }},
}


async def test_webhook_bad_signature_raises_typed_error(fake_stripe):
    fake_stripe(SUB_EVENT)
    with pytest.raises(billing.WebhookSignatureError):
        await billing.handle_event(b"{}", "bad")


@respx.mock
async def test_webhook_duplicate_event_is_skipped(fake_stripe):
    fake_stripe(SUB_EVENT)
    respx.post(f"{REST}/stripe_events").mock(return_value=httpx.Response(409, json={"code": "23505"}))
    upd = respx.patch(f"{REST}/user_memberships")
    out = await billing.handle_event(b"{}", "good")
    assert out["status"] == "duplicate"
    assert not upd.called


@respx.mock
async def test_webhook_renewal_same_tier_sends_no_welcome(fake_stripe, emails, monkeypatch):
    """Regression: _get_current_tier used select(params=…) → TypeError →
    always 'basic' → a welcome email on every monthly renewal."""
    fake_stripe(SUB_EVENT)
    monkeypatch.setitem(billing.TIER_FROM_PRICE_CACHE, "price_premium", "premium")
    respx.post(f"{REST}/stripe_events").mock(return_value=httpx.Response(201, json=[]))
    tier_q = respx.get(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[{"tier": "premium"}]))
    respx.patch(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{REST}/user_memberships").mock(return_value=httpx.Response(201, json=[]))
    processed = respx.patch(f"{REST}/stripe_events").mock(return_value=httpx.Response(200, json=[]))
    out = await billing.handle_event(b"{}", "good")
    assert out["handled"] is True
    assert tier_q.calls[0].request.url.params["is_active"] == "eq.True"
    assert emails == []
    assert processed.called


@respx.mock
async def test_webhook_first_subscription_sends_welcome_once(fake_stripe, emails, monkeypatch):
    fake_stripe(SUB_EVENT)
    monkeypatch.setitem(billing.TIER_FROM_PRICE_CACHE, "price_premium", "premium")
    respx.post(f"{REST}/stripe_events").mock(return_value=httpx.Response(201, json=[]))
    respx.get(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))  # basic
    respx.patch(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    ins = respx.post(f"{REST}/user_memberships").mock(return_value=httpx.Response(201, json=[]))
    respx.patch(f"{REST}/stripe_events").mock(return_value=httpx.Response(200, json=[]))
    await billing.handle_event(b"{}", "good")
    assert [e["template_slug"] for e in emails] == ["welcome_premium"]
    row = json.loads(ins.calls.last.request.content)
    assert row["tier"] == "premium" and row["is_active"] is True and row["stripe_subscription_id"] == "sub_1"


@respx.mock
async def test_webhook_plan_change_sends_upgraded(fake_stripe, emails, monkeypatch):
    fake_stripe(SUB_EVENT)
    monkeypatch.setitem(billing.TIER_FROM_PRICE_CACHE, "price_premium", "premium")
    respx.post(f"{REST}/stripe_events").mock(return_value=httpx.Response(201, json=[]))
    respx.get(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[{"tier": "premium-lite"}]))
    respx.patch(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{REST}/user_memberships").mock(return_value=httpx.Response(201, json=[]))
    respx.patch(f"{REST}/stripe_events").mock(return_value=httpx.Response(200, json=[]))
    await billing.handle_event(b"{}", "good")
    assert [e["template_slug"] for e in emails] == ["membership_upgraded"]


@respx.mock
async def test_webhook_processing_failure_releases_claim_for_retry(fake_stripe, emails, monkeypatch):
    fake_stripe(SUB_EVENT)
    monkeypatch.setitem(billing.TIER_FROM_PRICE_CACHE, "price_premium", "premium")
    respx.post(f"{REST}/stripe_events").mock(return_value=httpx.Response(201, json=[]))
    respx.get(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    respx.patch(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{REST}/user_memberships").mock(return_value=httpx.Response(503, text="db unavailable"))
    release = respx.delete(f"{REST}/stripe_events").mock(return_value=httpx.Response(204))
    processed = respx.patch(f"{REST}/stripe_events")
    with pytest.raises(RuntimeError):
        await billing.handle_event(b"{}", "good")
    assert release.calls.last.request.url.params["event_id"] == "eq.evt_1"
    assert not processed.called  # not marked processed → Stripe retry reprocesses


@respx.mock
async def test_invoice_payment_failed_emails_and_records_status(fake_stripe, emails):
    fake_stripe({"id": "evt_2", "type": "invoice.payment_failed",
                 "data": {"object": {"id": "in_1", "subscription": "sub_1", "amount_due": 5900, "currency": "usd"}}})
    respx.post(f"{REST}/stripe_events").mock(return_value=httpx.Response(201, json=[]))
    lookup = respx.get(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[{"user_id": U}]))
    upd = respx.patch(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    respx.patch(f"{REST}/stripe_events").mock(return_value=httpx.Response(200, json=[]))
    await billing.handle_event(b"{}", "good")
    assert lookup.calls.last.request.url.params["stripe_subscription_id"] == "eq.sub_1"
    assert emails and emails[0]["template_slug"] == "payment_failed" and emails[0]["extra_vars"]["amount"] == "59.00"
    assert json.loads(upd.calls.last.request.content)["last_invoice_status"] == "failed"


@respx.mock
async def test_subscription_deleted_drops_to_basic_and_emails(fake_stripe, emails):
    ev = {"id": "evt_3", "type": "customer.subscription.deleted",
          "data": {"object": {"id": "sub_1", "customer": "cus_1", "metadata": {"cheripic_user_id": U}}}}
    fake_stripe(ev)
    respx.post(f"{REST}/stripe_events").mock(return_value=httpx.Response(201, json=[]))
    respx.get(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[{"tier": "premium"}]))
    respx.patch(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    ins = respx.post(f"{REST}/user_memberships").mock(return_value=httpx.Response(201, json=[]))
    respx.patch(f"{REST}/stripe_events").mock(return_value=httpx.Response(200, json=[]))
    await billing.handle_event(b"{}", "good")
    assert json.loads(ins.calls.last.request.content)["tier"] == "basic"
    # X-EMAIL.6: the subscription has ENDED → premium_ended (membership_cancelled
    # now fires when cancel_at_period_end is scheduled; see test_F1_billing).
    assert [e["template_slug"] for e in emails] == ["premium_ended"]


# --------------------------------------------------------------------------
# Accept official — finalize_partnership guard
# --------------------------------------------------------------------------

PA, PB = "pa000000-0000-0000-0000-000000000000", "pb000000-0000-0000-0000-000000000000"
PG = partnership._PG_BASE


@respx.mock
async def test_finalize_refuses_when_partner_official_elsewhere():
    def matches(request):
        q = request.url.params
        if q.get("id") == "eq.m1":
            return httpx.Response(200, json=[{"id": "m1", "user_a_id": PA, "user_b_id": PB, "is_active": True,
                                              "partner_accepted_by_a": True, "partner_accepted_by_b": True,
                                              "partnered_at": None}])
        if q.get("partnered_at") == "not.is.null":
            return httpx.Response(200, json=[{"id": "m-other"}])
        return httpx.Response(200, json=[])

    respx.get(f"{PG}/matches").mock(side_effect=matches)
    stamp = respx.patch(f"{PG}/matches")
    with pytest.raises(partnership.PartnershipError) as e:
        await partnership.finalize_partnership("m1", PA)
    assert str(e.value) == "already_official_elsewhere"
    assert not stamp.called  # nothing committed


@pytest.mark.parametrize(
    "row,code",
    [
        ({"user_a_id": "x", "user_b_id": "y", "is_active": True, "partner_accepted_by_a": True, "partner_accepted_by_b": True}, "not_a_participant"),
        ({"user_a_id": PA, "user_b_id": PB, "is_active": False, "partner_accepted_by_a": True, "partner_accepted_by_b": True}, "match_inactive"),
        ({"user_a_id": PA, "user_b_id": PB, "is_active": True, "partner_accepted_by_a": True, "partner_accepted_by_b": False}, "both_must_accept_first"),
    ],
)
@respx.mock
async def test_finalize_preconditions(row, code):
    respx.get(f"{PG}/matches").mock(return_value=httpx.Response(200, json=[{"id": "m1", "partnered_at": None, **row}]))
    stamp = respx.patch(f"{PG}/matches")
    with pytest.raises(partnership.PartnershipError) as e:
        await partnership.finalize_partnership("m1", PA)
    assert str(e.value) == code
    assert not stamp.called


# --------------------------------------------------------------------------
# KYC URL allowlist unit checks
# --------------------------------------------------------------------------

def test_is_own_storage_image_url():
    base = os.environ["SUPABASE_URL"].rstrip("/")
    assert is_own_storage_image_url(f"{base}/storage/v1/object/public/avatars/{U}/kyc-face-1.jpg", U)
    assert is_own_storage_image_url(f"{base}/storage/v1/object/sign/kyc/{U}/id.jpg?token=x", U)
    assert not is_own_storage_image_url(f"{base}/storage/v1/object/public/avatars/other/kyc.jpg", U)
    assert not is_own_storage_image_url(f"{base}/storage/v1/object/public/avatars/{U}/%2e%2e/x/kyc.jpg", U)
    assert not is_own_storage_image_url(f"{base}/rest/v1/user_profiles", U)
    assert not is_own_storage_image_url("http://127.0.0.1:8000/x.jpg", U)
    assert not is_own_storage_image_url(f"{base.replace('https://', 'https://user:pw@')}/storage/v1/object/public/avatars/{U}/a.jpg", U)
    assert not is_own_storage_image_url("", U)


@respx.mock
async def test_finalize_stamp_is_conditional_and_reports_state_change():
    """Withdraw/end racing the final accept: zero rows stamped → 'state_changed',
    nothing else committed (no celebration notices, no sweep)."""
    reads = {"n": 0}

    def matches(request):
        q = request.url.params
        if q.get("partnered_at") == "not.is.null":
            return httpx.Response(200, json=[])  # not official elsewhere
        reads["n"] += 1
        if reads["n"] == 1:
            return httpx.Response(200, json=[{"id": "m1", "user_a_id": PA, "user_b_id": PB, "is_active": True,
                                              "partner_accepted_by_a": True, "partner_accepted_by_b": True,
                                              "partnered_at": None}])
        # authoritative re-read: the other member withdrew meanwhile
        return httpx.Response(200, json=[{"is_active": True, "partnered_at": None,
                                          "partner_accepted_by_a": True, "partner_accepted_by_b": False}])

    respx.get(f"{PG}/matches").mock(side_effect=matches)
    stamp = respx.patch(f"{PG}/matches").mock(return_value=httpx.Response(200, json=[]))
    notif = respx.post(f"{PG}/notifications")
    with pytest.raises(partnership.PartnershipError) as e:
        await partnership.finalize_partnership("m1", PA)
    assert str(e.value) == "state_changed"
    q = stamp.calls.last.request.url.params
    assert q["is_active"] == "eq.true" and q["partnered_at"] == "is.null"
    assert q["partner_accepted_by_a"] == "eq.true" and q["partner_accepted_by_b"] == "eq.true"
    assert stamp.call_count == 1 and not notif.called
