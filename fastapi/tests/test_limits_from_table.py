"""
Tests for reading Cheri AI limits from the cheri_ai_tier_limits Supabase table.

Precedence is table → env var → hardcoded default, and the property that
matters most is that a transient Supabase failure keeps serving the last good
values instead of silently reverting users to the defaults.
"""

import math

import pytest

import tier_limits


@pytest.fixture(autouse=True)
def _clear_cache():
    tier_limits.invalidate_cache()
    yield
    tier_limits.invalidate_cache()


@pytest.fixture
def table(monkeypatch):
    """Patch supabase.select to return canned cheri_ai_tier_limits rows."""
    state = {"calls": 0}

    def _install(rows=None, error=None):
        async def _select(table_name, **kwargs):
            assert table_name == "cheri_ai_tier_limits"
            state["calls"] += 1
            if error:
                raise error
            return rows or []
        monkeypatch.setattr(tier_limits.supabase, "select", _select)
        return state

    return _install


async def test_table_values_win_over_defaults(table):
    table([
        {"tier": "basic", "monthly_limit": 20},
        {"tier": "premium-lite", "monthly_limit": 250},
        {"tier": "premium", "monthly_limit": 1000},
    ])
    got = await tier_limits.get_monthly_limits()
    assert got == {"basic": 20, "premium-lite": 250, "premium": 1000}


async def test_null_limit_means_unlimited(table):
    table([{"tier": "premium", "monthly_limit": None}])
    got = await tier_limits.get_monthly_limits()
    assert math.isinf(got["premium"])


async def test_zero_disables_a_tier(table):
    table([{"tier": "basic", "monthly_limit": 0}])
    got = await tier_limits.get_monthly_limits()
    assert got["basic"] == 0


async def test_tier_missing_from_table_falls_back_to_default(table):
    table([{"tier": "basic", "monthly_limit": 20}])
    got = await tier_limits.get_monthly_limits()
    assert got["basic"] == 20
    assert got["premium"] == 700  # untouched default


async def test_bad_row_only_affects_its_own_tier(table):
    """One typo in the dashboard must not take every tier down."""
    table([
        {"tier": "basic", "monthly_limit": "abc"},
        {"tier": "premium", "monthly_limit": 900},
    ])
    got = await tier_limits.get_monthly_limits()
    assert got["basic"] == 10    # fell back
    assert got["premium"] == 900  # still applied


async def test_negative_row_is_rejected(table):
    table([{"tier": "basic", "monthly_limit": -5}])
    got = await tier_limits.get_monthly_limits()
    assert got["basic"] == 10


async def test_unknown_tier_row_is_ignored(table):
    table([{"tier": "enterprise", "monthly_limit": 5000}])
    got = await tier_limits.get_monthly_limits()
    assert set(got) == set(tier_limits.TIERS)


async def test_result_is_cached(table):
    state = table([{"tier": "basic", "monthly_limit": 20}])
    await tier_limits.get_monthly_limits()
    await tier_limits.get_monthly_limits()
    await tier_limits.get_monthly_limits()
    assert state["calls"] == 1, "should hit Supabase once, not once per message"


async def test_force_refresh_bypasses_cache(table):
    state = table([{"tier": "basic", "monthly_limit": 20}])
    await tier_limits.get_monthly_limits()
    await tier_limits.get_monthly_limits(force=True)
    assert state["calls"] == 2


async def test_outage_reuses_last_good_values(table, monkeypatch):
    """The important one: a blip must not silently drop users from 20 to 10."""
    table([{"tier": "basic", "monthly_limit": 20}])
    first = await tier_limits.get_monthly_limits()
    assert first["basic"] == 20

    async def _boom(table_name, **kwargs):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(tier_limits.supabase, "select", _boom)

    after = await tier_limits.get_monthly_limits(force=True)
    assert after["basic"] == 20, "should reuse cached value, not revert to default"


async def test_cold_start_outage_uses_defaults(table):
    """No cache to fall back on — env/defaults keep chat alive."""
    table(error=RuntimeError("supabase down"))
    got = await tier_limits.get_monthly_limits()
    assert got == {"basic": 10, "premium-lite": 200, "premium": 700}


async def test_empty_table_uses_defaults(table):
    table([])
    got = await tier_limits.get_monthly_limits()
    assert got == {"basic": 10, "premium-lite": 200, "premium": 700}
