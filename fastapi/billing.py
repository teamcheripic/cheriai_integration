"""
Stripe billing for CheriPic memberships.

Two responsibilities:
  1. Create Stripe Checkout sessions when a user clicks "Upgrade" so the
     frontend can redirect them to Stripe's hosted checkout page.
  2. Handle the resulting webhook events to flip the user's tier in
     user_memberships when their subscription is created/updated/cancelled.

Setup checklist (one-time, in https://dashboard.stripe.com):
  - Create three Products with monthly recurring USD Prices and paste the
    price_xxx IDs into src/utils/constants/membership.ts.
  - In Developers → Webhooks, add an endpoint pointed at
    https://<your-host>/billing/webhook listening to:
        checkout.session.completed
        customer.subscription.created
        customer.subscription.updated
        customer.subscription.deleted
        invoice.payment_succeeded
        invoice.payment_failed
  - Put two env vars in ai_integration/fastapi/.env:
        STRIPE_SECRET_KEY=sk_test_…   (or sk_live_…)
        STRIPE_WEBHOOK_SECRET=whsec_…
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from supabase_client import supabase

logger = logging.getLogger(__name__)

# Lazy-imported so the module loads even if stripe isn't pip-installed yet
# (we only crash when an endpoint is actually called).
_stripe = None
_secret_key = None
_webhook_secret = None


def _get_stripe():
    global _stripe, _secret_key, _webhook_secret
    if _stripe is not None:
        return _stripe
    import stripe as _s  # type: ignore

    _secret_key = os.getenv("STRIPE_SECRET_KEY")
    _webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not _secret_key:
        raise RuntimeError(
            "STRIPE_SECRET_KEY missing from ai_integration/fastapi/.env. "
            "Add it before calling billing endpoints."
        )
    _s.api_key = _secret_key
    _stripe = _s
    return _stripe


def get_webhook_secret() -> str:
    _get_stripe()  # ensures env loaded
    if not _webhook_secret:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET missing from ai_integration/fastapi/.env."
        )
    return _webhook_secret


# -----------------------------------------------------------------------------
# Customer + checkout session
# -----------------------------------------------------------------------------
async def _find_or_create_customer(user_id: str) -> str:
    """
    Look up an existing stripe_customer_id on the active membership row;
    if none, create a new Stripe customer keyed to the user_id and remember
    it. Re-uses the customer across plan changes so Stripe shows one
    history per user.
    """
    rows = await supabase.select(
        "user_memberships",
        columns="stripe_customer_id",
        eq={"user_id": user_id, "is_active": True},
        limit=1,
    )
    if rows and rows[0].get("stripe_customer_id"):
        return rows[0]["stripe_customer_id"]

    stripe = _get_stripe()
    customer = stripe.Customer.create(
        metadata={"cheripic_user_id": user_id},
    )
    cust_id = customer.id

    # Best-effort: persist the customer id immediately so the next call
    # re-uses it. If there's no active membership row yet we don't insert one
    # — the webhook will create it after checkout completes.
    try:
        await supabase.update(
            "user_memberships",
            payload={"stripe_customer_id": cust_id},
            eq={"user_id": user_id, "is_active": True},
        )
    except Exception as e:
        logger.info("No active membership to attach customer to (will set via webhook): %s", e)

    return cust_id


async def create_checkout_session(
    user_id: str,
    price_id: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Returns the hosted-checkout URL the frontend should redirect to."""
    if not price_id or not price_id.startswith("price_"):
        raise ValueError("price_id must be a Stripe Price ID (price_…)")

    stripe = _get_stripe()
    customer_id = await _find_or_create_customer(user_id)

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        # The webhook reads these from the resulting event so it can update
        # the right CheriPic user without trusting the client.
        client_reference_id=user_id,
        metadata={"cheripic_user_id": user_id},
        subscription_data={
            "metadata": {"cheripic_user_id": user_id},
        },
        allow_promotion_codes=True,
    )
    return session.url


