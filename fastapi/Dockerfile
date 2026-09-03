# =============================================================================
# CheriPic FastAPI — production image (CORE build)
# =============================================================================
# Installs requirements.core.txt only: FastAPI + Supabase + Stripe + OpenAI.
# Everything is a pure-Python or manylinux wheel, so no compiler is needed and
# the image lands around ~250 MB instead of multiple GB.
#
# /kyc/verify-face returns a clean 503 in this build — face_verification.py
# lazily imports insightface and raises FaceServiceUnavailable when it's
# missing. Every other route (chat, billing, membership, admin) is unaffected.
#
# TO RE-ENABLE FACE MATCH: swap requirements.core.txt for requirements.txt in
# the two lines marked ⬇ FACE below, and uncomment the build-essential /
# libgl1 apt block plus the model-warmup RUN. Budget ~15 min of build time,
# a multi-GB image, and >= 1 GB RAM at runtime.
#
# Listens on $PORT (Render / Railway / Fly inject this).
# =============================================================================

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ⬇ FACE — uncomment for the full ML stack:
#   build-essential — compiles insightface's Cython extension
#   libgl1, libglib2.0-0 — required by opencv-python-headless at runtime
# RUN apt-get update && apt-get install -y --no-install-recommends \
#       build-essential libgl1 libglib2.0-0 \
#     && rm -rf /var/lib/apt/lists/*

# ⬇ FACE — change both to requirements.txt for the full stack.
COPY requirements.core.txt ./
RUN pip install -r requirements.core.txt

# ⬇ FACE — uncomment to pre-download the buffalo_l model pack (~280 MB) so the
# first /kyc/verify-face call doesn't stall on the download.
# RUN python -c "from insightface.app import FaceAnalysis; \
#     a = FaceAnalysis(name='buffalo_l', allowed_modules=['detection','recognition']); \
#     a.prepare(ctx_id=-1, det_size=(640, 640))" \
#     || echo "insightface model warmup failed — first /kyc call will be slow but functional"

COPY . .

# Render/Railway/Fly all inject $PORT. Default to 8000 for local docker run.
ENV PORT=8000
EXPOSE 8000

# `--proxy-headers` so the app sees real client IP through the platform's
# reverse proxy; `--forwarded-allow-ips=*` is safe because only the platform
# proxy can reach the container.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=*"]
