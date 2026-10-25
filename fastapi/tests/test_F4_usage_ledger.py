"""
F4 — verifies the AI usage-ledger persistence already wired into main.py
(H10.17): rows go to public.ai_usage_ledger, and until migration 050 is
applied a missing table (404) or a network error is logged, never raised.
No network: respx.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("ONESIGNAL_APP_ID", "")
os.environ.setdefault("ONESIGNAL_REST_API_KEY", "")

import json

import httpx
import respx

import llm_client
import main

URL = f"{main.SUPABASE_URL}/rest/v1/ai_usage_ledger"
ROW = llm_client.build_usage_ledger_row(
    user_id="u1", purpose="chat", tier="basic", model="gpt-4o-mini",
    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
)


@respx.mock
async def test_ledger_row_is_inserted_when_table_exists(caplog):
    route = respx.post(URL).mock(return_value=httpx.Response(201))
    caplog.set_level("INFO")
    await main._record_ai_usage(ROW)
    assert route.called and json.loads(route.calls.last.request.content) == ROW
    assert "ledger table unavailable" not in caplog.text


@respx.mock
async def test_missing_table_falls_back_to_log_without_raising(caplog):
    respx.post(URL).mock(return_value=httpx.Response(404, json={"code": "42P01"}))
    caplog.set_level("INFO")
    await main._record_ai_usage(ROW)
    assert "ledger table unavailable [404]" in caplog.text and '"total_tokens": 15' in caplog.text


@respx.mock
async def test_network_error_falls_back_to_log_without_raising(caplog):
    respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    caplog.set_level("INFO")
    await main._record_ai_usage(ROW)
    assert "ledger insert failed" in caplog.text


def test_main_app_mounts_cheripass_routes():
    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert {"/cheripass/token", "/cheripass/verify", "/analytics/sponsored-click"} <= paths