async def create_portal_session(user_id: str, return_url: str) -> str:
    """
    Returns a Stripe Customer Portal URL where the user can cancel, change
    payment method, or update their subscription. Only works for users that
    already have a stripe_customer_id (i.e. they completed at least one
    checkout). Free-tier users with no customer record will hit ValueError.
    """
    rows = await supabase.select(
        "user_memberships",
        columns="stripe_customer_id",
        eq={"user_id": user_id, "is_active": True},
        limit=1,
    )
    customer_id = rows[0].get("stripe_customer_id") if rows else None
    if not customer_id:
        raise ValueError(
            "No Stripe customer on file. Upgrade to a paid tier first."
        )

    stripe = _get_stripe()
    portal = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return portal.url


# -----------------------------------------------------------------------------
# Webhook handlers
# -----------------------------------------------------------------------------
TIER_FROM_PRICE_CACHE: dict[str, str] = {}


def _register_price_to_tier_mapping(mapping: dict[str, str]) -> None:
    """Called from main.py at startup with the mapping from membership.ts."""
    TIER_FROM_PRICE_CACHE.update(mapping)


def _tier_for_price(price_id: Optional[str]) -> Optional[str]:
    if not price_id:
        return None
    return TIER_FROM_PRICE_CACHE.get(price_id)


async def _record_event_or_skip(event_id: str, event_type: str) -> bool:
    """
    Returns True if this is the first time we've seen this event_id (caller
    should process it); False if we've already handled it (caller should
    short-circuit). Idempotent against Stripe's redelivery storms.
    """
    try:
        await supabase.insert(
            "stripe_events",
            {"event_id": event_id, "event_type": event_type},
        )
        return True
    except RuntimeError as e:
        # 23505 = unique_violation on event_id → we've seen this one before.
        if "23505" in str(e):
            logger.info("Skipping duplicate Stripe event %s", event_id)
            return False
        raise


