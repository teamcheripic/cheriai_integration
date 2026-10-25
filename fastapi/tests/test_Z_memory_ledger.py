"""
Handoff §10 "AI usage implementation": background memory LLM calls (seed on
registration, distill after chat turns) must be attributed to their own
purpose in the usage ledger and never include prompt/reply text.
Deterministic: Supabase and billing are monkeypatched; no network.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import inspect
import sys
import types

import pytest

import user_memory as um

USAGE = {"model": "gpt-4o-mini", "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


@pytest.fixture
def fake_billing(monkeypatch):
    mod = types.ModuleType("billing")

    async def tier(_uid):
        return "premium-lite"

    mod.get_membership_tier = tier
    monkeypatch.setitem(sys.modules, "billing", mod)
    return mod


async def test_memory_rows_carry_purpose_and_no_text(monkeypatch, fake_billing):
    inserted = []

    async def insert(table, row):
        inserted.append((table, row))
        return []

    monkeypatch.setattr(um.supabase, "insert", insert)
    await um._ledger("u1", "memory_distill", USAGE)
    await um._ledger("u1", "memory_seed", USAGE)
    assert [t for t, _ in inserted] == ["ai_usage_ledger", "ai_usage_ledger"]
    assert [r["purpose"] for _, r in inserted] == ["memory_distill", "memory_seed"]
    row = inserted[0][1]
    assert row["membership_tier"] == "premium-lite" and row["total_tokens"] == 150
    assert set(row) == {
        "user_id", "purpose", "membership_tier", "model", "prompt_tokens",
        "completion_tokens", "total_tokens", "cost_estimate_usd", "canned",
    }


async def test_missing_table_is_logged_not_raised(monkeypatch, fake_billing, caplog):
    async def insert(table, row):
        raise RuntimeError("Supabase insert into ai_usage_ledger failed [404]: relation does not exist")

    monkeypatch.setattr(um.supabase, "insert", insert)
    with caplog.at_level("INFO"):
        await um._ledger("u1", "memory_seed", USAGE)  # must not raise
    assert any("ledger unavailable" in m for m in caplog.messages)


async def test_tier_lookup_failure_falls_back_to_unknown(monkeypatch):
    mod = types.ModuleType("billing")

    async def boom(_uid):
        raise RuntimeError("db down")

    mod.get_membership_tier = boom
    monkeypatch.setitem(sys.modules, "billing", mod)
    rows = []

    async def insert(table, row):
        rows.append(row)

    monkeypatch.setattr(um.supabase, "insert", insert)
    await um._ledger("u1", "memory_distill", USAGE)
    assert rows[0]["membership_tier"] == "unknown"


def test_seed_and_distill_use_usage_aware_llm_call():
    seed = inspect.getsource(um.seed_memory_from_profile)
    distill = inspect.getsource(um.update_memory_async)
    assert "send_to_llm_with_usage" in seed and '"memory_seed"' in seed
    assert "send_to_llm_with_usage" in distill and '"memory_distill"' in distill
