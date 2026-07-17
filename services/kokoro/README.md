# kokoro — Text-to-Speech (Kokoro-82M)

Serves [Kokoro](https://github.com/hexgrad/kokoro) (Kokoro-82M, Apache-2.0)
speech synthesis on your own Citadel node: **OpenAI-compatible**, English +
Mandarin voices, `opus`/`mp3` output, a content-addressed cache, and per-item
usage receipts. **Runs on CPU (the org's M1 node) or GPU (3090) from the same
image** — Kokoro-82M is small and roughly real-time on CPU.

This module backs the fabric **`tts` serving engine** (aceteam
`ServingEngine.TTS` / the `tts-speech` provisioning template) — the synthesis
counterpart to `whisper-service`, which backs `transcribe`. It is named for its
implementation (`kokoro`), the same convention as `whisper-service`/`gliner2`.

First dogfood: regenerating the [jasonsun.org](https://jasonsun.org) book
narration (1,071 EN paragraphs) through this service with per-item ACET receipts
instead of the current local hash-caching script
([aceteam-ai/citadel-services#11](https://github.com/aceteam-ai/citadel-services/issues/11)).

## Install

```bash
citadel module install kokoro
```

Or run directly with Docker Compose:

```bash
cd services/kokoro
docker compose up -d          # CPU
# NVIDIA node (see GPU below):
# docker compose -f compose.yml -f compose.gpu.yml up -d
```

First start downloads the Kokoro-82M weights + the requested voices into
`~/citadel-cache/huggingface` (persisted across restarts).

## HTTP API

The container serves on `:8080`; citadel publishes it on host `8210`
(`CITADEL_TTS_HOST_PORT`, **loopback only**). A standalone `docker compose up`
defaults the host publish to `8080`. Examples below use `8080`.

### `POST /v1/audio/speech` (OpenAI-compatible, single item)

```bash
curl -s http://localhost:8080/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input":"Hello from the Citadel.","voice":"am_michael","response_format":"opus","speed":1.0}' \
  -o hello.opus
```

Request fields (`model` is accepted and ignored for OpenAI-client
compatibility):

| Field | Default | Notes |
|-------|---------|-------|
| `input` | (required) | Text to synthesize |
| `voice` | `am_michael` | Any voice from `GET /info` |
| `response_format` | `opus` | `opus`, `mp3`, or `wav` |
| `speed` | `1.0` | 0.5–2.0 |

Response body is the audio bytes. Usage metadata is returned in headers so a
fabric worker can emit a receipt without re-decoding:

```
X-TTS-Cache-Hit: 0|1
X-TTS-Duration-Seconds: 3.575
X-TTS-Chars: 24
X-TTS-Model-Version: kokoro-0.9.4+hexgrad/Kokoro-82M
X-TTS-Cache-Key: <sha256>
```

### `POST /v1/audio/speech/batch` (chapter / paragraph list)

Synthesize a list of items in one call. Responds with an **NDJSON stream** — one
receipt line per item as it completes — so a ~200-paragraph chapter never times
out. Audio is stored in the cache and referenced by URL (fetch each via
`GET /v1/audio/cache/<key>`), so the whole chapter's audio is not buffered in
memory.

```bash
curl -s -N http://localhost:8080/v1/audio/speech/batch \
  -H 'Content-Type: application/json' \
  -d '{"voice":"am_michael","response_format":"opus","items":[
        {"id":"p1","input":"First paragraph."},
        {"id":"p2","input":"Second paragraph."}]}'
```

```
{"chars":16,"seconds":1.35,"cache_hit":false,"voice":"am_michael","format":"opus","model_version":"kokoro-0.9.4+hexgrad/Kokoro-82M","cache_key":"…","index":0,"id":"p1","audio_url":"/v1/audio/cache/…","mime":"audio/ogg"}
{"chars":17,"seconds":1.42,"cache_hit":false,…,"index":1,"id":"p2",…}
```

Each receipt carries **chars in, audio seconds out, cache-hit flag, and model
version** — everything the fabric worker needs for a per-item ACET receipt.

### `GET /info`

Capacity, advertised engine, formats, the full voice list, and cache stats:

```json
{
  "service": "kokoro",
  "engine": "tts",
  "model_version": "kokoro-0.9.4+hexgrad/Kokoro-82M",
  "device": "cpu",
  "capacity": {"slots": 2},
  "formats": ["opus","mp3","wav"],
  "voices": {"american_english":[…],"british_english":[…],"mandarin":[…]},
  "cache": {"entries": 12, "bytes": 480123, "max_bytes": 5000000000}
}
```

### `GET /health`

```json
{"status":"up","model_loaded":true,"model_version":"…","device":"cpu","slots":2}
```

### `POST /v1/audio/cache/bust`

Manual cache bust for a bad-weights incident. Whole cache, or one key:

```bash
curl -s -X POST http://localhost:8080/v1/audio/cache/bust                          # all
curl -s -X POST http://localhost:8080/v1/audio/cache/bust -d '{"key":"<sha256>"}'  # one
```

Normal model upgrades don't need a manual bust: the model+service version is
part of the cache key, so a weights change misses the old entries automatically.

## Voices

Selected per request. `GET /info` lists them; the language is inferred from the
voice-name prefix (Kokoro convention).

- **American English** (`af_*` / `am_*`): `am_michael` (default, the book voice),
  `af_heart`, `af_bella`, `am_adam`, `am_onyx`, … (20 voices)
- **British English** (`bf_*` / `bm_*`): `bf_emma`, `bm_george`, `bm_fable`, …
  (8 voices)
- **Mandarin** (`zf_*` / `zm_*`, via `misaki[zh]`): `zf_xiaoxiao`, `zf_xiaobei`,
  `zm_yunxi`, `zm_yunyang`, … (8 voices)

## Formats

`opus` is the default (Ogg/Opus, `-application voip`, ~32 kbps mono — plenty for
narration and small). `mp3` (libmp3lame, 64 kbps) is the fallback: Safari's
Ogg/Opus support only landed in 18.4 and is still flaky. `wav` is available for
debugging. All encoding is done by `ffmpeg` (bundled in the image) over the raw
24 kHz PCM Kokoro produces.

## Cache

Content-addressed, node-local under the data volume (`~/citadel-cache/kokoro`),
**LRU-capped** by total bytes (`KOKORO_CACHE_MAX_GB`, default 5 GB). The key is
`sha256(model+service version, voice, format, speed, lang, text)`, so identical
requests hit instantly (`cache_hit: true`) and a model/version change busts the
cache without a manual step. The store sits behind a small `CacheStore`
interface (`LocalLRUCache` today) so an org-global blob store can slot in later
without touching the request path.

**Batch durability.** A batch returns `audio_url` references rather than inline
bytes (so a 200-item chapter isn't buffered in memory). To keep those URLs from
404-ing under LRU pressure, every key a batch produces is **pinned against
eviction for the life of that NDJSON response** and released when the stream
finishes. Guarantee: a batch item's audio stays reachable from when its receipt
is emitted until the stream completes — **fetch promptly after the stream
ends.** (Pinning is refcounted, so concurrent batches and repeated items are
safe; eviction skips pinned keys and still evicts the rest.)

## Concurrency / backpressure

`TTS_SLOTS` (default 2) bounds concurrent synthesis via a semaphore and is
advertised on `/health` and `/info`. Requests beyond the slot count **queue**
(await a slot) rather than piling on and OOMing the node — the fabric can read
the advertised capacity to schedule/shed load.

Slots bound *concurrency*; a single oversized `input` can OOM the node on its
own regardless of slots, so each item is also capped at
`KOKORO_MAX_INPUT_CHARS` characters (default 5000). An over-cap single request
gets a `413`; in a batch it becomes a per-item error line and the rest of the
chapter still synthesizes. Split long text into a batch of paragraph-sized
items — which is the batch endpoint's intended shape anyway.

## GPU

The default `:latest` image is **CPU-torch** so it runs on the M1 node and on
NVIDIA nodes alike (on CPU there). **The nvidia device reservation alone is not
enough for acceleration** — a GPU reserved against the CPU-torch image still
runs on CPU. For real GPU acceleration on an NVIDIA node, use the CUDA image via
the GPU override, which builds it, runs it, and demands the GPU explicitly:

```bash
docker compose -f compose.yml -f compose.gpu.yml build   # builds the :cuda image (TORCH_BACKEND=cu124)
docker compose -f compose.yml -f compose.gpu.yml up -d    # runs it with the GPU reserved
```

`compose.gpu.yml` overrides three things vs the CPU compose: `image:` → the
`:cuda` tag, `build.args.TORCH_BACKEND=cu124` (uv installs the CUDA torch
wheel), and `KOKORO_DEVICE=cuda`. That last one is deliberate: the server then
**fails loudly at startup** if CUDA isn't actually usable — either because the
image is the CPU-torch build (`torch.version.cuda is None`) or because no GPU is
visible to the container (missing nvidia runtime / driver / toolkit) — instead
of silently running on CPU. `KOKORO_DEVICE=auto` (the CPU compose default) still
picks CUDA when a GPU happens to be visible and falls back to CPU otherwise.
`GET /health` reports `device` and `torch_cuda_build` (the built backend, `null`
on a CPU image) so you can confirm which path is live.

CPU synthesis is already ~real-time for Kokoro-82M, so GPU is a throughput
optimization, not a requirement.

Or build the CUDA image by hand (equivalent to the compose build):

```bash
docker build --build-arg TORCH_BACKEND=cu124 -t ghcr.io/aceteam-ai/kokoro-service:cuda ./build
```

## Fabric seam (aligning the two sides)

This catalog module is only the **compose service** (the backing image). The
node→fabric engine advertisement and the provisioning template live in other
repos, and a few things must line up for `citadel module install kokoro` to be
fully wired into the fabric `tts` engine:

1. **Port registration (citadel-cli).** Register
   `EnvTTSHostPort = "CITADEL_TTS_HOST_PORT"` / `TTSHostPort = 8210` in
   `services/ports.go` (next free slot after gotenberg's 8209) and add `kokoro`
   to `ServiceHostPorts`/`serviceHostPortEnv`. Until then only the standalone
   `docker compose up` path (host 8080) works — same follow-up shape as
   gotenberg's 8209.
2. **Provisioning template (aceteam python-backend).** The `tts-speech` template
   in `data/provisioning_templates.json` is currently a Coqui/XTTS placeholder
   (`ghcr.io/coqui-ai/tts`, port 5002, `/api/tts`). Per `serving_engine.py`'s
   own docstring the node compose is authoritative on divergence, so point the
   template at this service: `ghcr.io/aceteam-ai/kokoro-service`, port 8080/8210,
   `inference_endpoint` `/v1/audio/speech`. `min_vram_gb` can drop to 0
   (CPU-capable).
3. **Node-side synthesis handler (citadel-cli), optional.** `transcribe` is
   dispatched as a `TRANSCRIBE_AUDIO` job to a Go `TranscribeAudioHandler` that
   proxies `localhost:8101/transcribe`. A symmetric `SYNTHESIZE_SPEECH` handler
   proxying `localhost:8210/v1/audio/speech` (plus the batch route) would give
   `tts` the same job-dispatch path; until it exists the fabric can call the
   OpenAI-compatible endpoint directly. The response headers / NDJSON receipts
   already expose chars-in / seconds-out / cache-hit / model-version for
   per-item metering.
4. **Node capability advertisement.** The `node_tags` in `service.yaml`
   (`engine:tts`, `tts:kokoro`, `model:kokoro-82m`) merge into `citadel.yaml`'s
   node tags on install (per `cmd/manifest.go`'s `addServiceToManifestWithTags`),
   making the node routable for `tts`. If the manifest's
   `capabilities.engines` list is used for scheduling, add `tts` there too.

## Pricing

Proposed at **$0.003 per 1,000 characters** (see the PR description) — mid-band
of the requested $0.002–0.005 and ~5× cheaper than OpenAI `tts-1` ($0.015/1k).
Wired through the existing fabric pricing mechanism (there is no pricing field
in this catalog's schema); the per-item receipts (chars in, seconds out,
cache-hit, model version) are what the fabric worker meters against. Jason
approves the final number at review.

## Requirements

| Requirement | Value |
|-------------|-------|
| GPU | Optional (CPU-capable) |
| Architecture | amd64, arm64 |
| Disk | ~model cache (Kokoro-82M ≈ 350 MB) + audio cache (LRU-capped) |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_SLOTS` | `2` | Concurrent synthesis slots (advertised for backpressure). |
| `KOKORO_MAX_INPUT_CHARS` | `5000` | Max characters per item; over-cap → `413` (per-item error line in a batch). |
| `KOKORO_DEVICE` | `auto` | `auto` (GPU when present) / `cpu` / `cuda`. |
| `KOKORO_DEFAULT_VOICE` | `am_michael` | Default voice. |
| `KOKORO_DEFAULT_FORMAT` | `opus` | `opus` or `mp3`. |
| `KOKORO_CACHE_MAX_GB` | `5` | LRU cap for the node-local audio cache. |
| `KOKORO_OPUS_BITRATE` | `32k` | Opus bitrate. |
| `KOKORO_MP3_BITRATE` | `64k` | MP3 bitrate. |

## Ports

| Host | Container | Description |
|------|-----------|-------------|
| 127.0.0.1:8080 (standalone default) / 127.0.0.1:8210 (citadel-managed, `CITADEL_TTS_HOST_PORT`) | 8080 | Kokoro TTS HTTP API, **loopback only** |

Published to `127.0.0.1` deliberately: the service has no auth of its own and
the only intended consumer is the co-located citadel worker on the same node.

## Volumes

| Host | Container | Description |
|------|-----------|-------------|
| `~/citadel-cache/huggingface` | `/root/.cache/huggingface` | Kokoro-82M weights + voices |
| `~/citadel-cache/kokoro` | `/data` | Content-addressed audio cache (LRU) |

## Links

- [Kokoro (hexgrad/kokoro)](https://github.com/hexgrad/kokoro)
- [Kokoro-82M weights](https://huggingface.co/hexgrad/Kokoro-82M)
- [misaki G2P](https://github.com/hexgrad/misaki)
- [aceteam-ai/citadel-services#11](https://github.com/aceteam-ai/citadel-services/issues/11)
