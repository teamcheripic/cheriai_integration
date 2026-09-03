"""
CheriAI long-term user memory.

What lives here (one row per user in cheri_ai_user_memory):
- summary   : 1–2 sentence portrait of the person Cheri is talking to
- insights  : small array of concrete facts she's learned
              (each: {"fact": "...", "ts": "ISO date"})

The chat endpoint:
1. Loads memory + last few messages BEFORE building the prompt
   → Cheri replies with full context
2. Fires update_memory_async AFTER the reply is sent
   → cheap second LLM call, non-blocking, only when the turn was substantive
"""

import json
import logging
from datetime import datetime
from typing import Any

from llm_client import send_to_llm
from supabase_client import supabase

logger = logging.getLogger(__name__)

MAX_INSIGHTS = 12          # keep memory small + cheap
RECENT_TURNS = 6           # last 6 messages in short-term context
MIN_MSG_CHARS = 25         # skip distillation for "ok" / "thanks" / etc.
DISTILL_MAX_TOKENS = 220   # ~2x typical reply — enough for JSON output


# --------------------------------------------------------------- READ helpers

async def load_memory(user_id: str) -> dict[str, Any]:
    """Returns {"summary": str, "insights": list, "turn_count": int}."""
    try:
        rows = await supabase.select(
            "cheri_ai_user_memory",
            columns="summary,insights,turn_count",
            eq={"user_id": user_id},
            limit=1,
        )
        if rows:
            row = rows[0]
            return {
                "summary": row.get("summary") or "",
                "insights": row.get("insights") or [],
                "turn_count": row.get("turn_count") or 0,
            }
    except Exception as e:
        logger.warning(f"[memory] load failed: {e}")
    return {"summary": "", "insights": [], "turn_count": 0}


async def load_recent_messages(user_id: str, limit: int = RECENT_TURNS) -> list[dict]:
    """Last N messages across all conversations, oldest-first."""
    try:
        rows = await supabase.select(
            "chat_history",
            columns="role,content,created_at",
            eq={"user_id": user_id},
            order="created_at.desc",
            limit=limit,
        )
        return list(reversed(rows))
    except Exception as e:
        logger.warning(f"[memory] history load failed: {e}")
        return []


# --------------------------------------------------------------- WRITE / distill

_DISTILL_SYSTEM = (
    "You maintain a private notebook on a user so a friend (Cheri) can talk to "
    "them with continuity. Output STRICT JSON only, no prose:\n"
    '{ "summary": "1–2 sentence portrait, updated", "new_insights": ["fact 1", "fact 2"] }\n'
    "Rules:\n"
    "- summary: revise the existing one if you learned something new; otherwise return it unchanged.\n"
    "- new_insights: 0–3 short concrete facts ONLY if they're durable (values, situation, preferences). "
    "  Skip small talk, greetings, and anything Cheri already knows.\n"
    "- Never include opinions, advice, or interpretations — only what the user revealed.\n"
    "- Keep each insight under 110 characters."
)


def _build_distill_prompt(
    existing_summary: str,
    existing_insights: list[dict],
    user_message: str,
    assistant_reply: str,
) -> str:
    known = "\n".join(f"- {i.get('fact', '')}" for i in existing_insights[-MAX_INSIGHTS:])
    return (
        f"Existing summary: {existing_summary or '(none yet)'}\n"
        f"Existing insights:\n{known or '(none yet)'}\n\n"
        f"Latest user message: {user_message}\n"
        f"Cheri's reply: {assistant_reply}\n\n"
        "Return updated JSON."
    )


def _merge_insights(existing: list[dict], new_facts: list[str]) -> list[dict]:
    """Append unique new facts; cap at MAX_INSIGHTS keeping the most recent."""
    seen = {(i.get("fact") or "").strip().lower() for i in existing}
    now = datetime.now().isoformat()
    out = list(existing)
    for fact in new_facts:
        f = (fact or "").strip()
        if f and f.lower() not in seen:
            out.append({"fact": f, "ts": now})
            seen.add(f.lower())
    return out[-MAX_INSIGHTS:]


# --------------------------------------------------------------- SEED from profile

_SEED_SYSTEM = (
    "You maintain a private notebook on a user so a friend (Cheri) can talk to "
    "them with continuity. You are being SEEDED with the user's freshly-filled "
    "profile fields. Output STRICT JSON only, no prose:\n"
    '{ "summary": "1–2 sentence portrait", "insights": ["fact 1", "fact 2", ...] }\n'
    "Rules:\n"
    "- summary: warm, third-person, factual. 1–2 sentences. Mention name, key "
    "  identity facts (age band, nationality/background, profession), and what "
    "  they say they want. Never invent — only use what's in the fields.\n"
    "- insights: 3–8 short concrete facts pulled from the profile. Each ≤110 chars. "
    "  Skip empty fields. No commentary or interpretation.\n"
    "- If almost every field is empty, return a minimal seed (short summary, 0-2 insights)."
)


