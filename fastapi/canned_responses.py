"""
canned_responses.py — cheap intent router for short pleasantry messages.

Why this exists:
    Every "hi" / "thanks" / "how are you" that reaches /chat used to hit
    OpenAI, burning tokens and money for what is really a fixed reply.
    This module classifies short messages BEFORE the LLM call and, when
    it matches a known pleasantry, returns a hand-written Cheri-voice
    response — zero tokens spent, zero latency waiting on OpenAI, zero
    quota consumed.

Design rules the classifier follows on purpose:
    1. NEVER swallow a real relationship question. If a message has more
       than 6 words OR is longer than 60 characters, we always fall
       through to the LLM — "hi how do I know if he still loves me" is a
       real question, not a greeting.
    2. Match on the NORMALISED whole message, not a substring. "hi" is a
       greeting; "hi darling I need help" is not.
    3. Categories are conservative. When in doubt → LLM. False negatives
       (LLM handles a greeting) are fine; false positives (canned reply
       to a real question) are harmful.

Adding categories:
    Extend PATTERNS with a new (category, phrases) pair, then add a
    matching entry to _POOLS with reply variants. Variants can use
    {name} which is substituted from user_profiles.nick_name /
    full_name (falls back to "there").
"""

from __future__ import annotations

import random
import re
from typing import Optional

