"""
main.py - FastAPI + Supabase CRUD endpoints

Usage:
  1. Ensure your venv is active:
     source venv/bin/activate

  2. Create a .env file (see .env.example) with:
     SUPABASE_URL=...
     SUPABASE_KEY=...
     HOST=0.0.0.0
     PORT=8000

  3. Install dependencies (if not already):
     pip install fastapi uvicorn python-dotenv supabase

  4. Run:
     uvicorn main:app --reload --host 0.0.0.0 --port 8000

Interactive docs:
  http://127.0.0.1:8000/docs
"""

import os
import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Path, Query, Body, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# Load .env
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

# Import supabase client (synchronous)
from supabase import create_client, Client

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# FastAPI app
app = FastAPI(title="Cheripic Supabase API", version="1.0")

# Allow CORS for development (adjust origins for production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change to your RN app origin(s) in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Generic Pydantic type for incoming record data
class RecordPayload(BaseModel):
    data: Dict[str, Any]


# --- Utility helpers -------------------------------------------------------
def supabase_select(table: str, select: str = "*", limit: Optional[int] = None, offset: Optional[int] = None,
                    filters: Optional[List[Dict[str, Any]]] = None, order_by: Optional[str] = None):
    """
    Helper to build a select query with optional filters/order/pagination.
    filters: list of {"col": "name", "op": "eq"/"neq"/"like"/"ilike"/"gt"/"lt", "val": value}
    """
    try:
        query = supabase.table(table).select(select)
        if filters:
            for f in filters:
                col = f.get("col")
                op = f.get("op", "eq")
                val = f.get("val")
                # apply common operators
                if op == "eq":
                    query = query.eq(col, val)
                elif op == "neq":
                    query = query.neq(col, val)
                elif op == "gt":
                    query = query.gt(col, val)
                elif op == "lt":
                    query = query.lt(col, val)
                elif op == "like":
                    query = query.like(col, val)
                elif op == "ilike":
                    query = query.ilike(col, val)
                elif op == "in":
                    # val expected to be list
                    query = query.in_(col, val)
                else:
                    # fallback to eq if unknown
                    query = query.eq(col, val)

        if order_by:
            # expecting e.g. "created_at.desc" or "name.asc"
            parts = order_by.split(".")
            col = parts[0]
            direction = parts[1] if len(parts) > 1 else "asc"
            query = query.order(col, {"ascending": direction == "asc"})

        if limit is not None:
            query = query.limit(limit)
        if offset is not None:
            query = query.offset(offset)

        resp = query.execute()
        return resp
    except Exception as e:
        raise


# --- Health ----------------------------------------------------------------
@app.get("/health")
async def health():
    """
    Simple health check.
    """
    return {"status": "ok"}


