"""
Shared rate limiting (audit X-SEC.3).

slowapi, in-process memory storage (per worker). Bucket = the VERIFIED
member id when the request's bearer token was verified by
get_current_user_id (auth cache), otherwise the client IP. An unverified
JWT `sub` is never used, so nobody can drain another member's bucket.
Limits are env-tunable at boot: RATE_LIMIT_<NAME>="20/minute".
RATE_LIMIT_TRUST_PROXY=true uses the first X-Forwarded-For hop (Railway).

main.py registers `limiter` on `app.state`; feature routers import it from
here so every endpoint shares the same buckets and exception handler.
"""

import os

from fastapi import Request
from slowapi import Limiter

from auth import cached_user_id as _cached_user_id


def _client_ip(request: Request) -> str:
    if (os.getenv("RATE_LIMIT_TRUST_PROXY") or "").strip().lower() in ("1", "true", "yes"):
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if fwd:
            return fwd
    return request.client.host if request.client else "unknown"


def rate_limit_key(request: Request) -> str:
    authz = request.headers.get("authorization") or ""
    if authz.lower().startswith("bearer "):
        uid = _cached_user_id(authz.split(" ", 1)[1].strip())
        if uid:
            return f"user:{uid}"
    return f"ip:{_client_ip(request)}"


def _rl(name: str, default: str) -> str:
    return (os.getenv(f"RATE_LIMIT_{name}") or default).strip()


RATE_LIMITS = {
    "chat": _rl("CHAT", "20/minute"),
    "notify": _rl("NOTIFY", "30/minute"),
    "account_delete": _rl("ACCOUNT_DELETE", "3/hour"),
    "verify_face": _rl("VERIFY_FACE", "5/hour"),
    "withdraw_interest": _rl("WITHDRAW_INTEREST", "30/minute"),
    "trigger_self_email": _rl("TRIGGER_SELF_EMAIL", "5/hour"),
    "unsubscribe": _rl("UNSUBSCRIBE", "30/minute"),
    # Feature routers
    "media": _rl("MEDIA", "60/minute"),
    "cheripass_token": _rl("CHERIPASS_TOKEN", "30/minute"),
    "cheripass_verify": _rl("CHERIPASS_VERIFY", "120/minute"),
    "sponsored_click": _rl("SPONSORED_CLICK", "60/minute"),
    "assessment_submit": _rl("ASSESSMENT_SUBMIT", "10/hour"),
    "connect_state": _rl("CONNECT_STATE", "20/minute"),
    # Payment providers (payments_api.py)
    "billing_checkout": _rl("BILLING_CHECKOUT", "10/minute"),
    "billing_config": _rl("BILLING_CONFIG", "60/minute"),
    "admin_payments": _rl("ADMIN_PAYMENTS", "30/minute"),
}

limiter = Limiter(
    key_func=rate_limit_key,
    enabled=(os.getenv("RATE_LIMIT_ENABLED") or "true").strip().lower() not in ("0", "false", "no"),
)


def rate_limit(name: str):
    """
    FastAPI dependency for router endpoints: `dependencies=[Depends(rate_limit("media"))]`.

    Same storage, key function and 429 shape as the `@limiter.limit`
    decorators in main.py, but without wrapping the endpoint — slowapi's
    decorator breaks body-parameter detection in modules that use
    `from __future__ import annotations`.
    """
    from fastapi import HTTPException
    from limits import parse

    item = parse(RATE_LIMITS[name])

    async def _check(request: Request) -> None:
        if not limiter.enabled:
            return
        if not limiter.limiter.hit(item, f"router:{name}", rate_limit_key(request)):
            raise HTTPException(status_code=429, detail="too_many_requests", headers={"Retry-After": "60"})

    return _check
