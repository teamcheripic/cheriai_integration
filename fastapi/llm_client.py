# llm_client.py
"""
Thin OpenAI Chat Completions client.

Defaults are tuned for cost and brevity:
- max_tokens=180     → fits a main 1–3 sentence reply PLUS the optional 1-sentence
                       follow-up bubble. Bumped from 120 when the two-bubble
                       output format landed.
- temperature=0.6    → conversational without ignoring style rules.

Accepts either:
  send_to_llm(prompt_str)
  send_to_llm((system_str, user_str))
so existing callers using a single string still work.
"""
import os
import httpx
from dotenv import load_dotenv
from typing import Any, Optional, Union, Tuple

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or None
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE = "https://api.openai.com/v1"

_DEFAULT_SYSTEM = (
    "You are Cheri, a warm, grounded friend on the CheriPic dating app. "
    "Reply in 2–3 short sentences, plain language, no bullet points. "
    "Ask at most one question per reply."
)


class LLMResponseError(RuntimeError):
    """The provider answered but the body had no usable assistant text.
    Raised instead of returning the raw provider payload, which used to be
    shown to the member and saved to chat_history as "[LLM parse error] {...}".
    """


# USD per 1M tokens (input, output). Estimates only, for the spend ledger —
# update when the provider's price list changes. Unknown model → None.
MODEL_PRICES_PER_MTOK: dict[str, Tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    """Cost estimate for one call, or None when the model has no price entry."""
    price = MODEL_PRICES_PER_MTOK.get((model or "").strip())
    if price is None:
        # Dated snapshots ("gpt-4o-mini-2024-07-18") price like their base model.
        price = next((v for k, v in MODEL_PRICES_PER_MTOK.items() if (model or "").startswith(k + "-")), None)
    if price is None:
        return None
    pin, pout = price
    return round((max(0, prompt_tokens) * pin + max(0, completion_tokens) * pout) / 1_000_000, 8)


def extract_reply_text(data: Any) -> str:
    """Assistant text from a chat-completions body; raises LLMResponseError otherwise."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMResponseError("LLM response had no choices[0].message.content")
    if not isinstance(content, str) or not content.strip():
        raise LLMResponseError("LLM returned empty content")
    return content.strip()


LEDGER_PURPOSES = ("chat", "assessment", "memory_distill", "memory_seed")


def build_usage_ledger_row(
    *,
    user_id: str,
    purpose: str,
    tier: str,
    model: str,
    usage: Optional[dict],
    canned: bool = False,
) -> dict:
    """
    Privacy-minimised usage-ledger row (handoff §10 "AI usage implementation"):
    model, tokens, cost estimate, request purpose and membership — and nothing
    else. Never put prompt or reply text in here; spend measurement must not
    log sensitive content. `purpose` keeps assessment cost separate from
    ordinary conversation. NOT persisted yet: needs an approved table (see
    audit D, X-AI ledger row) and a call site in main.py.
    """
    if purpose not in LEDGER_PURPOSES:
        raise ValueError(f"unknown ledger purpose {purpose!r}")
    u = usage or {}
    pt = int(u.get("prompt_tokens") or 0)
    ct = int(u.get("completion_tokens") or 0)
    return {
        "user_id": user_id,
        "purpose": purpose,
        "membership_tier": tier,
        "model": None if canned else model,
        "prompt_tokens": 0 if canned else pt,
        "completion_tokens": 0 if canned else ct,
        "total_tokens": 0 if canned else int(u.get("total_tokens") or pt + ct),
        "cost_estimate_usd": 0.0 if canned else estimate_cost_usd(model, pt, ct),
        "canned": canned,
    }


async def send_to_llm(
    prompt: Union[str, Tuple[str, str]],
    max_tokens: int = 180,
    user: Optional[str] = None,
    json_mode: bool = False,
) -> str:
    """Send prompt to OpenAI and return the assistant text (see send_to_llm_with_usage)."""
    text, _usage = await send_to_llm_with_usage(prompt, max_tokens=max_tokens, user=user, json_mode=json_mode)
    return text


async def send_to_llm_with_usage(
    prompt: Union[str, Tuple[str, str]],
    max_tokens: int = 180,
    user: Optional[str] = None,
    json_mode: bool = False,
) -> Tuple[str, dict]:
    """
    Send prompt to OpenAI. Returns (assistant text, usage) where usage is
    {"model", "prompt_tokens", "completion_tokens", "total_tokens"}.
    If OPENAI_API_KEY is missing, returns a mock reply so the wiring still works.
    One attempt only — no automatic retries (caps runaway spend); a 30 s
    timeout bounds each call.

    json_mode=True forces OpenAI's structured-output mode (response_format=
    json_object), which guarantees the returned string parses as valid JSON.
    Caller is responsible for json.loads(). Used by /chat to lock the
    two-bubble shape so the model can't drift back to a single text bubble.
    """
    if isinstance(prompt, tuple):
        system_msg, user_msg = prompt
    else:
        system_msg, user_msg = _DEFAULT_SYSTEM, prompt

    mock_usage = {"model": "mock", "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not OPENAI_API_KEY:
        if json_mode:
            # Keep the mock parseable so the parser path in main.py works in dev.
            import json
            return json.dumps({"reply": f"[MOCK LLM] {user_msg[:120]}", "follow_up": ""}), mock_usage
        return f"[MOCK LLM] {user_msg[:160]}", mock_usage

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "frequency_penalty": 0.3,  # discourage list-y, repetitive phrasing
        "presence_penalty": 0.2,
    }
    if json_mode:
        # OpenAI requires the word "JSON" to appear in the messages; the
        # cheriai_prompts.py system message already includes it.
        payload["response_format"] = {"type": "json_object"}
    if user:
        payload["user"] = user  # helps OpenAI's abuse heuristics, no cost

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{OPENAI_BASE}/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    text = extract_reply_text(data)
    raw_usage = data.get("usage") if isinstance(data, dict) else None
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
    usage = {
        "model": (data.get("model") if isinstance(data, dict) else None) or OPENAI_MODEL,
        "prompt_tokens": int(raw_usage.get("prompt_tokens") or 0),
        "completion_tokens": int(raw_usage.get("completion_tokens") or 0),
        "total_tokens": int(raw_usage.get("total_tokens") or 0),
    }
    return text, usage
