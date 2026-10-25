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

class WebhookSignatureError(Exception):
    """Stripe webhook payload/signature could not be verified (→ HTTP 400)."""


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
    """Called from main.py at startup with the mapping from membership.ts
    (hardcoded fallback). DB-sourced entries are layered on top by
    refresh_price_to_tier_from_db() so admin edits in the panel take
    effect without a redeploy."""
    TIER_FROM_PRICE_CACHE.update(mapping)


async def refresh_price_to_tier_from_db() -> None:
    """
    Merge the DB-sourced stripe_price_id → tier mapping from
    tier_config into TIER_FROM_PRICE_CACHE. Called at startup AND at
    the top of every webhook so a Stripe price rotation edited in the
    admin panel is respected on the very next webhook — no Railway
    redeploy needed. Hardcoded placeholders from membership.ts stay
    in the map as a fallback so a bad DB row can't blank the mapping.
    """
    try:
        rows = await supabase.select(
            "tier_config",
            columns="tier,stripe_price_id",
        )
    except Exception as e:
        logger.warning("[billing] refresh_price_to_tier_from_db failed: %s", e)
        return
    added = 0
    for r in rows or []:
        pid = r.get("stripe_price_id")
        tier = r.get("tier")
        # Skip nulls + placeholder values ('price_REPLACE_...') so a
        # partially-populated tier_config row doesn't shadow a real
        # env/hardcoded mapping.
        if not pid or not tier or "REPLACE" in pid or not pid.startswith("price_"):
            continue
        TIER_FROM_PRICE_CACHE[pid] = tier
        added += 1
    logger.info(
        "[billing] price→tier cache: %d entries (%d from DB)",
        len(TIER_FROM_PRICE_CACHE), added,
    )


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
    # Refresh the price→tier cache from tier_config on every webhook so
    # a Stripe price rotation edited in the admin panel takes effect
    # immediately. Best-effort; a failure leaves the previous cache in
    # place so we don't lose the ability to identify existing prices.
    await refresh_price_to_tier_from_db()

    stripe = _get_stripe()
    secret = get_webhook_secret()

    try:
        event = stripe.Webhook.construct_event(
            payload=payload, sig_header=signature, secret=secret
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.error("Invalid Stripe webhook signature: %s", e)
        raise WebhookSignatureError(str(e)) from e

    # stripe-python >= 15 returns StripeObject, which is no longer a dict
    # (`.get` raises AttributeError) — every handler below uses dict access.
    event = _plain(event)
    event_id = event["id"]
    event_type = event["type"]

    if not await _record_event_or_skip(event_id, event_type):
        return {"event_id": event_id, "event_type": event_type, "status": "duplicate"}

    obj = event["data"]["object"]
    handled = False
    failed = False
    stale = False
    # H13.17 · out-of-order delivery: remember which subscription this event
    # is about and when Stripe created it; an event older than the newest one
    # already applied for that subscription must not regress state.
    sub_ref = _subscription_ref(event_type, obj)
    created = _as_int(event.get("created"))
    if sub_ref and created is not None:
        last_applied = await _last_applied_event_created(sub_ref)
        if last_applied is not None and created < last_applied:
            stale = True

    try:
        if stale:
            logger.info(
                "Ignoring out-of-order Stripe event %s (%s): older than last applied for its subscription",
                event_id, event_type,
            )
        elif event_type == "checkout.session.completed":
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
    except Exception:
        # Processing failed (e.g. Supabase blip). Release the idempotency
        # claim so Stripe's automatic retry reprocesses this event instead of
        # it being skipped forever as a "duplicate" — otherwise a transient
        # error permanently loses a membership change. Stripe stops retrying
        # after ~3 days, which bounds any poison message.
        failed = True
        try:
            await supabase.delete("stripe_events", eq={"event_id": event_id})
        except Exception as e:
            logger.error("Could not release stripe_events claim for %s: %r", event_id, e)
        raise
    finally:
        if not failed:
            try:
                if stale:
                    notes = "stale: older than last applied event"
                elif handled and sub_ref and created is not None:
                    # Machine-readable marker read back by
                    # _last_applied_event_created (no schema change needed).
                    notes = f"{_APPLIED_PREFIX}{sub_ref} created={created}"
                else:
                    notes = None if handled else "unhandled event type"
                await supabase.update(
                    "stripe_events",
                    payload={
                        "processed_at": datetime.now(timezone.utc).isoformat(),
                        "notes": notes,
                    },
                    eq={"event_id": event_id},
                )
            except Exception:
                pass

    out = {"event_id": event_id, "event_type": event_type, "handled": handled}
    if stale:
        out["status"] = "stale"
    return out


# -----------------------------------------------------------------------------
# H13.17 · event ordering + reconciliation helpers
# -----------------------------------------------------------------------------
_APPLIED_PREFIX = "applied sub="
_SUBSCRIPTION_EVENTS = (
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "checkout.session.completed",
)


def _plain(obj):
    """StripeObject → plain dict (recursive); dicts pass through."""
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict) and not isinstance(obj, dict):
        try:
            return to_dict()
        except TypeError:
            return to_dict(recursive=True)
    return obj


