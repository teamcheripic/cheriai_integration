"""
F1 · H14S.14 POST /safety/report-submitted — private receipt for the
reporter only. Supabase respx-mocked; get_current_user_id overridden.
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
from fastapi.testclient import TestClient

import main

A = "aaaaaaaa-0000-0000-0000-00000000000a"
REPORT = "dddddddd-0000-0000-0000-00000000000d"
REST = f"{main.SUPABASE_URL}/rest/v1"


@pytest.fixture
def client():
    main.app.dependency_overrides[main.get_current_user_id] = lambda: A
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _mock(report_rows, existing=(), insert_status=201, insert_body=None):
    rep = respx.get(f"{REST}/user_reports").mock(return_value=httpx.Response(200, json=list(report_rows)))
    ex = respx.get(f"{REST}/notifications").mock(return_value=httpx.Response(200, json=list(existing)))
    ins = respx.post(f"{REST}/notifications").mock(
        return_value=httpx.Response(insert_status, json=insert_body if insert_body is not None else [])
    )
    return rep, ex, ins


@respx.mock
def test_receipt_created_for_own_report_with_no_subject_details(client):
    rep, ex, ins = _mock([{"id": REPORT}])
    r = client.post("/safety/report-submitted", json={"report_id": REPORT})
    assert r.status_code == 200 and r.json() == {"ok": True, "receipt": "created"}
    q = rep.calls.last.request.url.params
    assert q["id"] == f"eq.{REPORT}" and q["reporter_id"] == f"eq.{A}"  # ownership from the JWT
    row = json.loads(ins.calls.last.request.content)
    assert row == {
        "user_id": A, "type": "report_submitted", "title": "Report received",
        "body": "Thanks for telling us. Our team will review it.", "related_id": REPORT,
    }
    assert "related_user_id" not in row  # reported member never referenced


@respx.mock
def test_someone_elses_or_unknown_report_is_404_and_writes_nothing(client):
    _, _, ins = _mock([])
    r = client.post("/safety/report-submitted", json={"report_id": REPORT})
    assert r.status_code == 404 and r.json() == {"detail": "report_not_found"}
    assert not ins.called


@respx.mock
def test_idempotent_per_report(client):
    _, ex, ins = _mock([{"id": REPORT}], existing=[{"id": "n1"}])
    r = client.post("/safety/report-submitted", json={"report_id": REPORT})
    assert r.json() == {"ok": True, "receipt": "exists"}
    assert not ins.called
    q = ex.calls.last.request.url.params
    assert q["type"] == "eq.report_submitted" and q["related_id"] == f"eq.{REPORT}" and q["user_id"] == f"eq.{A}"


@respx.mock
def test_type_constraint_before_051_is_deferred_not_an_error(client):
    _mock([{"id": REPORT}], insert_status=400,
          insert_body={"code": "23514", "message": 'violates check constraint "notifications_type_check"'})
    r = client.post("/safety/report-submitted", json={"report_id": REPORT})
    assert r.status_code == 200 and r.json() == {"ok": True, "receipt": "deferred"}


def test_validation_and_auth():
    main.app.dependency_overrides[main.get_current_user_id] = lambda: A
    try:
        assert TestClient(main.app).post("/safety/report-submitted", json={"report_id": "nope"}).status_code == 400
    finally:
        main.app.dependency_overrides.clear()
    assert TestClient(main.app).post("/safety/report-submitted", json={"report_id": REPORT}).status_code == 401


@respx.mock
def test_lookup_failure_is_502(client):
    respx.get(f"{REST}/user_reports").mock(return_value=httpx.Response(500))
    assert client.post("/safety/report-submitted", json={"report_id": REPORT}).status_code == 502
