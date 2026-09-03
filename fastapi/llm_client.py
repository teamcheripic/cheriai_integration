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
from typing import Optional, Union, Tuple

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or None
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE = "https://api.openai.com/v1"

_DEFAULT_SYSTEM = (
    "You are Cheri, a warm, grounded friend on the CheriPic dating app. "
    "Reply in 2–3 short sentences, plain language, no bullet points. "
    "Ask at most one question per reply."
)


async def send_to_llm(
    prompt: Union[str, Tuple[str, str]],
    max_tokens: int = 180,
    user: Optional[str] = None,
    json_mode: bool = False,
) -> str:
    """
    Send prompt to OpenAI. Returns the assistant text.
    If OPENAI_API_KEY is missing, returns a mock reply so the wiring still works.

    json_mode=True forces OpenAI's structured-output mode (response_format=
    json_object), which guarantees the returned string parses as valid JSON.
    Caller is responsible for json.loads(). Used by /chat to lock the
    two-bubble shape so the model can't drift back to a single text bubble.
    """
    if isinstance(prompt, tuple):
        system_msg, user_msg = prompt
    else:
        system_msg, user_msg = _DEFAULT_SYSTEM, prompt

    if not OPENAI_API_KEY:
        if json_mode:
            # Keep the mock parseable so the parser path in main.py works in dev.
            import json
            return json.dumps({"reply": f"[MOCK LLM] {user_msg[:120]}", "follow_up": ""})
        return f"[MOCK LLM] {user_msg[:160]}"

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

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return f"[LLM parse error] {data}"
