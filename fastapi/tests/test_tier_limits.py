"""
Guards the billing tier config. These numbers gate real money (how many AI
messages each paid tier gets) and MUST stay in lockstep with the frontend
mirror in src/utils/constants/membership.ts. If someone tweaks one number,
this test is the tripwire.
"""

from tier_limits import CHERI_AI_MONTHLY_LIMITS, STRIPE_PRICE_TO_TIER


def test_all_tiers_present():
    assert set(CHERI_AI_MONTHLY_LIMITS) == {"basic", "premium-lite", "premium"}


def test_limits_increase_with_tier():
    assert (
        CHERI_AI_MONTHLY_LIMITS["basic"]
        < CHERI_AI_MONTHLY_LIMITS["premium-lite"]
        < CHERI_AI_MONTHLY_LIMITS["premium"]
    )


def test_price_map_targets_are_valid_tiers():
    for tier in STRIPE_PRICE_TO_TIER.values():
        assert tier in CHERI_AI_MONTHLY_LIMITS