def _as_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _subscription_ref(event_type: str, obj: dict) -> Optional[str]:
    """Subscription id an ordering-sensitive event is about (or None)."""
    if event_type not in _SUBSCRIPTION_EVENTS:
        return None
    if event_type == "checkout.session.completed":
        sid = obj.get("subscription")
    else:
        sid = obj.get("id")
    return sid if isinstance(sid, str) and sid else None


async def _last_applied_event_created(sub_id: str) -> Optional[int]:
    """Newest Stripe `created` (epoch s) already applied for this
    subscription, read from the stripe_events notes marker. None when
    unknown — then the event is applied (pre-existing behaviour)."""
    import httpx
    from supabase_client import SUPABASE_URL, SUPABASE_KEY

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/stripe_events",
                params={
                    "select": "notes",
                    "notes": f"like.{_APPLIED_PREFIX}{sub_id} created=*",
                    "processed_at": "not.is.null",
                },
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            )
        if r.status_code >= 300:
            return None
        best: Optional[int] = None
        for row in r.json() or []:
            note = row.get("notes") or ""
            if not note.startswith(f"{_APPLIED_PREFIX}{sub_id} created="):
                continue
            ts = _as_int(note.rsplit("=", 1)[-1])
            if ts is not None and (best is None or ts > best):
                best = ts
        return best
    except Exception as e:
        logger.warning("[billing] last-applied lookup failed for %s: %r", sub_id, e)
        return None


