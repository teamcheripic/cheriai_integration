"""
Shared test hooks.

main.py's slowapi limiter keeps in-memory counters for the life of the
process. Every test starts with empty buckets so endpoint tests stay
independent of order; the limits themselves are exercised in
test_F1_rate_limit.py.

Compliance checks (compliance.py: standing, AI consent, moderation, country,
age, legal holds, purchase agreements) are stubbed to their "nothing
changes" answers for every test, so the pre-existing tests keep exercising
today's behaviour without network calls. Tests that exercise the real
helpers are marked `@pytest.mark.real_compliance`.
"""

import os
import sys

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    main = sys.modules.get("main")
    limiter = getattr(main, "limiter", None) if main else None
    if limiter is not None:
        limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _stub_compliance(request, monkeypatch):
    import compliance

    compliance.forget_standing()
    if request.node.get_closest_marker("real_compliance"):
        yield
        return

    async def _v(value):
        return value

    monkeypatch.setattr(compliance, "member_standing", lambda uid: _v("active"))
    monkeypatch.setattr(compliance, "has_ai_consent", lambda uid: _v(True))
    monkeypatch.setattr(compliance, "country_allows", lambda code, feature: _v(True))
    monkeypatch.setattr(compliance, "min_member_age", lambda: _v(21))
    monkeypatch.setattr(compliance, "fetch_profile_basics",
                        lambda uid: _v({"user_id": uid, "date_of_birth": "1990-01-01", "country_of_residence": None}))
    monkeypatch.setattr(compliance, "has_legal_hold", lambda uid: _v(False))
    monkeypatch.setattr(compliance, "has_open_report", lambda uid: _v(False))
    monkeypatch.setattr(compliance, "moderate",
                        lambda text: _v({"flagged": False, "categories": [], "self_harm": False, "error": False}))

    async def _log(*a, **k):
        return True

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(compliance, "log_ai_safety_event", _log)
    monkeypatch.setattr(compliance, "record_purchase_agreement", _none)
    monkeypatch.setattr(compliance, "active_paid_membership", _none)
    monkeypatch.setattr(compliance, "tier_price",
                        lambda tier: _v({"price": None, "currency": "USD", "billing_interval": "month"}))
    yield
