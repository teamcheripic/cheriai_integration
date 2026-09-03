"""
Detects when the user is asking for gift / date / experience help, then pulls
relevant merchants and events from Supabase so Cheri can suggest CheriPic's
own deals instead of generic OpenAI ideas.

Kept dead simple on purpose: a keyword scan (fast, free) + a small SELECT.
No extra LLM call, no embeddings — for an MVP this gets ~95% of the lift.
"""

import logging
import re
from typing import Optional

from supabase_client import supabase

logger = logging.getLogger(__name__)

# Keyword → category cues used to filter the offers we surface to Cheri.
# Categories match the `merchants.category` text in the seed data.
_CUES: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\b(gifts?|presents?|surprises?|anniversar(?:y|ies)|birthdays?)\b", re.I),
     ["Gift Cards", "Fashion & Beauty", "Spa & Wellness"]),
    (re.compile(r"\b(date|dates|dinner|restaurants?|eat|food|brunch|lunch|cuisine|meal)\b", re.I),
     ["Dining", "Coffee & Desserts", "Food & Dining"]),
    (re.compile(r"\b(coffee|caf[eé]s?|desserts?)\b", re.I),
     ["Coffee & Desserts"]),
    (re.compile(r"\b(movies?|cinemas?|theatres?|theaters?|films?|show)\b", re.I),
     ["Movie Tickets", "Experiences"]),
    (re.compile(r"\b(spas?|massages?|relax|pamper|wellness)\b", re.I),
     ["Spa & Wellness"]),
    (re.compile(r"\b(trips?|travel|getaways?|vacations?|hotels?|stay|weekend away)\b", re.I),
     ["Travel Deals", "Experiences"]),
    (re.compile(r"\b(experiences?|activit(?:y|ies)|fun|adventures?|do\s+something|hang\s*out)\b", re.I),
     ["Experiences", "Travel Deals"]),
    (re.compile(r"\b(events?|festivals?|concerts?|celebrations?)\b", re.I),
     ["__events__"]),
    (re.compile(r"\b(romantic|romance|special|memorable|celebrate)\b", re.I),
     ["Spa & Wellness", "Dining", "Travel Deals"]),
    # Generic "let's do/spend something together" intent — broadest net,
    # offers a mix of categories so Cheri can pick the best fit.
    (re.compile(
        r"\b(place|places|spot|spots|venue|where\s+(can|to)\s+(we|i)\s+go|"
        r"go\s+(out|together)|spend\s+time|time\s+together|together)\b", re.I),
     ["Experiences", "Dining", "Travel Deals", "Spa & Wellness"]),
    # User explicitly asking for suggestions / alternatives ("any other", "more ideas", "what else")
    (re.compile(
        r"\b(any\s+(other|more)|other\s+(ideas?|options?)|what\s+else|more\s+(ideas?|options?|suggestions?)|"
        r"alternatives?|else|recommend|suggest|tip|tips)\b", re.I),
     ["__followup__"]),
]


def _intent_from_text(message: str) -> tuple[set[str], bool, bool]:
    """Returns (categories, wants_events, is_followup_ask)."""
    cats: set[str] = set()
    wants_events = False
    is_followup = False
    if not message:
        return cats, wants_events, is_followup
    for pattern, mapping in _CUES:
        if pattern.search(message):
            for c in mapping:
                if c == "__events__":
                    wants_events = True
                elif c == "__followup__":
                    is_followup = True
                else:
                    cats.add(c)
    return cats, wants_events, is_followup


def detect_intent(message: str) -> tuple[list[str], bool]:
    """Back-compat shim: returns (categories, wants_events)."""
    cats, wants_events, _ = _intent_from_text(message)
    return sorted(cats), wants_events


