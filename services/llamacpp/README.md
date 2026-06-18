# llama.cpp

Lightweight GGUF model inference server. Runs quantized models efficiently on both CPU and GPU.

## Quick Start

```bash
docker compose up -d
```

Download a GGUF model into the models volume, then load it:

```bash
# Download a model
curl -L -o ~/citadel-cache/llamacpp/model.gguf \
  "https://huggingface.co/TheBloke/Llama-2-7B-Chat-GGUF/resolve/main/llama-2-7b-chat.Q4_K_M.gguf"

# Chat with the model
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Hello"}],
    "model": "/models/model.gguf"
  }'
```

## Requirements

| Requirement | Value |
|-------------|-------|
| GPU | Optional (uses CUDA if available, falls back to CPU) |
| Architecture | amd64, arm64 |

## Configuration

The server accepts command-line arguments. Edit the `command` in `compose.yml` to customize:

```yaml
command: ["--host", "0.0.0.0", "--port", "8080", "--model", "/models/model.gguf", "--n-gpu-layers", "99"]
```

Common flags:

| Flag | Description |
|------|-------------|
| `--model` | Path to GGUF model file |
| `--n-gpu-layers` | Number of layers to offload to GPU (-1 for all) |
| `--ctx-size` | Context size (default: 2048) |
| `--threads` | Number of CPU threads |

## Ports

| Host | Container | Description |
|------|-----------|-------------|
| 8080 | 8080 | llama.cpp HTTP API |

## Volumes

| Host Path | Container Path | Description |
|-----------|---------------|-------------|
| `~/citadel-cache/llamacpp` | `/models` | GGUF model files |

## Links

- [llama.cpp Documentation](https://github.com/ggml-org/llama.cpp)
- [Server API Reference](https://github.com/ggml-org/llama.cpp/blob/master/examples/server/README.md)
- [GGUF Model Hub](https://huggingface.co/models?library=gguf)
