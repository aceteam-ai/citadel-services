# Kokoro text-to-speech service.
#
# Built and published as ghcr.io/aceteam-ai/kokoro-service, the image the
# catalog compose.yml (and, once registered, citadel-cli's embedded tts compose)
# reference. Backs the fabric `tts` serving engine (aceteam ServingEngine.TTS /
# the "tts-speech" provisioning template), the synthesis counterpart to the
# whisper-service that backs `transcribe`.
#
# CPU by default so the same image runs on the org's M1 node (Kokoro-82M is
# small, ~real-time on CPU). Pass `--build-arg TORCH_BACKEND=cu124` to build a
# CUDA variant for NVIDIA nodes (paired with compose.gpu.yml's device
# reservation); the server picks CUDA automatically at runtime when a GPU is
# visible (KOKORO_DEVICE=auto).
FROM python:3.12-slim

# Build-time torch backend: cpu (default, lean) or a CUDA tag like cu124.
ARG TORCH_BACKEND=cpu

# uv for fast, reproducible installs (workspace convention: uv, never pip).
COPY --from=ghcr.io/astral-sh/uv:0.9.10 /uv /usr/local/bin/uv

# System deps:
#  - ffmpeg: opus (libopus) + mp3 (libmp3lame) encoding and duration probing.
#  - espeak-ng: misaki's English out-of-vocabulary G2P fallback.
#  - libsndfile1: audio I/O backing for the misaki/kokoro stack.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_SYSTEM_PYTHON=1 \
    UV_TORCH_BACKEND=${TORCH_BACKEND} \
    HF_HOME=/root/.cache/huggingface \
    PORT=8080 \
    KOKORO_CACHE_DIR=/data/cache \
    KOKORO_DEVICE=auto

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN uv pip install --system \
        "kokoro>=0.9.4" \
        "misaki[en,zh]>=0.9.4" \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
        "pydantic>=2.0" \
        "numpy>=1.26"

# misaki's English G2P needs the spaCy en_core_web_sm model. Without it, the
# first EN request shells out to `pip download` (which fails under uv/no-venv)
# and 500s. Bake it in so EN works offline on first request.
RUN uv pip install --system \
    "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"

COPY server.py ./

EXPOSE 8080

# curl-free healthcheck (slim image has no curl); mirrors GET /health.
HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=180s \
    CMD python -c "import urllib.request,json,sys; d=json.load(urllib.request.urlopen('http://localhost:8080/health')); sys.exit(0 if d.get('model_loaded') else 1)"

CMD ["python", "server.py"]
