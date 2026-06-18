# Ollama

Local LLM runner with built-in model management. Pull and run models with a single command.

## Quick Start

```bash
docker compose up -d
```

Once running, pull and run a model:

```bash
curl http://localhost:11434/api/pull -d '{"name": "llama3.1:8b"}'
curl http://localhost:11434/api/generate -d '{"model": "llama3.1:8b", "prompt": "Hello"}'
```

## Requirements

| Requirement | Value |
|-------------|-------|
| GPU | Optional (uses GPU if available via NVIDIA Container Toolkit) |
| Architecture | amd64, arm64 |

## Configuration

Ollama requires no configuration to start. Models are pulled on demand.

## Ports

| Host | Container | Description |
|------|-----------|-------------|
| 11434 | 11434 | Ollama API |

## Volumes

| Host Path | Container Path | Description |
|-----------|---------------|-------------|
| `~/citadel-cache/ollama` | `/root/.ollama` | Model storage and configuration |

## Links

- [Ollama Documentation](https://github.com/ollama/ollama)
- [Model Library](https://ollama.com/library)
- [API Reference](https://github.com/ollama/ollama/blob/main/docs/api.md)
