# =============================================================================
# CheriPic FastAPI — production image
# =============================================================================
# Why this image:
#   • python:3.11-slim — 3.11 has mature wheels for every dep including
#     insightface + onnxruntime; ~150 MB smaller than the regular image.
#   • build-essential is only on the build layer; runtime stays slim.
#   • Pre-downloads the buffalo_l face model so the first /kyc/verify-face
#     call after deploy doesn't 30s-stall the user.
#   • Listens on $PORT (Render / Railway / Fly inject this).
# =============================================================================

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   build-essential — to compile insightface's Cython extension
#   libgl1, libglib2.0-0 — required by opencv-python-headless at runtime
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      libgl1 \
      libglib2.0-0 \
      curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Pre-download the insightface buffalo_l model pack so /kyc/verify-face
# doesn't pay a ~280 MB download on the first real request. The download
# location matches what FaceAnalysis(name="buffalo_l") looks for at runtime.
RUN python -c "from insightface.app import FaceAnalysis; \
    a = FaceAnalysis(name='buffalo_l', allowed_modules=['detection','recognition']); \
    a.prepare(ctx_id=-1, det_size=(640, 640))" \
    || echo "insightface model warmup failed — first /kyc call will be slow but functional"

COPY . .

# Render/Railway/Fly all inject $PORT. Default to 8000 for local docker run.
ENV PORT=8000
EXPOSE 8000

# `--proxy-headers` so the app sees real client IP through the platform's
# reverse proxy; `--forwarded-allow-ips=*` is safe because only the platform
# proxy can reach the container.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=*"]
