"""
Shared membership writer for non-Stripe providers' NormalizedEvents.

Reuses billing.py's primitives so every provider gets the same guarantees
and emails as Stripe:
  • idempotency — claim "<provider>:<event_id>" in stripe_events (PK), and
    release it when processing fails so the provider's retry reprocesses;
  • out-of-order protection — the same "applied sub=<provider>:<sub>
    created=<epoch>" marker billing uses;
  • one user_memberships row (payment_provider + provider ids);
  • welcome_* / membership_upgraded / membership_cancelled / premium_ended /
    payment_successful / payment_failed emails.
Stripe itself does NOT go through here (billing.handle_event is richer).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import billing
from payments.base import (
    EVENT_PAYMENT_FAILED,
    EVENT_PAYMENT_SUCCEEDED,
    EVENT_SUBSCRIPTION_ACTIVE,
    EVENT_SUBSCRIPTION_CANCELLED,
    NormalizedEvent,
)
from supabase_client import supabase

logger = logging.getLogger(__name__)


async def _member_by_subscription(provider: str, sub_id: str) -> Optional[Dict[str, Any]]:
    try:
        rows = await supabase.select(
            "user_memberships",
            columns="user_id,tier,provider_subscription_id,cancel_at_period_end",
            eq={"payment_provider": provider, "provider_subscription_id": sub_id, "is_active": True},
            limit=1,
        )
        return rows[0] if rows else None
    except Exception as e:
        logger.warning("[payments] member lookup by subscription failed: %r", e)
        return None


async def _current(user_id: str) -> Dict[str, Any]:
    try:
        rows = await supabase.select(
            "user_memberships",
            columns="tier,payment_provider,provider_subscription_id,cancel_at_period_end",
            eq={"user_id": user_id, "is_active": True},
            limit=1,
        )
        if rows:
            row = dict(rows[0])
            row["tier"] = row.get("tier") or "basic"
            return row
    except Exception:
        pass
    return {"tier": "basic", "payment_provider": None, "provider_subscription_id": None, "cancel_at_period_end": None}


async def apply_normalized_event(ev: NormalizedEvent) -> Dict[str, Any]:
    claim = f"{ev.provider}:{ev.event_id}"
    if not await billing._record_event_or_skip(claim, f"{ev.provider}.{ev.kind}"):
        return {"provider": ev.provider, "event_id": ev.event_id, "status": "duplicate"}

    sub_ref = f"{ev.provider}:{ev.subscription_id}" if ev.subscription_id else None
    ordered = ev.kind in (EVENT_SUBSCRIPTION_ACTIVE, EVENT_SUBSCRIPTION_CANCELLED)
    stale = False
    if ordered and sub_ref and ev.created is not None:
        last = await billing._last_applied_event_created(sub_ref)
        stale = last is not None and ev.created < last

    handled = False
    failed = False
    try:
        if stale:
            logger.info("[payments] ignoring out-of-order %s event %s", ev.provider, ev.event_id)
        elif ev.kind == EVENT_SUBSCRIPTION_ACTIVE:
            handled = await _subscription_active(ev)
        elif ev.kind == EVENT_SUBSCRIPTION_CANCELLED:
            handled = await _subscription_cancelled(ev)
        elif ev.kind in (EVENT_PAYMENT_SUCCEEDED, EVENT_PAYMENT_FAILED):
            handled = await _payment(ev, paid=ev.kind == EVENT_PAYMENT_SUCCEEDED)
    except Exception:
        failed = True
        try:
            await supabase.delete("stripe_events", eq={"event_id": claim})
        except Exception as e:
            logger.error("[payments] could not release claim %s: %r", claim, e)
        raise
    finally:
        if not failed:
            if stale:
                notes = "stale: older than last applied event"
            elif handled and ordered and sub_ref and ev.created is not None:
                notes = f"{billing._APPLIED_PREFIX}{sub_ref} created={ev.created}"
            else:
                notes = None if handled else "unhandled event"
            try:
                await supabase.update(
                    "stripe_events",
                    payload={"processed_at": datetime.now(timezone.utc).isoformat(), "notes": notes},
                    eq={"event_id": claim},
                )
            except Exception:
                pass

    out: Dict[str, Any] = {"provider": ev.provider, "event_id": ev.event_id, "kind": ev.kind, "handled": handled}
    if stale:
        out["status"] = "stale"
    return out


async def _resolve_user(ev: NormalizedEvent) -> Optional[str]:
    if ev.user_id:
        return ev.user_id
    if ev.subscription_id:
        row = await _member_by_subscription(ev.provider, ev.subscription_id)
        if row:
            return row.get("user_id")
    return None


async def _subscription_active(ev: NormalizedEvent) -> bool:
    user_id = await _resolve_user(ev)
    if not user_id or not ev.tier:
        logger.warning("[payments] %s subscription event without user/tier; skipping", ev.provider)
        return False
    prior = await _current(user_id)
    await billing._upsert_active_membership(
        user_id=user_id,
        tier=ev.tier,
        current_period_start=ev.period_start,
        current_period_end=ev.period_end,
        cancel_at_period_end=bool(ev.cancel_at_period_end),
        last_invoice_status=ev.status,
        payment_provider=ev.provider,
        provider_customer_id=ev.customer_id,
        provider_subscription_id=ev.subscription_id,
    )
    await billing._send_membership_change_email(user_id, prior["tier"], ev.tier)
    if ev.cancel_at_period_end and not prior.get("cancel_at_period_end") and ev.tier != "basic":
        from transactional_email import send_transactional_email as _send_email

        try:
            await _send_email(user_id=user_id, template_slug="membership_cancelled",
                              extra_vars={"prior_tier": ev.tier}, dedup_window_hours=24)
        except Exception as e:
            logger.warning("[payments] cancellation email failed: %r", e)
    return True


async def _subscription_cancelled(ev: NormalizedEvent) -> bool:
    user_id = await _resolve_user(ev)
    if not user_id:
        return False
    prior = await _current(user_id)
    current_sub = prior.get("provider_subscription_id")
    if ev.subscription_id and current_sub and current_sub != ev.subscription_id:
        logger.info("[payments] ended subscription is not the member's current one; no downgrade")
        return True
    await billing._upsert_active_membership(
        user_id=user_id,
        tier="basic",
        cancel_at_period_end=False,
        last_invoice_status="canceled",
        payment_provider=ev.provider,
        provider_customer_id=ev.customer_id,
    )
    if prior["tier"] != "basic":
        await billing._send_premium_ended_once(
            user_id, f"{ev.provider}:{ev.subscription_id}" if ev.subscription_id else None, prior["tier"]
        )
    return True


async def _payment(ev: NormalizedEvent, *, paid: bool) -> bool:
    if not ev.subscription_id:
        return False
    row = await _member_by_subscription(ev.provider, ev.subscription_id)
    user_id = (row or {}).get("user_id") or ev.user_id
    if user_id:
        from transactional_email import send_transactional_email as _send_email

        slug = "payment_successful" if paid else "payment_failed"
        try:
            await _send_email(
                user_id=user_id,
                template_slug=slug,
                extra_vars={
                    "amount": f"{(ev.amount or 0):.2f}",
                    "currency": (ev.currency or "usd").upper(),
                    "invoice_url": ev.invoice_url or "",
                },
                dedup_window_hours=0,
            )
        except Exception as e:
            logger.warning("[payments] %s email failed: %r", slug, e)
    try:
        await supabase.update(
            "user_memberships",
            payload={"last_invoice_status": "paid" if paid else "failed",
                     "updated_at": datetime.now(timezone.utc).isoformat()},
            eq={"payment_provider": ev.provider, "provider_subscription_id": ev.subscription_id, "is_active": True},
        )
    except Exception as e:
        logger.warning("[payments] could not record payment status: %r", e)
    return True
