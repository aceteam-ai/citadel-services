"""Kokoro text-to-speech HTTP service for Citadel nodes.

Serves speech synthesis (Kokoro-82M, Apache-2.0) behind three surfaces:

    GET  /health                 -> {"model_loaded", "model_version", ...}
    GET  /info                   -> capacity/slots + voices + cache stats
    POST /v1/audio/speech        -> single item, OpenAI-compatible, audio bytes
    POST /v1/audio/speech/batch  -> array of items, NDJSON stream of per-item
                                     receipts (audio ref + duration + cache-hit)
    GET  /v1/audio/cache/<hash>  -> fetch a cached batch item's audio bytes
    POST /v1/audio/cache/bust    -> manual cache bust (all, or a specific key)

Design notes (issue aceteam-ai/citadel-services#11):

* **Content-addressed cache** keyed by (service+model version, voice, format,
  speed, lang, text hash), node-local under the service data volume, LRU-capped
  by total bytes. The store is behind a small `CacheStore` interface so an
  org-global blob store can slot in later without touching the request path.
* **Capacity / backpressure**: an asyncio.Semaphore of TTS_SLOTS bounds
  concurrent synthesis; the count is advertised on /info and /health so the
  fabric can shed/queue load rather than OOMing the node. Requests beyond the
  slot count queue (await the semaphore) instead of failing.
* **Receipts**: every synthesized item reports chars in, audio seconds out,
  cache-hit, and the model version so the fabric worker can emit per-item ACET
  receipts.
* **Formats**: opus (default; Ogg/Opus, speech-tuned bitrate) and mp3, both via
  ffmpeg over the raw 24 kHz PCM Kokoro produces.
* **CPU + GPU, one image**: torch runs CPU by default (the M1 node) and uses
  CUDA automatically when a GPU is visible (KOKORO_DEVICE=auto). Kokoro-82M is
  small enough to be roughly real-time on CPU.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Iterable

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kokoro-service")

# --- Configuration --------------------------------------------------------

MODEL_REPO = os.environ.get("KOKORO_REPO_ID", "hexgrad/Kokoro-82M")
DEFAULT_VOICE = os.environ.get("KOKORO_DEFAULT_VOICE", "am_michael")
DEFAULT_FORMAT = os.environ.get("KOKORO_DEFAULT_FORMAT", "opus")
DEVICE_PREF = os.environ.get("KOKORO_DEVICE", "auto")  # auto|cpu|cuda
TTS_SLOTS = max(1, int(os.environ.get("TTS_SLOTS", "2")))
CACHE_DIR = os.environ.get("KOKORO_CACHE_DIR", "/data/cache")
CACHE_MAX_GB = float(os.environ.get("KOKORO_CACHE_MAX_GB", "5"))
OPUS_BITRATE = os.environ.get("KOKORO_OPUS_BITRATE", "32k")
MP3_BITRATE = os.environ.get("KOKORO_MP3_BITRATE", "64k")
PORT = int(os.environ.get("PORT", "8080"))
SAMPLE_RATE = 24000  # Kokoro fixed output rate.

# Kokoro package version + repo id form the model-version component of the cache
# key and the receipt; a weights change (new repo id) or a package bump busts
# the cache automatically, which is what the issue's "bad-weights incident"
# story relies on. Resolved lazily so import cost stays at startup.
_MODEL_VERSION: str | None = None
_SERVICE_VERSION = "0.1.0"

FORMAT_MIME = {"opus": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav"}

# Full Kokoro English + Mandarin voice set. lang_code is inferred from the
# voice-name prefix (Kokoro convention): a=American EN, b=British EN, z=Mandarin.
VOICES: dict[str, str] = {}
for _v in (
    "af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova "
    "af_river af_sarah af_sky am_adam am_echo am_eric am_fenrir am_liam "
    "am_michael am_onyx am_puck am_santa"
).split():
    VOICES[_v] = "a"
for _v in "bf_alice bf_emma bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis".split():
    VOICES[_v] = "b"
for _v in "zf_xiaobei zf_xiaoni zf_xiaoxiao zf_xiaoyi zm_yunjian zm_yunxi zm_yunxia zm_yunyang".split():
    VOICES[_v] = "z"


def lang_for_voice(voice: str) -> str:
    lang = VOICES.get(voice)
    if lang is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown voice '{voice}'; see GET /info for the voice list",
        )
    return lang


# --- Model state (populated at startup) -----------------------------------

_model: Any = None
_device = "cpu"
_pipelines: dict[str, Any] = {}  # lang_code -> KPipeline (shares the KModel)
_pipeline_lock = threading.Lock()
_slots: asyncio.Semaphore | None = None


def _get_pipeline(lang_code: str):
    """Lazily build one KPipeline per language, all sharing the loaded KModel."""
    with _pipeline_lock:
        if lang_code not in _pipelines:
            from kokoro import KPipeline

            logger.info("Building Kokoro pipeline for lang '%s'", lang_code)
            _pipelines[lang_code] = KPipeline(
                lang_code=lang_code, model=_model, repo_id=MODEL_REPO
            )
        return _pipelines[lang_code]


# --- Audio encoding -------------------------------------------------------


def _pcm16(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _encode(samples: np.ndarray, fmt: str) -> bytes:
    """Encode float32 [-1,1] PCM to the requested container via ffmpeg."""
    if fmt == "wav":
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(_pcm16(samples))
        return buf.getvalue()

    if fmt == "opus":
        args = ["-c:a", "libopus", "-b:a", OPUS_BITRATE, "-application", "voip", "-f", "ogg"]
    elif fmt == "mp3":
        args = ["-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-f", "mp3"]
    else:
        raise HTTPException(status_code=400, detail=f"unsupported format '{fmt}'")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
        *args, "pipe:1",
    ]
    proc = subprocess.run(cmd, input=_pcm16(samples), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        logger.error("ffmpeg failed: %s", proc.stderr.decode("utf-8", "replace")[:500])
        raise HTTPException(status_code=500, detail="audio encoding failed")
    return proc.stdout


# --- Synthesis core -------------------------------------------------------


def _synthesize(text: str, voice: str, speed: float, lang: str) -> np.ndarray:
    pipeline = _get_pipeline(lang)
    chunks = [audio for _, _, audio in pipeline(text, voice=voice, speed=speed)]
    if not chunks:
        return np.zeros(0, dtype="float32")
    return np.concatenate([c.detach().cpu().numpy() for c in chunks]).astype("float32")


# --- Content-addressed cache ----------------------------------------------


def _format_bitrate(fmt: str) -> str:
    """The encoder bitrate that will be baked into the audio for this format.

    Part of the cache key so that changing a bitrate env var busts stale audio
    encoded at the old rate rather than serving it.
    """
    if fmt == "opus":
        return OPUS_BITRATE
    if fmt == "mp3":
        return MP3_BITRATE
    return ""


def cache_key(text: str, voice: str, fmt: str, speed: float, lang: str) -> str:
    """Stable key over (model+service version, voice, format, bitrate, speed, lang, text)."""
    h = hashlib.sha256()
    h.update(
        f"{model_version()}\0{_SERVICE_VERSION}\0{voice}\0{fmt}\0{_format_bitrate(fmt)}"
        f"\0{speed:.3f}\0{lang}\0".encode()
    )
    h.update(text.encode("utf-8"))
    return h.hexdigest()


class CacheStore:
    """Pluggable cache interface. `LocalLRUCache` is the node-local default; an
    org-global blob store can implement the same three methods later."""

    def get(self, key: str) -> bytes | None: ...
    def put(self, key: str, data: bytes, seconds: float) -> None: ...
    def duration(self, key: str) -> float | None: ...
    def bust(self, key: str | None) -> int: ...


class LocalLRUCache(CacheStore):
    """Content-addressed, byte-capped LRU on the service data volume.

    Files are named by key; an in-memory OrderedDict tracks size + recency and
    is rebuilt from disk on startup so the cap survives restarts.
    """

    def __init__(self, directory: str, max_bytes: int):
        self.dir = directory
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, int]" = OrderedDict()
        self._total = 0
        os.makedirs(self.dir, exist_ok=True)
        self._scan()

    def _path(self, key: str) -> str:
        return os.path.join(self.dir, key)

    def _scan(self) -> None:
        items = []
        for name in os.listdir(self.dir):
            if name.endswith(".dur"):
                continue  # sidecar duration files are not cache entries
            p = os.path.join(self.dir, name)
            if os.path.isfile(p):
                st = os.stat(p)
                items.append((st.st_mtime, name, st.st_size))
        for _, name, size in sorted(items):  # oldest first
            self._entries[name] = size
            self._total += size
        logger.info("Cache: %d entries, %.1f MB on disk", len(self._entries), self._total / 1e6)

    def get(self, key: str) -> bytes | None:
        with self._lock:
            if key not in self._entries:
                return None
            p = self._path(key)
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            os.utime(p, None)
            return data

    def put(self, key: str, data: bytes, seconds: float) -> None:
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return
            p = self._path(key)
            tmp = p + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            # Persist the exact audio duration alongside the blob so a cache hit
            # reports real seconds without re-probing (ffprobe-from-pipe is
            # unreliable for Ogg/Opus and would report 0.0).
            try:
                with open(p + ".dur", "w") as f:
                    f.write(f"{seconds:.3f}")
            except OSError:
                pass
            self._entries[key] = len(data)
            self._total += len(data)
            self._evict_locked()

    def duration(self, key: str) -> float | None:
        try:
            with open(self._path(key) + ".dur") as f:
                return float(f.read().strip())
        except (OSError, ValueError):
            return None

    def _evict_locked(self) -> None:
        while self._total > self.max_bytes and len(self._entries) > 1:
            old_key, size = self._entries.popitem(last=False)
            self._total -= size
            for suffix in ("", ".dur"):
                try:
                    os.remove(self._path(old_key) + suffix)
                except FileNotFoundError:
                    pass

    def bust(self, key: str | None) -> int:
        with self._lock:
            if key is None:
                n = len(self._entries)
                self._entries.clear()
                self._total = 0
                for name in os.listdir(self.dir):
                    fp = os.path.join(self.dir, name)
                    if os.path.isfile(fp):
                        try:
                            os.remove(fp)
                        except FileNotFoundError:
                            pass
                return n
            size = self._entries.pop(key, None)
            if size is None:
                return 0
            self._total -= size
            for suffix in ("", ".dur"):
                try:
                    os.remove(self._path(key) + suffix)
                except FileNotFoundError:
                    pass
            return 1

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._total,
                "max_bytes": self.max_bytes,
            }


_cache: LocalLRUCache | None = None


def model_version() -> str:
    global _MODEL_VERSION
    if _MODEL_VERSION is None:
        try:
            from importlib.metadata import version

            pkg = version("kokoro")
        except Exception:
            pkg = "unknown"
        _MODEL_VERSION = f"kokoro-{pkg}+{MODEL_REPO}"
    return _MODEL_VERSION


# --- Request / response models --------------------------------------------


class SpeechRequest(BaseModel):
    # OpenAI-compatible: `input`, `voice`, `response_format`, `speed`.
    input: str = Field(..., description="Text to synthesize")
    voice: str = DEFAULT_VOICE
    response_format: str = DEFAULT_FORMAT  # opus | mp3 | wav
    speed: float = Field(1.0, ge=0.5, le=2.0)
    model: str | None = None  # accepted + ignored for OpenAI-client compatibility

    model_config = {"protected_namespaces": ()}


class BatchItem(BaseModel):
    input: str
    id: str | None = None  # optional caller ref echoed back in the receipt


class BatchRequest(BaseModel):
    items: list[BatchItem]
    voice: str = DEFAULT_VOICE
    response_format: str = DEFAULT_FORMAT
    speed: float = Field(1.0, ge=0.5, le=2.0)
    model: str | None = None

    model_config = {"protected_namespaces": ()}


# --- Synthesis with cache + slots -----------------------------------------


async def synth_cached(text: str, voice: str, fmt: str, speed: float) -> tuple[bytes, dict]:
    """Return (audio_bytes, receipt). Serves from cache when possible."""
    assert _slots is not None and _cache is not None
    lang = lang_for_voice(voice)
    if fmt not in FORMAT_MIME:
        raise HTTPException(status_code=400, detail=f"unsupported format '{fmt}'")
    key = cache_key(text, voice, fmt, speed, lang)

    cached = _cache.get(key)
    if cached is not None:
        seconds = _cache.duration(key)
        if seconds is None:
            seconds = _duration_of(cached, fmt)
        return cached, _receipt(text, voice, fmt, seconds, True, key)

    async with _slots:
        # Re-check after acquiring: an identical concurrent request may have
        # populated the cache while we waited for a slot.
        cached = _cache.get(key)
        if cached is not None:
            seconds = _cache.duration(key)
            if seconds is None:
                seconds = _duration_of(cached, fmt)
            return cached, _receipt(text, voice, fmt, seconds, True, key)
        samples = await asyncio.to_thread(_synthesize, text, voice, speed, lang)
        data = await asyncio.to_thread(_encode, samples, fmt)

    seconds = round(len(samples) / SAMPLE_RATE, 3)
    _cache.put(key, data, seconds)
    return data, _receipt(text, voice, fmt, seconds, False, key)


# Rough per-format constant-bitrate estimate used only when we serve a cached
# blob and don't have the raw sample count; kept out of receipts' seconds when
# an exact figure is available.
def _duration_of(data: bytes, fmt: str) -> float:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", "pipe:0"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return round(float(proc.stdout.decode().strip()), 3)
    except Exception:
        return 0.0


def _receipt(text: str, voice: str, fmt: str, seconds: float, cache_hit: bool, key: str) -> dict:
    return {
        "chars": len(text),
        "seconds": seconds,
        "cache_hit": cache_hit,
        "voice": voice,
        "format": fmt,
        "model_version": model_version(),
        "cache_key": key,
    }


# --- App ------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model, _device, _slots, _cache
    import torch
    from kokoro import KModel

    if DEVICE_PREF == "cuda" or (DEVICE_PREF == "auto" and torch.cuda.is_available()):
        _device = "cuda"
    else:
        _device = "cpu"
    logger.info("Loading Kokoro model %s on %s", MODEL_REPO, _device)
    _model = KModel(repo_id=MODEL_REPO).to(_device).eval()
    _slots = asyncio.Semaphore(TTS_SLOTS)
    _cache = LocalLRUCache(CACHE_DIR, int(CACHE_MAX_GB * 1e9))
    logger.info("Kokoro ready: version=%s device=%s slots=%d", model_version(), _device, TTS_SLOTS)
    yield
    _model = None


app = FastAPI(title="Kokoro TTS Service", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "up" if _model is not None else "loading",
        "model_loaded": _model is not None,
        "model_version": model_version(),
        "device": _device,
        "slots": TTS_SLOTS,
    }


@app.get("/info")
async def info() -> dict:
    by_lang: dict[str, list[str]] = {"a": [], "b": [], "z": []}
    for v, lang in VOICES.items():
        by_lang[lang].append(v)
    return {
        "service": "kokoro",
        # Fabric serving-engine this service backs (aceteam ServingEngine.TTS /
        # the "tts-speech" provisioning template). The catalog service is named
        # for its implementation (kokoro), same as whisper-service backs the
        # "transcribe"/whisper engine; this field states the generic engine it
        # advertises so the fabric side lines up.
        "engine": "tts",
        "service_version": _SERVICE_VERSION,
        "model_version": model_version(),
        "device": _device,
        "capacity": {"slots": TTS_SLOTS},
        "default_voice": DEFAULT_VOICE,
        "default_format": DEFAULT_FORMAT,
        "formats": list(FORMAT_MIME.keys()),
        "voices": {
            "american_english": sorted(by_lang["a"]),
            "british_english": sorted(by_lang["b"]),
            "mandarin": sorted(by_lang["z"]),
        },
        "cache": _cache.stats() if _cache else None,
    }


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    data, receipt = await synth_cached(req.input, req.voice, req.response_format, req.speed)
    headers = {
        "X-TTS-Cache-Hit": "1" if receipt["cache_hit"] else "0",
        "X-TTS-Duration-Seconds": str(receipt["seconds"]),
        "X-TTS-Chars": str(receipt["chars"]),
        "X-TTS-Model-Version": receipt["model_version"],
        "X-TTS-Cache-Key": receipt["cache_key"],
    }
    return Response(content=data, media_type=FORMAT_MIME[req.response_format], headers=headers)


@app.post("/v1/audio/speech/batch")
async def speech_batch(req: BatchRequest):
    """Synthesize a list of text items. Streams one NDJSON receipt per item so a
    ~200-paragraph chapter doesn't time out; audio is stored in the cache and
    referenced by key (fetch via GET /v1/audio/cache/<key>)."""
    voice = req.voice
    fmt = req.response_format
    lang_for_voice(voice)  # validate up front
    if fmt not in FORMAT_MIME:
        raise HTTPException(status_code=400, detail=f"unsupported format '{fmt}'")

    async def gen():
        for idx, item in enumerate(req.items):
            # Fan-out partial-success: a per-item failure (over-cap input,
            # disk-full on cache write, a CUDA OOM/torch error, missing ffmpeg)
            # emits an error line and the batch continues. Any native exception
            # would otherwise abort the whole stream mid-flight -- after a 200 OK
            # -- silently dropping every remaining item.
            try:
                _, receipt = await synth_cached(item.input, voice, fmt, req.speed)
            except HTTPException as e:
                yield json.dumps({"index": idx, "id": item.id, "error": e.detail}) + "\n"
                continue
            except Exception as e:  # noqa: BLE001 -- one bad item must not kill the stream
                logger.exception("batch item %d (id=%s) failed", idx, item.id)
                yield json.dumps(
                    {"index": idx, "id": item.id, "error": f"{type(e).__name__}: {e}"}
                ) + "\n"
                continue
            receipt.update(
                {
                    "index": idx,
                    "id": item.id,
                    "audio_url": f"/v1/audio/cache/{receipt['cache_key']}",
                    "mime": FORMAT_MIME[fmt],
                }
            )
            yield json.dumps(receipt) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/v1/audio/cache/{key}")
async def cache_fetch(key: str):
    if _cache is None:
        raise HTTPException(status_code=503, detail="cache not ready")
    data = _cache.get(key)
    if data is None:
        raise HTTPException(status_code=404, detail="not in cache")
    # We don't persist the format alongside the blob; sniff the container.
    mime = "application/octet-stream"
    if data[:4] == b"OggS":
        mime = "audio/ogg"
    elif data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        mime = "audio/mpeg"  # ID3 tag or any MPEG-audio frame sync (0xFFEx/Fx)
    elif data[:4] == b"RIFF":
        mime = "audio/wav"
    return Response(content=data, media_type=mime)


@app.post("/v1/audio/cache/bust")
async def cache_bust(request: Request):
    if _cache is None:
        raise HTTPException(status_code=503, detail="cache not ready")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    key = body.get("key") if isinstance(body, dict) else None
    removed = _cache.bust(key)
    return JSONResponse({"busted": removed, "scope": "key" if key else "all"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
