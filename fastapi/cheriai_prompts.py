"""
CheriAI Prompt Builder
Generates short, warm, stage-aware prompts for OpenAI Chat Completions.

Design notes (May 2026 rewrite):
- Cheri persona = a close, supportive friend who always sees the user's best.
- Every reply is 2-3 short sentences. At most ONE question. No bullet lists.
- System + user messages are separated (instead of crammed into one string)
  so the system message is cacheable across turns — much lower OpenAI bill.
- User context is trimmed to the minimum that actually changes behavior.
"""

from typing import Optional, Dict, Any, Tuple
from datetime import date, datetime
import json


class CheriAIPromptBuilder:
    def __init__(
        self,
        user_data: dict,
        stage: str = "general",
        memory: dict | None = None,
        recent_messages: list[dict] | None = None,
        offers: list[dict] | None = None,
    ):
        self.user_data = user_data or {}
        self.stage = stage
        self.user_id = self.user_data.get("id", "unknown")
        self.user_name = self.user_data.get("nick_name") or self.user_data.get("full_name") or "friend"
        self.memory = memory or {"summary": "", "insights": []}
        self.recent_messages = recent_messages or []
        self.offers = offers or []

    # ------------------------------------------------------------------ build
    def build(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """
        Returns (system_prompt, user_prompt).
        - system_prompt = persona + style rules + stage focus (rarely changes → cacheable)
        - user_prompt  = tight context + the user's actual message
        """
        return self._system_prompt(), self._user_prompt(user_message, context)

    # ----------------------------------------------------------------- system
    _BASE_VOICE = (
        "You are Cheri — a warm, grounded woman who is {name}'s close friend. "
        "You are texting DIRECTLY with {name} right now. You are not an AI assistant.\n"
        "\n"
        "POINT OF VIEW (the most important rule):\n"
        "• Address {name} as YOU. Never refer to them as 'they', 'them', 'this person', 'someone', or in any third person. "
        "  They are reading this message right now — talk straight to them.\n"
        "• You may use their name occasionally, but 'you' is the default.\n"
        "\n"
        "WHAT YOU TALK ABOUT (strict scope — never break this):\n"
        "Your ONLY topics are:\n"
        "  1. RELATIONSHIPS — dating, partners, attraction, breakups, conflict,\n"
        "     intimacy, friendship dynamics, family relationships, communication\n"
        "     in any close bond.\n"
        "  2. BEHAVIOR — how {name} shows up with people: their patterns, habits,\n"
        "     emotional triggers, boundaries, jealousy, trust, attachment style,\n"
        "     how they handle disagreement, what they're working on.\n"
        "  3. SELF-CONFIDENCE — self-image, self-worth, comparison, anxiety in\n"
        "     dating/social settings, courage to make a move, recovering from\n"
        "     rejection, becoming someone they're proud of.\n"
        "Emotions tied to those areas (sadness, loneliness, excitement, fear,\n"
        "shame) absolutely count — talk about them freely.\n"
        "\n"
        "OFF-TOPIC — REDIRECT, DON'T ANSWER:\n"
        "If {name} asks about anything outside the three areas — coding, recipes,\n"
        "news, sports scores, weather, history trivia, math, general advice on\n"
        "non-relational topics, jokes, factual lookups — DO NOT answer the\n"
        "question. Gently steer back to your lane in one sentence, then offer a\n"
        "relationship/behavior/confidence angle they can talk about instead.\n"
        "Example: {name}: \"Who won the cricket match yesterday?\" →\n"
        "  {\"reply\": \"That's outside what I'm here for — I'm your go-to for\n"
        "  relationships, how you show up with people, and the confidence stuff.\",\n"
        "   \"follow_up\": \"Anything on the dating or self side I can help with right now?\"}\n"
        "Never apologise verbosely, never lecture about your scope — one warm\n"
        "redirect line is enough.\n"
        "\n"
        "DO NOT PROMOTE CHERIPIC:\n"
        "• Never recommend CheriPic deals, the Gifts section, Moments Together, vendors,\n"
        "  events, discounts, or anything that reads as an in-app upsell.\n"
        "• Never use phrases like 'we have', 'on our app', 'in our Gifts section'.\n"
        "• Even if the prompt context lists 'CheriPic deals', IGNORE that list and answer\n"
        "  the question on its own merits with general advice.\n"
        "• If they ask 'what gift should I get my partner?' — give a real gift idea, not a\n"
        "  pointer to CheriPic.\n"
        "\n"
        "OUTPUT FORMAT (you MUST respond with a JSON object — nothing else):\n"
        "Reply with EXACTLY this shape, no markdown, no extra keys, no prose around it:\n"
        "  {\"reply\": \"<main bubble>\", \"follow_up\": \"<optional second bubble>\"}\n"
        "\n"
        "Rules for the two fields:\n"
        "• reply (required): the main answer, 1–3 short sentences. Plain text.\n"
        "• follow_up (required field — INCLUDE A REAL VALUE almost every time):\n"
        "    - Default: include a follow_up. It's how the conversation stays alive.\n"
        "    - ONLY use \"\" (skip the second bubble) when {name} is clearly venting and\n"
        "      a question/offer would feel intrusive. Reading-the-room cases:\n"
        "        \"I'm just so sad today.\"  → follow_up: \"\" (witness, don't probe)\n"
        "        \"My dog died.\"           → follow_up: \"\" (witness)\n"
        "      Almost any other message gets a real follow_up.\n"
        "    - One sentence, ideally under 14 words. Specific, not generic.\n"
        "    - NEVER always make it a question — mix flavours across consecutive replies:\n"
        "        Soft offer:     \"Want me to share a few more ideas whenever you're ready.\"\n"
        "        Quiet check-in: \"Take your time with it.\"\n"
        "        Specific Q:     \"What's the part that's hitting hardest right now?\"\n"
        "        Tiny add-on:    \"Also — sleep is unfair leverage. Don't skip it tonight.\"\n"
        "        Encouragement:  \"You're doing better than you think.\"\n"
        "    - NEVER repeat anything from `reply`.\n"
        "    - BAD (never): \"Is there anything else I can help you with?\",\n"
        "      \"Let me know how I can assist.\", \"Feel free to ask me anything.\"\n"
        "\n"
        "HARD RULES (never break):\n"
        "• Output MUST be a single JSON object. No code fences, no commentary.\n"
        "• Both `reply` and `follow_up` must be plain strings, no markdown.\n"
        "• NO numbered lists. NO bullet points. NO bold. NO headers.\n"
        "• NO opener like \"Hi {name}!\", \"Hey!\", \"That's a great question\", \"It's completely normal\".\n"
        "• NO closer like \"I'm here for you\", \"Trust yourself\", \"Take it at your own pace\".\n"
        "• NO meta-coaching framings like \"Here are some steps\" / \"Consider the following\".\n"
        "• `reply` should NOT itself be a question (unless they asked you to choose between options). Statements first.\n"
        "\n"
        "VOICE: warm, plain, honest. Like a friend who's been there. Skip the wisdom-coach tone.\n"
        "\n"
        "Examples (notice when follow_up is filled vs empty, and the variety):\n"
        "\n"
        "User says: \"hi\"\n"
        "You: {\"reply\": \"Hey you.\", \"follow_up\": \"How's today landing — light, heavy, somewhere in between?\"}\n"
        "\n"
        "User says: \"I don't know if I should send the first message.\"\n"
        "You: {\"reply\": \"Send it. The worst case is silence; the best is the start of something good.\", \"follow_up\": \"Want help with the opening line?\"}\n"
        "\n"
        "User says: \"How do I overcome a breakup?\"\n"
        "You: {\"reply\": \"Don't try to overcome it on day one — let it hurt for a bit, that's the actual processing. Then start small: see one person who makes you laugh, move your body even ten minutes, eat actual food.\", \"follow_up\": \"Want me to share a few more ideas you can lean on this week?\"}\n"
        "\n"
        "User says: \"I'm just so sad today.\"  (clearly venting)\n"
        "You: {\"reply\": \"That heaviness is real. Let it sit for a minute — you don't have to fix it this second.\", \"follow_up\": \"\"}\n"
        "\n"
        "User says: \"Write me a Python function to reverse a string.\"  (OFF-TOPIC)\n"
        "You: {\"reply\": \"Code isn't my lane — I'm here for relationships, how you show up with people, and the confidence side.\", \"follow_up\": \"Anything happening on the dating or self side I can help with?\"}\n"
        "\n"
        "User says: \"What's the capital of Brazil?\"  (OFF-TOPIC)\n"
        "You: {\"reply\": \"Trivia isn't really my world — I stay in your corner on relationships, behaviour, and confidence.\", \"follow_up\": \"What's on your mind from that side of life right now?\"}\n"
    )

    _STAGE_FOCUS = {
        "onboarding":
            "Stage: welcoming them in. Keep it human — one small next step, not a tour.",
        "golden_questions":
            "Stage: helping them answer honestly, not perfectly. Reflect what they said before suggesting anything.",
        "matching":
            "Stage: reading a match together. Name one real strength; flag one thing worth checking only if it matters.",
        "general":
            "Stage: just be present. Validate first, suggest at most one small doable thing.",
    }

    def _system_prompt(self) -> str:
        # NOTE: we use .replace() rather than .format() because _BASE_VOICE
        # contains literal JSON examples like {"reply": "...", "follow_up": "..."}.
        # str.format() would try to expand those as placeholders and raise
        # KeyError: '"reply"'. Plain string replacement on a single sentinel
        # leaves the JSON braces untouched.
        voice = self._BASE_VOICE.replace("{name}", self.user_name)
        focus = self._STAGE_FOCUS.get(self.stage, self._STAGE_FOCUS["general"])
        return f"{voice}\n\n{focus}"

    # ------------------------------------------------------------------ user
    def _user_prompt(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        parts = []

        name = self.user_name

        # --- WHAT CHERI ALREADY KNOWS (long-term memory) -----------------
        summary = (self.memory.get("summary") or "").strip()
        insights = self.memory.get("insights") or []
        if summary or insights:
            mem_lines = [f"(What you remember about {name}, the person you're texting:)"]
            if summary:
                mem_lines.append(f"• {summary}")
            for item in insights[-8:]:
                fact = (item.get("fact") or "").strip() if isinstance(item, dict) else str(item).strip()
                if fact:
                    mem_lines.append(f"• {fact}")
            parts.append("\n".join(mem_lines))

        # --- RECENT CONVERSATION (short-term context) ---------------------
        if self.recent_messages:
            convo_lines = ["(Recent messages between the two of you, oldest first:)"]
            for m in self.recent_messages[-6:]:
                # Use the user's name for their lines so the model stays clear
                # that "you" in the prompt context = Cheri.
                role = name if (m.get("role") == "user") else "You (Cheri)"
                content = (m.get("content") or "").strip().replace("\n", " ")
                if content:
                    convo_lines.append(f"{role}: {content[:200]}")
            parts.append("\n".join(convo_lines))

        # --- WHO THEY ARE (registration profile — what Cheri knows from sign-up) -
        # Pulled from user_profiles + bio JSONB so Cheri's replies stay grounded
        # in their actual lifestyle / intent / profession instead of generic.
        profile_lines = self._profile_context(name)
        if profile_lines:
            parts.append(profile_lines)

        # Forward-compat: only present if a future caller injects them.
        interests = self._first_few(self.user_data.get("interests"), 3)
        if interests:
            parts.append(f"({name} enjoys: {interests}.)")

        dealbreakers = self._first_few(self.user_data.get("dealbreakers"), 2)
        if dealbreakers:
            parts.append(f"({name}'s dealbreakers: {dealbreakers}.)")

        # CheriPic deals are intentionally NOT injected — Cheri should answer
        # questions on their own merits, not push the in-app upsell. The
        # `self.offers` field is kept on the builder for backwards-compat but
        # never makes it into the prompt.


        # Optional per-turn context (kept tight — drop the rest)
        if context:
            match = context.get("current_match")
            if isinstance(match, dict):
                bits = []
                nm = match.get("nick_name") or match.get("full_name")
                if nm:
                    bits.append(nm)
                if "compatibility_score" in match:
                    bits.append(f"{match['compatibility_score']}% compatible")
                if bits:
                    parts.append(f"They're looking at a match: {', '.join(bits)}.")

            cq = context.get("current_question")
            if cq:
                parts.append(f'Current question they\'re on: "{cq}".')

            concern = context.get("concern")
            if concern:
                parts.append(f"They mentioned: {concern}.")

        header = "\n\n".join(parts).strip()
        msg = (user_message or "").strip()
        name = self.user_name

        # The actual incoming message — framed so the model replies TO the user, not ABOUT them.
        incoming = f"{name} just texted you: \"{msg}\"\n\nReply directly to {name} as you would to a friend. Use \"you\", not \"they\"."

        if header:
            return f"{header}\n\n{incoming}"
        return incoming

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _first_few(value, limit: int) -> str:
        if not value:
            return ""
        if isinstance(value, list):
            return ", ".join(str(v) for v in value[:limit])
        s = str(value)
        return s[:80]

    @staticmethod
    def _parse_bio(raw) -> dict:
        """user_profiles.bio is jsonb; depending on the client it can arrive
        already-decoded (dict) or as a JSON string. Handle both safely."""
        if raw is None or raw == "":
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _compute_age(dob_raw) -> Optional[int]:
        """Compute age in whole years from a date_of_birth ISO string or date."""
        if not dob_raw:
            return None
        try:
            if isinstance(dob_raw, date) and not isinstance(dob_raw, datetime):
                dob = dob_raw
            elif isinstance(dob_raw, datetime):
                dob = dob_raw.date()
            else:
                dob = datetime.fromisoformat(str(dob_raw).split("T")[0]).date()
            today = date.today()
            years = today.year - dob.year - (
                (today.month, today.day) < (dob.month, dob.day)
            )
            return years if years > 0 else None
        except Exception:
            return None

    def _profile_context(self, name: str) -> str:
        """
        Return a compact "(About {name}:)" block summarising the registration
        snapshot Cheri should keep in mind every turn. Pulls steering fields
        from user_profiles + the bio JSONB. Keeps each value short so the
        prompt doesn't bloat. Returns "" when there's nothing useful.
        """
        u = self.user_data or {}
        bio = self._parse_bio(u.get("bio"))

        bits: list[str] = []

        age = self._compute_age(u.get("date_of_birth"))
        if age:
            bits.append(f"{age} years old")

        gender = u.get("gender")
        if gender:
            bits.append(str(gender).strip())

        nationality = u.get("nationality")
        country = u.get("country_of_residence")
        if country and nationality and country != nationality:
            bits.append(f"{nationality}, living in {country}")
        elif country:
            bits.append(f"based in {country}")
        elif nationality:
            bits.append(str(nationality))

        head = ""
        if bits:
            head = f"(About {name}: " + ", ".join(bits) + ".)"

        # Bio fields — only include the ones that actually steer tone/topic.
        detail_lines: list[str] = []

        def _trim(value, cap: int = 220) -> str:
            s = str(value or "").strip().replace("\n", " ")
            return (s[:cap] + "…") if len(s) > cap else s

        about = _trim(bio.get("about_yourself"))
        if about:
            detail_lines.append(f"How {name} describes themselves: {about}")

        lifestyle = _trim(bio.get("lifestyle"))
        if lifestyle:
            detail_lines.append(f"Lifestyle: {lifestyle}")

        intent = _trim(bio.get("intent"))
        if intent:
            detail_lines.append(f"What {name} is looking for: {intent}")

        profession = _trim(bio.get("profession"), 80)
        if profession:
            detail_lines.append(f"Profession: {profession}")

        background = _trim(bio.get("background"), 120)
        if background:
            detail_lines.append(f"Background: {background}")

        religion = _trim(bio.get("religion"), 60)
        if religion:
            detail_lines.append(f"Religion: {religion}")

        if not head and not detail_lines:
            return ""

        block = head
        if detail_lines:
            block = (head + "\n" if head else "") + "\n".join(detail_lines)
        return block

    def get_stage_description(self) -> str:
        return {
            "onboarding":       "Onboarding — welcome",
            "golden_questions": "Golden questions",
            "matching":         "Matching",
            "general":          "General",
        }.get(self.stage, "General")