# --- Users endpoints -------------------------------------------------------
@app.get("/users", summary="List users (with optional search/pagination)")
def list_users(
    q: Optional[str] = Query(None, description="search query to match name/email (ILIKE)"),
    limit: Optional[int] = Query(50, description="limit"),
    offset: Optional[int] = Query(0, description="offset"),
    order_by: Optional[str] = Query("created_at.desc", description="order_by e.g. created_at.desc")
):
    """
    Example:
      GET /users?q=alice&limit=10&offset=0
    """
    try:
        filters = []
        if q:
            # using ilike to search in name or email (modify columns as per your schema)
            # supabase-py does not support OR easily; for simple cases try ilike on a single column.
            # If you need complex queries use RPC or SQL.
            # We'll attempt to search 'full_name' then 'email' fallbacks.
            # For safety, we apply ilike on full_name. Adjust to your schema.
            filters.append({"col": "full_name", "op": "ilike", "val": f"%{q}%"})
        resp = supabase_select("users", select="*", limit=limit, offset=offset, filters=filters, order_by=order_by)
        # Response object from supabase-py typically has .data and .error
        data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
        return {"count": len(data) if data else 0, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.get("/users/{user_id}", summary="Get user by id")
def get_user(user_id: str = Path(..., description="user id (uuid or string)")):
    """
    Example:
      GET /users/123
    """
    try:
        resp = supabase.table("users").select("*").eq("id", user_id).limit(1).execute()
        data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
        if not data:
            raise HTTPException(status_code=404, detail="User not found")
        return {"data": data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# --- Generic table endpoints ----------------------------------------------
@app.get("/table/{table_name}", summary="Fetch rows from any table with optional filters")
def fetch_table_rows(
    table_name: str,
    select: Optional[str] = Query("*", description="columns to select, use comma separated or '*'"),
    limit: Optional[int] = Query(None),
    offset: Optional[int] = Query(None),
    # you can pass filters as JSON encoded list of objects:
    filters: Optional[str] = Query(None, description='JSON array of filters, e.g. [{"col":"status","op":"eq","val":"open"}]'),
    order_by: Optional[str] = Query(None, description="order_by e.g. created_at.desc")
):
    """
    Example:
      GET /table/products?filters=[{"col":"price","op":"gt","val":10}]&limit=10
    """
    try:
        parsed_filters = None
        if filters:
            try:
                parsed_filters = json.loads(filters)
                if not isinstance(parsed_filters, list):
                    raise ValueError("filters must be a JSON array")
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid filters JSON: {e}")

        resp = supabase_select(table_name, select=select, limit=limit, offset=offset, filters=parsed_filters, order_by=order_by)
        data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
        return {"count": len(data) if data else 0, "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.get("/table/{table_name}/{row_id}", summary="Get single row by id (assumes column 'id')")
def fetch_table_row_by_id(table_name: str, row_id: str):
    try:
        resp = supabase.table(table_name).select("*").eq("id", row_id).limit(1).execute()
        data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
        if not data:
            raise HTTPException(status_code=404, detail="Row not found")
        return {"data": data[0]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.post("/table/{table_name}", summary="Insert a new row into a table")
def insert_table_row(table_name: str, payload: RecordPayload = Body(...)):
    """
    Body:
      { "data": { "name": "abc", "price": 10 } }
    """
    try:
        # NOTE: depending on your Supabase row-level security you may need service_role key
        resp = supabase.table(table_name).insert(payload.data).execute()
        data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
        err = getattr(resp, "error", None) or (resp.get("error") if isinstance(resp, dict) else None)
        if err:
            raise HTTPException(status_code=400, detail=f"Insert error: {err}")
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.put("/table/{table_name}/{row_id}", summary="Update a row by id")
def update_table_row(table_name: str, row_id: str, payload: RecordPayload = Body(...)):
    """
    Body:
      { "data": { "name": "new name" } }
    """
    try:
        resp = supabase.table(table_name).update(payload.data).eq("id", row_id).execute()
        data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
        err = getattr(resp, "error", None) or (resp.get("error") if isinstance(resp, dict) else None)
        if err:
            raise HTTPException(status_code=400, detail=f"Update error: {err}")
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.delete("/table/{table_name}/{row_id}", summary="Delete a row by id")
def delete_table_row(table_name: str, row_id: str):
    try:
        resp = supabase.table(table_name).delete().eq("id", row_id).execute()
        data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
        err = getattr(resp, "error", None) or (resp.get("error") if isinstance(resp, dict) else None)
        if err:
            raise HTTPException(status_code=400, detail=f"Delete error: {err}")
        return {"deleted": True, "data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# --- Optional: RPC / raw SQL helper (use with caution) ---------------------
@app.post("/rpc", summary="Call Supabase RPC / run SQL (if enabled)")
def rpc_call(fn_name: str = Body(..., embed=True), params: Dict[str, Any] = Body({}, embed=True)):
    """
    Call stored procedure / RPC on Supabase.
    Example body:
      { "fn_name": "my_rpc", "params": {"arg1": 1, "arg2":"x"} }
    """
    try:
        resp = supabase.rpc(fn_name, params).execute()
        data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
        return {"data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"RPC error: {e}")


# --- Root info -------------------------------------------------------------
@app.get("/")
def root_info():
    """
    Quick overview and pointer to your local screenshot for sanity.
    """
    return {
        "app": "Cheripic Supabase API",
        "docs": "/docs",
        "note": "Confirm your venv exists (screenshot available at file:///mnt/data/99a772ad-473f-4fcb-b779-67222a00615a.png)"
    }
