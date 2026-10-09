"""Voice-clone text-to-speech HTTP service for Citadel nodes.

Two cloning engines behind one OpenAI-style API (aceteam-ai/citadel-services#28):

* ``chatterbox`` (Resemble AI, MIT code and weights): the default, commercially
  usable, every output carries Chatterbox's built-in Perth watermark.
* ``omnivoice`` (k2-fsa, code Apache-2.0, weights CC-BY-NC): opt-in only, behind
  OMNIVOICE_ACCEPT_NONCOMMERCIAL_LICENSE=true. Adds voice design (``instruct``).

Surfaces:

    GET    /health               -> {"model_loaded", ...}
    GET    /info                 -> engines with weights licenses, capacity, limits
    POST   /v1/voices            -> enroll a consented 3 to 10 s reference clip
    GET    /v1/voices            -> list enrolled voices (with consent records)
    DELETE /v1/voices/{voice_id} -> delete a voice and its prompts
    POST   /v1/audio/speech      -> OpenAI-compatible synthesis (wav | mp3 | opus),
                                    JSON {audio_base64, words, receipt} when
                                    word_timestamps=true

Design notes:

* **Consent first.** Enrollment is rejected (422) before the audio is even
  decoded unless it carries a complete consent record. The record is stored with
  the voice and echoed in every receipt rendered from it.
* **Voices are node-local.** Each voice is a directory under the data volume
  holding meta.json (consent, transcript), the consented reference clip
  (normalized ref.wav) and one prompt file per engine, built lazily. Keeping
  ref.wav lets a voice enrolled while OmniVoice was disabled get an OmniVoice
  prompt later without re-enrolling.
* **License gate.** OmniVoice requests are refused (403) unless the node owner
  opted in; its weights are never loaded or downloaded otherwise. Every
  OmniVoice response and receipt carries model_license "CC-BY-NC".
* **Capacity.** An asyncio.Semaphore of TTS_SLOTS bounds concurrent GPU work
  (enrollment prompt builds and synthesis); excess requests queue.
* **Receipts.** Kokoro's X-TTS-* header contract (citadel-cli's
  SYNTHESIZE_SPEECH handler parses it) plus X-TTS-Engine, X-TTS-Model-License,
  X-TTS-Voice-Id and X-TTS-Receipt (base64url JSON of the full receipt).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import wave
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator

import engines as engines_mod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice-clone")

# --- Configuration --------------------------------------------------------

SERVICE_VERSION = "0.1.0"
PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("VOICE_CLONE_DATA_DIR", "/data")
TTS_SLOTS = max(1, int(os.environ.get("TTS_SLOTS", "1")))
MAX_INPUT_CHARS = max(1, int(os.environ.get("VOICE_CLONE_MAX_INPUT_CHARS", "2000")))
DEFAULT_ENGINE = os.environ.get("VOICE_CLONE_DEFAULT_ENGINE", "chatterbox")
DEFAULT_FORMAT = os.environ.get("VOICE_CLONE_DEFAULT_FORMAT", "mp3")
REF_MIN_SECONDS = float(os.environ.get("VOICE_CLONE_REF_MIN_SECONDS", "3"))
REF_MAX_SECONDS = float(os.environ.get("VOICE_CLONE_REF_MAX_SECONDS", "10"))
MAX_UPLOAD_BYTES = int(os.environ.get("VOICE_CLONE_MAX_UPLOAD_MB", "20")) * 1024 * 1024
PRELOAD_DEFAULT_ENGINE = os.environ.get("VOICE_CLONE_PRELOAD", "true").lower() == "true"
OPUS_BITRATE = os.environ.get("VOICE_CLONE_OPUS_BITRATE", "32k")
MP3_BITRATE = os.environ.get("VOICE_CLONE_MP3_BITRATE", "64k")
# Chatterbox generates at most ~40 s per call; longer input is split on
# sentence boundaries into chunks of at most this many characters.
CHUNK_CHARS = int(os.environ.get("VOICE_CLONE_CHUNK_CHARS", "300"))
REF_SAMPLE_RATE = 24000


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


# Read once at startup: the node owner's explicit opt-in to CC-BY-NC weights.
OMNIVOICE_LICENSE_ACCEPTED = _env_flag("OMNIVOICE_ACCEPT_NONCOMMERCIAL_LICENSE")

FORMAT_MIME = {"wav": "audio/wav", "mp3": "audio/mpeg", "opus": "audio/ogg"}
VOICE_ID_RE = re.compile(r"^voice_[0-9a-f]{24}$")

# Boundaries (replaced in tests).
ENGINES: dict[str, engines_mod.Engine] = {
    "chatterbox": engines_mod.ChatterboxEngine(),
    "omnivoice": engines_mod.OmniVoiceEngine(),
}
TRANSCRIBER: Any = engines_mod.Transcriber()
ALIGNER: Any = engines_mod.Aligner()

_slots: asyncio.Semaphore | None = None


def voices_dir() -> str:
    return os.path.join(DATA_DIR, "voices")


def engine_enabled(engine: engines_mod.Engine) -> bool:
    return engine.commercial_use or OMNIVOICE_LICENSE_ACCEPTED


def _license_refusal(engine: engines_mod.Engine) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=(
            f"engine '{engine.name}' is disabled: its pretrained weights are licensed "
            f"{engine.weights_license} (non-commercial use only). The node owner must set "
            "OMNIVOICE_ACCEPT_NONCOMMERCIAL_LICENSE=true in the module config to enable it."
        ),
    )


def get_engine(name: str) -> engines_mod.Engine:
    engine = ENGINES.get(name)
    if engine is None:
        raise HTTPException(
            status_code=400, detail=f"unknown engine '{name}'; one of {sorted(ENGINES)}"
        )
    if not engine_enabled(engine):
        raise _license_refusal(engine)
    return engine


# --- Audio helpers --------------------------------------------------------


def _pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(_pcm16(samples))
    return buf.getvalue()


def _ffmpeg(args: list[str], data: bytes) -> bytes:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        logger.error("ffmpeg failed: %s", proc.stderr.decode("utf-8", "replace")[:500])
        raise HTTPException(status_code=500, detail="audio processing failed")
    return proc.stdout


def encode(samples: np.ndarray, sample_rate: int, fmt: str) -> bytes:
    """Encode float32 [-1, 1] mono PCM to wav, mp3 or Ogg/Opus."""
    if fmt == "wav":
        return _wav_bytes(samples, sample_rate)
    if fmt == "mp3":
        codec = ["-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-f", "mp3"]
    elif fmt == "opus":
        codec = ["-c:a", "libopus", "-b:a", OPUS_BITRATE, "-application", "voip", "-f", "ogg"]
    else:
        raise HTTPException(status_code=400, detail=f"unsupported format '{fmt}'")
    pcm_in = ["-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0"]
    return _ffmpeg([*pcm_in, *codec, "pipe:1"], _pcm16(samples))


def apply_tempo(samples: np.ndarray, sample_rate: int, speed: float) -> np.ndarray:
    """Pitch-preserving tempo change (ffmpeg atempo, valid for 0.5 to 2.0)."""
    if speed == 1.0 or samples.size == 0:
        return samples
    out = _ffmpeg(
        [
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
            "-filter:a", f"atempo={speed:.4f}", "-f", "s16le", "pipe:1",
        ],
        _pcm16(samples),
    )
    return np.frombuffer(out, dtype="<i2").astype("float32") / 32767.0


def normalize_reference(raw: bytes) -> tuple[bytes, float]:
    """Decode any ffmpeg-readable upload to 24 kHz mono 16-bit WAV + duration."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "upload")
        with open(src, "wb") as f:
            f.write(raw)
        # Raw PCM out, so the sample count (and so the duration) is exact.
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src,
                "-ac", "1", "-ar", str(REF_SAMPLE_RATE), "-f", "s16le", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    if proc.returncode != 0 or not proc.stdout:
        raise HTTPException(status_code=422, detail="reference audio could not be decoded")
    samples = np.frombuffer(proc.stdout, dtype="<i2").astype("float32") / 32767.0
    return _wav_bytes(samples, REF_SAMPLE_RATE), round(samples.size / REF_SAMPLE_RATE, 3)


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def split_for_engine(text: str, max_chars: int) -> list[str]:
    """Split text on sentence boundaries into chunks of at most max_chars.

    A single sentence longer than max_chars is split on whitespace. Chunks keep
    their words in order and no text is dropped.
    """
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text.strip()):
        pieces = [sentence]
        if len(sentence) > max_chars:
            pieces, buf = [], ""
            for word in sentence.split():
                if buf and len(buf) + 1 + len(word) > max_chars:
                    pieces.append(buf)
                    buf = word
                else:
                    buf = f"{buf} {word}".strip()
            if buf:
                pieces.append(buf)
        for piece in pieces:
            if current and len(current) + 1 + len(piece) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = f"{current} {piece}".strip()
    if current:
        chunks.append(current)
    return chunks