def _scan_recent_for_intent(recent_messages: list[dict]) -> tuple[set[str], bool]:
    """
    Look at the last few messages to carry forward an intent. If Cheri or the
    user already framed this thread as gift/date/travel/etc., a follow-up like
    "any other place we can spent both of us" should keep using that intent.
    """
    cats: set[str] = set()
    wants_events = False
    if not recent_messages:
        return cats, wants_events
    # Scan the last 4 messages, most recent first
    for m in reversed(recent_messages[-4:]):
        content = (m.get("content") or "") if isinstance(m, dict) else ""
        c, w, _ = _intent_from_text(content)
        cats |= c
        wants_events = wants_events or w
        if cats or wants_events:
            break  # nearest match wins
    return cats, wants_events


async def fetch_offers(
    country_code: Optional[str],
    message: str,
    merchant_limit: int = 4,
    event_limit: int = 2,
    recent_messages: Optional[list[dict]] = None,
) -> list[dict]:
    """
    Returns a small flat list of suggestion dicts:
      {kind: 'merchant'|'event', name, title, discount, category, location?}
    Empty list if no relevant intent or no data.
    """
    cats_set, wants_events, is_followup = _intent_from_text(message)

    # Carry-forward: if the current message has no concrete intent but is a
    # follow-up ask, or has no signal at all, borrow intent from recent context.
    if (is_followup or (not cats_set and not wants_events)) and recent_messages:
        carried_cats, carried_events = _scan_recent_for_intent(recent_messages)
        cats_set |= carried_cats
        wants_events = wants_events or carried_events

    if not cats_set and not wants_events:
        return []

    cats = sorted(cats_set)

    suggestions: list[dict] = []

    # --- Merchants ----------------------------------------------------
    if cats:
        try:
            # PostgREST: in.("a","b") for IN filter via custom params
            params_eq: dict[str, str | int] = {"is_active": True}
            rows = await supabase.select(
                "merchants",
                columns="name,title,description,discount,category,membership_tier,discounted_price,original_price",
                eq=params_eq,  # type: ignore[arg-type]
                limit=40,
            )
            # JS-side filter for category match (PostgREST `in` would need a tweak
            # to the helper; cheaper to filter here while the result set is small)
            cat_set = {c.lower() for c in cats}
            matched = [r for r in rows if (r.get("category") or "").lower() in cat_set]
            # Lowest membership tier first, biggest discount first
            tier_rank = {"basic": 0, "premium-lite": 1, "premium": 2}
            matched.sort(
                key=lambda r: (tier_rank.get(r.get("membership_tier") or "basic", 0),
                               -int(r.get("discount") or 0))
            )
            for r in matched[:merchant_limit]:
                suggestions.append({
                    "kind": "merchant",
                    "name": r.get("name", ""),
                    "title": r.get("title", ""),
                    "discount": r.get("discount") or 0,
                    "category": r.get("category", ""),
                    "tier": r.get("membership_tier", "basic"),
                })
        except Exception as e:
            logger.warning(f"[merchants_lookup] merchants fetch failed: {e}")

    # --- Events (filtered to user's country if known) ----------------
    if wants_events or cats:
        try:
            eq: dict[str, str | int] = {"is_active": True}
            if country_code:
                # events.location uses lowercased country codes like 'india', 'usa'
                eq["location"] = country_code.lower() if len(country_code) <= 3 else country_code
            rows = await supabase.select(
                "events",
                columns="title,description,location,date,category",
                eq=eq,  # type: ignore[arg-type]
                order="date.asc",
                limit=event_limit,
            )
            for r in rows[:event_limit]:
                suggestions.append({
                    "kind": "event",
                    "name": r.get("title", ""),
                    "title": r.get("title", ""),
                    "category": r.get("category", ""),
                    "location": r.get("location", ""),
                    "date": r.get("date", ""),
                })
        except Exception as e:
            logger.warning(f"[merchants_lookup] events fetch failed: {e}")

    logger.info(f"[merchants_lookup] offers={len(suggestions)} cats={cats} events={wants_events}")
    return suggestions