async def _upsert_active_membership(
    user_id: str,
    tier: str,
    *,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    stripe_price_id: Optional[str] = None,
    current_period_start: Optional[datetime] = None,
    current_period_end: Optional[datetime] = None,
    cancel_at_period_end: Optional[bool] = None,
    last_invoice_status: Optional[str] = None,
) -> None:
    """
    Make `tier` the user's only active membership. Deactivates any previous
    active row first (the partial unique index would otherwise reject the
    insert), then upserts the new one.
    """
    # Deactivate any other active rows for this user (catches the
    # downgrade/upgrade case where the user already had a different tier).
    try:
        await supabase.update(
            "user_memberships",
            payload={"is_active": False, "updated_at": datetime.now(timezone.utc).isoformat()},
            eq={"user_id": user_id, "is_active": True},
        )
    except Exception as e:
        logger.warning("Couldn't deactivate previous membership for %s: %s", user_id, e)

    payload = {
        "user_id": user_id,
        "tier": tier,
        "is_active": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if stripe_customer_id:
        payload["stripe_customer_id"] = stripe_customer_id
    if stripe_subscription_id:
        payload["stripe_subscription_id"] = stripe_subscription_id
    if stripe_price_id:
        payload["stripe_price_id"] = stripe_price_id
    # current_period_start anchors the message-quota window — see
    # get_quota_period(). Without it a subscriber silently falls back to
    # calendar-month resets.
    if current_period_start:
        payload["current_period_start"] = current_period_start.isoformat()
    if current_period_end:
        payload["current_period_end"] = current_period_end.isoformat()
        payload["expires_at"] = current_period_end.isoformat()
    if cancel_at_period_end is not None:
        payload["cancel_at_period_end"] = cancel_at_period_end
    if last_invoice_status:
        payload["last_invoice_status"] = last_invoice_status

    await supabase.insert("user_memberships", payload)


async def handle_event(payload: bytes, signature: str) -> dict:
    """Validate + dispatch a Stripe webhook payload. Returns a summary dict."""
    stripe = _get_stripe()
    secret = get_webhook_secret()

    try:
        event = stripe.Webhook.construct_event(
            payload=payload, sig_header=signature, secret=secret
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.error("Invalid Stripe webhook signature: %s", e)
        raise

    event_id = event["id"]
    event_type = event["type"]

    if not await _record_event_or_skip(event_id, event_type):
        return {"event_id": event_id, "event_type": event_type, "status": "duplicate"}

    obj = event["data"]["object"]
    handled = False

    try:
        if event_type == "checkout.session.completed":
            await _handle_checkout_completed(obj)
            handled = True
        elif event_type in (
            "customer.subscription.created",
            "customer.subscription.updated",
        ):
            await _handle_subscription_active(obj)
            handled = True
        elif event_type == "customer.subscription.deleted":
            await _handle_subscription_cancelled(obj)
            handled = True
        elif event_type == "invoice.payment_succeeded":
            await _handle_invoice(obj, status="paid")
            handled = True
        elif event_type == "invoice.payment_failed":
            await _handle_invoice(obj, status="failed")
            handled = True
        else:
            logger.info("Ignoring Stripe event type %s", event_type)
    finally:
        # Mark processed even on failure so we don't loop on a poison message
        # — manual intervention via stripe_events.notes is the recovery path.
        try:
            await supabase.update(
                "stripe_events",
                payload={
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                    "notes": None if handled else "unhandled or errored",
                },
                eq={"event_id": event_id},
            )
        except Exception:
            pass

    return {"event_id": event_id, "event_type": event_type, "handled": handled}


def _user_id_from_object(obj: dict) -> Optional[str]:
    """Look up our CheriPic user_id from the various places we stash it."""
    md = obj.get("metadata") or {}
    if md.get("cheripic_user_id"):
        return md["cheripic_user_id"]
    if obj.get("client_reference_id"):
        return obj["client_reference_id"]
    return None


async def _handle_checkout_completed(session: dict) -> None:
    user_id = _user_id_from_object(session)
    if not user_id:
        logger.warning("checkout.session.completed without user_id metadata; skipping")
        return
    # The session has the customer + subscription IDs we want to remember,
    # but the price id sits on the line_items which we don't always get.
    # The follow-up customer.subscription.created event has everything we
    # need, so this handler just stashes the customer + sub IDs.
    stripe = _get_stripe()
    sub_id = session.get("subscription")
    cust_id = session.get("customer")
    price_id = None
    tier = None
    if sub_id:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            price_id = sub["items"]["data"][0]["price"]["id"]
            tier = _tier_for_price(price_id)
        except Exception as e:
            logger.warning("Could not retrieve sub %s right away: %s", sub_id, e)
    if tier:
        await _upsert_active_membership(
            user_id=user_id,
            tier=tier,
            stripe_customer_id=cust_id,
            stripe_subscription_id=sub_id,
            stripe_price_id=price_id,
        )


async def _handle_subscription_active(sub: dict) -> None:
    user_id = _user_id_from_object(sub)
    if not user_id:
        # Look up via customer metadata as a fallback.
        cust_id = sub.get("customer")
        if cust_id:
            try:
                stripe = _get_stripe()
                cust = stripe.Customer.retrieve(cust_id)
                user_id = (cust.get("metadata") or {}).get("cheripic_user_id")
            except Exception:
                pass
    if not user_id:
        logger.warning("subscription.* without user_id; skipping")
        return

    items = sub.get("items", {}).get("data") or []
    price_id = items[0]["price"]["id"] if items else None
    tier = _tier_for_price(price_id)
    if not tier:
        logger.warning("Unknown price_id %s on subscription %s", price_id, sub.get("id"))
        return

    cps = sub.get("current_period_start")
    cps_dt = datetime.fromtimestamp(cps, tz=timezone.utc) if cps else None
    cpe = sub.get("current_period_end")
    cpe_dt = datetime.fromtimestamp(cpe, tz=timezone.utc) if cpe else None

    status = sub.get("status")
    # 'active', 'trialing' → keep tier; 'past_due', 'unpaid', 'incomplete' → still
    # treat as active for grace, but mark last_invoice_status so the UI can warn.
    if status in ("canceled", "incomplete_expired"):
        await _handle_subscription_cancelled(sub)
        return

    await _upsert_active_membership(
        user_id=user_id,
        tier=tier,
        stripe_customer_id=sub.get("customer"),
        stripe_subscription_id=sub.get("id"),
        stripe_price_id=price_id,
        current_period_start=cps_dt,
        current_period_end=cpe_dt,
        cancel_at_period_end=bool(sub.get("cancel_at_period_end")),
        last_invoice_status=status,
    )


async def _handle_subscription_cancelled(sub: dict) -> None:
    user_id = _user_id_from_object(sub)
    if not user_id:
        return
    # Drop them back to the free tier.
    await _upsert_active_membership(
        user_id=user_id,
        tier="basic",
        stripe_customer_id=sub.get("customer"),
        stripe_subscription_id=None,
        current_period_start=None,
        stripe_price_id=None,
        current_period_end=None,
        cancel_at_period_end=False,
        last_invoice_status="canceled",
    )


async def _handle_invoice(inv: dict, *, status: str) -> None:
    sub_id = inv.get("subscription")
    if not sub_id:
        return
    try:
        await supabase.update(
            "user_memberships",
            payload={"last_invoice_status": status, "updated_at": datetime.now(timezone.utc).isoformat()},
            eq={"stripe_subscription_id": sub_id, "is_active": True},
        )
    except Exception as e:
        logger.warning("Could not record invoice %s status: %s", inv.get("id"), e)


# -----------------------------------------------------------------------------
# Cheri AI daily message cap
# -----------------------------------------------------------------------------
def _utc_today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _utc_month_start_iso() -> str:
    """First day of the current UTC calendar month, as an ISO date."""
    return datetime.now(timezone.utc).date().replace(day=1).isoformat()


async def get_quota_period(user_id: str) -> dict:
    """
    Resolve the message-quota window for a user.

    Subscribers are anchored to their Stripe billing cycle, so a user who
    subscribed on the 12th resets on the 12th rather than on the 1st. Users
    with no active subscription (basic tier) have no cycle to anchor to and
    fall back to the UTC calendar month.

    Returns:
        {
          "period_start": "YYYY-MM-DD",   # doubles as the usage row key
          "period_end":   ISO8601 | None, # None for calendar-month fallback
          "source":       "billing_cycle" | "calendar_month",
        }

    Falls back to the calendar month on any lookup failure — a Supabase blip
    must not hand someone an unlimited quota or lock them out entirely.
    """
    try:
        rows = await supabase.select(
            "user_memberships",
            columns="current_period_start,current_period_end",
            eq={"user_id": user_id, "is_active": True},
            limit=1,
        )
    except Exception as e:
        logger.warning("quota period lookup failed for %s: %r", user_id, e)
        rows = []

    start_raw = rows[0].get("current_period_start") if rows else None
    end_raw = rows[0].get("current_period_end") if rows else None

    if start_raw:
        try:
            start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            end_dt = (
                datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                if end_raw
                else None
            )
            # A period that already ended means the webhook hasn't advanced the
            # row yet (renewal in flight, or a cancelled sub). Falling through
            # to the calendar month is the safe read: the user keeps a quota
            # rather than being stuck against a stale, exhausted window.
            if end_dt is None or end_dt > datetime.now(timezone.utc):
                return {
                    "period_start": start_dt.date().isoformat(),
                    "period_end": end_dt.isoformat() if end_dt else None,
                    "source": "billing_cycle",
                }
        except (ValueError, TypeError) as e:
            logger.warning("Unparseable billing period for %s: %r", user_id, e)

    return {
        "period_start": _utc_month_start_iso(),
        "period_end": None,
        "source": "calendar_month",
    }


async def get_period_usage(user_id: str, period_start: str) -> int:
    """Messages sent by this user within the given quota period."""
    rows = await supabase.select(
        "cheri_ai_usage_periods",
        columns="messages_sent",
        eq={"user_id": user_id, "period_start": period_start},
        limit=1,
    )
    return int(rows[0]["messages_sent"]) if rows else 0


async def increment_period_usage(user_id: str, period_start: str) -> int:
    """
    Bump this period's counter (upsert with merge-duplicates) and return the
    new count. Two concurrent /chat calls can both succeed and end up at +2 —
    that's fine; we tolerate ±1 around the cap.
    """
    current = await get_period_usage(user_id, period_start)
    new_count = current + 1
    try:
        await supabase.upsert(
            "cheri_ai_usage_periods",
            {
                "user_id": user_id,
                "period_start": period_start,
                "messages_sent": new_count,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_id,period_start",
        )
    except Exception as e:
        logger.warning("period usage upsert failed for %s: %s", user_id, e)
    return new_count


async def get_membership_tier(user_id: str) -> str:
    rows = await supabase.select(
        "user_memberships",
        columns="tier",
        eq={"user_id": user_id, "is_active": True},
        limit=1,
    )
    t = rows[0]["tier"] if rows else "basic"
    if t not in ("basic", "premium-lite", "premium"):
        t = "basic"
    return t
