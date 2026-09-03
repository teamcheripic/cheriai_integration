"""
Minimal async PostgREST client for Supabase.

We don't use the official supabase-py package because version 2.24.0 (and its
storage3 transitive dependency) hangs on import under Python 3.14. The backend
only needs simple SELECT/INSERT against two tables, so a thin httpx wrapper
covers the surface without the compatibility issue.
"""

import os
from typing import Any, Optional
import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or ""

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")


class SupabaseRest:
    def __init__(self, url: str, key: str) -> None:
        self._base = f"{url}/rest/v1"
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    async def select(
        self,
        table: str,
        *,
        columns: str = "*",
        eq: Optional[dict[str, Any]] = None,
        order: Optional[str] = None,  # e.g. "created_at.asc" or "created_at.desc"
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"select": columns}
        if eq:
            for col, val in eq.items():
                params[col] = f"eq.{val}"
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self._base}/{table}", params=params, headers=self._headers)
            resp.raise_for_status()
            return resp.json()

    async def insert(self, table: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{self._base}/{table}", json=payload, headers=self._headers)
            if resp.status_code >= 400:
                # Surface the actual PG error so the caller can act on it
                raise RuntimeError(
                    f"Supabase insert into {table} failed "
                    f"[{resp.status_code}]: {resp.text}"
                )
            return resp.json() if resp.content else []

    async def update(
        self,
        table: str,
        *,
        payload: dict[str, Any],
        eq: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """PATCH /rest/v1/<table>?<col>=eq.<val>&... with the payload."""
        if not eq:
            raise ValueError("update() requires an eq filter — refusing to update every row.")
        params = {col: f"eq.{val}" for col, val in eq.items()}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(
                f"{self._base}/{table}",
                params=params,
                json=payload,
                headers=self._headers,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Supabase update on {table} failed "
                    f"[{resp.status_code}]: {resp.text}"
                )
            return resp.json() if resp.content else []

    async def upsert(
        self,
        table: str,
        payload: dict[str, Any],
        *,
        on_conflict: str,
    ) -> list[dict[str, Any]]:
        headers = {
            **self._headers,
            "Prefer": "return=representation,resolution=merge-duplicates",
        }
        params = {"on_conflict": on_conflict}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._base}/{table}",
                params=params,
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else []

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base}/", headers=self._headers)
                return resp.status_code < 500
        except Exception:
            return False


supabase = SupabaseRest(SUPABASE_URL, SUPABASE_KEY)