def render(
    engine: engines_mod.Engine,
    text: str,
    prompt_path: str | None,
    instruct: str | None,
    speed: float,
    language: str | None,
) -> tuple[np.ndarray, int]:
    """Run the engine (chunked when it has no long-form mode) and apply speed."""
    native_speed = speed if engine.native_speed else 1.0
    if engine.native_long_form:
        samples, sr = engine.synthesize(text, prompt_path, instruct, native_speed, language)
    else:
        parts: list[np.ndarray] = []
        sr = 0
        for chunk in split_for_engine(text, CHUNK_CHARS):
            part, sr = engine.synthesize(chunk, prompt_path, instruct, native_speed, language)
            if parts:
                parts.append(np.zeros(int(sr * 0.12), dtype="float32"))
            parts.append(np.asarray(part, dtype="float32").reshape(-1))
        samples = np.concatenate(parts) if parts else np.zeros(0, dtype="float32")
    samples = np.asarray(samples, dtype="float32").reshape(-1)
    if not engine.native_speed:
        samples = apply_tempo(samples, sr, speed)
    return samples, sr


# --- Voice store ----------------------------------------------------------


class Consent(BaseModel):
    """Attestation that the speaker agreed to having their voice cloned."""

    speaker_name: str = Field(min_length=1, max_length=200)
    attested_by: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=2000)
    attested_at: str = Field(min_length=1, max_length=64)

    @field_validator("speaker_name", "attested_by", "statement", "attested_at")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("attested_at")
    @classmethod
    def _iso8601(cls, v: str) -> str:
        try:
            dt.datetime.fromisoformat(v)
        except ValueError as e:
            raise ValueError("must be an ISO 8601 timestamp") from e
        return v


