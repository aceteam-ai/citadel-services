# GLiNER2 NER + relation extraction service.
# Built and published as ghcr.io/aceteam-ai/gliner2-service, the image referenced
# by citadel-cli's embedded services/compose/extraction.yml.
FROM python:3.12-slim

# uv for fast, reproducible installs (workspace convention: uv, never pip).
COPY --from=ghcr.io/astral-sh/uv:0.9.10 /uv /usr/local/bin/uv

ENV UV_SYSTEM_PYTHON=1 \
    UV_TORCH_BACKEND=cpu \
    HF_HOME=/root/.cache/huggingface \
    PORT=8100 \
    MODEL_NAME=fastino/gliner2-base-v1

WORKDIR /app

# Install deps first for layer caching. CPU torch keeps the image lean; GLiNER2
# (205M params) is CPU-first and needs no GPU.
COPY pyproject.toml ./
RUN uv pip install --system \
        "gliner2[local]==1.3.2" \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
        "pydantic>=2.0"

COPY server.py ./

EXPOSE 8100

# curl-free healthcheck (slim image has no curl); mirrors GET /health.
HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=120s \
    CMD python -c "import urllib.request,json,sys; d=json.load(urllib.request.urlopen('http://localhost:8100/health')); sys.exit(0 if d.get('model_loaded') else 1)"

CMD ["python", "server.py"]
