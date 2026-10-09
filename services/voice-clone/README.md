# voice-clone: Voice-cloning Text-to-Speech

Voice cloning and voice design on your own Citadel node, behind one
OpenAI-style API. Two engines:

| Engine | Code license | Weights license | Commercial use | Status | Extras |
|--------|--------------|-----------------|----------------|--------|--------|
| [Chatterbox](https://github.com/resemble-ai/chatterbox) (Resemble AI) | MIT | MIT | yes | **default** | built-in Perth perceptual watermark, always on |
| [OmniVoice](https://github.com/k2-fsa/OmniVoice) (k2-fsa) | Apache-2.0 | **CC-BY-NC** | **no** | opt-in | voice design (`instruct`), 600+ languages, inline non-verbal tags |

[kokoro](../kokoro/) stays the stock-voice engine; this module adds cloning
from a short reference clip and attribute-based voice design.

Tracking issue: [aceteam-ai/citadel-services#28](https://github.com/aceteam-ai/citadel-services/issues/28).

## Licensing: read before enabling OmniVoice

The OmniVoice model card states: "The pre-trained model is licensed under the
CC-BY-NC due to constraints from its training data (e.g., Emilia)." Audio made
with those weights must not be used commercially. So:

- OmniVoice is **off** unless the node owner sets
  `OMNIVOICE_ACCEPT_NONCOMMERCIAL_LICENSE=true` in the module config. While it
  is off, every OmniVoice request is refused with `403` and the weights are
  never downloaded.
- Every OmniVoice response carries `X-TTS-Model-License: CC-BY-NC`, and every
  receipt carries `"model_license": "CC-BY-NC"` and `"commercial_use": false`.
- `GET /info` lists each engine with its weights license and whether it is
  enabled.

Chatterbox weights are MIT and commercially usable; it is the default for that
reason.

## Consent and misuse controls

- **Consent is mandatory.** `POST /v1/voices` is rejected (`422`) unless it
  carries a `consent` object with `speaker_name`, `attested_by`, `statement`
  and `attested_at` (ISO 8601). Nothing is decoded or stored without it.
- **Consent travels with the voice.** The record is stored next to the voice
  and echoed in the receipt of every clip rendered from it.
- **Voices stay on the node.** They live under `~/citadel-cache/voice-clone`
  and the API is published on `127.0.0.1` only, with no public endpoint.
  `DELETE /v1/voices/{id}` removes the reference clip, prompts and consent
  record.
- **Watermarking.** Chatterbox applies its built-in Perth implicit watermark to
  every output. The service refuses to load Chatterbox if the watermarker is
  unavailable. OmniVoice has no watermark; its receipts say `"watermark": null`.

## Install

GPU required (no CPU fallback). The image ships CUDA torch, so the host needs
only the NVIDIA driver and container toolkit.

```bash
citadel module install voice-clone
```

Or run directly with Docker Compose. The GPU override is required:

```bash
cd services/voice-clone
docker compose -f compose.yml -f compose.gpu.yml up -d
```

First start downloads the Chatterbox weights into `~/citadel-cache/huggingface`.
OmniVoice (only once enabled), the Whisper ASR model (only for transcript-less
enrollment used with OmniVoice) and the MMS forced aligner (only for
`word_timestamps`) download on first use into the same cache.

## Host port

The container serves `:8080`. citadel publishes it on host **8215**
(`CITADEL_VOICE_CLONE_HOST_PORT`), the next free slot in citadel-cli's
`services/ports.go` 8200 block after omnivoice's 8214. A standalone
`docker compose up` also defaults to 8215. Registering the port in citadel-cli
is a pending follow-up (see below). Examples use 8215.

## HTTP API

### `POST /v1/voices`: enroll a voice

3 to 10 seconds of clean reference speech (any format ffmpeg can decode), an
optional transcript, and the required consent record. Multipart:

```bash
curl -s http://127.0.0.1:8215/v1/voices \
  -F audio=@reference.wav \
  -F transcript='Exact words spoken in the clip.' \
  -F name='Narrator A' \
  -F consent='{"speaker_name":"Narrator A","attested_by":"node-owner","statement":"I consent to my voice being cloned for narration on this node.","attested_at":"2026-10-08T12:00:00Z"}'
```

or JSON with `audio_base64` in place of the file. Optional `engines` (list, or a
comma-separated form field) limits which engine prompts are built now; the
default is every enabled engine. A missing prompt is built lazily from the
stored reference clip on first use, so a voice enrolled while OmniVoice was off
works with OmniVoice after it is enabled.

If `transcript` is omitted it is auto-transcribed with Whisper when an
OmniVoice prompt is first built (Chatterbox does not need it).

Response (`201`):

```json
{
  "voice_id": "voice_3f9c...",
  "name": "Narrator A",
  "created_at": "2026-10-08T12:00:05+00:00",
  "reference_seconds": 8.2,
  "transcript": "Exact words spoken in the clip.",
  "transcript_source": "provided",
  "engines": ["chatterbox"],
  "consent": {"speaker_name": "Narrator A", "attested_by": "node-owner", "statement": "...", "attested_at": "2026-10-08T12:00:00Z"}
}
```

Errors: `422` missing or incomplete consent, undecodable audio, or a clip
outside 3 to 10 s; `403` an `engines` entry that is license-gated off; `413`
upload over `VOICE_CLONE_MAX_UPLOAD_MB`.

On disk each voice is `voices/<voice_id>/` with `meta.json` (consent,
transcript, prompt provenance), `ref.wav` (24 kHz mono) and one prompt per
engine: `chatterbox.pt` (Chatterbox `Conditionals.save`) and `omnivoice.pt`
(OmniVoice `VoiceClonePrompt.save`).

### `GET /v1/voices`, `DELETE /v1/voices/{voice_id}`

List voices with their consent records; delete one (`204`, then `404`).

### `POST /v1/audio/speech`: synthesize (OpenAI-compatible)

```bash
curl -s http://127.0.0.1:8215/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input":"Hello from the Citadel.","voice":"voice_3f9c...","response_format":"mp3"}' \
  -o hello.mp3
```

| Field | Default | Notes |
|-------|---------|-------|
| `input` | (required) | Text, up to `VOICE_CLONE_MAX_INPUT_CHARS` |
| `engine` | `chatterbox` | `chatterbox` or `omnivoice` (license-gated) |
| `voice` | `auto` | An enrolled `voice_id`, or `auto` (Chatterbox built-in voice; OmniVoice picks or designs one) |
| `instruct` | none | OmniVoice only, with `voice: "auto"`: voice design, for example `"female, low pitch, british accent"` |
| `language` | none | OmniVoice language name or code (`"en"`, `"English"`) |
| `speed` | `1.0` | 0.5 to 2.0. OmniVoice natively; Chatterbox via pitch-preserving ffmpeg `atempo` |
| `response_format` | `mp3` | `mp3`, `opus` (Ogg/Opus) or `wav` |
| `word_timestamps` | `false` | Returns JSON instead of audio bytes (see below) |
| `model` | none | Accepted and ignored, for OpenAI clients |

OmniVoice also understands inline non-verbal tags in `input` such as
`[laughter]` and `[sigh]`. Chatterbox input longer than about 300 characters is
split on sentence boundaries and joined with a short pause.

Response headers keep kokoro's receipt contract (parsed by citadel-cli's
SYNTHESIZE_SPEECH handler) and add engine, license and voice:

```
X-TTS-Cache-Hit: 0
X-TTS-Duration-Seconds: 2.84
X-TTS-Chars: 23
X-TTS-Model-Version: chatterbox-0.1.7+ResembleAI/chatterbox
X-TTS-Cache-Key: <receipt id>
X-TTS-Engine: chatterbox
X-TTS-Model-License: MIT
X-TTS-Voice-Id: voice_3f9c...
X-TTS-Render-Seconds: 1.92
X-TTS-Receipt: <base64url JSON receipt>
```

The full receipt (also the `receipt` field of the JSON response):

```json
{
  "receipt_id": "<sha256>", "service": "voice-clone", "service_version": "0.1.0",
  "engine": "omnivoice", "model_version": "omnivoice-0.2.1+k2-fsa/OmniVoice",
  "model_license": "CC-BY-NC", "commercial_use": false, "watermark": null,
  "voice": "voice_3f9c...", "voice_id": "voice_3f9c...", "consent": {"speaker_name": "..."},
  "instruct": null, "chars": 23, "seconds": 2.84, "format": "mp3", "speed": 1.0,
  "render_seconds": 3.1, "cache_hit": false, "created_at": "2026-10-08T12:01:00+00:00"
}
```

### Word timestamps

With `"word_timestamps": true` the response is JSON:

```json
{
  "audio_base64": "<encoded audio>",
  "mime": "audio/mpeg",
  "words": [{"word": "Hello", "start": 0.12, "end": 0.41}],
  "receipt": {"...": "...", "word_count": 4, "unaligned_words": []}
}
```

Times come from forced alignment of the rendered audio against the input text
(torchaudio MMS_FA, CTC). Alignment is English and Latin script only: words are
folded to lowercase a to z plus apostrophe, and words with nothing alignable
(numbers, symbols, other scripts) are listed in `unaligned_words` rather than
given invented times. Spell numbers out for full coverage.

### `GET /health`, `GET /info`

`/health` reports `model_loaded` once the default engine is loaded. `/info`
lists engines (model, code and weights license, commercial use, enabled,
loaded, watermark), formats, limits and current/peak GPU memory.

## Configuration

| Variable | Default | Notes |
|----------|---------|-------|
| `OMNIVOICE_ACCEPT_NONCOMMERCIAL_LICENSE` | `false` | `true` enables OmniVoice (CC-BY-NC weights) |
| `TTS_SLOTS` | `1` | Concurrent GPU work (prompt builds plus synthesis); excess queues |
| `VOICE_CLONE_DEFAULT_ENGINE` | `chatterbox` | Engine when a request omits `engine` |
| `VOICE_CLONE_DEFAULT_FORMAT` | `mp3` | |
| `VOICE_CLONE_MAX_INPUT_CHARS` | `2000` | Over-cap requests get `413` |
| `VOICE_CLONE_PRELOAD` | `true` | Load the default engine at startup |
| `VOICE_CLONE_ASR_MODEL` | `openai/whisper-large-v3-turbo` | Transcript-less enrollment (OmniVoice) |
| `OMNIVOICE_MODEL` | `k2-fsa/OmniVoice` | |
| `OMNIVOICE_NUM_STEP` | `32` | Decoding steps, quality vs latency |
| `VOICE_CLONE_MP3_BITRATE` / `VOICE_CLONE_OPUS_BITRATE` | `64k` / `32k` | |
| `VOICE_CLONE_GPU_COUNT` | `all` | GPUs reserved by compose.gpu.yml |

FlashInfer: omnivoice 0.2.1 has no FlashInfer code path (attention runs through
transformers SDPA), so there is no flag for it yet. Adding one is a follow-up
once upstream exposes it.

## Dependencies

chatterbox-tts 0.1.7 pins `transformers==5.2.0` and omnivoice 0.2.1 needs
`transformers>=5.3.0`, so the Dockerfile installs both packages without their
dependency sets and lists the runtime dependencies itself, on transformers
5.3.0. `setuptools<81` is pinned because resemble-perth imports
`pkg_resources` and otherwise silently disables the watermarker; the build
fails if the watermarker does not import.

## Tests

API contract tests run on any machine with ffmpeg, no GPU and no torch. The
engines, transcriber and aligner are replaced at their boundary with fakes that
return real PCM, so the consent gate, license gate, voice store and the real
ffmpeg encode and tempo paths are exercised:

```bash
cd services/voice-clone/build
uv run pytest -q tests
```

## Follow-ups outside this repo

- citadel-cli: register `CITADEL_VOICE_CLONE_HOST_PORT` (8215) in
  `services/ports.go`, wire compose.gpu.yml into the managed GPU path, and pass
  `voice`, `engine`, `instruct` and `word_timestamps` through the
  SYNTHESIZE_SPEECH job to this module as a new backend.
- Platform: voice enrollment UI and API (collecting the consent record), and
  SynthesizeSpeech node params for engine, voice and instruct.
