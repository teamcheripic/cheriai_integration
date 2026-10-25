"""
Member photo delivery (development handoff §13 "Security and media").

  • POST /media/blurred          — after a member uploads a primary/gallery
                                   photo, the server (service key) downloads
                                   it and stores a heavily blurred, downscaled
                                   JPEG derivative at avatars/<uid>/blur/<name>.jpg.
                                   Members who aren't revealed only ever get
                                   this derivative (or a neutral placeholder).
  • GET  /media/photos/{user_id} — the target's ORIGINAL photo URLs, only
                                   when the caller is the target or the two
                                   have an active accepted match (and neither
                                   blocked the other). Otherwise only the
                                   blurred derivatives.
  • POST /media/photos/batch     — the same for up to 50 members (lists).
  • POST /admin/media/backfill-blur — admin-only: create missing derivatives
                                   for members who uploaded before this shipped.

Originals stored as `storage://media/<uid>/…` refs (private bucket `media`,
migration 048) are delivered as short-lived signed URLs, so access ends with
the connection (handoff: "After ending/blocking, stop new media URL issuance
and use short-lived signed access"). Legacy originals still live in the public
`avatars` bucket; for those the existing public URL is returned to authorised
viewers only. Previously copied images cannot be recalled.

Wire-up (main.py, owned elsewhere):
    from media import router as media_router
    app.include_router(media_router)
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status, Request

from ratelimit import rate_limit
from pydantic import BaseModel, Field

from auth import get_current_admin_id, get_current_user_id

logger = logging.getLogger("cheripic.media")

router = APIRouter()

SIGNED_URL_TTL = 600  # seconds — "short-lived signed access"
MAX_SOURCE_BYTES = 10 * 1024 * 1024
BLUR_BUCKET = "avatars"          # public derivative is acceptable (spec)
_PHOTO_BUCKETS = ("avatars", "media")
_UUIDISH = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _base() -> str:
    return (os.getenv("SUPABASE_URL") or "").rstrip("/")


def _service_headers(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    key = os.getenv("SUPABASE_KEY") or ""
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if extra:
        h.update(extra)
    return h


# --------------------------------------------------------------------------
# Storage references
# --------------------------------------------------------------------------

def parse_storage_ref(value: str) -> Optional[tuple[str, str]]:
    """
    `storage://<bucket>/<path>` → (bucket, path). Also accepts this project's
    Supabase object URLs (`/storage/v1/object/{public|sign|authenticated}/
    <bucket>/<path>`). Returns None for anything else or for path tricks.
    """
    if not value or not isinstance(value, str):
        return None
    if value.startswith("storage://"):
        rest = value[len("storage://"):]
        bucket, _, path = rest.partition("/")
    else:
        base = _base()
        if not base:
            return None
        try:
            u, b = urlsplit(value), urlsplit(base)
        except ValueError:
            return None
        if u.scheme != b.scheme or u.netloc != b.netloc or u.username or u.password:
            return None
        p = unquote(u.path)
        m = re.match(r"^/storage/v1/object/(?:public|sign|authenticated)/([^/]+)/(.+)$", p)
        if not m:
            return None
        bucket, path = m.group(1), m.group(2)
    path = unquote(path)
    if not bucket or not path or ".." in path or "//" in path or path.startswith("/") or "\\" in path:
        return None
    return bucket, path


def owner_of(path: str) -> str:
    return path.split("/", 1)[0]


def public_url(bucket: str, path: str) -> str:
    return f"{_base()}/storage/v1/object/public/{bucket}/{quote(path)}"


def blur_path_for(user_id: str, source_path: str) -> str:
    """Deterministic: re-requesting a derivative overwrites the same object."""
    stem = source_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "", stem)[:80] or "photo"
    return f"{user_id}/blur/{stem}.jpg"


def is_blur_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) >= 3 and parts[1] == "blur"


# --------------------------------------------------------------------------
# Image processing
# --------------------------------------------------------------------------

def make_blurred_jpeg(data: bytes) -> bytes:
    """
    Heavy, irreversible blur: downscale to ≤24 px on the long side (discards
    detail), blur, upscale to ≤320 px, blur again, JPEG q=60. The result
    shows colour/shape only — no recognisable face.
    """
    from PIL import Image, ImageFilter, ImageOps  # lazy: optional at import

    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        w, h = im.size
        if not w or not h:
            raise ValueError("empty image")
        tiny_scale = 24 / max(w, h)
        tiny = im.resize((max(1, round(w * tiny_scale)), max(1, round(h * tiny_scale))), Image.BILINEAR)
        tiny = tiny.filter(ImageFilter.GaussianBlur(1.5))
        out_scale = 320 / max(w, h) if max(w, h) > 320 else 1.0
        out = tiny.resize((max(1, round(w * out_scale)), max(1, round(h * out_scale))), Image.BICUBIC)
        out = out.filter(ImageFilter.GaussianBlur(8))
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=60, optimize=True)
        return buf.getvalue()


# --------------------------------------------------------------------------
# Supabase helpers (service key — callers verified first)
# --------------------------------------------------------------------------

async def _download_object(client: httpx.AsyncClient, bucket: str, path: str) -> bytes:
    r = await client.get(
        f"{_base()}/storage/v1/object/authenticated/{bucket}/{quote(path)}",
        headers=_service_headers(),
    )
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="photo_not_found")
    if r.status_code >= 300:
        logger.error("[media] download failed [%s] %s/%s", r.status_code, bucket, path)
        raise HTTPException(status_code=502, detail="download_failed")
    if len(r.content) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail="image_too_large")
    return r.content


async def _upload_object(client: httpx.AsyncClient, bucket: str, path: str, body: bytes) -> None:
    r = await client.post(
        f"{_base()}/storage/v1/object/{bucket}/{quote(path)}",
        content=body,
        headers=_service_headers({"Content-Type": "image/jpeg", "x-upsert": "true", "cache-control": "3600"}),
    )
    if r.status_code >= 300:
        logger.error("[media] upload failed [%s] %s/%s", r.status_code, bucket, path)
        raise HTTPException(status_code=502, detail="upload_failed")


async def _sign(client: httpx.AsyncClient, bucket: str, path: str, ttl: int = SIGNED_URL_TTL) -> Optional[str]:
    r = await client.post(
        f"{_base()}/storage/v1/object/sign/{bucket}/{quote(path)}",
        json={"expiresIn": ttl},
        headers=_service_headers({"Content-Type": "application/json"}),
    )
    if r.status_code >= 300:
        logger.warning("[media] sign failed [%s] %s", r.status_code, bucket)
        return None
    signed = (r.json() or {}).get("signedURL") or (r.json() or {}).get("signedUrl")
    if not signed:
        return None
    return f"{_base()}/storage/v1{signed}" if signed.startswith("/") else signed


async def _rest_get(client: httpx.AsyncClient, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    r = await client.get(f"{_base()}/rest/v1/{table}", params=params, headers=_service_headers())
    if r.status_code >= 300:
        logger.error("[media] select %s failed [%s]", table, r.status_code)
        raise HTTPException(status_code=502, detail="lookup_failed")
    return r.json() or []


def parse_bio(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


async def revealed_peer_ids(client: httpx.AsyncClient, caller: str, targets: list[str]) -> set[str]:
    """
    Targets whose originals the caller may see: an ACTIVE match row with the
    caller (accepted on both sides — handoff PHOTO_REVEAL_STAGE 'mutual') and
    no block in either direction. Ended/blocked pairs drop out immediately.
    """
    others = [t for t in targets if t != caller]
    if not others:
        return set()
    rows = await _rest_get(client, "matches", {
        "select": "user_a_id,user_b_id",
        "or": f"(user_a_id.eq.{caller},user_b_id.eq.{caller})",
        "is_active": "eq.true",
    })
    peers = {r["user_b_id"] if r.get("user_a_id") == caller else r.get("user_a_id") for r in rows}
    peers &= set(others)
    if not peers:
        return set()
    blocks = await _rest_get(client, "blocked_users", {
        "select": "blocker_id,blocked_id",
        "or": f"(blocker_id.eq.{caller},blocked_id.eq.{caller})",
    })
    for b in blocks:
        peers.discard(b.get("blocked_id"))
        peers.discard(b.get("blocker_id"))
    return peers


async def _deliver_original(client: httpx.AsyncClient, url: Optional[str]) -> Optional[str]:
    """Private-bucket refs → short-lived signed URL; legacy public URLs as-is."""
    if not url:
        return None
    ref = parse_storage_ref(url)
    if ref and (url.startswith("storage://") or "/object/sign/" in url or "/object/authenticated/" in url):
        bucket, path = ref
        return await _sign(client, bucket, path)
    return url


async def photos_for(client: httpx.AsyncClient, profile: dict[str, Any], revealed: bool) -> dict[str, Any]:
    bio = parse_bio(profile.get("bio"))
    if revealed:
        gallery = [g for g in (bio.get("gallery_urls") or []) if isinstance(g, str) and g]
        avatar, *signed_gallery = await asyncio.gather(
            _deliver_original(client, profile.get("avatar_url")),
            *[_deliver_original(client, g) for g in gallery],
        )
        return {
            "user_id": profile.get("user_id"),
            "revealed": True,
            "avatar_url": avatar,
            "gallery_urls": [g for g in signed_gallery if g],
            "expires_in": SIGNED_URL_TTL,
        }
    blur = bio.get("blur_url") if isinstance(bio.get("blur_url"), str) else None
    gblur = [g for g in (bio.get("gallery_blur_urls") or []) if isinstance(g, str) and g]
    return {
        "user_id": profile.get("user_id"),
        "revealed": False,
        "avatar_url": blur,
        "gallery_urls": gblur,
        "expires_in": None,
    }


def _validate_ids(ids: list[str]) -> list[str]:
    out = []
    for i in ids:
        if not isinstance(i, str) or not _UUIDISH.match(i):
            raise HTTPException(status_code=422, detail="invalid_user_id")
        if i not in out:
            out.append(i)
    return out


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

class BlurRequest(BaseModel):
    source_url: str = Field(..., min_length=1, max_length=2048)


class BlurResponse(BaseModel):
    blur_url: str
    path: str


async def create_blur_derivative(client: httpx.AsyncClient, user_id: str, source: str) -> BlurResponse:
    ref = parse_storage_ref(source)
    if not ref:
        raise HTTPException(status_code=400, detail="unsupported_source")
    bucket, path = ref
    if bucket not in _PHOTO_BUCKETS or owner_of(path) != user_id or is_blur_path(path):
        raise HTTPException(status_code=403, detail="not_your_photo")
    data = await _download_object(client, bucket, path)
    try:
        jpeg = await asyncio.to_thread(make_blurred_jpeg, data)
    except ImportError:
        logger.error("[media] Pillow not installed — add it from requirements.core.txt")
        raise HTTPException(status_code=503, detail="image_processing_unavailable")
    except Exception as e:  # undecodable / unsupported (e.g. HEIC without plugin)
        logger.warning("[media] could not process %s/%s: %r", bucket, path, e)
        raise HTTPException(status_code=422, detail="unreadable_image")
    out_path = blur_path_for(user_id, path)
    await _upload_object(client, BLUR_BUCKET, out_path, jpeg)
    return BlurResponse(blur_url=public_url(BLUR_BUCKET, out_path), path=out_path)


@router.post("/media/blurred", response_model=BlurResponse, tags=["Media"], dependencies=[Depends(rate_limit("media"))])
async def media_blurred(req: BlurRequest, user_id: str = Depends(get_current_user_id)) -> BlurResponse:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        return await create_blur_derivative(client, user_id, req.source_url)


@router.get("/media/photos/{target_id}", tags=["Media"], dependencies=[Depends(rate_limit("media"))])
async def media_photos(target_id: str, response: Response, user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    _validate_ids([target_id])
    response.headers["Cache-Control"] = "no-store"
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        rows = await _rest_get(client, "user_profiles", {
            "select": "user_id,avatar_url,bio",
            "user_id": f"eq.{target_id}",
            "limit": "1",
        })
        if not rows:
            raise HTTPException(status_code=404, detail="member_not_found")
        revealed = target_id == user_id or target_id in await revealed_peer_ids(client, user_id, [target_id])
        return await photos_for(client, rows[0], revealed)


class BatchRequest(BaseModel):
    user_ids: list[str] = Field(..., max_length=50)


@router.post("/media/photos/batch", tags=["Media"], dependencies=[Depends(rate_limit("media"))])
async def media_photos_batch(req: BatchRequest, response: Response, user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
    ids = _validate_ids(req.user_ids)
    response.headers["Cache-Control"] = "no-store"
    if not ids:
        return {"photos": []}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        rows = await _rest_get(client, "user_profiles", {
            "select": "user_id,avatar_url,bio",
            "user_id": f"in.({','.join(ids)})",
        })
        allowed = await revealed_peer_ids(client, user_id, ids)
        allowed.add(user_id)
        photos = await asyncio.gather(*[photos_for(client, r, r.get("user_id") in allowed) for r in rows])
        return {"photos": list(photos)}


class BackfillRequest(BaseModel):
    limit: int = Field(25, ge=1, le=200)


@router.post("/admin/media/backfill-blur", tags=["Media", "Admin"])
async def admin_backfill_blur(req: BackfillRequest, admin_id: str = Depends(get_current_admin_id)) -> dict[str, Any]:
    """Creates missing derivatives (bio.blur_url / gallery_blur_urls) for up to `limit` members."""
    done, failed = 0, 0
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        rows = await _rest_get(client, "user_profiles", {
            "select": "user_id,avatar_url,bio",
            "avatar_url": "not.is.null",
            "limit": "1000",
        })
        for row in rows:
            if done >= req.limit:
                break
            bio = parse_bio(row.get("bio"))
            gallery = [g for g in (bio.get("gallery_urls") or []) if isinstance(g, str)]
            have_avatar = isinstance(bio.get("blur_url"), str)
            have_gallery = len(bio.get("gallery_blur_urls") or []) == len(gallery)
            if have_avatar and have_gallery:
                continue
            uid = row["user_id"]
            try:
                patch: dict[str, Any] = {}
                if not have_avatar:
                    patch["blur_url"] = (await create_blur_derivative(client, uid, row["avatar_url"])).blur_url
                if not have_gallery:
                    blurs = []
                    for g in gallery:
                        try:
                            blurs.append((await create_blur_derivative(client, uid, g)).blur_url)
                        except HTTPException:
                            blurs.append("")
                    patch["gallery_blur_urls"] = blurs
                r = await client.patch(
                    f"{_base()}/rest/v1/user_profiles",
                    params={"user_id": f"eq.{uid}"},
                    json={"bio": json.dumps({**bio, **patch})},
                    headers=_service_headers({"Content-Type": "application/json"}),
                )
                if r.status_code >= 300:
                    raise RuntimeError(f"patch {r.status_code}")
                done += 1
            except Exception as e:  # keep going; report counts only (no PII)
                failed += 1
                logger.warning("[media-backfill] user=%s failed: %r", uid, e)
    logger.info("[media-backfill] admin=%s done=%d failed=%d", admin_id, done, failed)
    return {"ok": True, "updated": done, "failed": failed}
