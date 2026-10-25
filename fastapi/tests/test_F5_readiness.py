"""
F5 — server-side reflection scoring + Connect activation (readiness.py).

H1.13 / H15.A3: the score and connect_active are computed and written by the
server; a client-sent score is ignored; activation is refused with each
missing item; an official pair blocks turning introductions on.
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
from urllib.parse import unquote

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import readiness
from auth import get_current_user_id

REST = readiness._REST
U = "u0000000-0000-0000-0000-000000000000"
OTHER = "o0000000-0000-0000-0000-000000000000"
DIMS = [f"d{i}" for i in range(1, 8)]


def _bank():
    """7 questions (one per dimension), options g (good) and r (red flag)."""
    questions = [{"id": f"q{i}", "dimension_id": f"d{i}", "question_text": f"Q{i}"} for i in range(1, 8)]
    options = []
    for i in range(1, 8):
        options.append({"id": f"o{i}g", "question_id": f"q{i}", "option_key": "A", "option_text": "good", "red_flag": False})
        options.append({"id": f"o{i}r", "question_id": f"q{i}", "option_key": "D", "option_text": "red", "red_flag": True})
    return questions, options


def _ids(request):
    raw = unquote(request.url.params.get("id", ""))
    if raw.startswith("in.("):
        return {x.strip('"') for x in raw[4:-1].split(",")}
    return {raw[3:]} if raw.startswith("eq.") else set()


GOOD_BIO = json.dumps({"about_yourself": "I love long walks and good books.", "languages": ["English"],
                       "intent": "long_term", "gallery_urls": ["https://x/p.jpg"], "intros_paused": True})


@pytest.fixture
def env(monkeypatch):
    questions, options = _bank()
    state = {
        "session": {"id": "s1", "user_id": U, "is_completed": False},
        "responses": [],
        "profile": {"user_id": U, "bio": GOOD_BIO, "gender": "female", "verification_final_status": "verified"},
        "score_row": {"total_score": 86, "assessment_count": 1},
        "official": [],
        "session_patches": [], "score_upserts": [], "bio_patches": [], "emails": [],
    }

    async def fake_email(**kw):
        state["emails"].append(kw)
        return True

    monkeypatch.setattr(readiness, "send_transactional_email", fake_email)
    app = FastAPI()
    app.include_router(readiness.router)
    app.dependency_overrides[get_current_user_id] = lambda: U

    def sessions_patch(request):
        state["session_patches"].append((request.url.params, json.loads(request.content)))
        return httpx.Response(200, json=[] if state["session"]["is_completed"] else [{"id": "s1"}])

    def upsert(request):
        if state.get("upsert_fails"):
            return httpx.Response(400, json={"message": "x"})
        state["score_upserts"].append((request, json.loads(request.content)))
        return httpx.Response(201, json=[{}])

    def bio_patch(request):
        body = json.loads(request.content)
        if state.get("bio_conflicts", 0) > 0:
            state["bio_conflicts"] -= 1
            state["bio_conflict_hits"] = state.get("bio_conflict_hits", 0) + 1
            return httpx.Response(200, json=[])  # bio changed under us
        state["bio_patches"].append((request.url.params, body))
        state["profile"]["bio"] = body["bio"]
        return httpx.Response(200, json=[{"user_id": U}])

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{REST}/cheri_ai_assessment_sessions").mock(
            side_effect=lambda r: httpx.Response(200, json=[state["session"]] if state["session"] else []))
        mock.patch(f"{REST}/cheri_ai_assessment_sessions").mock(side_effect=sessions_patch)
        mock.get(f"{REST}/cheri_ai_assessment_responses").mock(
            side_effect=lambda r: httpx.Response(200, json=state["responses"]))
        mock.get(f"{REST}/cheri_ai_question_options").mock(
            side_effect=lambda r: httpx.Response(200, json=[o for o in options if o["id"] in _ids(r)]))
        mock.get(f"{REST}/cheri_ai_laws_questions").mock(
            side_effect=lambda r: httpx.Response(200, json=[q for q in questions if q["id"] in _ids(r)]))
        mock.get(f"{REST}/cheri_ai_dimensions").mock(
            return_value=httpx.Response(200, json=[{"id": d} for d in DIMS]))
        mock.get(f"{REST}/cheri_ai_user_profiles").mock(
            side_effect=lambda r: httpx.Response(200, json=[state["score_row"]] if state["score_row"] else []))
        mock.post(f"{REST}/cheri_ai_user_profiles").mock(side_effect=upsert)
        mock.get(f"{REST}/user_profiles").mock(
            side_effect=lambda r: httpx.Response(200, json=[state["profile"]] if state["profile"] else []))
        mock.patch(f"{REST}/user_profiles").mock(side_effect=bio_patch)
        mock.get(f"{REST}/matches").mock(side_effect=lambda r: httpx.Response(200, json=state["official"]))
        yield state, TestClient(app)


def _answers(reds):
    return [{"question_id": f"q{i}", "option_id": f"o{i}{'r' if i <= reds else 'g'}"} for i in range(1, 8)]


# ------------------------------------------------------------------ pure parity with the app formula

def test_formula_parity_with_client():
    assert [readiness.score_from_red_flags(n) for n in (0, 1, 2, 3, 7, 9)] == [100, 86, 72, 58, 2, 0]
    assert readiness.is_eligible(70) and not readiness.is_eligible(69) and not readiness.is_eligible(None)
    assert [readiness.profile_tier_for(n) for n in (0, 1, 2, 3, 5)] == \
        ["aligned", "conscious", "developing", "cautionary", "high_risk"]
    # Latest answer per question wins (stored rows ordered by created_at).
    rows = [{"question_id": "q1", "option_id": "a", "created_at": "2026-10-25T10:00:01Z"},
            {"question_id": "q2", "option_id": "b", "created_at": "2026-10-25T10:00:02Z"},
            {"question_id": "q1", "option_id": "c", "created_at": "2026-10-25T10:00:09Z"}]
    assert [(a["question_id"], a["option_id"]) for a in readiness.dedupe_answers(rows)] == [("q2", "b"), ("q1", "c")]
    # Body answers: later entry wins.
    body = [{"question_id": "q1", "option_id": "a"}, {"question_id": "q1", "option_id": "z"}]
    assert readiness.dedupe_answers(body) == [{"question_id": "q1", "option_id": "z"}]


def test_profile_rule_matches_profileStatus():
    p = {"bio": GOOD_BIO, "gender": "male"}
    assert readiness.missing_profile_fields(p) == []
    # Questions are optional; short about / no photo / plain-text bio are incomplete.
    assert readiness.missing_profile_fields({**p, "gender": None}) == ["gender"]
    short = json.dumps({**json.loads(GOOD_BIO), "about_yourself": "  too short   ", "gallery_urls": []})
    assert readiness.missing_profile_fields({**p, "bio": short}) == ["about", "photo"]
    assert readiness.missing_profile_fields({**p, "bio": "plain text bio more than twenty chars"}) == \
        ["about", "languages", "intent", "photo"]


# ------------------------------------------------------------------ /assessment/submit

def test_submit_recomputes_score_and_ignores_client_score(env):
    state, client = env
    res = client.post("/assessment/submit", json={"session_id": "s1", "answers": _answers(3), "score": 100, "eligible": True})
    assert res.status_code == 200, res.text
    assert res.json()["score"] == 58 and res.json()["eligible"] is False and res.json()["red_flags"] == 3
    params, body = state["session_patches"][0]
    assert params["is_completed"] == "not.is.true" and params["user_id"] == f"eq.{U}"
    assert body["is_completed"] is True and body["completion_score"] == 58 and body["total_score"] == 58
    snap = body["qa_snapshot"]
    assert snap["scoring_version"] == "redflag14-v1" and snap["threshold"] == 70 and snap["scored_by"] == "server"
    assert len(snap["answers"]) == 7 and snap["answers"][0]["is_red_flag"] is True
    req, up = state["score_upserts"][0]
    assert up["total_score"] == 58 and up["user_id"] == U and up["assessment_count"] == 2
    assert req.url.params["on_conflict"] == "user_id" and "merge-duplicates" in req.headers["prefer"]
    assert state["emails"][0]["extra_vars"]["score"] == 58


def test_submit_dedupes_duplicate_answers_latest_wins(env):
    state, client = env
    answers = _answers(0) + [{"question_id": "q3", "option_id": "o3r"}]
    assert client.post("/assessment/submit", json={"session_id": "s1", "answers": answers}).json()["score"] == 86
    state["session"]["is_completed"] = False
    answers = _answers(1) + [{"question_id": "q1", "option_id": "o1g"}]
    assert client.post("/assessment/submit", json={"session_id": "s1", "answers": answers}).json()["score"] == 100


def test_submit_uses_stored_responses_and_option_red_flag_not_row_flag(env):
    state, client = env
    # Stored rows claim is_red_flag=false, but the options chosen are red flags.
    state["responses"] = [{"question_id": f"q{i}", "selected_option_id": f"o{i}{'r' if i <= 2 else 'g'}",
                           "is_red_flag": False, "created_at": f"2026-10-25T10:00:0{i}Z"} for i in range(1, 8)]
    res = client.post("/assessment/submit", json={"session_id": "s1"})
    assert res.json()["score"] == 72 and res.json()["eligible"] is True


def test_submit_clamps_at_zero(env):
    state, client = env
    assert client.post("/assessment/submit", json={"session_id": "s1", "answers": _answers(7)}).json()["score"] == 2


def test_submit_not_owner_is_403_and_writes_nothing(env):
    state, client = env
    state["session"]["user_id"] = OTHER
    res = client.post("/assessment/submit", json={"session_id": "s1", "answers": _answers(0)})
    assert res.status_code == 403 and res.json()["detail"]["code"] == "not_owner"
    assert state["session_patches"] == [] and state["score_upserts"] == []


def test_submit_completed_or_missing_session(env):
    state, client = env
    state["session"]["is_completed"] = True
    assert client.post("/assessment/submit", json={"session_id": "s1", "answers": _answers(0)}).status_code == 409
    state["session"] = None
    assert client.post("/assessment/submit", json={"session_id": "s1", "answers": _answers(0)}).status_code == 404
    assert state["score_upserts"] == []


def test_submit_refuses_incomplete_and_foreign_options(env):
    state, client = env
    res = client.post("/assessment/submit", json={"session_id": "s1", "answers": _answers(0)[:5]})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "incomplete"
    bad = _answers(0)
    bad[0] = {"question_id": "q1", "option_id": "o2g"}  # option of another question
    res = client.post("/assessment/submit", json={"session_id": "s1", "answers": bad})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "invalid_answer"
    assert state["session_patches"] == []


def test_submit_score_write_failure_surfaces(env):
    state, client = env
    state["upsert_fails"] = True
    res = client.post("/assessment/submit", json={"session_id": "s1", "answers": _answers(0)})
    assert res.status_code == 502 and res.json()["detail"]["code"] == "score_write_failed"


# ------------------------------------------------------------------ /connect/activate

def test_activate_when_ready_writes_bio_with_service_key(env):
    state, client = env
    res = client.post("/connect/activate")
    assert res.status_code == 200, res.text
    params, body = state["bio_patches"][0]
    bio = json.loads(body["bio"])
    assert bio["connect_active"] is True and bio["relationship_focus"] == "single" and bio["intros_paused"] is False
    assert bio["languages"] == ["English"]  # other keys kept
    # Optimistic concurrency: only patches the bio version it read.
    assert params["bio"] == f"eq.{GOOD_BIO}"


@pytest.mark.parametrize("mutate,item", [
    (lambda s: s["profile"].update(verification_final_status="pending"), "identity"),
    (lambda s: s["profile"].update(gender=None), "profile"),
    (lambda s: s["score_row"].update(total_score=69), "reflection"),
    (lambda s: s.update(score_row=None), "reflection"),
    (lambda s: s.update(official=[{"id": "m1"}]), "official"),
])
def test_activate_refused_for_each_missing_item(env, mutate, item):
    state, client = env
    mutate(state)
    res = client.post("/connect/activate")
    assert res.status_code == 409
    d = res.json()["detail"]
    assert d["code"] == "not_ready" and d["missing"] == [item]
    assert state["bio_patches"] == []


def test_identity_alone_cannot_activate(env):
    state, client = env
    state["profile"]["bio"] = json.dumps({"gallery_urls": []})
    state["score_row"]["total_score"] = 58
    d = client.post("/connect/activate").json()["detail"]
    assert d["missing"] == ["profile", "reflection"] and "about" in d["profile_fields"]
    assert state["bio_patches"] == []


def test_exactly_70_qualifies(env):
    state, client = env
    state["score_row"]["total_score"] = 70
    assert client.post("/connect/activate").status_code == 200


def test_bio_write_retries_after_concurrent_change(env):
    state, client = env
    state["bio_conflicts"] = 1
    assert client.post("/connect/activate").status_code == 200
    assert state["bio_conflict_hits"] == 1 and len(state["bio_patches"]) == 1
    state["bio_conflicts"] = 5  # never settles -> 409, nothing written
    assert client.post("/connect/introductions", json={"paused": True}).status_code == 409


# ------------------------------------------------------------------ /connect/introductions

def test_official_blocks_intros_on_but_pause_allowed(env):
    state, client = env
    state["official"] = [{"id": "m1"}]
    res = client.post("/connect/introductions", json={"paused": False})
    assert res.status_code == 409 and "official" in res.json()["detail"]["missing"]
    assert state["bio_patches"] == []
    assert client.post("/connect/introductions", json={"paused": True}).status_code == 200
    bio = json.loads(state["bio_patches"][0][1]["bio"])
    assert bio["intros_paused"] is True and bio["official_end_choice_pending"] is False


def test_intros_on_requires_readiness(env):
    state, client = env
    state["score_row"]["total_score"] = 40
    res = client.post("/connect/introductions", json={"paused": False})
    assert res.status_code == 409 and res.json()["detail"]["missing"] == ["reflection"]
    state["score_row"]["total_score"] = 90
    assert client.post("/connect/introductions", json={"paused": False}).status_code == 200
    assert json.loads(state["profile"]["bio"])["intros_paused"] is False


def test_routes_require_auth():
    app = FastAPI()
    app.include_router(readiness.router)
    c = TestClient(app)
    assert c.post("/connect/activate").status_code == 401
    assert c.post("/assessment/submit", json={"session_id": "s1"}).status_code == 401


def test_router_registered_in_main_feature_loop():
    src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    assert '"readiness"' in src.split("for _mod in (", 1)[1].split(")", 1)[0]


# ------------------------------------------------------------------ migrations (static; NOT applied)

_MIG = os.path.join(os.path.dirname(__file__), "..", "migrations")


def test_053_guard_is_marked_unapplied_and_parses():
    src = open(os.path.join(_MIG, "053_connect_eligibility_guard.sql")).read()
    assert src.startswith("-- NOT APPLIED — needs approval")
    for needle in ("trg_guard_reflection_score", "trg_guard_assessment_session", "trg_guard_user_profiles_connect",
                   "current_user NOT IN ('authenticated', 'anon')", "public.is_admin()"):
        assert needle in src
    pglast = pytest.importorskip("pglast")
    pglast.parse_sql(src)


def test_049_merge_profile_bio_refuses_connect_active_true():
    src = open(os.path.join(_MIG, "049_official_transactions.sql")).read()
    body = src.split("CREATE OR REPLACE FUNCTION public.merge_profile_bio", 1)[1].split("END $$;", 1)[0]
    assert "protected_key:connect_active" in body and "'false'::jsonb" in body
    pglast = pytest.importorskip("pglast")
    pglast.parse_sql(src)
