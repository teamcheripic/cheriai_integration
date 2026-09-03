"""
Tests for env-var overrides of the Cheri AI monthly limits.

The important property is that a bad value can never take chat down: every
malformed input must fall back to the shipped default, not raise.
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
def limits(monkeypatch):
    """Set CHERI_LIMIT_* vars and re-resolve the table."""
    def _load(**env):
        for tier in ("basic", "premium-lite", "premium"):
            monkeypatch.delenv(tier_limits._env_var_for(tier), raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return tier_limits.load_monthly_limits()
    return _load


def test_defaults_when_no_env_set(limits):
    assert limits() == {"basic": 10, "premium-lite": 200, "premium": 700}


def test_raising_a_limit(limits):
    assert limits(CHERI_LIMIT_BASIC="20")["basic"] == 20


def test_lowering_a_limit(limits):
    assert limits(CHERI_LIMIT_PREMIUM="500")["premium"] == 500


def test_hyphenated_tier_maps_to_underscored_var(limits):
    assert limits(CHERI_LIMIT_PREMIUM_LITE="250")["premium-lite"] == 250


def test_each_tier_overrides_independently(limits):
    got = limits(CHERI_LIMIT_BASIC="20", CHERI_LIMIT_PREMIUM_LITE="250")
    assert got["basic"] == 20
    assert got["premium-lite"] == 250
    assert got["premium"] == 700  # untouched


@pytest.mark.parametrize("word", ["unlimited", "inf", "none", "-1", "UNLIMITED"])
def test_unlimited_keywords(limits, word):
    assert math.isinf(limits(CHERI_LIMIT_PREMIUM=word)["premium"])


def test_zero_is_valid_and_blocks_the_tier(limits):
    """0 is a legitimate setting — useful to switch a tier off entirely."""
    assert limits(CHERI_LIMIT_BASIC="0")["basic"] == 0


@pytest.mark.parametrize("bad", ["abc", "10.5", "", "   ", "1_000_000_000x"])
def test_malformed_values_fall_back_to_default(limits, bad):
    assert limits(CHERI_LIMIT_BASIC=bad)["basic"] == 10


def test_negative_other_than_sentinel_falls_back(limits):
    assert limits(CHERI_LIMIT_BASIC="-7")["basic"] == 10


def test_whitespace_is_tolerated(limits):
    assert limits(CHERI_LIMIT_BASIC=" 42 ")["basic"] == 42