class EnrollFields(BaseModel):
    consent: Consent
    name: str | None = Field(None, max_length=200)
    transcript: str | None = Field(None, max_length=2000)
    engines: list[str] | None = None


CONSENT_REQUIRED = (
    "consent is required: an object with speaker_name, attested_by, statement and "
    "attested_at (ISO 8601) recording that the speaker agreed to be cloned"
)


def _voice_path(voice_id: str) -> str:
    if not VOICE_ID_RE.match(voice_id):
        raise HTTPException(status_code=404, detail=f"voice '{voice_id}' not found")
    return os.path.join(voices_dir(), voice_id)


def load_voice(voice_id: str) -> dict:
    path = _voice_path(voice_id)
    try:
        with open(os.path.join(path, "meta.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"voice '{voice_id}' not found") from None


def save_voice(meta: dict) -> None:
    path = _voice_path(meta["voice_id"])
    tmp = os.path.join(path, "meta.json.tmp")
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, os.path.join(path, "meta.json"))


def public_voice(meta: dict) -> dict:
    return {
        "voice_id": meta["voice_id"],
        "name": meta["name"],
        "created_at": meta["created_at"],
        "reference_seconds": meta["reference_seconds"],
        "transcript": meta.get("transcript"),
        "transcript_source": meta.get("transcript_source"),
        "engines": sorted(meta.get("prompts", {})),
        "consent": meta["consent"],
    }


def ensure_prompt(meta: dict, engine: engines_mod.Engine) -> str:
    """Return the engine's prompt file for this voice, building it if missing.

    Runs on a worker thread under a slot. Auto-transcribes the reference clip
    first if this engine needs a transcript and none was provided.
    """
    path = _voice_path(meta["voice_id"])
    prompt_path = os.path.join(path, engine.prompt_filename)
    if engine.name in meta.get("prompts", {}) and os.path.isfile(prompt_path):
        return prompt_path
    ref = os.path.join(path, "ref.wav")
    if engine.needs_transcript() and not meta.get("transcript"):
        meta["transcript"] = TRANSCRIBER.transcribe(ref)
        meta["transcript_source"] = "asr"
    tmp = prompt_path + ".tmp"
    engine.build_prompt(ref, meta.get("transcript"), tmp)
    os.replace(tmp, prompt_path)
    meta.setdefault("prompts", {})[engine.name] = {
        "file": engine.prompt_filename,
        "built_at": _now(),
        "model_version": engine.model_version(),
        "model_license": engine.weights_license,
    }
    save_voice(meta)
    return prompt_path


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


async def _parse_enrollment(request: Request) -> tuple[EnrollFields, bytes]:
    ctype = request.headers.get("content-type", "")
    audio: bytes | None = None
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        fields: dict[str, Any] = {}
        raw_consent = form.get("consent")
        if isinstance(raw_consent, str) and raw_consent.strip():
            try:
                fields["consent"] = json.loads(raw_consent)
            except ValueError:
                raise HTTPException(status_code=422, detail="consent must be a JSON object") from None
        for key in ("name", "transcript"):
            if isinstance(form.get(key), str) and form.get(key):
                fields[key] = form.get(key)
        if isinstance(form.get("engines"), str) and form.get("engines"):
            fields["engines"] = [e.strip() for e in str(form.get("engines")).split(",") if e.strip()]
        upload = form.get("audio")
        if upload is not None and hasattr(upload, "read"):
            audio = await upload.read(MAX_UPLOAD_BYTES + 1)
    elif ctype.startswith("application/json"):
        try:
            fields = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="body is not valid JSON") from None
        if not isinstance(fields, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        b64 = fields.pop("audio_base64", None)
        if isinstance(b64, str):
            try:
                audio = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(status_code=422, detail="audio_base64 is not valid base64") from None
    else:
        raise HTTPException(
            status_code=415, detail="use multipart/form-data (audio file) or application/json"
        )

    # Consent is checked before anything else touches the audio.
    if not fields.get("consent"):
        raise HTTPException(status_code=422, detail=CONSENT_REQUIRED)
    try:
        parsed = EnrollFields.model_validate(fields)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise HTTPException(status_code=422, detail=f"invalid enrollment: {problems}") from None
    if not audio:
        raise HTTPException(status_code=422, detail="reference audio is required")
    if len(audio) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="reference audio upload is too large")
    return parsed, audio


