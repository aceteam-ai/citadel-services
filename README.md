# Citadel Services

Service catalog for [Citadel](https://github.com/aceteam-ai/citadel-cli) nodes. Each service is a self-contained Docker Compose stack that can be installed and managed by the Citadel CLI.

## Available Services

| Service | Category | GPU | Description |
|---------|----------|-----|-------------|
| [ollama](services/ollama/) | inference | optional | Local LLM runner with model management |
| [vllm](services/vllm/) | inference | required | High-performance LLM inference with PagedAttention |
| [llamacpp](services/llamacpp/) | inference | optional | Lightweight GGUF model inference server |
| [gliner2](services/gliner2/) | inference | no | GLiNER2 NER + relation extraction (AdaExtract `GLINER_EXTRACTION` provider) |
| [wechat](services/wechat/) | tools | no | Personal WeChat REST API via WeChatFerry (Windows VM) [^host] |
| [claudecode](services/claudecode/) | agent-runtime | no | Headless Claude Code agent-runtime (BYOC: agent + model on your own node) |
| [livekit](services/livekit/) | media | no | LiveKit WebRTC SFU hosting AceTeam voice huddles (team-chat calls) |
| [gotenberg](services/gotenberg/) | tools | no | Document conversion API (LibreOffice Office->PDF + Chromium HTML->PDF); Sovereign Sign's sovereign DOCX->PDF conversion |

[^host]: `wechat` is **host-provisioned** on a Windows VM (WeChatFerry DLL injection), not a Docker Compose stack. It has no `compose.yml` and is **not** installable via `citadel service catalog install`; the catalog entry exists for discoverability. See [services/wechat/](services/wechat/) for provisioning.

## Browse Services

Each service lives in `services/<name>/` and contains:

- **service.yaml** -- Machine-readable service metadata (name, ports, GPU requirements, health checks, config options)
- **compose.yml** -- Docker Compose file to run the service
- **README.md** -- Human-readable documentation with quick start and configuration

The top-level `registry.yaml` is a machine-readable index of all services.

## Install a Service

```bash
citadel service install <name>
```

> Service management via the Citadel CLI is coming soon. For now, you can run services directly with Docker Compose:
>
> ```bash
> cd services/ollama
> docker compose up -d
> ```

## Contributing a New Service

1. Fork this repo
2. Copy `templates/service-template/` to `services/<your-service>/`
3. Fill in `service.yaml` with your service's metadata
4. Write a `compose.yml` that runs the service
5. Write a `README.md` with quick start and configuration docs
6. Add your service to `registry.yaml`
7. Validate `service.yaml` against `schema/service-schema.yaml`
8. Open a PR

### service.yaml Schema

Every service must include a `service.yaml` with at minimum:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique service identifier |
| `version` | string | yes | Service version or "latest" |
| `description` | string | yes | One-line description |
| `category` | enum | yes | One of: inference, tools, media, dev, monitoring |
| `requires.gpu` | bool | no | Whether a GPU is required |
| `requires.vram_min_gb` | number | no | Minimum VRAM in GB |
| `ports` | array | no | Port mappings (host, container, protocol) |
| `config` | array | no | Environment variables with defaults |
| `health_check` | object | no | HTTP health check endpoint |
| `volumes` | array | no | Persistent volume mounts |
| `tags` | array | no | Searchable tags |

See [schema/service-schema.yaml](schema/service-schema.yaml) for the full JSON Schema definition.
