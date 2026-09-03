# =============================================================================
# CheriPic FastAPI — production image (FULL build, with face match)
# =============================================================================
# Installs requirements.txt: the core API stack plus the insightface ML stack
# for /kyc/verify-face. Needs a compiler and bakes in ~280 MB of model weights,
# so expect a ~10-20 min build, a multi-GB image, and >= 1 GB RAM at runtime.
#
# TO GO BACK TO THE LIGHT BUILD: point the COPY and pip install at
# requirements.core.txt, and comment out the apt block and the model warmup.
# /kyc/verify-face then returns a clean 503 — face_verification.py imports
# insightface lazily and raises FaceServiceUnavailable — and every other
# route (chat, billing, membership, admin) is unaffected.
#
# Listens on $PORT (Render / Railway / Fly inject this).
# =============================================================================

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Full ML stack build deps:
#   build-essential — compiles insightface's Cython extension
#   libgl1, libglib2.0-0 — required by opencv-python-headless at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt is `-r requirements.core.txt` + `-r requirements.face.txt`,
# so all three files must be present before pip runs.
COPY requirements.txt requirements.core.txt requirements.face.txt ./

# Installed in three steps rather than one `-r requirements.txt`, because
# insightface's setup.py imports numpy and Cython at build time. Under pip's
# default build isolation those aren't visible to the build env and the install
# fails with ModuleNotFoundError. So: seed the build deps, install the core
# stack, then install the ML stack with isolation off so it can see them.
RUN pip install cython numpy
RUN pip install -r requirements.core.txt
RUN pip install --no-build-isolation -r requirements.face.txt

# Pre-download the buffalo_l model pack (~280 MB) so the first /kyc/verify-face
# call doesn't stall on the download.
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
