"""
F10 · Admin-controlled settings (migration 065): scheduled jobs follow
app_config, every run is recorded, push copy is admin-editable.
Supabase is respx-mocked; no network.
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
import pytest
import respx

import app_config
import notification_copy
import scheduled_jobs as sj


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    app_config.invalidate_config_cache()
    app_config._cache = {}
    notification_copy.invalidate()
    sj._armed.clear()
    for j in sj.JOBS.values():
        monkeypatch.delenv(j.enabled_env, raising=False)
        monkeypatch.delenv(j.cron_env, raising=False)
    yield


def cfg(rows):
    respx.get(f"{app_config.SUPABASE_URL}/rest/v1/app_config").mock(
        return_value=httpx.Response(200, json=[{"key": k, "value": v} for k, v in rows.items()]))


class FakeJob:
    def __init__(self, next_run=None):
        self.next_run_time = next_run


class FakeScheduler:
    def __init__(self):
        self.jobs, self.calls = {}, []

    def get_job(self, jid):
        return self.jobs.get(jid)

    def add_job(self, func, trigger, id, **kw):
        self.jobs[id] = FakeJob()
        self.calls.append(("add", id, str(trigger)))

    def remove_job(self, jid):
        self.jobs.pop(jid)
        self.calls.append(("remove", jid))

    def reschedule_job(self, jid, trigger):
        self.calls.append(("reschedule", jid, str(trigger)))


@respx.mock
async def test_admin_setting_wins_over_env_and_default(monkeypatch):
    monkeypatch.setenv("ENABLE_INTRO_DELIVERY", "false")
    cfg({"intro_delivery_enabled": "true", "intro_delivery_cron": "5 9 * * *"})
    assert await sj.job_settings(sj.JOBS["intro_delivery"]) == (True, "5 9 * * *")


@respx.mock
async def test_env_and_default_are_fallbacks_when_no_row(monkeypatch):
    monkeypatch.setenv("ENABLE_KYC_RETENTION", "off")
    cfg({})
    assert await sj.job_settings(sj.JOBS["kyc_retention"]) == (False, "45 3 * * *")
    assert await sj.job_settings(sj.JOBS["intro_delivery"]) == (True, "0 8 * * *")


@respx.mock
async def test_reconcile_arms_disarms_and_reschedules():
    s = FakeScheduler()
    cfg({"daily_email_cron_enabled": "true", "intro_delivery_enabled": "true",
         "stripe_reconcile_enabled": "false", "kyc_retention_enabled": "true"})
    await sj.reconcile(s)
    assert {c[1] for c in s.calls if c[0] == "add"} == {"daily_match_nudge", "intro_delivery", "kyc_retention"}
    # Admin turns KYC retention off and changes the delivery time.
    app_config.invalidate_config_cache()
    cfg({"daily_email_cron_enabled": "true", "intro_delivery_enabled": "true", "intro_delivery_cron": "0 9 * * *",
         "kyc_retention_enabled": "false", "stripe_reconcile_enabled": "false"})
    s.calls.clear()
    await sj.reconcile(s)
    assert ("remove", "kyc_retention") in s.calls
    assert any(c[0] == "reschedule" and c[1] == "intro_delivery" for c in s.calls)


@respx.mock
async def test_a_bad_crontab_does_not_stop_other_jobs():
    s = FakeScheduler()
    cfg({"daily_email_cron_enabled": "true", "daily_email_cron_schedule": "not a cron",
         "intro_delivery_enabled": "true", "stripe_reconcile_enabled": "false", "kyc_retention_enabled": "false"})
    await sj.reconcile(s)
    assert [c[1] for c in s.calls if c[0] == "add"] == ["intro_delivery"]


@respx.mock
async def test_run_job_records_start_and_result(monkeypatch):
    async def fake():
        return {"delivered": 3}

    monkeypatch.setattr(sj, "JOBS", {**sj.JOBS, "intro_delivery": sj.Job(
        "intro_delivery", "x", fake, "a", "b", "C", "D", True, "0 8 * * *")})
    start = respx.post(f"{sj.REST}/job_runs").mock(return_value=httpx.Response(201, json=[{"id": 7}]))
    end = respx.patch(f"{sj.REST}/job_runs").mock(return_value=httpx.Response(204))
    out = await sj.run_job("intro_delivery", "admin:abc")
    assert out["status"] == "ok"
    assert json.loads(start.calls.last.request.content) == {"job": "intro_delivery", "triggered_by": "admin:abc"}
    body = json.loads(end.calls.last.request.content)
    assert body["status"] == "ok" and body["result"] == {"delivered": 3}


@respx.mock
async def test_failed_job_is_recorded_not_raised(monkeypatch):
    async def boom():
        raise RuntimeError("upstream down")

    monkeypatch.setattr(sj, "JOBS", {**sj.JOBS, "kyc_retention": sj.Job(
        "kyc_retention", "x", boom, "a", "b", "C", "D", True, "0 8 * * *")})
    respx.post(f"{sj.REST}/job_runs").mock(return_value=httpx.Response(201, json=[{"id": 8}]))
    end = respx.patch(f"{sj.REST}/job_runs").mock(return_value=httpx.Response(204))
    out = await sj.run_job("kyc_retention")
    assert out["status"] == "failed"
    assert json.loads(end.calls.last.request.content)["status"] == "failed"


DEFAULT = {"title": "Built-in", "body": "Built-in body", "url": "/#/matching", "dedup_window_hours": 1}


@respx.mock
async def test_push_copy_admin_override_and_disable():
    respx.get(f"{notification_copy.SUPABASE_URL}/rest/v1/notification_templates").mock(return_value=httpx.Response(200, json=[
        {"slug": "interest_received", "title": "Someone is interested", "body": "", "url": None, "enabled": True, "dedup_window_hours": 2},
        {"slug": "interest_declined", "title": "x", "body": "y", "url": None, "enabled": False, "dedup_window_hours": 1},
    ]))
    got = await notification_copy.push_template("interest_received", DEFAULT)
    assert got == {"title": "Someone is interested", "body": "Built-in body", "url": "/#/matching", "dedup_window_hours": 2.0}
    assert await notification_copy.push_template("interest_declined", DEFAULT) is None
    assert await notification_copy.push_template("not_in_table", DEFAULT) == DEFAULT


@respx.mock
async def test_push_copy_falls_back_when_table_missing():
    respx.get(f"{notification_copy.SUPABASE_URL}/rest/v1/notification_templates").mock(
        return_value=httpx.Response(404, json={"code": "PGRST205"}))
    assert await notification_copy.push_template("interest_received", DEFAULT) == DEFAULT


def test_peek_config_reads_cache_without_network():
    app_config._cache = {"membership_grace_days": "5", "openai_model": "gpt-x"}
    assert app_config.peek_int("membership_grace_days", 3) == 5
    assert app_config.peek_config("openai_model") == "gpt-x"
    assert app_config.peek_int("missing", 3) == 3
