"""
F3 — official connection atomicity (migration 049 ready + graceful fallback).

partnership.finalize_partnership on both paths:
  • RPC path (049 applied): accept_official_request already stamped the pair
    in one transaction and left an unprocessed 'accepted' event; finalize
    must NOT re-stamp, must send the official notices only if it claims
    that event, and must notify the third parties the RPC closed.
  • Legacy path (049 not applied): the event table is missing (404); the
    conditional stamp + sweep run unchanged and still succeed.
official.py /matching/notify-official-withdrawn (B H14.7-c).
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
from fastapi import FastAPI
from fastapi.testclient import TestClient

import official
import partnership

PG = partnership._PG_BASE
PA, PB = "pa000000-0000-0000-0000-000000000000", "pb000000-0000-0000-0000-000000000000"
T = "tt000000-0000-0000-0000-000000000000"


@pytest.fixture
def emails(monkeypatch):
    calls = []

    async def fake(**kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(partnership, "send_transactional_email", fake)
    return calls


def _mock_sweep(match_row):
    """Mocks every REST call finalize makes; returns the routes for assertions."""
    def matches(request):
        q = request.url.params
        if q.get("id") == "eq.m1":
            return httpx.Response(200, json=[match_row])
        return httpx.Response(200, json=[])  # no official elsewhere, no active side matches

    r = {
        "matches_get": respx.get(f"{PG}/matches").mock(side_effect=matches),
        "matches_patch": respx.patch(f"{PG}/matches").mock(
            return_value=httpx.Response(200, json=[{"id": "m1"}])),
        "notif_post": respx.post(f"{PG}/notifications").mock(return_value=httpx.Response(201, json=[])),
        "notif_get": respx.get(f"{PG}/notifications").mock(return_value=httpx.Response(200, json=[])),
        "notif_delete": respx.delete(f"{PG}/notifications").mock(return_value=httpx.Response(204)),
        "profiles": respx.get(f"{PG}/public_profiles").mock(return_value=httpx.Response(
            200, json=[{"user_id": PA, "nick_name": "Alex"}, {"user_id": PB, "nick_name": "Sofia"}])),
        "req_get": respx.get(f"{PG}/match_requests").mock(return_value=httpx.Response(200, json=[])),
        "req_delete": respx.delete(f"{PG}/match_requests").mock(return_value=httpx.Response(204)),
        "views_delete": respx.delete(f"{PG}/user_match_views").mock(return_value=httpx.Response(204)),
    }
    return r


OFFICIAL_ROW = {"id": "m1", "user_a_id": PA, "user_b_id": PB, "is_active": True,
                "partner_accepted_by_a": True, "partner_accepted_by_b": True,
                "partnered_at": "2026-10-25T10:00:00+00:00"}
PENDING_BOTH = {**OFFICIAL_ROW, "partnered_at": None}


# --------------------------------------------------------------- RPC path

@respx.mock
async def test_rpc_path_claims_event_sends_notices_once_and_never_restamps(emails):
    r = _mock_sweep(OFFICIAL_ROW)
    claim = respx.patch(f"{PG}/official_request_events").mock(return_value=httpx.Response(200, json=[{
        "id": "ev1", "match_id": "m1", "event": "accepted",
        "details": {"closed": [{"match_id": "s1", "partner": PA, "third_party": T}], "invalidated": ["s1"]},
    }]))
    res = await partnership.finalize_partnership("m1", PB)

    # The RPC already committed the pair: no stamp PATCH on matches.
    assert not r["matches_patch"].called
    # Claim is conditional on processed_at IS NULL for this match's accepted event.
    q = claim.calls[0].request.url.params
    assert q["match_id"] == "eq.m1" and q["event"] == "eq.accepted" and q["processed_at"] == "is.null"
    assert res["side_effects_sent"] is True and res["already_finalized"] is True
    # Celebration notices for both + partner_dropped for the closed third party.
    posted = [json.loads(c.request.content) for c in r["notif_post"].calls]
    flat = [row for p in posted for row in (p if isinstance(p, list) else [p])]
    assert sorted(x["user_id"] for x in flat if x["type"] == "partnered") == sorted([PA, PB])
    assert [x["user_id"] for x in flat if x["type"] == "partner_dropped"] == [T]
    slugs = sorted((e["template_slug"], e["user_id"]) for e in emails)
    assert ("connection_lost", T) in slugs
    assert [e["user_id"] for e in emails if e["template_slug"] == "partner_proposal_accepted"] == [PA, PB]
    assert res["side_matches_deactivated"] == 1


@respx.mock
async def test_rpc_path_already_claimed_is_a_quiet_self_heal(emails):
    r = _mock_sweep(OFFICIAL_ROW)
    respx.patch(f"{PG}/official_request_events").mock(return_value=httpx.Response(200, json=[]))
    res = await partnership.finalize_partnership("m1", PA)
    assert res["side_effects_sent"] is False
    assert not r["notif_post"].called and emails == []
    assert not r["matches_patch"].called
    # Idempotent sweep still runs (interests deleted, recent views cleared).
    assert r["req_delete"].called and r["views_delete"].called


@respx.mock
async def test_rpc_path_failure_while_sending_releases_the_claim(emails):
    r = _mock_sweep(OFFICIAL_ROW)
    r["profiles"].mock(return_value=httpx.Response(503, json={}))
    events = respx.patch(f"{PG}/official_request_events").mock(side_effect=[
        httpx.Response(200, json=[{"id": "ev1", "event": "accepted", "details": {}}]),
        httpx.Response(200, json=[]),
    ])
    with pytest.raises(httpx.HTTPStatusError):
        await partnership.finalize_partnership("m1", PA)
    release = events.calls[1].request
    assert release.url.params["id"] == "in.(ev1)"
    assert json.loads(release.content) == {"processed_at": None}


# --------------------------------------------------------------- legacy fallback

@respx.mock
async def test_legacy_path_without_event_table_still_finalizes(emails):
    r = _mock_sweep(PENDING_BOTH)
    ev = respx.post(f"{PG}/official_request_events").mock(
        return_value=httpx.Response(404, json={"code": "PGRST205", "message": "Could not find the table"}))
    res = await partnership.finalize_partnership("m1", PA)
    assert r["matches_patch"].called  # conditional stamp
    stamp_q = r["matches_patch"].calls[0].request.url.params
    assert stamp_q["partnered_at"] == "is.null" and stamp_q["is_active"] == "eq.true"
    assert ev.called  # tried to record history, tolerated the missing table
    assert res["side_effects_sent"] is True and res["already_finalized"] is False
    assert [e["user_id"] for e in emails if e["template_slug"] == "partner_proposal_accepted"] == [PA, PB]


@respx.mock
async def test_legacy_path_records_processed_accepted_event_when_table_exists(emails):
    _mock_sweep(PENDING_BOTH)
    ev = respx.post(f"{PG}/official_request_events").mock(return_value=httpx.Response(201, json=[]))
    claim = respx.patch(f"{PG}/official_request_events")
    await partnership.finalize_partnership("m1", PA)
    body = json.loads(ev.calls[0].request.content)
    assert body["match_id"] == "m1" and body["actor_id"] == PA and body["event"] == "accepted"
    assert body["processed_at"]  # backend sent the notices itself → already processed
    assert not claim.called  # a fresh stamp never claims


# --------------------------------------------------------------- official.py

@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(official.router)
    app.dependency_overrides[official.get_current_user_id] = lambda: PA
    return TestClient(app)


def _match(**over):
    return {"user_a_id": PA, "user_b_id": PB, "is_active": True, "partnered_at": None,
            "partner_accepted_by_a": False, "partner_accepted_by_b": False, **over}


@respx.mock
def test_withdrawn_clears_only_the_recipients_stale_proposal_notice(api):
    respx.get(f"{official._REST}/matches").mock(return_value=httpx.Response(200, json=[_match()]))
    d = respx.delete(f"{official._REST}/notifications").mock(return_value=httpx.Response(200, json=[{"id": "n1"}]))
    r = api.post("/matching/notify-official-withdrawn", json={"match_id": "m1"})
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": 1}
    q = d.calls[0].request.url.params
    assert q["user_id"] == f"eq.{PB}" and q["type"] == "eq.partner_proposal" and q["related_user_id"] == f"eq.{PA}"


@pytest.mark.parametrize("row,skipped", [
    (_match(partner_accepted_by_a=True), "request_not_withdrawn"),   # still pending
    (_match(partnered_at="t"), "request_not_withdrawn"),              # accept won
    (_match(is_active=False), "request_not_withdrawn"),               # ended
    (_match(user_a_id="x", user_b_id="y"), "not_a_participant"),
])
@respx.mock
def test_withdrawn_endpoint_refuses_without_a_real_withdrawal(api, row, skipped):
    respx.get(f"{official._REST}/matches").mock(return_value=httpx.Response(200, json=[row]))
    d = respx.delete(f"{official._REST}/notifications")
    r = api.post("/matching/notify-official-withdrawn", json={"match_id": "m1"})
    assert r.json()["skipped"] == skipped
    assert not d.called


# --------------------------------------------------------------- E H14S.16

@respx.mock
async def test_third_parties_are_never_told_why_the_connection_closed(emails):
    """partner_dropped in-app body + connection_lost emails (mutual-match and
    pending-interest third parties) carry no reason (handoff §14)."""
    P2 = "pp000000-0000-0000-0000-000000000000"
    r = _mock_sweep(OFFICIAL_ROW)
    r["req_get"].mock(return_value=httpx.Response(200, json=[{"sender_id": P2, "receiver_id": PB}]))
    respx.patch(f"{PG}/official_request_events").mock(return_value=httpx.Response(200, json=[{
        "id": "ev1", "event": "accepted",
        "details": {"closed": [{"match_id": "s1", "partner": PA, "third_party": T}]},
    }]))
    await partnership.finalize_partnership("m1", PA)

    posted = [json.loads(c.request.content) for c in r["notif_post"].calls]
    dropped = [x for p in posted for x in (p if isinstance(p, list) else [p]) if x["type"] == "partner_dropped"]
    assert [(d["user_id"], d["title"], d["body"]) for d in dropped] == [(T, "A connection has ended", "Your connection has ended.")]

    lost = [e for e in emails if e["template_slug"] == "connection_lost"]
    assert sorted(e["user_id"] for e in lost) == sorted([T, P2])
    banned = ("official", "committed", "partnership", "someone else")
    for e in lost:
        assert not e.get("extra_vars")  # no caller-supplied context line at all
        assert not any(b in json.dumps(e).lower() for b in banned)
    for d in dropped:
        assert not any(b in (d["title"] + d["body"]).lower() for b in banned)