# --- Receipts -------------------------------------------------------------


def build_receipt(
    *,
    engine: engines_mod.Engine,
    text: str,
    voice: str,
    meta: dict | None,
    fmt: str,
    seconds: float,
    speed: float,
    instruct: str | None,
    render_seconds: float,
    words: list[dict] | None,
    unaligned: list[str] | None,
) -> dict:
    receipt_id = hashlib.sha256(
        f"{engine.name}\0{voice}\0{fmt}\0{speed}\0{instruct}\0{text}\0{time.time_ns()}".encode()
    ).hexdigest()
    receipt: dict[str, Any] = {
        "receipt_id": receipt_id,
        "service": "voice-clone",
        "service_version": SERVICE_VERSION,
        "engine": engine.name,
        "model_version": engine.model_version(),
        "model_license": engine.weights_license,
        "commercial_use": engine.commercial_use,
        "watermark": engine.watermark,
        "voice": voice,
        "voice_id": meta["voice_id"] if meta else None,
        "consent": meta["consent"] if meta else None,
        "instruct": instruct,
        "chars": len(text),
        "seconds": seconds,
        "format": fmt,
        "speed": speed,
        "render_seconds": render_seconds,
        "cache_hit": False,
        "created_at": _now(),
    }
    if words is not None:
        receipt["word_count"] = len(words)
        receipt["unaligned_words"] = unaligned or []
    return receipt


