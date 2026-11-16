"""
scheduled_jobs.py — every background job, controlled from the admin panel.

Each job's on/off switch and UTC crontab live in public.app_config (seeded by
migration 065); the old env vars are only fallbacks. A supervisor tick every
15 minutes (and right after any admin settings save, via
/admin/invalidate-app-config-cache) arms, disarms or reschedules the
APScheduler jobs to match. Every run — scheduled or "Run now" from the admin
panel — is recorded in public.job_runs (start, finish, status, result).

Jobs:
  daily_match_nudge  daily email about unread introductions / interests
  intro_delivery     server-side introduction delivery + push (063)
  stripe_reconcile   Stripe ↔ user_memberships reconciliation (website)
  kyc_retention      delete ID / selfie images after the retention period
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

from supabase_client import SUPABASE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

REST = f"{SUPABASE_URL}/rest/v1"
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _env_on(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


async def _daily_nudge() -> Any:
    from cron_daily_email import run_once

    return await run_once()


async def _intro_delivery() -> Any:
    from introduction_delivery import run_intro_delivery

    return await run_intro_delivery()


async def _stripe_reconcile() -> Any:
    import billing

    return await billing.reconcile_subscriptions()


async def _kyc_retention() -> Any:
    from kyc_retention import run_kyc_retention

    return await run_kyc_retention()


@dataclass(frozen=True)
class Job:
    id: str
    label: str
    func: Callable[[], Awaitable[Any]]
    enabled_key: str
    cron_key: str
    enabled_env: str
    cron_env: str
    default_enabled: bool
    default_cron: str


JOBS: dict[str, Job] = {
    j.id: j
    for j in (
        Job("daily_match_nudge", "Daily email nudge", _daily_nudge,
            "daily_email_cron_enabled", "daily_email_cron_schedule",
            "ENABLE_DAILY_EMAIL_CRON", "DAILY_EMAIL_CRON_SCHEDULE", False, "30 8 * * *"),
        Job("intro_delivery", "Introduction delivery", _intro_delivery,
            "intro_delivery_enabled", "intro_delivery_cron",
            "ENABLE_INTRO_DELIVERY", "INTRO_DELIVERY_CRON", True, "0 8 * * *"),
        Job("stripe_reconcile", "Stripe reconciliation (website)", _stripe_reconcile,
            "stripe_reconcile_enabled", "stripe_reconcile_cron",
            "ENABLE_STRIPE_RECONCILE", "STRIPE_RECONCILE_CRON", True, "15 3 * * *"),
        Job("kyc_retention", "KYC image retention", _kyc_retention,
            "kyc_retention_enabled", "kyc_retention_cron",
            "ENABLE_KYC_RETENTION", "KYC_RETENTION_CRON", True, "45 3 * * *"),
    )
}

# What each job is currently armed with (APScheduler jobs can't carry extra attrs).
_armed: dict[str, str] = {}


async def job_settings(job: Job) -> tuple[bool, str]:
    from app_config import get_config

    raw_on = await get_config(job.enabled_key, env_key="__none__", default=None)
    if raw_on is None:
        enabled = _env_on(job.enabled_env, job.default_enabled)
    else:
        enabled = raw_on.strip().lower() in ("1", "true", "yes", "on")
    cron = (await get_config(job.cron_key, env_key="__none__", default=None)) or os.getenv(job.cron_env) or job.default_cron
    return enabled, cron.strip()


async def _record_start(client: httpx.AsyncClient, job_id: str, triggered_by: str) -> Optional[int]:
    try:
        r = await client.post(f"{REST}/job_runs", json={"job": job_id, "triggered_by": triggered_by},
                              headers={**HEADERS, "Prefer": "return=representation"})
        if r.status_code < 300:
            rows = r.json() or []
            return rows[0]["id"] if rows else None
    except Exception as e:
        logger.warning("[jobs] could not record start of %s: %r", job_id, e)
    return None


async def _record_end(client: httpx.AsyncClient, run_id: Optional[int], status: str, result: Any, error: Optional[str]) -> None:
    if run_id is None:
        return
    try:
        payload: dict[str, Any] = {"finished_at": datetime.now(timezone.utc).isoformat(), "status": status, "error": error}
        if result is not None:
            payload["result"] = result if isinstance(result, (dict, list)) else {"value": str(result)}
        await client.patch(f"{REST}/job_runs", params={"id": f"eq.{run_id}"}, json=payload,
                           headers={**HEADERS, "Prefer": "return=minimal"})
    except Exception as e:
        logger.warning("[jobs] could not record end of run %s: %r", run_id, e)


async def run_job(job_id: str, triggered_by: str = "schedule") -> dict[str, Any]:
    """Run one job now, recording it in job_runs. Never raises."""
    return await run_recorded(job_id, JOBS[job_id].func, triggered_by)


async def run_recorded(job_id: str, func: Callable[[], Awaitable[Any]], triggered_by: str) -> dict[str, Any]:
    """Run any task with a job_runs record (also used by admin wipes). Never raises."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        run_id = await _record_start(client, job_id, triggered_by)
        try:
            result = await func()
            await _record_end(client, run_id, "ok", result, None)
            return {"job": job_id, "status": "ok", "result": result}
        except Exception as e:
            logger.error("[jobs] %s failed: %r", job_id, e, exc_info=True)
            await _record_end(client, run_id, "failed", None, repr(e)[:2000])
            return {"job": job_id, "status": "failed", "error": repr(e)[:500]}


def _runner(job_id: str) -> Callable[[], Awaitable[Any]]:
    async def _run() -> Any:
        return await run_job(job_id, "schedule")

    return _run


async def reconcile(scheduler) -> None:
    """Arm / disarm / reschedule every job to match app_config."""
    if scheduler is None:
        return
    from apscheduler.triggers.cron import CronTrigger

    for job in JOBS.values():
        try:
            enabled, cron = await job_settings(job)
            existing = scheduler.get_job(job.id)
            if not enabled:
                if existing:
                    scheduler.remove_job(job.id)
                    _armed.pop(job.id, None)
                    logger.info("[jobs] %s DISARMED (admin setting off)", job.id)
                continue
            trigger = CronTrigger.from_crontab(cron, timezone="UTC")
            if existing is None:
                scheduler.add_job(_runner(job.id), trigger, id=job.id, max_instances=1, coalesce=True,
                                  misfire_grace_time=3600, replace_existing=True)
                _armed[job.id] = cron
                logger.info("[jobs] %s ARMED (UTC '%s')", job.id, cron)
            elif _armed.get(job.id) != cron:
                scheduler.reschedule_job(job.id, trigger=trigger)
                _armed[job.id] = cron
                logger.info("[jobs] %s RESCHEDULED (UTC '%s')", job.id, cron)
        except Exception as e:
            # A bad crontab typed in the admin panel must not stop the other jobs.
            logger.error("[jobs] reconcile of %s failed: %r", job.id, e)


async def status(scheduler) -> list[dict[str, Any]]:
    out = []
    for job in JOBS.values():
        enabled, cron = await job_settings(job)
        sj = scheduler.get_job(job.id) if scheduler is not None else None
        out.append({
            "id": job.id,
            "label": job.label,
            "enabled": enabled,
            "cron": cron,
            "enabled_key": job.enabled_key,
            "cron_key": job.cron_key,
            "armed": sj is not None,
            "next_run_at": sj.next_run_time.isoformat() if sj is not None and sj.next_run_time else None,
        })
    return out
