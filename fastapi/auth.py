"""
Server-side authentication for the CheriPic AI backend.

WHY THIS EXISTS
---------------
Every user-data endpoint used to take `user_id` straight from the request
body / URL path and trust it. That was a critical IDOR: anyone could call
`GET /conversations/<someone-else's-uuid>` and read another person's entire
Cheri AI chat history, or `POST /chat` as another user to burn their daily
quota and poison their AI "memory". UUIDs are not secrets.

THE FIX
-------
The frontend now sends the caller's Supabase **access token** in the
`Authorization: Bearer <jwt>` header. We verify that token here and derive
the user id from the *verified* token — never from anything the client can
freely set.

VERIFICATION STRATEGY
---------------------
We call Supabase Auth's `GET /auth/v1/user` with the token. Supabase checks
the signature + expiry and returns the user record. Advantages:
  * No new dependency — we already use httpx.
  * Can't drift from Supabase's own validation rules (rotation, ban, etc.).
A small in-process TTL cache keeps the hot chat path from hitting the auth
server on every single message.

If you later want to drop the per-request network hop, swap `_verify_token`
for local HS256 verification using SUPABASE_JWT_SECRET (adds PyJWT). The
public surface (`get_current_user_id`, `require_self`) stays the same.
"""

import os
import time
import logging

import httpx
from fastapi import Header, HTTPException, status
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
# The auth endpoint needs *an* apikey header; the anon key is the right one.
# Fall back to SUPABASE_KEY so the backend still boots if only the service
# key is configured (works, just less clean than a dedicated anon key).
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY") or ""

# Token → (expires_at_epoch, user_id). Short TTL: a compromised/rotated token
# stops working within CACHE_TTL seconds even though it's cached.
_CACHE_TTL = 120
_MAX_CACHE = 5000
_cache: dict[str, tuple[float, str]] = {}


def _prune(now: float) -> None:
    """Drop expired entries; hard-cap total size so a token storm can't grow
    the cache without bound."""
    if len(_cache) < _MAX_CACHE:
        return
    for tok in [t for t, (exp, _) in _cache.items() if exp <= now]:
        _cache.pop(tok, None)
    # Still too big after dropping expired? Clear it — correctness is
    # unaffected (worst case we re-verify), and this only triggers under
    # pathological load.
    if len(_cache) >= _MAX_CACHE:
        _cache.clear()


async def _verify_token(token: str) -> str:
    now = time.time()
    hit = _cache.get(token)
    if hit and hit[0] > now:
        return hit[1]

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        # Misconfiguration — fail closed, never open.
        logger.error("Auth not configured: SUPABASE_URL / SUPABASE_ANON_KEY missing.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on this server.",
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": SUPABASE_ANON_KEY,
                },
            )
    except httpx.HTTPError as e:
        logger.warning("Auth upstream error verifying token: %r", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach the auth server. Try again.",
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = resp.json()
    uid = user.get("id")
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token did not resolve to a user.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    _prune(now)
    _cache[token] = (now + _CACHE_TTL, uid)
    return uid


async def get_current_user_id(authorization: str = Header(default="")) -> str:
    """
    FastAPI dependency. Returns the verified caller's user id (a Supabase auth
    UUID). Raises 401 if the bearer token is missing / malformed / invalid.

    Usage:
        @app.post("/chat")
        async def chat(req: ChatRequest, user_id: str = Depends(get_current_user_id)):
            ...  # use `user_id`, NOT req.user_id
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await _verify_token(token)


async def get_current_admin_id(authorization: str = Header(default="")) -> str:
    """
    FastAPI dependency for admin-only endpoints. Returns the verified admin
    caller's user id. Raises 403 unless the JWT's `app_metadata.is_admin`
    claim is exactly `true`.

    Same claim the admin panel checks client-side (see admin_panel/src/lib/auth.ts)
    and the same one the SQL `is_admin()` helper reads from `auth.jwt()` —
    so the three enforcement points can't drift.

    Deliberately does a fresh network call to Supabase's /auth/v1/user
    (no cache reuse) so a demoted admin loses access within one request,
    not after CACHE_TTL. Admin endpoints are cold-path — this is cheap.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on this server.",
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": SUPABASE_ANON_KEY,
                },
            )
    except httpx.HTTPError as e:
        logger.warning("Auth upstream error verifying admin token: %r", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach the auth server. Try again.",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = resp.json()
    is_admin = bool((user.get("app_metadata") or {}).get("is_admin"))
    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    uid = user.get("id")
    if not uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token did not resolve to a user.",
        )
    return uid


def require_self(path_user_id: str, authed_user_id: str) -> None:
    """
    Guard for endpoints that still carry a `user_id` in the URL path (kept for
    URL compatibility). Rejects if the path id isn't the authenticated caller.
    Raises 403 rather than 404 so we don't leak whether the id exists.
    """
    if path_user_id != authed_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only access your own data.",
        )