def _period_bounds(sub: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    """current_period_start/end as datetimes. Newer Stripe API versions
    carry them on the subscription item instead of the subscription."""
    cps = sub.get("current_period_start")
    cpe = sub.get("current_period_end")
    if cps is None or cpe is None:
        items = (sub.get("items") or {}).get("data") or []
        if items:
            cps = cps if cps is not None else items[0].get("current_period_start")
            cpe = cpe if cpe is not None else items[0].get("current_period_end")
    to_dt = lambda v: datetime.fromtimestamp(v, tz=timezone.utc) if v else None  # noqa: E731
    return to_dt(cps), to_dt(cpe)


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
        prior_tier = await _get_current_tier(user_id)
        await _upsert_active_membership(
            user_id=user_id,
            tier=tier,
            stripe_customer_id=cust_id,
            stripe_subscription_id=sub_id,
            stripe_price_id=price_id,
        )
        await _send_membership_change_email(user_id, prior_tier, tier)


async def _handle_subscription_active(sub: dict) -> None:
    user_id = _user_id_from_object(sub)
    if not user_id:
        # Look up via customer metadata as a fallback.
        cust_id = sub.get("customer")
        if cust_id:
            try:
                stripe = _get_stripe()
                cust = _plain(stripe.Customer.retrieve(cust_id))
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

    cps_dt, cpe_dt = _period_bounds(sub)

    status = sub.get("status")
    # 'active', 'trialing' → keep tier; 'past_due', 'unpaid', 'incomplete' → still
    # treat as active for grace, but mark last_invoice_status so the UI can warn.
    if status in ("canceled", "incomplete_expired"):
        await _handle_subscription_cancelled(sub)
        return

    # Snapshot the user's prior active tier BEFORE we upsert so we can tell
    # "first-time subscriber" (basic → paid → welcome_* email) apart from
    # "existing subscriber changed plans" (lite → premium → membership_upgraded).
    prior = await _get_current_membership(user_id)
    prior_tier = prior["tier"]
    cancel_at_period_end = bool(sub.get("cancel_at_period_end"))

    await _upsert_active_membership(
        user_id=user_id,
        tier=tier,
        stripe_customer_id=sub.get("customer"),
        stripe_subscription_id=sub.get("id"),
        stripe_price_id=price_id,
        current_period_start=cps_dt,
        current_period_end=cpe_dt,
        cancel_at_period_end=cancel_at_period_end,
        last_invoice_status=status,
    )

    await _send_membership_change_email(user_id, prior_tier, tier)

    # Scenario 21 (membership_cancelled): the member scheduled a
    # cancellation — access continues until the period ends, which is
    # exactly what the template/push copy says. Sent on the false → true
    # transition only; premium_ended follows when Stripe deletes the
    # subscription at period end.
    if cancel_at_period_end and not prior.get("cancel_at_period_end") and tier != "basic":
        from transactional_email import send_transactional_email as _send_email
        try:
            await _send_email(
                user_id=user_id,
                template_slug="membership_cancelled",
                extra_vars={"prior_tier": tier},
                dedup_window_hours=24,
            )
        except Exception as e:
            logger.warning("[billing] cancellation email failed for %s: %r", user_id, e)


async def _send_membership_change_email(user_id: str, prior_tier: str, tier: str) -> None:
    """
    Scenarios 15-18 of the 2026-10-12 spec. Fire-and-forget; EMAIL ONLY
    (welcome_* / membership_upgraded aren't in NOTIFICATION_REGISTRY).
      basic → paid        : welcome_<tier>   (first event to land wins; the
                            second sees the paid tier and sends nothing)
      paid A → paid B     : membership_upgraded
      same tier (renewal) : nothing
    """
    from transactional_email import send_transactional_email as _send_email
    if prior_tier == "basic" and tier != "basic":
        welcome_slug = {
            "premium": "welcome_premium",
            "premium-lite": "welcome_premium_lite",
        }.get(tier)
        if welcome_slug:
            try:
                await _send_email(user_id=user_id, template_slug=welcome_slug, dedup_window_hours=24)
            except Exception as e:
                logger.warning("[billing] welcome email failed for %s: %r", user_id, e)
    elif prior_tier != "basic" and prior_tier != tier and tier != "basic":
        try:
            await _send_email(
                user_id=user_id,
                template_slug="membership_upgraded",
                extra_vars={"new_plan_name": tier},
                dedup_window_hours=24,
            )
        except Exception as e:
            logger.warning("[billing] upgraded email failed for %s: %r", user_id, e)


async def _get_current_membership(user_id: str) -> dict:
    """Active membership snapshot {tier, stripe_subscription_id,
    cancel_at_period_end}; tier falls back to 'basic' like _get_current_tier."""
    try:
        rows = await supabase.select(
            "user_memberships",
            columns="tier,stripe_subscription_id,cancel_at_period_end",
            eq={"user_id": user_id, "is_active": True},
            limit=1,
        )
        if rows:
            row = dict(rows[0])
            row["tier"] = row.get("tier") or "basic"
            return row
    except Exception:
        pass
    return {"tier": "basic", "stripe_subscription_id": None, "cancel_at_period_end": None}


async def _get_current_tier(user_id: str) -> str:
    """Snapshot the user's currently-active membership tier so webhook
    handlers can tell "first subscription" vs "plan change". Falls back
    to 'basic' when there's no active row — same semantic the matching
    quota code uses."""
    try:
        # NB: SupabaseRest.select takes columns/eq/limit — an earlier
        # `params=` call raised TypeError here, which was swallowed and made
        # every user look like "basic" (welcome email on each renewal, no
        # upgrade/cancellation emails).
        rows = await supabase.select(
            "user_memberships",
            columns="tier",
            eq={"user_id": user_id, "is_active": True},
            limit=1,
        )
        if rows and rows[0].get("tier"):
            return rows[0]["tier"]
    except Exception:
        pass
    return "basic"


async def _handle_subscription_cancelled(sub: dict, *, send_email: bool = True) -> None:
    """The subscription has actually ENDED (customer.subscription.deleted,
    or status canceled / incomplete_expired): drop to basic and tell the
    member their paid access ended (X-EMAIL.6 premium_ended)."""
    user_id = _user_id_from_object(sub)
    if not user_id:
        return
    # Capture prior state BEFORE we flip them to basic — so the email
    # copy can reference what they had.
    prior = await _get_current_membership(user_id)
    prior_tier = prior["tier"]
    ended_sub = sub.get("id")
    current_sub = prior.get("stripe_subscription_id")
    if ended_sub and current_sub and current_sub != ended_sub:
        # A late "deleted" for an OLD subscription must not downgrade a
        # member who has since started a new one (H13.17).
        logger.info("[billing] ended subscription is not the member's current one; no downgrade")
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

    if send_email and prior_tier != "basic":
        await _send_premium_ended_once(user_id, ended_sub, prior_tier)


async def _send_premium_ended_once(user_id: str, sub_id: Optional[str], prior_tier: str) -> bool:
    """
    Scenario 22 (premium_ended): email + push (registry) exactly once per
    ended subscription. The claim is a synthetic stripe_events row keyed
    `premium_ended:<sub_id>` (primary key → unique), so webhook
    redeliveries, a canceled-status update followed by the deleted event,
    and the reconciliation job can't send it twice. Without a sub id the
    24h template dedup is the only guard.
    """
    if sub_id:
        try:
            if not await _record_event_or_skip(f"premium_ended:{sub_id}", "internal.premium_ended_email"):
                return False
        except Exception as e:
            logger.warning("[billing] premium_ended claim failed (%r); relying on template dedup", e)
    from transactional_email import send_transactional_email as _send_email
    try:
        await _send_email(
            user_id=user_id,
            template_slug="premium_ended",
            extra_vars={"prior_tier": prior_tier},
            dedup_window_hours=24,
        )
        return True
    except Exception as e:
        logger.warning("[billing] premium_ended email failed for %s: %r", user_id, e)
        return False


async def _handle_invoice(inv: dict, *, status: str) -> None:
    sub_id = inv.get("subscription")
    if not sub_id:
        return

    # Scenarios 19 (payment_successful) + 20 (payment_failed) of the
    # 2026-10-12 spec: EMAIL ONLY. We fire these from the Stripe
    # invoice.payment_succeeded / invoice.payment_failed events.
    # payment_* slugs are intentionally OMITTED from NOTIFICATION_REGISTRY
    # so no push fires. Billing emails are permanent records the user
    # should refer back to, not transient push alerts.
    try:
        # Look up the user_id for this subscription so we can email them.
        rows = await supabase.select(
            "user_memberships",
            columns="user_id",
            eq={"stripe_subscription_id": sub_id, "is_active": True},
            limit=1,
        )
        user_id = rows[0]["user_id"] if rows else None
        if user_id:
            from transactional_email import send_transactional_email as _send_email
            amount = (inv.get("amount_paid") or inv.get("amount_due") or 0) / 100.0
            currency = (inv.get("currency") or "usd").upper()
            template_slug = "payment_successful" if status == "paid" else "payment_failed"
            try:
                await _send_email(
                    user_id=user_id,
                    template_slug=template_slug,
                    extra_vars={
                        "amount": f"{amount:.2f}",
                        "currency": currency,
                        "invoice_url": inv.get("hosted_invoice_url") or "",
                    },
                    dedup_window_hours=0,  # each invoice is a distinct event
                )
            except Exception as e:
                logger.warning("[billing] %s email failed for %s: %r", template_slug, user_id, e)
    except Exception as e:
        logger.warning("[billing] invoice-side email lookup failed: %r", e)

    try:
        await supabase.update(
            "user_memberships",
            payload={"last_invoice_status": status, "updated_at": datetime.now(timezone.utc).isoformat()},
            eq={"stripe_subscription_id": sub_id, "is_active": True},
        )
    except Exception as e:
        logger.warning("Could not record invoice %s status: %s", inv.get("id"), e)


# -----------------------------------------------------------------------------
# H13.17 · Daily subscription reconciliation (ENABLE_STRIPE_RECONCILE)
# -----------------------------------------------------------------------------
def reconcile_enabled() -> bool:
    return (os.getenv("ENABLE_STRIPE_RECONCILE") or "").strip().lower() in ("1", "true", "yes", "on")


def _iso_to_epoch(v) -> Optional[int]:
    if not v:
        return None
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


async def reconcile_subscriptions() -> dict:
    """
    Compare every active membership that carries a stripe_subscription_id
    with the subscription's live state in Stripe and correct tier / expiry
    drift (missed or out-of-order webhooks). Idempotent: a row that already
    matches is left alone, and an ended subscription goes through the same
    path as the deleted webhook (premium_ended claimed once per sub).
    Logs counts only — no member ids or emails.
    """
    import asyncio

    counts = {"checked": 0, "ok": 0, "corrected": 0, "ended": 0, "skipped": 0, "errors": 0}
    try:
        rows = await supabase.select(
            "user_memberships",
            columns="user_id,tier,stripe_subscription_id,stripe_customer_id,current_period_end,cancel_at_period_end",
            eq={"is_active": True},
        )
    except Exception as e:
        logger.error("[billing-reconcile] membership listing failed: %r", e)
        counts["errors"] += 1
        return counts

    rows = [r for r in rows or [] if r.get("stripe_subscription_id")]
    if not rows:
        logger.info("[billing-reconcile] done %s", counts)
        return counts
    await refresh_price_to_tier_from_db()
    stripe = _get_stripe()

    for row in rows:
        counts["checked"] += 1
        sub_id = row["stripe_subscription_id"]
        try:
            sub = _plain(await asyncio.to_thread(stripe.Subscription.retrieve, sub_id))
            status = sub.get("status")
            md = dict(sub.get("metadata") or {})
            md["cheripic_user_id"] = row["user_id"]  # the row being reconciled
            sub["metadata"] = md
            if status in ("canceled", "incomplete_expired"):
                await _handle_subscription_cancelled(sub)
                counts["ended"] += 1
                continue
            items = (sub.get("items") or {}).get("data") or []
            price_id = items[0]["price"]["id"] if items else None
            tier = _tier_for_price(price_id)
            if not tier:
                counts["skipped"] += 1
                continue
            cps_dt, cpe_dt = _period_bounds(sub)
            want_end = int(cpe_dt.timestamp()) if cpe_dt else None
            have_end = _iso_to_epoch(row.get("current_period_end"))
            cape = bool(sub.get("cancel_at_period_end"))
            if tier == row.get("tier") and want_end == have_end and cape == bool(row.get("cancel_at_period_end")):
                counts["ok"] += 1
                continue
            await _upsert_active_membership(
                user_id=row["user_id"],
                tier=tier,
                stripe_customer_id=sub.get("customer") or row.get("stripe_customer_id"),
                stripe_subscription_id=sub_id,
                stripe_price_id=price_id,
                current_period_start=cps_dt,
                current_period_end=cpe_dt,
                cancel_at_period_end=cape,
                last_invoice_status=status,
            )
            counts["corrected"] += 1
        except Exception as e:
            if "No such subscription" in str(e) or getattr(e, "code", None) == "resource_missing":
                # Gone in Stripe → treat as ended.
                try:
                    await _handle_subscription_cancelled(
                        {"id": sub_id, "customer": row.get("stripe_customer_id"),
                         "metadata": {"cheripic_user_id": row["user_id"]}}
                    )
                    counts["ended"] += 1
                    continue
                except Exception:
                    pass
            counts["errors"] += 1
            logger.warning("[billing-reconcile] one subscription failed: %s", type(e).__name__)
    logger.info("[billing-reconcile] done %s", counts)
    return counts


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


async def try_reserve_message(user_id: str, period_start: str, limit: Optional[int]):
    """
    Atomically count one CheriAI message against the period BEFORE the LLM
    call (RPC public.reserve_cheri_message, migration 050 — NOT APPLIED).

    Returns the new count (int) when reserved, False when the cap is already
    reached, or None when the RPC isn't deployed / errored — the caller then
    uses the legacy check-then-increment path.
    """
    import httpx
    from supabase_client import SUPABASE_URL, SUPABASE_KEY

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/reserve_cheri_message",
                json={"p_user_id": user_id, "p_period_start": period_start, "p_limit": limit},
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                         "Content-Type": "application/json"},
            )
    except Exception as e:
        logger.warning("reserve_cheri_message unreachable for %s: %r", user_id, e)
        return None
    if r.status_code >= 300:
        if r.status_code != 404:
            logger.warning("reserve_cheri_message failed [%s]: %s", r.status_code, r.text[:200])
        return None
    try:
        val = r.json()
    except ValueError:
        return None
    if val is None:
        return False
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


async def release_message(user_id: str, period_start: str) -> None:
    """Give back a reservation when the LLM call failed (best-effort)."""
    import httpx
    from supabase_client import SUPABASE_URL, SUPABASE_KEY

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/release_cheri_message",
                json={"p_user_id": user_id, "p_period_start": period_start},
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                         "Content-Type": "application/json"},
            )
    except Exception as e:
        logger.warning("release_cheri_message failed for %s: %r", user_id, e)


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