def receipt_headers(receipt: dict) -> dict[str, str]:
    encoded = base64.urlsafe_b64encode(json.dumps(receipt).encode()).decode("ascii")
    return {
        # Kokoro-compatible contract parsed by citadel-cli's SYNTHESIZE_SPEECH.
        "X-TTS-Cache-Hit": "0",
        "X-TTS-Duration-Seconds": str(receipt["seconds"]),
        "X-TTS-Chars": str(receipt["chars"]),
        "X-TTS-Model-Version": receipt["model_version"],
        "X-TTS-Cache-Key": receipt["receipt_id"],
        # Voice-clone additions.
        "X-TTS-Engine": receipt["engine"],
        "X-TTS-Model-License": receipt["model_license"],
        "X-TTS-Voice-Id": receipt["voice_id"] or "",
        "X-TTS-Render-Seconds": str(receipt["render_seconds"]),
        "X-TTS-Receipt": encoded,
    }


# --- App ------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _slots
    os.makedirs(voices_dir(), exist_ok=True)
    _slots = asyncio.Semaphore(TTS_SLOTS)
    default = ENGINES.get(DEFAULT_ENGINE)
    if default is None:
        raise RuntimeError(f"VOICE_CLONE_DEFAULT_ENGINE={DEFAULT_ENGINE} is not an engine")
    if PRELOAD_DEFAULT_ENGINE and engine_enabled(default):
        await asyncio.to_thread(default.ensure_loaded)
    logger.info(
        "voice-clone ready: default=%s slots=%d omnivoice_license_accepted=%s",
        DEFAULT_ENGINE, TTS_SLOTS, OMNIVOICE_LICENSE_ACCEPTED,
    )
    yield


