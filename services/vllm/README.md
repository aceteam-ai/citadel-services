# vLLM

High-performance LLM inference engine with PagedAttention. Serves models via an OpenAI-compatible API with native tool calling support.

## Quick Start

```bash
docker compose up -d
```

The server starts with Qwen3-8B by default. Send requests using the OpenAI API format:

```bash
curl http://localhost:8100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-8B",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Requirements

| Requirement | Value |
|-------------|-------|
| GPU | Required (NVIDIA with CUDA) |
| Min VRAM | 8 GB |
| Architecture | amd64 |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL` | `Qwen/Qwen3-8B` | HuggingFace model ID to serve |
| `GPU_MEMORY_UTILIZATION` | `0.85` | Fraction of GPU memory to use (0.0-1.0) |
| `MAX_MODEL_LEN` | `16384` | Maximum sequence length |

Override via environment variables:

```bash
MODEL=meta-llama/Llama-3.1-8B-Instruct docker compose up -d
```

## Ports

| Host | Container | Description |
|------|-----------|-------------|
| 8100 | 8000 | OpenAI-compatible API |

## Volumes

| Host Path | Container Path | Description |
|-----------|---------------|-------------|
| `~/citadel-cache/huggingface` | `/root/.cache/huggingface` | HuggingFace model cache (shared across services) |

## Links

- [vLLM Documentation](https://docs.vllm.ai)
- [Supported Models](https://docs.vllm.ai/en/latest/models/supported_models.html)
- [OpenAI API Reference](https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html)
