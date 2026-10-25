"""
F1 · Billing: X-EMAIL.6 premium_ended, H13.17 out-of-order events +
daily reconciliation. Stripe is a fake object; Supabase is respx-mocked.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import json
import types

import httpx
import pytest
import respx

import billing

REST = billing.supabase._base
U = "uuuuuuuu-0000-0000-0000-00000000000u"


class _FakeSigError(Exception):
    pass


def _install(monkeypatch, event, *, subs=None):
    """Fake stripe module: Webhook.construct_event → `event`;
    Subscription.retrieve(id) → subs[id] (or raises if it's an Exception)."""
    subs = subs or {}

    def retrieve(sid):
        v = subs[sid]
        if isinstance(v, Exception):
            raise v
        return v

    fake = types.SimpleNamespace(
        error=types.SimpleNamespace(SignatureVerificationError=_FakeSigError),
        Webhook=types.SimpleNamespace(construct_event=lambda payload, sig_header, secret: event),
        Subscription=types.SimpleNamespace(retrieve=retrieve),
    )
    monkeypatch.setattr(billing, "_get_stripe", lambda: fake)
    monkeypatch.setattr(billing, "get_webhook_secret", lambda: "whsec_test")

    async def no_refresh():
        return None

    monkeypatch.setattr(billing, "refresh_price_to_tier_from_db", no_refresh)
    monkeypatch.setitem(billing.TIER_FROM_PRICE_CACHE, "price_premium", "premium")
    monkeypatch.setitem(billing.TIER_FROM_PRICE_CACHE, "price_lite", "premium-lite")
    return fake


@pytest.fixture
def emails(monkeypatch):
    import transactional_email

    calls = []

    async def fake(**kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(transactional_email, "send_transactional_email", fake)
    return calls


def _sub(sid="sub_1", *, status="active", price="price_premium", cape=False, created_end=1762600000):
    return {
        "id": sid, "customer": "cus_1", "status": status, "metadata": {"cheripic_user_id": U},
        "items": {"data": [{"price": {"id": price}}]},
        "current_period_start": 1760000000, "current_period_end": created_end, "cancel_at_period_end": cape,
    }


def _mock_db(*, membership=None, last_applied_notes=(), claim_status=None):
    """Common Supabase mocks. `claim_status` overrides POST stripe_events for
    the synthetic premium_ended claim (event ids starting premium_ended:)."""
    def events_post(request):
        body = json.loads(request.content)
        if body["event_id"].startswith("premium_ended:") and claim_status:
            return httpx.Response(claim_status, json={"code": "23505"})
        return httpx.Response(201, json=[])

    routes = types.SimpleNamespace()
    routes.events_post = respx.post(f"{REST}/stripe_events").mock(side_effect=events_post)
    routes.events_get = respx.get(f"{REST}/stripe_events").mock(
        return_value=httpx.Response(200, json=[{"notes": n} for n in last_applied_notes])
    )
    routes.events_patch = respx.patch(f"{REST}/stripe_events").mock(return_value=httpx.Response(200, json=[]))
    routes.mem_get = respx.get(f"{REST}/user_memberships").mock(
        return_value=httpx.Response(200, json=[membership] if membership else [])
    )
    routes.mem_patch = respx.patch(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    routes.mem_post = respx.post(f"{REST}/user_memberships").mock(return_value=httpx.Response(201, json=[]))
    return routes


# --------------------------------------------------------------------------
# X-EMAIL.6 · premium_ended
# --------------------------------------------------------------------------

@respx.mock
async def test_subscription_deleted_sends_premium_ended_once_per_subscription(monkeypatch, emails):
    ev = {"id": "evt_del", "type": "customer.subscription.deleted", "created": 1762600001,
          "data": {"object": _sub(status="canceled")}}
    _install(monkeypatch, ev)
    db = _mock_db(membership={"tier": "premium", "stripe_subscription_id": "sub_1"})
    out = await billing.handle_event(b"{}", "good")
    assert out["handled"] is True
    assert json.loads(db.mem_post.calls.last.request.content)["tier"] == "basic"
    assert [e["template_slug"] for e in emails] == ["premium_ended"]
    claim = [json.loads(c.request.content) for c in db.events_post.calls]
    assert {"event_id": "premium_ended:sub_1", "event_type": "internal.premium_ended_email"} in claim


@respx.mock
async def test_premium_ended_claim_already_taken_sends_nothing(monkeypatch, emails):
    """Redelivery / canceled-update followed by deleted / reconcile → one email."""
    ev = {"id": "evt_del2", "type": "customer.subscription.deleted", "created": 1762600001,
          "data": {"object": _sub(status="canceled")}}
    _install(monkeypatch, ev)
    _mock_db(membership={"tier": "premium", "stripe_subscription_id": "sub_1"}, claim_status=409)
    await billing.handle_event(b"{}", "good")
    assert emails == []


@respx.mock
async def test_canceled_status_update_downgrades_and_sends_premium_ended(monkeypatch, emails):
    ev = {"id": "evt_upd_c", "type": "customer.subscription.updated", "created": 1762600001,
          "data": {"object": _sub(status="canceled")}}
    _install(monkeypatch, ev)
    db = _mock_db(membership={"tier": "premium-lite", "stripe_subscription_id": "sub_1"})
    await billing.handle_event(b"{}", "good")
    assert json.loads(db.mem_post.calls.last.request.content)["tier"] == "basic"
    assert [e["template_slug"] for e in emails] == ["premium_ended"]
    assert emails[0]["extra_vars"] == {"prior_tier": "premium-lite"}


@respx.mock
async def test_basic_member_deleted_event_sends_nothing(monkeypatch, emails):
    ev = {"id": "evt_del3", "type": "customer.subscription.deleted", "created": 1,
          "data": {"object": _sub(status="canceled")}}
    _install(monkeypatch, ev)
    _mock_db(membership=None)
    await billing.handle_event(b"{}", "good")
    assert emails == []


@respx.mock
async def test_scheduling_cancel_at_period_end_sends_membership_cancelled_not_premium_ended(monkeypatch, emails):
    ev = {"id": "evt_cape", "type": "customer.subscription.updated", "created": 1761000000,
          "data": {"object": _sub(cape=True)}}
    _install(monkeypatch, ev)
    db = _mock_db(membership={"tier": "premium", "stripe_subscription_id": "sub_1", "cancel_at_period_end": False})
    await billing.handle_event(b"{}", "good")
    assert json.loads(db.mem_post.calls.last.request.content)["tier"] == "premium"  # access continues
    assert [e["template_slug"] for e in emails] == ["membership_cancelled"]


@respx.mock
async def test_already_scheduled_cancel_does_not_resend(monkeypatch, emails):
    ev = {"id": "evt_cape2", "type": "customer.subscription.updated", "created": 1761000000,
          "data": {"object": _sub(cape=True)}}
    _install(monkeypatch, ev)
    _mock_db(membership={"tier": "premium", "stripe_subscription_id": "sub_1", "cancel_at_period_end": True})
    await billing.handle_event(b"{}", "good")
    assert emails == []


# --------------------------------------------------------------------------
# H13.17 · out-of-order events
# --------------------------------------------------------------------------

@respx.mock
async def test_older_event_than_last_applied_is_ignored(monkeypatch, emails):
    """deleted (t=200) already applied; a late updated(active, t=100) must not
    re-grant the paid tier."""
    ev = {"id": "evt_old", "type": "customer.subscription.updated", "created": 100,
          "data": {"object": _sub()}}
    _install(monkeypatch, ev)
    db = _mock_db(membership={"tier": "basic"}, last_applied_notes=["applied sub=sub_1 created=200"])
    out = await billing.handle_event(b"{}", "good")
    assert out["status"] == "stale" and out["handled"] is False
    assert not db.mem_post.called and not db.mem_patch.called
    assert emails == []
    q = db.events_get.calls.last.request.url.params
    assert q["notes"] == "like.applied sub=sub_1 created=*"
    assert json.loads(db.events_patch.calls.last.request.content)["notes"].startswith("stale")


@respx.mock
async def test_newer_event_is_applied_and_records_marker(monkeypatch, emails):
    ev = {"id": "evt_new", "type": "customer.subscription.updated", "created": 300,
          "data": {"object": _sub()}}
    _install(monkeypatch, ev)
    db = _mock_db(membership={"tier": "premium", "stripe_subscription_id": "sub_1"},
                  last_applied_notes=["applied sub=sub_1 created=200", "applied sub=sub_1 created=250"])
    out = await billing.handle_event(b"{}", "good")
    assert out["handled"] is True and "status" not in out
    assert db.mem_post.called
    assert json.loads(db.events_patch.calls.last.request.content)["notes"] == "applied sub=sub_1 created=300"


@respx.mock
async def test_ordering_lookup_failure_falls_back_to_applying(monkeypatch, emails):
    ev = {"id": "evt_x", "type": "customer.subscription.updated", "created": 300, "data": {"object": _sub()}}
    _install(monkeypatch, ev)
    db = _mock_db(membership={"tier": "premium", "stripe_subscription_id": "sub_1"})
    db.events_get.mock(return_value=httpx.Response(500))
    out = await billing.handle_event(b"{}", "good")
    assert out["handled"] is True and db.mem_post.called


@respx.mock
async def test_late_deleted_for_old_subscription_does_not_downgrade_new_one(monkeypatch, emails):
    ev = {"id": "evt_old_del", "type": "customer.subscription.deleted", "created": 500,
          "data": {"object": _sub("sub_OLD", status="canceled")}}
    _install(monkeypatch, ev)
    db = _mock_db(membership={"tier": "premium", "stripe_subscription_id": "sub_NEW"})
    await billing.handle_event(b"{}", "good")
    assert not db.mem_post.called and not db.mem_patch.called
    assert emails == []


async def test_stripe_object_events_are_normalised_to_dicts(monkeypatch):
    """stripe-python >= 15: StripeObject has no .get(); handlers need dicts."""
    import stripe

    obj = stripe.StripeObject.construct_from({"id": "evt", "type": "x", "data": {"object": {"id": "sub_1"}}}, "k")
    plain = billing._plain(obj)
    assert isinstance(plain, dict) and plain["data"]["object"].get("id") == "sub_1"
    assert billing._plain({"a": 1}) == {"a": 1}


def test_period_bounds_read_item_level_fields_on_new_api_versions():
    sub = {"items": {"data": [{"current_period_start": 1760000000, "current_period_end": 1762600000}]}}
    s, e = billing._period_bounds(sub)
    assert int(s.timestamp()) == 1760000000 and int(e.timestamp()) == 1762600000


# --------------------------------------------------------------------------
# H13.17 · reconciliation job
# --------------------------------------------------------------------------

def _recon_rows():
    return [
        # 1. matches Stripe → untouched
        {"user_id": "u1", "tier": "premium", "stripe_subscription_id": "sub_ok", "stripe_customer_id": "c1",
         "current_period_end": "2025-11-08T11:06:40+00:00", "cancel_at_period_end": False},
        # 2. tier drift (Stripe says lite)
        {"user_id": "u2", "tier": "premium", "stripe_subscription_id": "sub_drift", "stripe_customer_id": "c2",
         "current_period_end": "2025-11-08T11:06:40+00:00", "cancel_at_period_end": False},
        # 3. ended in Stripe but still paid here
        {"user_id": "u3", "tier": "premium", "stripe_subscription_id": "sub_ended", "stripe_customer_id": "c3",
         "current_period_end": "2025-11-08T11:06:40+00:00", "cancel_at_period_end": False},
        # 4. free row without a subscription → not checked
        {"user_id": "u4", "tier": "basic", "stripe_subscription_id": None},
    ]


@respx.mock
async def test_reconcile_corrects_drift_and_ends_cancelled_idempotently(monkeypatch, emails, caplog):
    subs = {
        "sub_ok": _sub("sub_ok", created_end=1762600000),
        "sub_drift": _sub("sub_drift", price="price_lite", created_end=1762600000),
        "sub_ended": _sub("sub_ended", status="canceled"),
    }
    _install(monkeypatch, {}, subs=subs)
    rows = _recon_rows()

    def mem_get(request):
        p = request.url.params
        if p.get("select", "").startswith("user_id,tier"):
            return httpx.Response(200, json=rows)
        # _get_current_membership(user) lookups
        uid = p["user_id"].split(".", 1)[1]
        row = next(r for r in rows if r["user_id"] == uid)
        return httpx.Response(200, json=[{"tier": row["tier"], "stripe_subscription_id": row["stripe_subscription_id"]}])

    respx.get(f"{REST}/user_memberships").mock(side_effect=mem_get)
    respx.patch(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    ins = respx.post(f"{REST}/user_memberships").mock(return_value=httpx.Response(201, json=[]))
    respx.post(f"{REST}/stripe_events").mock(return_value=httpx.Response(201, json=[]))

    caplog.set_level("INFO")
    counts = await billing.reconcile_subscriptions()
    assert counts == {"checked": 3, "ok": 1, "corrected": 1, "ended": 1, "skipped": 0, "errors": 0}
    written = {json.loads(c.request.content)["user_id"]: json.loads(c.request.content) for c in ins.calls}
    assert written["u2"]["tier"] == "premium-lite" and written["u2"]["stripe_subscription_id"] == "sub_drift"
    assert written["u3"]["tier"] == "basic"
    assert "u1" not in written
    assert [e["template_slug"] for e in emails] == ["premium_ended"]
    # counts only in the log — no member ids
    log = " ".join(r.getMessage() for r in caplog.records if "billing-reconcile" in r.getMessage())
    assert "u2" not in log and "u3" not in log and "corrected" in log


@respx.mock
async def test_reconcile_missing_subscription_is_treated_as_ended(monkeypatch, emails):
    class Missing(Exception):
        code = "resource_missing"

    _install(monkeypatch, {}, subs={"sub_gone": Missing("No such subscription: sub_gone")})
    rows = [{"user_id": "u9", "tier": "premium", "stripe_subscription_id": "sub_gone", "stripe_customer_id": "c9"}]

    def mem_get(request):
        if request.url.params.get("select", "").startswith("user_id,tier"):
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json=[{"tier": "premium", "stripe_subscription_id": "sub_gone"}])

    respx.get(f"{REST}/user_memberships").mock(side_effect=mem_get)
    respx.patch(f"{REST}/user_memberships").mock(return_value=httpx.Response(200, json=[]))
    ins = respx.post(f"{REST}/user_memberships").mock(return_value=httpx.Response(201, json=[]))
    respx.post(f"{REST}/stripe_events").mock(return_value=httpx.Response(201, json=[]))
    counts = await billing.reconcile_subscriptions()
    assert counts["ended"] == 1 and counts["errors"] == 0
    assert json.loads(ins.calls.last.request.content)["tier"] == "basic"


def test_reconcile_job_is_scheduled_only_when_enabled(monkeypatch):
    import main

    class Sched:
        def __init__(self):
            self.jobs = []

        def add_job(self, fn, trigger, **kw):
            self.jobs.append((fn, kw["id"]))

    monkeypatch.delenv("ENABLE_STRIPE_RECONCILE", raising=False)
    s = Sched()
    assert main._schedule_stripe_reconcile(s) is False and s.jobs == []
    monkeypatch.setenv("ENABLE_STRIPE_RECONCILE", "true")
    assert main._schedule_stripe_reconcile(s) is True
    assert s.jobs == [(billing.reconcile_subscriptions, "stripe_reconcile")]
