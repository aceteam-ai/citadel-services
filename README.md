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
| [kokoro](services/kokoro/) | inference | optional | Kokoro text-to-speech (Kokoro-82M): OpenAI-compatible synthesis, EN + ZH voices, opus/mp3, content-addressed cache + per-item receipts; backs the fabric `tts` engine |
| [paw-compile](services/paw-compile/) | inference | required | Self-hosted Program-as-Weights compile server (avra-m3/program-as-weights-server, MIT): compiles a spec into a runnable program (LoRA adapter + interpreter) on your own node; unmodified `programasweights` SDK works via `PAW_API_URL` |

[^host]: `wechat` is **host-provisioned** on a Windows VM (WeChatFerry DLL injection), not a Docker Compose stack. It has no `compose.yml` and is **not** installable via `citadel service catalog install`; the catalog entry exists for discoverability. See [services/wechat/](services/wechat/) for provisioning.

## Browse Services

Each service lives in `services/<name>/` and contains:

- **service.yaml** -- Machine-readable service metadata (name, ports, GPU requirements, health checks, config options)
- **compose.yml** -- Docker Compose file to run the service
- **README.md** -- Human-readable documentation with quick start and configuration

The top-level `registry.yaml` is a machine-readable index of all services. It
also lists the trusted AceTeam app runtime images that Citadel nodes may
pre-pull. Runtime image metadata is honored only from this built-in catalog;
community catalog sources cannot add or replace these images.

## App Runtime Images

The `runtime_images` entries name the multi-architecture images used by hosted
apps. Execution references are always immutable
`repository@sha256:<index-digest>` values. Their matching `stable` references
are discovery aliases only and Citadel never pulls or executes them.

The list remains empty until the first authorized AceTeam runtime release. That
release produces an `app-runtime-image-lock-vX.Y.Z` artifact containing the
real multi-architecture index digests. Maintainers copy those reviewed lock
entries into `registry.yaml`; placeholder or single-architecture digests are
not accepted.

After locked entries exist, operators can cache the images supported by their
node's architecture after refreshing the catalog:

```bash
citadel service catalog update
citadel service catalog pre-pull-runtimes
```

Use `citadel service catalog pre-pull-runtimes --dry-run` to inspect the trusted
digest references without contacting the registry. The command validates the
entire trusted list against the host architecture before any pull, so an
unsupported entry cannot leave a partially warmed cache. Publishing the
referenced images is a separate, credential-gated release operation in the
AceTeam repository.

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

Run `python scripts/validate_catalog.py` to validate the registry and every
service manifest locally. Run `python -m unittest discover -s tests` for the
negative schema contracts. Pull requests run both in CI.

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

The top-level index is defined by
[schema/registry-schema.yaml](schema/registry-schema.yaml). Only AceTeam
maintainers may add `runtime_images`; community catalogs cannot opt into the
trusted runtime pre-pull path.
