"""Model adapters for the voice-clone service.

Everything that touches torch or a model package lives here, behind three small
boundaries the HTTP layer (server.py) depends on:

* ``Engine``: load, build an engine-specific voice prompt from a consented
  reference clip, and synthesize float32 PCM.
* ``Transcriber``: auto-transcribe a reference clip (used only when an
  OmniVoice prompt is built for a voice enrolled without a transcript).
* ``Aligner``: forced alignment of synthesized audio against the input text,
  for ``word_timestamps``.

Heavy imports happen inside methods, never at module import, so the API layer
and its tests run on a machine with no torch and no GPU.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
from typing import Any

import numpy as np

logger = logging.getLogger("voice-clone.engines")

DEVICE = os.environ.get("VOICE_CLONE_DEVICE", "cuda")
OMNIVOICE_MODEL = os.environ.get("OMNIVOICE_MODEL", "k2-fsa/OmniVoice")
OMNIVOICE_DTYPE = os.environ.get("OMNIVOICE_DTYPE", "float16")
OMNIVOICE_NUM_STEP = int(os.environ.get("OMNIVOICE_NUM_STEP", "32"))
ASR_MODEL = os.environ.get("VOICE_CLONE_ASR_MODEL", "openai/whisper-large-v3-turbo")
CHATTERBOX_EXAGGERATION = float(os.environ.get("CHATTERBOX_EXAGGERATION", "0.5"))
CHATTERBOX_CFG_WEIGHT = float(os.environ.get("CHATTERBOX_CFG_WEIGHT", "0.5"))


def _pkg_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001 (metadata is informational only)
        return "unknown"


def _require_device(torch: Any) -> str:
    """Resolve the configured device, failing loudly instead of silently on CPU."""
    if DEVICE.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(
            f"VOICE_CLONE_DEVICE={DEVICE} but no GPU is visible to the container "
            f"(torch.version.cuda={torch.version.cuda}). Run with compose.gpu.yml "
            "layered on compose.yml, or set VOICE_CLONE_DEVICE=cpu for a slow test run."
        )
    return DEVICE


class Engine:
    """Base adapter. Subclasses set the metadata and implement the three hooks.

    ``lock`` serializes model use per engine: Chatterbox keeps the active voice
    conditioning on the model instance, so two concurrent requests on the same
    engine would race. TTS_SLOTS in server.py bounds concurrency across engines.
    """

    name = ""
    model_id = ""
    package = ""
    weights_license = ""
    code_license = ""
    commercial_use = False
    watermark: str | None = None
    supports_instruct = False
    native_speed = False
    native_long_form = False
    prompt_filename = ""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded

    def ensure_loaded(self) -> None:
        with self.lock:
            if not self._loaded:
                logger.info("loading engine %s (%s)", self.name, self.model_id)
                self._load()
                self._loaded = True

    def model_version(self) -> str:
        return f"{self.name}-{_pkg_version(self.package)}+{self.model_id}"

    def build_prompt(self, ref_wav: str, transcript: str | None, out_path: str) -> None:
        with self.lock:
            self.ensure_loaded()
            self._build_prompt(ref_wav, transcript, out_path)

    def synthesize(
        self,
        text: str,
        prompt_path: str | None,
        instruct: str | None,
        speed: float,
        language: str | None,
    ) -> tuple[np.ndarray, int]:
        with self.lock:
            self.ensure_loaded()
            return self._synthesize(text, prompt_path, instruct, speed, language)

    def needs_transcript(self) -> bool:
        return False

    def _load(self) -> None:
        raise NotImplementedError

    def _build_prompt(self, ref_wav: str, transcript: str | None, out_path: str) -> None:
        raise NotImplementedError

    def _synthesize(self, text, prompt_path, instruct, speed, language) -> tuple[np.ndarray, int]:
        raise NotImplementedError


class ChatterboxEngine(Engine):
    """Resemble AI Chatterbox (MIT code and weights). The default engine.

    Every output passes through Chatterbox's built-in Perth implicit
    watermarker inside ``generate()``. The adapter refuses to load when the
    watermarker is unavailable (resemble-perth silently degrades to ``None``
    when ``pkg_resources`` is missing), so unwatermarked clones are never served.
    """

    name = "chatterbox"
    model_id = "ResembleAI/chatterbox"
    package = "chatterbox-tts"
    weights_license = "MIT"
    code_license = "MIT"
    commercial_use = True
    watermark = "perth-implicit"
    prompt_filename = "chatterbox.pt"

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._default_conds: Any = None
        self._device = "cpu"

    def _load(self) -> None:
        import perth
        import torch
        from chatterbox.tts import ChatterboxTTS

        if getattr(perth, "PerthImplicitWatermarker", None) is None:
            raise RuntimeError(
                "Chatterbox watermarker (resemble-perth PerthImplicitWatermarker) failed to "
                "import; refusing to serve unwatermarked voice clones"
            )
        self._device = _require_device(torch)
        self._model = ChatterboxTTS.from_pretrained(device=self._device)
        self._default_conds = self._model.conds

    def _build_prompt(self, ref_wav: str, transcript: str | None, out_path: str) -> None:
        try:
            self._model.prepare_conditionals(ref_wav, exaggeration=CHATTERBOX_EXAGGERATION)
            self._model.conds.save(out_path)
        finally:
            self._model.conds = self._default_conds

    def _synthesize(self, text, prompt_path, instruct, speed, language) -> tuple[np.ndarray, int]:
        from chatterbox.tts import Conditionals

        conds = self._default_conds
        if prompt_path:
            conds = Conditionals.load(prompt_path, map_location=self._device).to(self._device)
        self._model.conds = conds
        try:
            wav = self._model.generate(
                text, exaggeration=CHATTERBOX_EXAGGERATION, cfg_weight=CHATTERBOX_CFG_WEIGHT
            )
        finally:
            self._model.conds = self._default_conds
        samples = wav.squeeze(0).detach().cpu().numpy().astype("float32")
        return samples, int(self._model.sr)


class OmniVoiceEngine(Engine):
    """k2-fsa OmniVoice. Code Apache-2.0, pretrained weights CC-BY-NC.

    Opt-in only: server.py refuses every OmniVoice request (and never loads or
    downloads the weights) unless OMNIVOICE_ACCEPT_NONCOMMERCIAL_LICENSE=true.
    """

    name = "omnivoice"
    model_id = OMNIVOICE_MODEL
    package = "omnivoice"
    weights_license = "CC-BY-NC"
    code_license = "Apache-2.0"
    commercial_use = False
    watermark = None
    supports_instruct = True
    native_speed = True
    native_long_form = True
    prompt_filename = "omnivoice.pt"

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None

    def needs_transcript(self) -> bool:
        return True

    def _load(self) -> None:
        import torch
        from omnivoice import OmniVoice

        device = _require_device(torch)
        dtype = getattr(torch, OMNIVOICE_DTYPE) if device != "cpu" else torch.float32
        self._model = OmniVoice.from_pretrained(
            OMNIVOICE_MODEL, device_map=device, dtype=dtype, attn_implementation="sdpa"
        )

    def _build_prompt(self, ref_wav: str, transcript: str | None, out_path: str) -> None:
        if not transcript:
            raise ValueError("OmniVoice prompts need a reference transcript")
        prompt = self._model.create_voice_clone_prompt(ref_wav, ref_text=transcript)
        prompt.save(out_path)

    def _synthesize(self, text, prompt_path, instruct, speed, language) -> tuple[np.ndarray, int]:
        from omnivoice import VoiceClonePrompt

        kwargs: dict[str, Any] = {"num_step": OMNIVOICE_NUM_STEP}
        if prompt_path:
            kwargs["voice_clone_prompt"] = VoiceClonePrompt.load(prompt_path)
        if instruct:
            kwargs["instruct"] = instruct
        if language:
            kwargs["language"] = language
        if speed != 1.0:
            kwargs["speed"] = speed
        audios = self._model.generate(text=text, **kwargs)
        samples = np.asarray(audios[0], dtype="float32").reshape(-1)
        return samples, int(self._model.sampling_rate)


class Transcriber:
    """Whisper ASR (transformers pipeline), loaded on first use only."""

    model_id = ASR_MODEL

    def __init__(self) -> None:
        self._pipe: Any = None
        self._lock = threading.Lock()

    def transcribe(self, wav_path: str) -> str:
        with self._lock:
            if self._pipe is None:
                import torch
                from transformers import pipeline

                device = _require_device(torch)
                self._pipe = pipeline(
                    "automatic-speech-recognition",
                    model=ASR_MODEL,
                    dtype=torch.float16 if device != "cpu" else torch.float32,
                    device=device,
                )
            return str(self._pipe(wav_path)["text"]).strip()


_ALIGN_KEEP = re.compile(r"[^a-z']")


def alignment_words(text: str) -> list[tuple[str, str]]:
    """Split text into (display word, aligner token) pairs.

    The MMS forced-alignment vocabulary is lowercase a-z plus apostrophe, so a
    word is normalized to that alphabet (curly apostrophes and accents folded first). Words
    with nothing left (numbers, symbols, non-Latin scripts) are dropped from the
    alignment and reported back as unaligned rather than given invented times.
    """
    pairs: list[tuple[str, str]] = []
    for raw in text.split():
        folded = unicodedata.normalize("NFKD", raw.lower().replace("\u2019", "'"))
        token = _ALIGN_KEEP.sub("", folded).strip("'")
        pairs.append((raw, token))
    return pairs


class Aligner:
    """torchaudio MMS_FA forced aligner (CTC, 16 kHz). English / Latin script."""

    model_id = "torchaudio.pipelines.MMS_FA"

    def __init__(self) -> None:
        self._bundle: Any = None
        self._model: Any = None
        self._device = "cpu"
        self._lock = threading.Lock()

    def align(self, samples: np.ndarray, sample_rate: int, text: str) -> tuple[list[dict], list[str]]:
        import torch
        import torchaudio

        with self._lock:
            if self._model is None:
                self._device = _require_device(torch)
                self._bundle = torchaudio.pipelines.MMS_FA
                self._model = self._bundle.get_model(with_star=False).to(self._device).eval()
            bundle = self._bundle
            pairs = alignment_words(text)
            tokens = [tok for _, tok in pairs if tok]
            unaligned = [raw for raw, tok in pairs if not tok]
            if not tokens:
                return [], unaligned
            wav = torch.from_numpy(np.ascontiguousarray(samples, dtype="float32")).unsqueeze(0)
            if sample_rate != bundle.sample_rate:
                wav = torchaudio.functional.resample(wav, sample_rate, bundle.sample_rate)
            with torch.inference_mode():
                emission, _ = self._model(wav.to(self._device))
            spans = bundle.get_aligner()(emission[0], bundle.get_tokenizer()(tokens))
            seconds_per_frame = wav.size(1) / emission.size(1) / bundle.sample_rate
            words: list[dict] = []
            aligned = iter(spans)
            for raw, tok in pairs:
                if not tok:
                    continue
                word_spans = next(aligned)
                words.append(
                    {
                        "word": raw,
                        "start": round(word_spans[0].start * seconds_per_frame, 3),
                        "end": round(word_spans[-1].end * seconds_per_frame, 3),
                    }
                )
            return words, unaligned


def cuda_memory() -> dict | None:
    """Peak/current CUDA allocation, if torch is loaded and a GPU is in use."""
    import sys

    torch = sys.modules.get("torch")
    if torch is None or not torch.cuda.is_available():
        return None
    return {
        "allocated_bytes": int(torch.cuda.memory_allocated()),
        "peak_allocated_bytes": int(torch.cuda.max_memory_allocated()),
        "reserved_bytes": int(torch.cuda.memory_reserved()),
    }