def _build_seed_prompt(profile: dict[str, Any], bio: dict[str, Any]) -> str:
    """Serialize the user_profiles row + bio JSON into a compact bullet list."""
    def _pair(label: str, val: Any) -> str | None:
        if val is None:
            return None
        s = str(val).strip()
        return f"- {label}: {s}" if s else None

    lines: list[str] = []
    lines.append("Profile fields")
    for label, key in [
        ("Nick name", "nick_name"),
        ("Full name", "full_name"),
        ("Gender", "gender"),
        ("Date of birth", "date_of_birth"),
        ("Nationality", "nationality"),
        ("Country of residence", "country_of_residence"),
    ]:
        line = _pair(label, profile.get(key))
        if line:
            lines.append(line)

    lines.append("")
    lines.append("Snapshot Bio (JSON)")
    for label, key in [
        ("Height", "height"),
        ("Religion", "religion"),
        ("Background", "background"),
        ("Profession", "profession"),
        ("Intent", "intent"),
        ("About yourself", "about_yourself"),
        ("Lifestyle", "lifestyle"),
        ("What matters most", "what_matters_most"),
    ]:
        line = _pair(label, bio.get(key))
        if line:
            lines.append(line)

    body = "\n".join(lines)
    return f"{body}\n\nReturn the seeded JSON now."


