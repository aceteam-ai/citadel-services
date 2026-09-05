# paw-compile -- Program-as-Weights compile server

Self-hosted [PAW compile server](https://github.com/avra-m3/program-as-weights-server)
(MIT, avra-m3): a FastAPI implementation of the
[programasweights.com](https://programasweights.com) REST compile protocol,
backed by a fully-local Program-as-Weights compile pipeline. Compiles a
natural-language spec into a runnable **program** (a small LoRA adapter over an
interpreter model) entirely on this node -- no hosted API is needed. The
**unmodified official `programasweights` SDK** works against it unchanged,
just by pointing `PAW_API_URL` at this server.

> [!IMPORTANT]
> This packages an **independent, unofficial reimplementation** of the
> compile protocol (avra-m3/program-as-weights-server), not affiliated with
> the Program-as-Weights paper authors or programasweights.com. See
> [Credits and license](#credits-and-license) below.

## Install

```bash
citadel module install paw-compile
```

Or run directly with Docker Compose:

```bash
cd services/paw-compile
docker compose up -d
```

First compile downloads the pseudo compiler, the trained compiler/mapper, and
the GGUF interpreter weights (~16 GB total) into `~/citadel-cache/huggingface`
(persisted across restarts, shared with other HF-based modules in this
catalog).

## HTTP API

The container serves on `:8100`; citadel will publish it on host `8214`
(`CITADEL_PAW_COMPILE_HOST_PORT`, once registered in citadel-cli, **loopback
only**). A standalone `docker compose up` defaults the host publish to `8100`.
Examples below use `8100`.

```bash
curl -s http://127.0.0.1:8100/health
# {"status":"ok"}

curl -s -X POST http://127.0.0.1:8100/api/v1/compile \
  -H 'Content-Type: application/json' \
  -d '{"spec": "Classify text sentiment as positive or negative."}'

curl -OJ http://127.0.0.1:8100/api/v1/programs/<program_id>/download
```

Or the official SDK, unmodified:

```bash
PAW_API_URL=http://127.0.0.1:8100 uv run python -c "
import programasweights as paw
program = paw.compile('Classify text sentiment as positive or negative.')
fn = paw.function(program.id)
print(fn('I love this'))
"
```

Full endpoint list and wire contract: upstream's own
[README](https://github.com/avra-m3/program-as-weights-server#endpoints) and
[docs/HOW_IT_WORKS.md](https://github.com/avra-m3/program-as-weights-server/blob/main/docs/HOW_IT_WORKS.md).

## GPU

Required. Compiling loads two 4B models sequentially (pseudo compiler +
trained compiler/mapper); upstream measures peak memory at ~8.5 GB on an
M4/24GB Mac over MPS (see their `docs/HOW_IT_WORKS.md`). CPU-only compiling
works per upstream's autodetection (`torch.cuda` -> `torch.mps` -> `cpu`) but
is impractically slow for anything beyond a demo, so -- like `vllm`/`tei` in
this catalog -- this module ships a single `compose.yml` with the GPU device
reservation and no CPU-only path.

## Image build

Built from a **pinned commit** of upstream's own `Dockerfile` via a git build
context, not a vendored copy:

```yaml
build:
  context: https://github.com/avra-m3/program-as-weights-server.git#627dcbf198f2c5729ab5756cad4c771d2fc45198
```

This reproduces exactly the image the reference node deploy (aep135) verified
a real compile against, with no source duplicated into this repo and nothing
to drift when upstream changes a dependency. Bumping the pinned ref is a
deliberate, reviewable change (re-verify a compile before merging).

## Data persistence

Upstream's own CLI defaults `--data-dir` to `data/server` under the
container's writable layer, so compiled programs do **not** survive a
container removal or image upgrade by default. This module's `compose.yml`
overrides the command to add `--data-dir /data` and mounts
`~/citadel-cache/paw-compile:/data`, so the program registry and `.paw`
bundles persist like every other stateful module in this catalog.

## Requirements

| Requirement | Value |
|-------------|-------|
| GPU | Required |
| VRAM | ~12 GB+ recommended (see GPU above) |
| Architecture | amd64 |
| Disk | ~16 GB HF cache (one-time) + program registry (grows with usage) |

## Configuration

| Variable | Default | Description |
|----------|---------|--------------|
| `HF_TOKEN` | (unset) | Optional Hugging Face token; without one, weight downloads are unauthenticated (lower rate limits). All artifacts fetched are public. |

## Ports

| Host | Container | Description |
|------|-----------|--------------|
| 127.0.0.1:8100 (standalone default) / 127.0.0.1:8214 (citadel-managed, `CITADEL_PAW_COMPILE_HOST_PORT`, pending citadel-cli registration) | 8100 | PAW compile HTTP API, **loopback only** |

Published to `127.0.0.1` deliberately: the server has no auth of its own
(no API key, no rate limiting).

## Volumes

| Host | Container | Description |
|------|-----------|--------------|
| `~/citadel-cache/huggingface` | `/root/.cache/huggingface` | Pseudo compiler + trained compiler/mapper + GGUF interpreter weights |
| `~/citadel-cache/paw-compile` | `/data` | Compiled program registry + `.paw` bundles |

## Fabric seam

This catalog module is only the **compose service** (the backing image), same
scope as claudecode/meeting/gotenberg/nvr. Two follow-ups, out of scope for
this PR:

1. **Port registration (citadel-cli).** Register
   `EnvPAWCompileHostPort = "CITADEL_PAW_COMPILE_HOST_PORT"` /
   `PAWCompileHostPort = 8214` in `services/ports.go` (next free slot after
   unlimited-ocr's 8213). Until then only the standalone `docker compose up`
   path (host 8100) works.
2. **Fabric engine wiring (aceteam).** There is no `paw-compile` fabric
   serving engine / provisioning template yet; wiring one (an
   `EngineHandler` proxying `localhost:8214/api/v1/compile` etc., mirroring
   `transcribe`/`tts`) is a separate product decision, not part of packaging
   this module.

## Credits and license

This server implementation
([avra-m3/program-as-weights-server](https://github.com/avra-m3/program-as-weights-server))
is released under the [MIT License](https://github.com/avra-m3/program-as-weights-server/blob/main/LICENSE),
copyright Avrami H. It is a reimplementation of the protocol and pipeline
described in:

> **Program-as-Weights: A Programming Paradigm for Fuzzy Functions.**
> Wentao Zhang, Liliana Hotsko, Woojeong Kim, Pengyu Nie, Stuart Shieber,
> Yuntian Deng. arXiv:2607.02512, 2026. <https://arxiv.org/abs/2607.02512>
> (CC BY 4.0)

**Weights license caveat.** The MIT license covers only the server code. This
server downloads (does not bundle) several `programasweights/*` Hugging Face
artifacts at runtime: `paw-4b-qwen3-0.6b`, `paw-4b-gpt2` (trained
compiler/mapper weights), `Qwen3-0.6B-GGUF-Q6_K`, `GPT2-GGUF-Q8_0` (runtime
interpreters), and `paw-programs` (published program artifacts). Per
upstream's own README, **these do not declare an explicit license at
source** -- absent an explicit grant, default copyright applies, and reuse
terms are unclear. This module does not resolve or change that; it downloads
the same artifacts the reference deploy did, onto the customer's own node.
The two base models it also downloads (`Qwen/Qwen3-4B-Instruct-2507`,
`Qwen/Qwen3-0.6B`) are separately and permissively licensed by Qwen.

## Links

- [avra-m3/program-as-weights-server](https://github.com/avra-m3/program-as-weights-server)
- [Program-as-Weights paper](https://arxiv.org/abs/2607.02512)
- [programasweights.com](https://programasweights.com)