# --- Normalisation ---------------------------------------------------------
# Turn a raw user message into a comparable form: lowercase, trimmed,
# trailing punctuation removed, run of internal whitespace collapsed.
_TRAILING_PUNCT_RE = re.compile(r"[\s\.,!?…\"'\)\]]+$")
_LEADING_PUNCT_RE = re.compile(r"^[\s\.,!?…\"'\(\[]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    t = text.strip().lower()
    t = _TRAILING_PUNCT_RE.sub("", t)
    t = _LEADING_PUNCT_RE.sub("", t)
    t = _WHITESPACE_RE.sub(" ", t)
    return t


# --- Category → phrase table ----------------------------------------------
# The classifier matches the normalised message against these EXACT
# strings (whole-message equality). Keep phrases short and canonical;
# variations like extra letters ("hii", "heyyy") are normalised by the
# _repeat_char pass below.

PATTERNS: dict[str, set[str]] = {
    "greeting": {
        "hi", "hello", "hey", "hola", "namaste", "namaskar",
        "sup", "yo", "hi there", "hello there", "hey there",
        "gm", "good morning", "good afternoon", "good evening",
        "ge", "howdy",
    },
    "farewell": {
        "bye", "goodbye", "gn", "good night", "goodnight", "night",
        "cya", "see you", "see ya", "see u", "later", "ttyl",
        "catch you later", "take care", "cheers",
    },
    "gratitude": {
        "thanks", "thank you", "ty", "thx", "tysm", "tnx", "thankyou",
        "thanks a lot", "thanks so much", "many thanks", "much appreciated",
    },
    "wellbeing": {
        "how are you", "how are u", "how r u", "how you doing",
        "how are you doing", "hru", "hows it going", "how's it going",
        "whats up", "what's up", "wassup", "wyd",
    },
    "identity": {
        "who are you", "what is your name", "whats your name",
        "what's your name", "who is cheri", "who is cheripic",
        "are you real", "are you human", "are you a bot", "are you ai",
        "are you an ai", "what are you", "are u human", "are u a bot",
    },
    "capability": {
        "help", "help me", "what can you do", "what do you do",
        "how does this work", "how do you work", "what is this",
        "what is cheripic", "what is cheri", "what can u do",
    },
    "affirmation": {
        "ok", "okay", "k", "kk", "cool", "sure", "yes", "yeah", "yep",
        "ya", "yup", "no", "nope", "nah", "nice", "great", "awesome",
        "alright", "fine", "got it", "makes sense",
    },
}

# Word / char caps — messages larger than this always fall through to
# the LLM, even if they LOOK like a greeting. That protects
# "hi actually I wanted to ask..." from getting a canned reply.
MAX_WORDS_FOR_CANNED = 6
MAX_CHARS_FOR_CANNED = 60

# --- Emoji-only messages ---------------------------------------------------
# Regex identifying strings that contain nothing but symbols / emoji /
# whitespace. Not perfect, but catches the common "🥰" / "❤️❤️❤️" cases.
_EMOJI_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def _repeat_char_normalise(text: str) -> str:
    """
    Trim TRAILING repeated letters down to a single letter so "hii",
    "heyyy", "hellooo" match "hi" / "hey" / "hello". We only touch the
    trailing run — never the middle — so "cool" and "tell" stay intact.
    """
    return re.sub(r"([a-z])\1+$", r"\1", text)


def classify(message: str) -> Optional[str]:
    """
    Return a canned-response category name, or None to fall through to
    the LLM. Fast — a handful of dict lookups, no I/O.
    """
    if not message:
        return None
    raw = message.strip()
    if not raw:
        return None

    # Emoji / symbol-only: match early so the length guard below doesn't
    # dismiss it as "too short with punctuation".
    if _EMOJI_ONLY_RE.fullmatch(raw) and len(raw) <= 8:
        return "emoji_only"

    norm = _normalize(raw)
    if not norm:
        return None

    # Length guard — real questions always hit the LLM.
    if len(norm) > MAX_CHARS_FOR_CANNED:
        return None
    if len(norm.split()) > MAX_WORDS_FOR_CANNED:
        return None

    norm_collapsed = _repeat_char_normalise(norm)

    for category, phrases in PATTERNS.items():
        if norm in phrases or norm_collapsed in phrases:
            return category

    return None


# --- Response pool ---------------------------------------------------------
# Each category has a small list of reply variants; a random one is
# picked per call so the same greeting doesn't get the same reply five
# times in a row. Every variant is a {reply, follow_up?} pair matching
# the shape the LLM returns, so downstream code (bubble rendering, chat
# history persistence, memory distillation opt-out) is unchanged.

_POOLS: dict[str, list[dict[str, str]]] = {
    "greeting": [
        {
            "reply": "Hey {name}! 💜",
            "follow_up": "What's on your mind today — a person, a feeling, a decision?",
        },
        {
            "reply": "Hi {name} — I'm right here whenever you're ready.",
            "follow_up": "Anything about connection, dating, or someone you'd like to unpack?",
        },
        {
            "reply": "Hello 👋",
            "follow_up": "Is there something on your heart I can help you think through?",
        },
    ],
    "farewell": [
        {"reply": "Take care, {name}. I'll be here whenever you want to talk 💜"},
        {"reply": "Bye for now — come back any time you want to think something through."},
        {"reply": "Catch you later, {name}. Rest well."},
    ],
    "gratitude": [
        {"reply": "Anytime 💜 That's what I'm here for."},
        {"reply": "Of course. Anything else on your mind?"},
        {"reply": "Happy to help, {name}."},
    ],
    "wellbeing": [
        {
            "reply": "I'm here and steady — always ready for you.",
            "follow_up": "But the question I care about: how are YOU today, {name}?",
        },
        {
            "reply": "Doing well, thanks for asking 💜",
            "follow_up": "What's actually on your mind right now?",
        },
    ],
    "identity": [
        {
            "reply": "I'm Cheri — your CheriPic companion. I help you reflect on connection, dating, and what matters most to you.",
            "follow_up": "Anything specific you'd like to talk through?",
        },
        {
            "reply": "I'm Cheri — think of me as a thoughtful coach for the questions we don't always say out loud.",
        },
    ],
    "capability": [
        {
            "reply": "I can help you reflect on relationships, unpack a tricky conversation, or think through what you actually want in a partner.",
            "follow_up": "What's on your mind right now, {name}?",
        },
        {
            "reply": "Ask me anything about dating, connection, communication, or what to say in a specific situation — I'll take it seriously.",
        },
    ],
    "affirmation": [
        {"reply": "Got it 💜"},
        {"reply": "Want to keep going, {name}? I'm here."},
        {"reply": "Anything else you'd like to explore?"},
    ],
    "emoji_only": [
        {"reply": "Say more? I want to hear what's behind that 💜"},
        {"reply": "Tell me more, {name}."},
    ],
}


def respond(category: str, name: Optional[str] = None) -> tuple[str, Optional[str]]:
    """
    Pick a variant for the given category and personalise it. Returns
    (reply, follow_up) matching the shape the LLM path returns so the
    caller doesn't branch on where the answer came from.
    """
    n = (name or "there").strip() or "there"
    pool = _POOLS.get(category)
    if not pool:
        return ("I'm here 💜", None)
    variant = random.choice(pool)
    reply = (variant.get("reply") or "").format(name=n)
    follow_up_tpl = variant.get("follow_up")
    follow_up = follow_up_tpl.format(name=n) if follow_up_tpl else None
    return reply, follow_up
