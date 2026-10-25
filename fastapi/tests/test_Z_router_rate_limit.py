"""Router endpoints (media, cheripass, official, readiness) share main.py's rate-limit buckets."""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import ratelimit


def _app(monkeypatch, limit="2/minute"):
    monkeypatch.setitem(ratelimit.RATE_LIMITS, "media", limit)
    ratelimit.limiter.reset()
    app = FastAPI()

    @app.get("/probe", dependencies=[Depends(ratelimit.rate_limit("media"))])
    async def probe():
        return {"ok": True}

    return TestClient(app)


def test_third_call_is_throttled_with_generic_429(monkeypatch):
    c = _app(monkeypatch)
    assert c.get("/probe").status_code == 200
    assert c.get("/probe").status_code == 200
    r = c.get("/probe")
    assert r.status_code == 429
    assert r.json() == {"detail": "too_many_requests"}
    assert r.headers["retry-after"] == "60"


def test_disabled_limiter_never_throttles(monkeypatch):
    c = _app(monkeypatch, "1/minute")
    monkeypatch.setattr(ratelimit.limiter, "enabled", False)
    assert all(c.get("/probe").status_code == 200 for _ in range(5))


def test_every_router_route_is_rate_limited():
    import main  # noqa: F401  (mounts the feature routers)

    paths = {
        "/media/blurred", "/media/photos/{target_id}", "/media/photos/batch",
        "/cheripass/token", "/cheripass/verify", "/analytics/sponsored-click",
        "/matching/notify-official-withdrawn",
        "/assessment/submit", "/connect/activate", "/connect/introductions",
    }
    found = {}
    for r in main.app.routes:
        if getattr(r, "path", None) in paths:
            deps = [d.call for d in r.dependant.dependencies]
            found[r.path] = any(getattr(fn, "__qualname__", "").startswith("rate_limit.") for fn in deps)
    assert set(found) == paths
    assert all(found.values()), {p: v for p, v in found.items() if not v}
