"""
Cheri AI message limits, and the Stripe price → tier map.

QUOTA WINDOW
    Quotas are per BILLING PERIOD, not per day. For a subscriber the window
    is their Stripe billing cycle (current_period_start → current_period_end),
    so someone who subscribes on the 12th resets on the 12th. Users with no
    active subscription have no cycle to anchor to, so they fall back to the
    UTC calendar month. See billing.get_quota_period().

WHERE THE LIMITS COME FROM (first hit wins)
    1. The public.cheri_ai_tier_limits Supabase table — edit a number in the
       Supabase table editor and it applies within CACHE_TTL seconds. No
       deploy, no restart. This is the normal way to tune limits.
    2. CHERI_LIMIT_* env vars — an escape hatch when the table is unreachable
       or you need to pin a value for local dev.
    3. The hardcoded defaults below — last resort so chat always has a number.

    A NULL monthly_limit in the table means that tier is uncapped.

    Changes apply to every user, mid-period included, because the limit is
    compared against the counter at request time and is never copied into the
    usage row. Both directions work:

      * Raising  10 → 20: someone already at 10 can send 10 more right away.
      * Lowering 20 → 10: someone already at 15 is over cap and blocked until
        their next period starts. Their counter keeps its value and resets
        normally — no retroactive charge or penalty.

    The table is readable by the frontend (see the RLS policy in
    migrations/002_tier_limits_table.sql), so the UI can render the same
    numbers the backend enforces instead of duplicating them in
    src/utils/constants/membership.ts.
"""

import logging
import math
import os
import time

from supabase_client import supabase

logger = logging.getLogger(__name__)

TIERS = ("basic", "premium-lite", "premium")

_UNLIMITED = {"unlimited", "inf", "infinite", "none", "-1"}

# Last-resort defaults if both the table and the env vars are unavailable.
_DEFAULT_MONTHLY_LIMITS: dict[str, float] = {
    "basic":          10,
    "premium-lite":  200,
    "premium":       700,
}

# How long a fetched limits table stays good. Short enough that an edit in
# Supabase shows up promptly, long enough that /chat isn't doing an extra
# round-trip per message.
CACHE_TTL = 60.0

# (expires_at_epoch, limits) — None until the first successful load.
_cache: tuple[float, dict[str, float]] | None = None


# --------------------------------------------------------------------------
# Layer 2/3: env vars over hardcoded defaults
# --------------------------------------------------------------------------

def _env_var_for(tier: str) -> str:
    """'premium-lite' → 'CHERI_LIMIT_PREMIUM_LITE'"""
    return f"CHERI_LIMIT_{tier.replace('-', '_').upper()}"


def _resolve_limit(tier: str, default: float) -> float:
    raw = os.getenv(_env_var_for(tier))
    if raw is None or not raw.strip():
        return default

    value = raw.strip().lower()
    if value in _UNLIMITED:
        return math.inf

    try:
        parsed = int(value)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer — falling back to default %s for tier %r",
            _env_var_for(tier), raw, default, tier,
        )
        return default

    if parsed < 0:
        logger.warning(
            "%s=%r is negative — falling back to default %s for tier %r",
            _env_var_for(tier), raw, default, tier,
        )
        return default

    return float(parsed)


def load_monthly_limits() -> dict[str, float]:
    """Defaults with env overrides applied. The floor under the DB lookup."""
    return {tier: _resolve_limit(tier, default)
            for tier, default in _DEFAULT_MONTHLY_LIMITS.items()}


# Static fallback, resolved once at import. Used verbatim when the table can't
# be read and nothing has been cached yet.
CHERI_AI_MONTHLY_LIMITS: dict[str, float] = load_monthly_limits()


# --------------------------------------------------------------------------
# Layer 1: the Supabase table
# --------------------------------------------------------------------------

def _coerce_row_limit(row: dict) -> float | None:
    """One table row → a limit, or None if the row is unusable.

    NULL means uncapped. A negative or non-integer value is treated as a bad
    edit and skipped so the fallback applies for that tier only — one typo in
    the table must not take every tier down.
    """
    if "monthly_limit" not in row:
        return None
    raw = row["monthly_limit"]
    if raw is None:
        return math.inf
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        logger.warning("cheri_ai_tier_limits: unusable monthly_limit %r for %r",
                       raw, row.get("tier"))
        return None
    if parsed < 0:
        logger.warning("cheri_ai_tier_limits: negative monthly_limit %r for %r",
                       raw, row.get("tier"))
        return None
    return float(parsed)


async def get_monthly_limits(*, force: bool = False) -> dict[str, float]:
    """
    The limits currently in force, table first.

    Cached for CACHE_TTL seconds. On any failure the last good values are
    reused rather than reverting to defaults — if the table says basic=20 and
    Supabase blips, users must not silently drop back to 10. Only a cold
    process with no cache at all falls through to env/defaults.
    """
    global _cache
    now = time.time()
    if not force and _cache and _cache[0] > now:
        return _cache[1]

    try:
        rows = await supabase.select(
            "cheri_ai_tier_limits",
            columns="tier,monthly_limit",
        )
    except Exception as e:
        if _cache:
            logger.warning("tier limits refresh failed (%r) — reusing cached values", e)
            _cache = (now + CACHE_TTL, _cache[1])
            return _cache[1]
        logger.warning("tier limits unavailable (%r) — using env/defaults", e)
        return CHERI_AI_MONTHLY_LIMITS

    # Start from env/defaults so a tier missing from the table still resolves.
    limits = dict(CHERI_AI_MONTHLY_LIMITS)
    for row in rows or []:
        tier = row.get("tier")
        if tier not in TIERS:
            continue
        value = _coerce_row_limit(row)
        if value is not None:
            limits[tier] = value

    _cache = (now + CACHE_TTL, limits)
    return limits


def invalidate_cache() -> None:
    """Drop the cached table so the next read refetches. Used by tests and
    available if you ever want an admin endpoint to force a refresh."""
    global _cache
    _cache = None


def describe_limits(limits: dict[str, float] | None = None) -> str:
    """Human-readable summary for the startup log, so the limits actually in
    force are visible without shelling into the container."""
    src = limits if limits is not None else CHERI_AI_MONTHLY_LIMITS
    return ", ".join(
        f"{tier}={'unlimited' if math.isinf(v) else int(v)}"
        for tier, v in src.items()
    )


# Stripe Price IDs → CheriPic tier. Replace the placeholders below with the
# Price IDs you copied from the Stripe Dashboard into membership.ts.
# Used by the webhook to know which tier to grant.
STRIPE_PRICE_TO_TIER: dict[str, str] = {
    "price_REPLACE_PREMIUM_LITE": "premium-lite",
    "price_REPLACE_PREMIUM":      "premium",
}
