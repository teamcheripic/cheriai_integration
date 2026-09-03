"""
Tests for the billing-cycle-anchored quota window (billing.get_quota_period).

The branch that matters most is the fallback: any failure to resolve a real
billing cycle must land on the calendar month rather than raising or handing
back an open-ended window.
"""

from datetime import datetime, timedelta, timezone

import pytest

import billing


def _month_start() -> str:
    return datetime.now(timezone.utc).date().replace(day=1).isoformat()


@pytest.fixture
def fake_membership(monkeypatch):
    """Patch supabase.select to return one canned user_memberships row."""
    def _install(rows):
        async def _select(table, **kwargs):
            assert table == "user_memberships"
            return rows
        monkeypatch.setattr(billing.supabase, "select", _select)
    return _install


async def test_active_subscription_anchors_to_billing_cycle(fake_membership):
    start = datetime(2026, 8, 12, tzinfo=timezone.utc)
    end = datetime.now(timezone.utc) + timedelta(days=9)
    fake_membership([{
        "current_period_start": start.isoformat(),
        "current_period_end": end.isoformat(),
    }])

    period = await billing.get_quota_period("u1")

    assert period["source"] == "billing_cycle"
    assert period["period_start"] == "2026-08-12"
    assert period["period_end"] is not None


async def test_expired_period_falls_back_to_calendar_month(fake_membership):
    """A stale row (renewal webhook not yet processed) must not strand the
    user against an exhausted window."""
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    end = datetime(2026, 2, 5, tzinfo=timezone.utc)  # long past
    fake_membership([{
        "current_period_start": start.isoformat(),
        "current_period_end": end.isoformat(),
    }])

    period = await billing.get_quota_period("u1")

    assert period["source"] == "calendar_month"
    assert period["period_start"] == _month_start()


async def test_no_membership_row_uses_calendar_month(fake_membership):
    fake_membership([])
    period = await billing.get_quota_period("u1")
    assert period["source"] == "calendar_month"
    assert period["period_start"] == _month_start()
    assert period["period_end"] is None


async def test_null_period_start_uses_calendar_month(fake_membership):
    """Subscriber rows written before the migration have no start anchor."""
    fake_membership([{"current_period_start": None, "current_period_end": None}])
    period = await billing.get_quota_period("u1")
    assert period["source"] == "calendar_month"


async def test_unparseable_date_uses_calendar_month(fake_membership):
    fake_membership([{"current_period_start": "not-a-date", "current_period_end": None}])
    period = await billing.get_quota_period("u1")
    assert period["source"] == "calendar_month"


async def test_supabase_failure_uses_calendar_month(monkeypatch):
    """A Supabase outage must not grant an unlimited quota or hard-fail chat."""
    async def _boom(table, **kwargs):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(billing.supabase, "select", _boom)

    period = await billing.get_quota_period("u1")

    assert period["source"] == "calendar_month"
    assert period["period_start"] == _month_start()
