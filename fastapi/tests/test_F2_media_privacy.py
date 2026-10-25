"""
F2 — photo / identity-document privacy (handoff §13 "Security and media").

  • media.py: blurred derivative generation, original delivery gated on an
    active accepted match (and no block), signed URLs for private refs.
  • face_verification.py: `storage://kyc/<uid>/…` refs accepted for the
    caller's own folder and downloaded with the service key.

All network is mocked with respx; the router is mounted on a throwaway
FastAPI app with get_current_user_id overridden. No live Supabase.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "service-test-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import io
import json

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import auth
import face_verification as fv
import media

BASE = os.environ["SUPABASE_URL"].rstrip("/")
A = "aaaaaaaa-0000-0000-0000-00000000000a"
B = "bbbbbbbb-0000-0000-0000-00000000000b"
C = "cccccccc-0000-0000-0000-00000000000c"


def _app(caller=A, admin=False):
    app = FastAPI()
    app.include_router(media.router)
    app.dependency_overrides[auth.get_current_user_id] = lambda: caller
    if admin:
        app.dependency_overrides[auth.get_current_admin_id] = lambda: caller
    return TestClient(app)


def _jpeg(w=600, h=800, color=(200, 30, 30)) -> bytes:
    im = Image.new("RGB", (w, h), color)
    # A sharp high-contrast pattern the blur must destroy.
    for x in range(0, w, 4):
        for y in range(h):
            im.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    return buf.getvalue()


def _profile(uid, *, avatar=None, gallery=(), blur=None, gblur=()):
    bio = {"gallery_urls": list(gallery), "about_yourself": "hi"}
    if blur:
        bio["blur_url"] = blur
    if gblur:
        bio["gallery_blur_urls"] = list(gblur)
    return {"user_id": uid, "avatar_url": avatar, "bio": json.dumps(bio)}


# --------------------------------------------------------------------------
# storage refs
# --------------------------------------------------------------------------

def test_parse_storage_ref_accepts_refs_and_project_urls_only():
    assert media.parse_storage_ref(f"storage://media/{A}/p.jpg") == ("media", f"{A}/p.jpg")
    assert media.parse_storage_ref(f"{BASE}/storage/v1/object/public/avatars/{A}/p.jpg") == ("avatars", f"{A}/p.jpg")
    assert media.parse_storage_ref(f"{BASE}/storage/v1/object/sign/media/{A}/p.jpg?token=x") == ("media", f"{A}/p.jpg")
    assert media.parse_storage_ref("https://evil.example.com/storage/v1/object/public/avatars/a/p.jpg") is None
    assert media.parse_storage_ref(f"storage://avatars/{A}/../{B}/p.jpg") is None
    assert media.parse_storage_ref(f"{BASE}/storage/v1/object/public/avatars/{A}/%2e%2e/x.jpg") is None
    assert media.parse_storage_ref("") is None


def test_blur_path_is_deterministic_and_sanitised():
    assert media.blur_path_for(A, f"{A}/primary-123.jpg") == f"{A}/blur/primary-123.jpg"
    assert media.blur_path_for(A, f"{A}/we ird$.png") == f"{A}/blur/weird.jpg"


def test_make_blurred_jpeg_downscales_and_destroys_detail():
    src = _jpeg()
    out = media.make_blurred_jpeg(src)
    im = Image.open(io.BytesIO(out))
    assert im.format == "JPEG"
    assert max(im.size) <= 320
    # The 1-px white stripes are gone: neighbouring pixels are nearly equal.
    px = [im.getpixel((x, im.size[1] // 2)) for x in range(10, 30)]
    spread = max(p[1] for p in px) - min(p[1] for p in px)
    assert spread < 25


# --------------------------------------------------------------------------
# POST /media/blurred
# --------------------------------------------------------------------------

@respx.mock
def test_blurred_creates_derivative_for_own_photo():
    src = f"{BASE}/storage/v1/object/public/avatars/{A}/primary-1.jpg"
    dl = respx.get(f"{BASE}/storage/v1/object/authenticated/avatars/{A}/primary-1.jpg").mock(
        return_value=httpx.Response(200, content=_jpeg())
    )
    up = respx.post(f"{BASE}/storage/v1/object/avatars/{A}/blur/primary-1.jpg").mock(
        return_value=httpx.Response(200, json={"Key": "x"})
    )
    r = _app().post("/media/blurred", json={"source_url": src})
    assert r.status_code == 200, r.text
    assert r.json()["blur_url"] == f"{BASE}/storage/v1/object/public/avatars/{A}/blur/primary-1.jpg"
    assert dl.called and up.called
    req = up.calls.last.request
    assert req.headers["x-upsert"] == "true"
    assert req.headers["content-type"] == "image/jpeg"
    assert req.headers["authorization"] == "Bearer service-test-key"  # server-side only
    assert max(Image.open(io.BytesIO(req.content)).size) <= 320


@pytest.mark.parametrize(
    "src",
    [
        f"{BASE}/storage/v1/object/public/avatars/{B}/primary-1.jpg",  # someone else's
        f"storage://kyc/{A}/kyc-face-1.jpg",  # never blur/expose identity documents
        f"{BASE}/storage/v1/object/public/avatars/{A}/blur/primary-1.jpg",  # no blur-of-blur
    ],
)
@respx.mock
def test_blurred_refuses_foreign_kyc_and_derivative_sources(src):
    route = respx.route(host="test.supabase.co")
    r = _app().post("/media/blurred", json={"source_url": src})
    assert r.status_code == 403
    assert not route.called


@respx.mock
def test_blurred_refuses_other_hosts():
    route = respx.route()
    r = _app().post("/media/blurred", json={"source_url": "http://169.254.169.254/latest/meta-data"})
    assert r.status_code == 400
    assert not route.called


@respx.mock
def test_blurred_unreadable_image_is_422_not_500():
    respx.get(f"{BASE}/storage/v1/object/authenticated/avatars/{A}/g.jpg").mock(
        return_value=httpx.Response(200, content=b"not an image")
    )
    r = _app().post("/media/blurred", json={"source_url": f"storage://avatars/{A}/g.jpg"})
    assert r.status_code == 422


# --------------------------------------------------------------------------
# GET /media/photos/{id} — originals only for self / active accepted pair
# --------------------------------------------------------------------------

ORIG = f"{BASE}/storage/v1/object/public/avatars/{B}/primary-1.jpg"
GAL = f"{BASE}/storage/v1/object/public/avatars/{B}/gallery-1.jpg"
BLUR = f"{BASE}/storage/v1/object/public/avatars/{B}/blur/primary-1.jpg"
GBLUR = f"{BASE}/storage/v1/object/public/avatars/{B}/blur/gallery-1.jpg"


def _mock_profile(row):
    respx.get(f"{BASE}/rest/v1/user_profiles").mock(return_value=httpx.Response(200, json=[row]))


@respx.mock
def test_stranger_gets_only_blurred_derivatives():
    _mock_profile(_profile(B, avatar=ORIG, gallery=[ORIG, GAL], blur=BLUR, gblur=[BLUR, GBLUR]))
    respx.get(f"{BASE}/rest/v1/matches").mock(return_value=httpx.Response(200, json=[]))
    r = _app().get(f"/media/photos/{B}")
    assert r.status_code == 200
    body = r.json()
    assert body["revealed"] is False
    assert body["avatar_url"] == BLUR
    assert body["gallery_urls"] == [BLUR, GBLUR]
    assert ORIG not in json.dumps(body) and GAL not in json.dumps(body)
    assert r.headers["cache-control"] == "no-store"


@respx.mock
def test_stranger_without_derivative_gets_placeholder_null():
    _mock_profile(_profile(B, avatar=ORIG, gallery=[ORIG]))
    respx.get(f"{BASE}/rest/v1/matches").mock(return_value=httpx.Response(200, json=[]))
    body = _app().get(f"/media/photos/{B}").json()
    assert body == {"user_id": B, "revealed": False, "avatar_url": None, "gallery_urls": [], "expires_in": None}


@respx.mock
def test_active_pair_gets_originals_legacy_public_urls():
    _mock_profile(_profile(B, avatar=ORIG, gallery=[ORIG, GAL], blur=BLUR))
    m = respx.get(f"{BASE}/rest/v1/matches").mock(
        return_value=httpx.Response(200, json=[{"user_a_id": B, "user_b_id": A}])
    )
    respx.get(f"{BASE}/rest/v1/blocked_users").mock(return_value=httpx.Response(200, json=[]))
    body = _app().get(f"/media/photos/{B}").json()
    assert body["revealed"] is True
    assert body["avatar_url"] == ORIG
    assert body["gallery_urls"] == [ORIG, GAL]
    # Only ACTIVE match rows count.
    assert m.calls.last.request.url.params["is_active"] == "eq.true"


@respx.mock
def test_private_media_refs_are_delivered_as_short_lived_signed_urls():
    ref = f"storage://media/{B}/primary-9.jpg"
    _mock_profile(_profile(B, avatar=ref, gallery=[ref]))
    respx.get(f"{BASE}/rest/v1/matches").mock(return_value=httpx.Response(200, json=[{"user_a_id": A, "user_b_id": B}]))
    respx.get(f"{BASE}/rest/v1/blocked_users").mock(return_value=httpx.Response(200, json=[]))
    sign = respx.post(f"{BASE}/storage/v1/object/sign/media/{B}/primary-9.jpg").mock(
        return_value=httpx.Response(200, json={"signedURL": f"/object/sign/media/{B}/primary-9.jpg?token=t"})
    )
    body = _app().get(f"/media/photos/{B}").json()
    assert body["avatar_url"] == f"{BASE}/storage/v1/object/sign/media/{B}/primary-9.jpg?token=t"
    assert body["expires_in"] == media.SIGNED_URL_TTL <= 900
    assert json.loads(sign.calls.last.request.content) == {"expiresIn": media.SIGNED_URL_TTL}
    assert "storage://" not in json.dumps(body)


@respx.mock
def test_ended_or_blocked_pair_stops_receiving_originals():
    _mock_profile(_profile(B, avatar=ORIG, blur=BLUR))
    respx.get(f"{BASE}/rest/v1/matches").mock(return_value=httpx.Response(200, json=[{"user_a_id": A, "user_b_id": B}]))
    respx.get(f"{BASE}/rest/v1/blocked_users").mock(
        return_value=httpx.Response(200, json=[{"blocker_id": B, "blocked_id": A}])
    )
    body = _app().get(f"/media/photos/{B}").json()
    assert body["revealed"] is False and body["avatar_url"] == BLUR


@respx.mock
def test_self_always_gets_originals_without_match_lookup():
    _mock_profile(_profile(A, avatar=ORIG.replace(B, A)))
    m = respx.get(f"{BASE}/rest/v1/matches")
    body = _app().get(f"/media/photos/{A}").json()
    assert body["revealed"] is True and body["avatar_url"] == ORIG.replace(B, A)
    assert not m.called


def test_invalid_target_id_is_422():
    assert _app().get("/media/photos/not-a-uuid;drop").status_code == 422


@respx.mock
def test_unknown_member_is_404():
    respx.get(f"{BASE}/rest/v1/user_profiles").mock(return_value=httpx.Response(200, json=[]))
    assert _app().get(f"/media/photos/{B}").status_code == 404


@respx.mock
def test_batch_reveals_only_active_pairs():
    respx.get(f"{BASE}/rest/v1/user_profiles").mock(
        return_value=httpx.Response(200, json=[
            _profile(B, avatar=ORIG, blur=BLUR),
            _profile(C, avatar=ORIG.replace(B, C), blur=BLUR.replace(B, C)),
        ])
    )
    respx.get(f"{BASE}/rest/v1/matches").mock(return_value=httpx.Response(200, json=[{"user_a_id": A, "user_b_id": B}]))
    respx.get(f"{BASE}/rest/v1/blocked_users").mock(return_value=httpx.Response(200, json=[]))
    r = _app().post("/media/photos/batch", json={"user_ids": [B, C]})
    assert r.status_code == 200
    by = {p["user_id"]: p for p in r.json()["photos"]}
    assert by[B]["revealed"] is True and by[B]["avatar_url"] == ORIG
    assert by[C]["revealed"] is False and by[C]["avatar_url"] == BLUR.replace(B, C)


def test_batch_caps_at_50():
    assert _app().post("/media/photos/batch", json={"user_ids": [B] * 51}).status_code == 422


# --------------------------------------------------------------------------
# face_verification — private kyc refs
# --------------------------------------------------------------------------

def test_kyc_storage_refs_allowed_only_in_own_folder():
    assert fv.is_own_storage_image_url(f"storage://kyc/{A}/kyc-face-1.jpg", A)
    assert fv.is_own_storage_image_url(f"storage://avatars/{A}/kyc-face-1.jpg", A)
    assert not fv.is_own_storage_image_url(f"storage://kyc/{B}/kyc-face-1.jpg", A)
    assert not fv.is_own_storage_image_url(f"storage://kyc/{A}/../{B}/x.jpg", A)
    assert not fv.is_own_storage_image_url(f"storage://media/{A}/p.jpg", A)  # not a KYC bucket
    assert not fv.is_own_storage_image_url("storage://", A)


@respx.mock
async def test_kyc_ref_download_uses_service_key_on_authenticated_endpoint():
    route = respx.get(f"{BASE}/storage/v1/object/authenticated/kyc/{A}/kyc-face-1.jpg").mock(
        return_value=httpx.Response(200, content=b"img")
    )
    path = await fv._download(f"storage://kyc/{A}/kyc-face-1.jpg", ".jpg")
    try:
        assert open(path, "rb").read() == b"img"
    finally:
        os.unlink(path)
    h = route.calls.last.request.headers
    assert h["authorization"] == "Bearer service-test-key" and h["apikey"] == "service-test-key"


@respx.mock
async def test_legacy_public_kyc_url_download_sends_no_service_key():
    url = f"{BASE}/storage/v1/object/public/avatars/{A}/kyc-face-1.jpg"
    route = respx.get(url).mock(return_value=httpx.Response(200, content=b"img"))
    path = await fv._download(url, ".jpg")
    os.unlink(path)
    assert "authorization" not in route.calls.last.request.headers
