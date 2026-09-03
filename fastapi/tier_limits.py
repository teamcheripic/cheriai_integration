"""
Server-side mirror of src/utils/constants/membership.ts.

Kept in a tiny separate file so when you tune limits in the TS constants you
can grep for the same numbers here and update them in lockstep. If these
drift the frontend will show "X messages left" while the backend cuts you
off at a different count.
"""

# tier → integer messages per UTC day. (math.inf only if a tier should
# be truly unlimited — current product spec caps every tier.)
CHERI_AI_DAILY_LIMITS: dict[str, float] = {
    "basic":         5,
    "premium-lite":  15,
    "premium":       25,
}

# Stripe Price IDs → CheriPic tier. Replace the placeholders below with the
# Price IDs you copied from the Stripe Dashboard into membership.ts.
# Used by the webhook to know which tier to grant.
STRIPE_PRICE_TO_TIER: dict[str, str] = {
    "price_REPLACE_PREMIUM_LITE": "premium-lite",
    "price_REPLACE_PREMIUM":      "premium",
}