def _parse_bio(bio_raw: Any) -> dict[str, Any]:
    """`user_profiles.bio` is stored as JSON text; be lenient."""
    if isinstance(bio_raw, dict):
        return bio_raw
    if isinstance(bio_raw, str) and bio_raw.strip():
        try:
            parsed = json.loads(bio_raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def seed_memory_from_profile(user_id: str) -> dict[str, Any]:
    """
    Distill the user's `user_profiles` row + Snapshot Bio into an initial
    Cheri memory row. Called at registration (or when the Snapshot Bio is
    edited) so Cheri opens the very first chat with real context instead
    of a blank slate.

    Idempotent — safe to call repeatedly; each run overwrites the row's
    summary + merges any brand-new insights. `turn_count` is preserved so
    the chat-side distillation logic still knows the age of the memory.

    Returns the persisted memory dict (or empty dict on any error — never
    raises, callers just log and move on).
    """
    try:
        # 1. Load the profile row. Only fields we can distill from.
        rows = await supabase.select(
            "user_profiles",
            columns="nick_name,full_name,gender,date_of_birth,nationality,country_of_residence,bio",
            eq={"user_id": user_id},
            limit=1,
        )
        if not rows:
            logger.info(f"[memory-seed] no profile row for {user_id}; skipping")
            return {}
        row = rows[0]
        bio = _parse_bio(row.get("bio"))

        # 2. Distill via LLM.
        current = await load_memory(user_id)
        prompt = _build_seed_prompt(row, bio)
        raw = await send_to_llm(
            (_SEED_SYSTEM, prompt),
            max_tokens=DISTILL_MAX_TOKENS,
            user=user_id,
        )

        # 3. Parse (lenient — model may wrap in prose despite instructions).
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            logger.info("[memory-seed] LLM returned no JSON; skipping upsert")
            return {}
        parsed = json.loads(text[start : end + 1])

        new_summary = (parsed.get("summary") or current["summary"] or "").strip()
        new_facts = parsed.get("insights") or []
        if not isinstance(new_facts, list):
            new_facts = []

        # 4. Merge — new facts join the existing insight pool (deduped).
        #    Preserves chat-derived insights already accumulated.
        merged = _merge_insights(current["insights"], [str(f) for f in new_facts])

        # Guard: don't persist an entirely-empty row. Previously we'd upsert
        # {summary: "", insights: []} whenever the LLM returned no useful
        # content (mock LLM, malformed JSON, empty profile) — which is what
        # made rows show up in the admin panel with zero content. Now we
        # refuse to write if we have nothing to offer AND the existing row
        # was already empty (nothing new to add). This preserves any prior
        # non-empty row untouched.
        has_summary = bool(new_summary.strip())
        has_new_insights = len(merged) > len(current["insights"])
        if not has_summary and not has_new_insights and not current["summary"] and not current["insights"]:
            logger.info(
                f"[memory-seed] nothing to persist for {user_id} — "
                "LLM returned empty, profile likely sparse or OPENAI_API_KEY is placeholder"
            )
            return {}

        persist = {
            "user_id": user_id,
            "summary": new_summary,
            "insights": merged,
            # Don't reset the turn counter — chat-side distillation uses it
            # to skip empty first-turn distills. Seeding is orthogonal.
            "turn_count": current["turn_count"] or 0,
            "updated_at": datetime.now().isoformat(),
        }
        await supabase.upsert(
            "cheri_ai_user_memory",
            persist,
            on_conflict="user_id",
        )
        logger.info(
            f"[memory-seed] seeded {user_id} (summary_len={len(new_summary)}, +{len(new_facts)} insights)"
        )
        return persist
    except Exception as e:
        logger.warning(f"[memory-seed] failed for {user_id}: {e}")
        return {}


# --------------------------------------------------------------- CHAT-TURN update

async def _touch_memory_row(user_id: str, current: dict[str, Any]) -> None:
    """
    Bump turn_count + updated_at even when distillation didn't produce new
    content. Gives the admin panel + diagnostics visibility that chat
    ACTIVITY happened, even if the LLM had nothing new to say about the
    user this turn (short reply, small talk, etc.).
    """
    try:
        await supabase.upsert(
            "cheri_ai_user_memory",
            {
                "user_id": user_id,
                "summary": current["summary"] or "",
                "insights": current["insights"] or [],
                "turn_count": (current["turn_count"] or 0) + 1,
                "updated_at": datetime.now().isoformat(),
            },
            on_conflict="user_id",
        )
    except Exception as e:
        logger.warning(f"[memory] activity-touch upsert failed for {user_id}: {e}")


async def update_memory_async(
    user_id: str,
    user_message: str,
    assistant_reply: str,
) -> None:
    """
    Fire-and-forget: distill what Cheri learned from this turn and merge into
    the user's memory row. Verbose logging on every step so a stalled memory
    trail can be diagnosed from the FastAPI console.

    Never raises (called via asyncio.create_task in main.py; an unhandled
    exception would be lost). All failure modes touch the row with a bumped
    turn_count so activity is still visible in the admin panel even when
    the distill produced no new content.
    """
    logger.info(
        f"[memory] update START user={user_id} msg_len={len(user_message.strip())} "
        f"reply_len={len(assistant_reply.strip())}"
    )
    # Outer try/except catches EVERYTHING — including load_memory() blowing
    # up on a Supabase auth error, which used to escape asyncio.create_task
    # silently. Anything raised inside gets logged with traceback below.
    try:
        if len(user_message.strip()) < MIN_MSG_CHARS:
            logger.info(
                f"[memory] skip distill for {user_id} — message under {MIN_MSG_CHARS} chars "
                "(still touching row for activity tracking)"
            )
            current_short = await load_memory(user_id)
            await _touch_memory_row(user_id, current_short)
            return

        current = await load_memory(user_id)
        logger.info(
            f"[memory] loaded existing row for {user_id}: "
            f"summary_len={len(current['summary'])} insights={len(current['insights'])} "
            f"turn={current['turn_count']}"
        )

        prompt = _build_distill_prompt(
            current["summary"], current["insights"], user_message, assistant_reply
        )
        raw = await send_to_llm(
            (_DISTILL_SYSTEM, prompt),
            max_tokens=DISTILL_MAX_TOKENS,
            user=user_id,
        )
        logger.info(f"[memory] LLM returned {len(raw)} chars for {user_id}")

        # Be lenient about model formatting
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            logger.warning(
                f"[memory] distill returned no JSON for {user_id}. "
                f"Raw response (first 300 chars): {text[:300]!r}"
            )
            await _touch_memory_row(user_id, current)
            return

        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as je:
            logger.warning(
                f"[memory] JSON decode failed for {user_id}: {je}. "
                f"Attempted: {text[start:end+1][:300]!r}"
            )
            await _touch_memory_row(user_id, current)
            return

        new_summary = (parsed.get("summary") or current["summary"] or "").strip()
        new_facts = parsed.get("new_insights") or []
        if not isinstance(new_facts, list):
            new_facts = []

        merged = _merge_insights(current["insights"], [str(f) for f in new_facts])

        persist = {
            "user_id": user_id,
            "summary": new_summary,
            "insights": merged,
            "turn_count": (current["turn_count"] or 0) + 1,
            "updated_at": datetime.now().isoformat(),
        }
        await supabase.upsert("cheri_ai_user_memory", persist, on_conflict="user_id")
        logger.info(
            f"[memory] UPDATED {user_id} — summary={len(new_summary)}chars "
            f"insights={len(merged)} (+{len(new_facts)} new) "
            f"turn={persist['turn_count']}"
        )
    except Exception as e:
        # never break the chat path — but log LOUDLY so a stuck memory
        # trail can be tracked back to its exception. exc_info=True
        # includes the traceback so upstream (Supabase 401/403/500) is
        # visible without needing to reproduce.
        logger.warning(f"[memory] update failed for {user_id}: {e}", exc_info=True)
        # Even on distill failure, bump the activity counter so admin sees
        # chat happened. Falls silently if the upsert itself is what broke.
        try:
            current_now = await load_memory(user_id)
            await _touch_memory_row(user_id, current_now)
        except Exception:
            pass
