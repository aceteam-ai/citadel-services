# Voice-clone text-to-speech service (Chatterbox default, OmniVoice opt-in).
#
# Built and published as ghcr.io/aceteam-ai/voice-clone-service. GPU only: the
# image ships CUDA torch (cu124 wheels bundle the CUDA runtime, so only the host
# NVIDIA driver and container toolkit are needed). CPU fallback is not a goal.
#
# Dependency note: chatterbox-tts 0.1.7 pins transformers==5.2.0 while
# omnivoice 0.2.1 needs transformers>=5.3.0, so neither can be installed with
# its own dependency set. Both packages go in with no-deps and their runtime
# dependencies are listed explicitly below (transformers 5.3, which both
# accept in practice). gradio, which both drag in for their demos, is left out.
FROM python:3.12-slim

ARG TORCH_BACKEND=cu124

COPY --from=ghcr.io/astral-sh/uv:0.9.10 /uv /usr/local/bin/uv

# ffmpeg: reference decoding, atempo speed control, mp3 (libmp3lame) and
# Ogg/Opus (libopus) encoding. libsndfile1: soundfile/librosa audio I/O.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_SYSTEM_PYTHON=1 \
    UV_TORCH_BACKEND=${TORCH_BACKEND} \
    HF_HOME=/root/.cache/huggingface \
    TORCH_HOME=/root/.cache/huggingface/torch \
    PORT=8080 \
    VOICE_CLONE_DATA_DIR=/data \
    VOICE_CLONE_DEVICE=cuda \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN uv pip install --system \
        "torch==2.6.0" \
        "torchaudio==2.6.0" \
        "transformers==5.3.0" \
        "accelerate>=1.0" \
        "diffusers==0.29.0" \
        "librosa==0.11.0" \
        "s3tokenizer==0.3.0" \
        "resemble-perth==1.0.1" \
        "conformer==0.3.2" \
        "safetensors>=0.5.3" \
        "pyloudnorm" \
        "omegaconf" \
        "einops" \
        "pydub" \
        "soundfile" \
        "numpy<2" \
        "setuptools<81" \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
        "pydantic>=2.0" \
        "python-multipart>=0.0.9" \
    && uv pip install --system --no-deps \
        "chatterbox-tts==0.1.7" \
        "omnivoice==0.2.1"

# setuptools<81 above is load-bearing: resemble-perth imports pkg_resources and,
# without it, silently replaces the watermarker with None. Fail the build here
# rather than at the first request.
RUN python -c "import perth, sys; sys.exit(0 if perth.PerthImplicitWatermarker else 'perth watermarker unavailable')"

COPY engines.py server.py ./

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=600s \
    CMD python -c "import urllib.request,json,sys; d=json.load(urllib.request.urlopen('http://localhost:8080/health')); sys.exit(0 if d.get('model_loaded') else 1)"

CMD ["python", "server.py"]
