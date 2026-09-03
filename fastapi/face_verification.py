"""
KYC face verification via insightface (ONNX, no TensorFlow).

Compares a live selfie against the face printed on an ID-proof image and
returns a 0-100 similarity score. Powers /kyc/verify-face which auto-flips
user_profiles.verification_final_status to 'verified' when similarity ≥
threshold (default 70).

Why insightface, not DeepFace:
    DeepFace pulls TensorFlow, which has no Python 3.14 wheel yet. insightface
    runs on ONNX Runtime and has 3.13/3.14 wheels for CPU. The `buffalo_l`
    model pack bundles RetinaFace (detection) + ArcFace-r100 (embeddings) —
    same model family DeepFace was using.

How the score is calibrated:
    ArcFace returns L2-normalized embeddings; cosine similarity sits in
    roughly [-1, 1]. Real-world meaning of cosine for ArcFace-r100:
        ~0.65+ : same person, high confidence
        ~0.45  : same person, plausible
        ~0.25  : ambiguous
        < 0.20 : different person
    To match the user's "70%" intuition we map cos linearly through the
    "useful" range [0.20 → 0.80] and clamp to [0, 100]. A 70% UI threshold
    therefore corresponds to cosine ≈ 0.62, which is solidly in
    "same person" territory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MODEL_NAME = "buffalo_l"
DETECTOR_BACKEND = "retinaface"  # for parity with the prior contract; insightface bundles this internally
MODEL_LABEL = "insightface-arcface-r100"

# Linear mapping from cosine similarity → 0-100 score (see header).
_COS_LOW = 0.20   # below this → 0
_COS_HIGH = 0.80  # at/above → 100

# Lazy import so FastAPI startup stays light when no one's verifying.
_face_app = None


class FaceServiceUnavailable(RuntimeError):
    """Raised when the optional ML stack (insightface + onnxruntime + opencv
    + numpy from requirements.face.txt) isn't installed on this host. The
    /kyc/verify-face handler catches this and returns a 503 so the rest of
    the FastAPI app keeps working on hosts where the heavy deps don't fit
    (cPanel shared, low-RAM tiers, etc.)."""


def _get_face_app():
    """Initialize (once per process) and return an insightface FaceAnalysis."""
    global _face_app
    if _face_app is not None:
        return _face_app

    try:
        from insightface.app import FaceAnalysis  # type: ignore
    except ImportError as e:
        raise FaceServiceUnavailable(
            "Face verification isn't installed on this host. Install the "
            "extras with `pip install -r requirements.face.txt` on a VPS / "
            "Render paid / EC2 t3.small+ — cPanel shared hosting can't "
            "compile insightface."
        ) from e

    app = FaceAnalysis(name=MODEL_NAME, allowed_modules=["detection", "recognition"])
    # ctx_id=-1 forces CPU. Use 0 if you ever wire up CUDA.
    app.prepare(ctx_id=-1, det_size=(640, 640))
    _face_app = app
    logger.info("insightface FaceAnalysis ready (model=%s, CPU)", MODEL_NAME)
    return _face_app


@dataclass
class FaceMatchResult:
    similarity: float          # 0-100, calibrated (see header)
    distance: float            # raw cosine distance (1 - cos), kept for audit
    model: str
    detector_backend: str
    matched: bool              # similarity >= threshold
    threshold: float


async def _download(url: str, suffix: str) -> str:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(resp.content)
    return path


def _embed_largest_face(image_path: str):
    """
    Load image, detect faces, return the embedding of the largest one (by
    bounding-box area). Picking the largest face works well for ID photos
    (the subject's face dominates the frame) and live selfies (single face
    in view). Raises ValueError if no face is found.
    """
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    img = cv2.imread(image_path)
    if img is None:
        raise ValueError("Could not decode image.")

    app = _get_face_app()
    faces = app.get(img)
    if not faces:
        raise ValueError("No face detected in image.")

    # Pick the largest detected face.
    def _area(f):
        x1, y1, x2, y2 = f.bbox
        return max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))

    face = max(faces, key=_area)
    emb = face.normed_embedding  # already L2-normalized in buffalo_l
    if emb is None:
        # Older insightface releases expose raw `embedding`; normalize manually.
        raw = face.embedding
        norm = np.linalg.norm(raw)
        emb = raw / norm if norm > 0 else raw
    return emb


def _verify_sync(face_path: str, id_path: str) -> dict:
    """Embed both images and return cosine similarity + distance."""
    import numpy as np  # type: ignore

    face_emb = _embed_largest_face(face_path)
    id_emb = _embed_largest_face(id_path)

    cos = float(np.dot(face_emb, id_emb))
    distance = 1.0 - cos  # cosine distance, kept for parity with prior schema
    return {"cosine": cos, "distance": distance}


def _calibrate(cos: float) -> float:
    """Map raw cosine into the user-friendly [0, 100] range (see header)."""
    pct = (cos - _COS_LOW) / (_COS_HIGH - _COS_LOW) * 100.0
    return max(0.0, min(100.0, pct))


async def compare_faces(
    face_image_url: str,
    id_image_url: str,
    threshold: float = 70.0,
    model: str = MODEL_LABEL,            # kept for API compatibility
    detector_backend: str = DETECTOR_BACKEND,
) -> FaceMatchResult:
    """
    Download both images, run insightface in a worker thread, return a
    normalized 0-100 similarity score plus pass/fail vs threshold.

    Raises:
        httpx.HTTPError: if either image can't be fetched
        ValueError:       if a face can't be detected in either image
    """
    face_path: Optional[str] = None
    id_path: Optional[str] = None
    try:
        face_path, id_path = await asyncio.gather(
            _download(face_image_url, ".jpg"),
            _download(id_image_url, ".jpg"),
        )

        # insightface ONNX inference is CPU-bound; offload from the event loop.
        result = await asyncio.to_thread(_verify_sync, face_path, id_path)

        cos = result["cosine"]
        distance = result["distance"]
        similarity = _calibrate(cos)

        logger.info(
            "Face match: cos=%.4f distance=%.4f similarity=%.2f threshold=%.1f",
            cos, distance, similarity, threshold,
        )

        return FaceMatchResult(
            similarity=round(similarity, 2),
            distance=distance,
            model=model,
            detector_backend=detector_backend,
            matched=similarity >= threshold,
            threshold=threshold,
        )
    finally:
        for p in (face_path, id_path):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