app = FastAPI(title="Voice Clone TTS Service", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    default = ENGINES[DEFAULT_ENGINE]
    loaded = default.is_loaded() or not PRELOAD_DEFAULT_ENGINE
    return {
        "status": "up" if loaded else "loading",
        "model_loaded": loaded,
        "default_engine": DEFAULT_ENGINE,
        "engines_loaded": sorted(n for n, e in ENGINES.items() if e.is_loaded()),
        "slots": TTS_SLOTS,
        "service_version": SERVICE_VERSION,
    }


@app.get("/info")
async def info() -> dict:
    return {
        "service": "voice-clone",
        "engine": "tts",
        "service_version": SERVICE_VERSION,
        "default_engine": DEFAULT_ENGINE,
        "default_format": DEFAULT_FORMAT,
        "formats": list(FORMAT_MIME),
        "capacity": {"slots": TTS_SLOTS, "max_input_chars": MAX_INPUT_CHARS},
        "reference_audio_seconds": {"min": REF_MIN_SECONDS, "max": REF_MAX_SECONDS},
        "engines": [
            {
                "name": e.name,
                "model": e.model_id,
                "weights_license": e.weights_license,
                "code_license": e.code_license,
                "commercial_use": e.commercial_use,
                "enabled": engine_enabled(e),
                "loaded": e.is_loaded(),
                "watermark": e.watermark,
                "supports_instruct": e.supports_instruct,
            }
            for e in ENGINES.values()
        ],
        "word_timestamps": {"aligner": getattr(ALIGNER, "model_id", None), "languages": ["en"]},
        "gpu_memory": engines_mod.cuda_memory(),
    }


@app.post("/v1/voices", status_code=201)
async def enroll_voice(request: Request):
    fields, audio = await _parse_enrollment(request)
    targets = fields.engines or [n for n, e in ENGINES.items() if engine_enabled(e)]
    selected = [get_engine(n) for n in dict.fromkeys(targets)]
    ref_wav, seconds = await asyncio.to_thread(normalize_reference, audio)
    if not (REF_MIN_SECONDS <= seconds <= REF_MAX_SECONDS):
        raise HTTPException(
            status_code=422,
            detail=(
                f"reference audio is {seconds:.2f}s; it must be between "
                f"{REF_MIN_SECONDS:g} and {REF_MAX_SECONDS:g} seconds"
            ),
        )
    voice_id = "voice_" + secrets.token_hex(12)
    path = _voice_path(voice_id)
    os.makedirs(path)
    meta = {
        "voice_id": voice_id,
        "name": fields.name or fields.consent.speaker_name,
        "created_at": _now(),
        "reference_seconds": seconds,
        "reference_sha256": hashlib.sha256(ref_wav).hexdigest(),
        "transcript": fields.transcript,
        "transcript_source": "provided" if fields.transcript else None,
        "consent": fields.consent.model_dump(),
        "prompts": {},
    }
    try:
        with open(os.path.join(path, "ref.wav"), "wb") as f:
            f.write(ref_wav)
        save_voice(meta)
        assert _slots is not None
        async with _slots:
            for engine in selected:
                await asyncio.to_thread(ensure_prompt, meta, engine)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return public_voice(meta)


@app.get("/v1/voices")
async def list_voices() -> dict:
    out = []
    root = voices_dir()
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if VOICE_ID_RE.match(name) and os.path.isfile(os.path.join(root, name, "meta.json")):
            out.append(public_voice(load_voice(name)))
    return {"voices": out}


@app.delete("/v1/voices/{voice_id}", status_code=204)
async def delete_voice(voice_id: str):
    load_voice(voice_id)
    shutil.rmtree(_voice_path(voice_id))
    return Response(status_code=204)


class SpeechRequest(BaseModel):
    # OpenAI-compatible core: input, voice, response_format, speed (model ignored).
    input: str = Field(..., min_length=1)
    voice: str = "auto"
    engine: str = DEFAULT_ENGINE
    instruct: str | None = Field(None, max_length=500)
    language: str | None = Field(None, max_length=64)
    response_format: str = DEFAULT_FORMAT
    speed: float = Field(1.0, ge=0.5, le=2.0)
    word_timestamps: bool = False
    model: str | None = None

    model_config = {"protected_namespaces": ()}


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    engine = get_engine(req.engine)
    fmt = req.response_format
    if fmt not in FORMAT_MIME:
        raise HTTPException(status_code=400, detail=f"unsupported format '{fmt}'; one of {list(FORMAT_MIME)}")
    if len(req.input) > MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"input is {len(req.input)} chars; max is {MAX_INPUT_CHARS} (VOICE_CLONE_MAX_INPUT_CHARS)",
        )
    if req.instruct and not engine.supports_instruct:
        raise HTTPException(status_code=400, detail=f"engine '{engine.name}' does not support instruct")
    if req.instruct and req.voice != "auto":
        raise HTTPException(
            status_code=400, detail="instruct designs a new voice; send voice='auto' with it"
        )
    meta = load_voice(req.voice) if req.voice != "auto" else None

    assert _slots is not None
    async with _slots:
        started = time.monotonic()
        prompt = await asyncio.to_thread(ensure_prompt, meta, engine) if meta else None
        samples, sr = await asyncio.to_thread(
            render, engine, req.input, prompt, req.instruct, req.speed, req.language
        )
        words = unaligned = None
        if req.word_timestamps:
            words, unaligned = await asyncio.to_thread(ALIGNER.align, samples, sr, req.input)
        data = await asyncio.to_thread(encode, samples, sr, fmt)
        render_seconds = round(time.monotonic() - started, 3)

    receipt = build_receipt(
        engine=engine,
        text=req.input,
        voice=req.voice,
        meta=meta,
        fmt=fmt,
        seconds=round(samples.size / sr, 3) if sr else 0.0,
        speed=req.speed,
        instruct=req.instruct,
        render_seconds=render_seconds,
        words=words,
        unaligned=unaligned,
    )
    headers = receipt_headers(receipt)
    if req.word_timestamps:
        return JSONResponse(
            content={
                "audio_base64": base64.b64encode(data).decode("ascii"),
                "mime": FORMAT_MIME[fmt],
                "words": words,
                "receipt": receipt,
            },
            headers=headers,
        )
    return Response(content=data, media_type=FORMAT_MIME[fmt], headers=headers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
