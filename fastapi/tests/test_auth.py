"""
Tests for the auth layer — the fix that closed the IDOR where the backend
trusted a client-supplied user_id.

We assert the SECURITY behaviour, not implementation details:
  * a missing / malformed / empty bearer token is rejected with 401
  * a token Supabase rejects → 401 (fail closed)
  * a valid token → the user id from the VERIFIED response (never the caller)
  * the TTL cache doesn't leak one user's id to another token
  * require_self blocks cross-user access with 403
"""

import os

# auth.py reads these at import time; set before importing.
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")

import httpx
import pytest
import respx
from fastapi import HTTPException

import auth

USER_ENDPOINT = "https://test.supabase.co/auth/v1/user"


@pytest.fixture(autouse=True)
def _clear_cache():
    auth._cache.clear()
    yield
    auth._cache.clear()


async def test_missing_header_is_401():
    with pytest.raises(HTTPException) as exc:
        await auth.get_current_user_id(authorization="")
    assert exc.value.status_code == 401


async def test_non_bearer_scheme_is_401():
    with pytest.raises(HTTPException) as exc:
        await auth.get_current_user_id(authorization="Basic abc123")
    assert exc.value.status_code == 401


async def test_empty_bearer_is_401():
    with pytest.raises(HTTPException) as exc:
        await auth.get_current_user_id(authorization="Bearer    ")
    assert exc.value.status_code == 401


@respx.mock
async def test_supabase_rejects_token_is_401():
    respx.get(USER_ENDPOINT).mock(return_value=httpx.Response(401, json={"msg": "bad jwt"}))
    with pytest.raises(HTTPException) as exc:
        await auth.get_current_user_id(authorization="Bearer forged.jwt.token")
    assert exc.value.status_code == 401


@respx.mock
async def test_valid_token_returns_verified_uid():
    # The id MUST come from Supabase's response, not from anything the caller
    # sent. We return a specific uid and assert we get exactly that back.
    respx.get(USER_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"id": "verified-uid-123", "email": "a@b.co"})
    )
    uid = await auth.get_current_user_id(authorization="Bearer good.jwt.token")
    assert uid == "verified-uid-123"


@respx.mock
async def test_cache_is_keyed_per_token():
    # Two different tokens must resolve to their own verified uids — the cache
    # must never serve one token's uid for a different token.
    route = respx.get(USER_ENDPOINT)
    route.side_effect = [
        httpx.Response(200, json={"id": "uid-A"}),
        httpx.Response(200, json={"id": "uid-B"}),
    ]
    a = await auth.get_current_user_id(authorization="Bearer token-A")
    b = await auth.get_current_user_id(authorization="Bearer token-B")
    assert (a, b) == ("uid-A", "uid-B")
    # Second call for token-A should hit the cache (no 3rd upstream call).
    a2 = await auth.get_current_user_id(authorization="Bearer token-A")
    assert a2 == "uid-A"
    assert route.call_count == 2


def test_require_self_allows_owner():
    # Same id → no exception.
    auth.require_self("uid-1", "uid-1")


def test_require_self_blocks_other_user():
    with pytest.raises(HTTPException) as exc:
        auth.require_self("victim-uid", "attacker-uid")
    assert exc.value.status_code == 403
