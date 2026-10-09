"""API contract tests for the voice-clone service. No GPU, no torch.

The model engines, transcriber and aligner are replaced at their boundary with
deterministic fakes that return real PCM, so the HTTP layer, the voice store,
the consent and license gates, and the real ffmpeg encode/tempo paths are all
exercised for real.
"""

import asyncio
import base64
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest

BUILD = Path(__file__).parents[1]
sys.path.insert(0, str(BUILD))
spec = importlib.util.spec_from_file_location("voice_clone_server", BUILD / "server.py")
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server  # pydantic resolves postponed annotations via sys.modules
spec.loader.exec_module(server)
import engines  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")

SR = 24000
CONSENT = {
    "speaker_name": "Test Narrator",
    "attested_by": "node-owner",
    "statement": "I consent to my voice being cloned for narration on this node.",
    "attested_at": "2026-10-08T12:00:00Z",
}


def tone(seconds: float, sr: int = SR, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype("float32")


def wav_bytes(seconds: float, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((tone(seconds, sr) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def probe_seconds(data: bytes, tmp_path: Path) -> float:
    path = tmp_path / "probe.bin"
    path.write_bytes(data)
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE,
        check=True,
    )
    return float(proc.stdout.decode().strip())


class FakeEngine(engines.Engine):
    """Boundary fake: records calls, writes a real prompt file, returns a tone."""

    def __init__(self, name, *, commercial, weights_license, seconds_per_char=0.05, **flags):
        super().__init__()
        self.name = name
        self.model_id = f"fake/{name}"
        self.weights_license = weights_license
        self.commercial_use = commercial
        self.prompt_filename = f"{name}.pt"
        self.seconds_per_char = seconds_per_char
        for k, v in flags.items():
            setattr(self, k, v)
        self.prompt_calls = []
        self.synth_calls = []

    def model_version(self):
        return f"{self.name}-test"

    def needs_transcript(self):
        return self.name == "omnivoice"

    def _load(self):
        pass

    def _build_prompt(self, ref_wav, transcript, out_path):
        with wave.open(ref_wav) as w:
            assert w.getframerate() == SR and w.getnchannels() == 1
        self.prompt_calls.append((ref_wav, transcript))
        Path(out_path).write_bytes(b"prompt:" + self.name.encode())

    def _synthesize(self, text, prompt_path, instruct, speed, language):
        if prompt_path:
            assert Path(prompt_path).read_bytes() == b"prompt:" + self.name.encode()
        self.synth_calls.append(
            {"text": text, "prompt": prompt_path, "instruct": instruct, "speed": speed}
        )
        return tone(len(text) * self.seconds_per_char / speed), SR


class FakeTranscriber:
    def __init__(self):
        self.calls = 0

    def transcribe(self, path):
        self.calls += 1
        return "auto transcript"


class FakeAligner:
    model_id = "fake-aligner"

    def align(self, samples, sr, text):
        pairs = engines.alignment_words(text)
        step = samples.size / sr / max(1, len(pairs))
        words = [
            {"word": raw, "start": round(i * step, 3), "end": round((i + 1) * step, 3)}
            for i, (raw, tok) in enumerate(pairs)
            if tok
        ]
        return words, [raw for raw, tok in pairs if not tok]


@pytest.fixture
def svc(monkeypatch, tmp_path):
    chatter = FakeEngine("chatterbox", commercial=True, weights_license="MIT", watermark="perth-implicit")
    omni = FakeEngine(
        "omnivoice",
        commercial=False,
        weights_license="CC-BY-NC",
        supports_instruct=True,
        native_speed=True,
        native_long_form=True,
    )
    transcriber = FakeTranscriber()
    monkeypatch.setattr(server, "ENGINES", {"chatterbox": chatter, "omnivoice": omni})
    monkeypatch.setattr(server, "TRANSCRIBER", transcriber)
    monkeypatch.setattr(server, "ALIGNER", FakeAligner())
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "OMNIVOICE_LICENSE_ACCEPTED", False)
    monkeypatch.setattr(server, "_slots", asyncio.Semaphore(1))
    (tmp_path / "voices").mkdir()

    def call(method, path, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                return await c.request(method, path, **kwargs)

        return asyncio.run(send())

    class S:
        pass

    s = S()
    s.call, s.chatter, s.omni, s.transcriber, s.dir = call, chatter, omni, transcriber, tmp_path
    return s


def enroll(svc, seconds=5.0, consent=CONSENT, **extra):
    body = {"audio_base64": base64.b64encode(wav_bytes(seconds)).decode(), **extra}
    if consent is not None:
        body["consent"] = consent
    return svc.call("POST", "/v1/voices", json=body)


# --- consent --------------------------------------------------------------


def test_enroll_without_consent_is_rejected_and_nothing_is_stored(svc):
    r = enroll(svc, consent=None)
    assert r.status_code == 422
    assert "consent is required" in r.json()["detail"]
    assert list((svc.dir / "voices").iterdir()) == []
    assert svc.chatter.prompt_calls == []


@pytest.mark.parametrize(
    "patch",
    [
        {"statement": "   "},
        {"attested_by": ""},
        {"attested_at": "last tuesday"},
        {"speaker_name": None},
    ],
)
def test_enroll_with_incomplete_consent_is_rejected(svc, patch):
    consent = {**CONSENT, **patch}
    consent = {k: v for k, v in consent.items() if v is not None}
    r = enroll(svc, consent=consent)
    assert r.status_code == 422, r.text
    assert list((svc.dir / "voices").iterdir()) == []


def test_multipart_enrollment_requires_consent_field(svc):
    files = {"audio": ("ref.wav", wav_bytes(5.0), "audio/wav")}
    r = svc.call("POST", "/v1/voices", files=files, data={"name": "x"})
    assert r.status_code == 422
    assert "consent is required" in r.json()["detail"]

    r = svc.call(
        "POST", "/v1/voices", files=files, data={"consent": json.dumps(CONSENT), "transcript": "hi"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["transcript"] == "hi"


# --- reference clip -------------------------------------------------------


@pytest.mark.parametrize("seconds", [2.0, 12.0])
def test_reference_outside_3_to_10_seconds_is_rejected(svc, seconds):
    r = enroll(svc, seconds=seconds)
    assert r.status_code == 422
    assert "between 3 and 10 seconds" in r.json()["detail"]
    assert list((svc.dir / "voices").iterdir()) == []


def test_undecodable_reference_is_rejected(svc):
    body = {"audio_base64": base64.b64encode(b"not audio at all").decode(), "consent": CONSENT}
    r = svc.call("POST", "/v1/voices", json=body)
    assert r.status_code == 422


# --- voice lifecycle ------------------------------------------------------


def test_enroll_list_synthesize_delete_round_trip(svc):
    r = enroll(svc, name="Narrator A")
    assert r.status_code == 201, r.text
    voice = r.json()
    vid = voice["voice_id"]
    assert voice["engines"] == ["chatterbox"]  # OmniVoice gated off: no prompt built
    assert voice["consent"]["speaker_name"] == "Test Narrator"
    vdir = svc.dir / "voices" / vid
    with wave.open(str(vdir / "ref.wav")) as w:  # resampled 16k -> 24k mono
        assert w.getframerate() == SR
        assert abs(w.getnframes() / SR - 5.0) < 0.01
    assert (vdir / "chatterbox.pt").is_file()
    assert svc.omni.prompt_calls == []

    listed = svc.call("GET", "/v1/voices").json()["voices"]
    assert [v["voice_id"] for v in listed] == [vid]

    r = svc.call("POST", "/v1/audio/speech", json={"input": "Hello there.", "voice": vid})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["X-TTS-Engine"] == "chatterbox"
    assert r.headers["X-TTS-Model-License"] == "MIT"
    assert r.headers["X-TTS-Voice-Id"] == vid
    receipt = json.loads(base64.urlsafe_b64decode(r.headers["X-TTS-Receipt"]))
    assert receipt["consent"] == voice["consent"]
    assert receipt["watermark"] == "perth-implicit"
    assert svc.chatter.synth_calls[-1]["prompt"].endswith(f"{vid}/chatterbox.pt")

    assert svc.call("DELETE", f"/v1/voices/{vid}").status_code == 204
    assert not vdir.exists()
    assert svc.call("DELETE", f"/v1/voices/{vid}").status_code == 404
    r = svc.call("POST", "/v1/audio/speech", json={"input": "Hello.", "voice": vid})
    assert r.status_code == 404


@pytest.mark.parametrize("bad", ["../../etc", "voice_xyz", "voice_" + "0" * 23])
def test_malformed_voice_ids_are_not_found(svc, bad):
    r = svc.call("POST", "/v1/audio/speech", json={"input": "Hi.", "voice": bad})
    assert r.status_code == 404


# --- license gate ---------------------------------------------------------


def test_omnivoice_refused_without_license_flag(svc):
    r = svc.call("POST", "/v1/audio/speech", json={"input": "Hello.", "engine": "omnivoice"})
    assert r.status_code == 403
    assert "CC-BY-NC" in r.json()["detail"]
    assert "OMNIVOICE_ACCEPT_NONCOMMERCIAL_LICENSE" in r.json()["detail"]
    assert svc.omni.synth_calls == [] and not svc.omni.is_loaded()

    r = enroll(svc, engines=["omnivoice"])
    assert r.status_code == 403
    assert svc.omni.prompt_calls == []

    info = svc.call("GET", "/info").json()
    omni = next(e for e in info["engines"] if e["name"] == "omnivoice")
    assert omni["enabled"] is False and omni["weights_license"] == "CC-BY-NC"
    chatter = next(e for e in info["engines"] if e["name"] == "chatterbox")
    assert chatter["enabled"] is True and chatter["weights_license"] == "MIT"


def test_omnivoice_with_license_flag_clones_designs_and_labels_license(svc, monkeypatch):
    vid = enroll(svc).json()["voice_id"]  # enrolled while OmniVoice was off
    monkeypatch.setattr(server, "OMNIVOICE_LICENSE_ACCEPTED", True)

    r = svc.call(
        "POST",
        "/v1/audio/speech",
        json={"input": "Cloned line.", "engine": "omnivoice", "voice": vid, "response_format": "wav"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["X-TTS-Model-License"] == "CC-BY-NC"
    receipt = json.loads(base64.urlsafe_b64decode(r.headers["X-TTS-Receipt"]))
    assert receipt["model_license"] == "CC-BY-NC" and receipt["commercial_use"] is False
    # The prompt was built lazily from the stored ref.wav, with an ASR transcript.
    assert svc.transcriber.calls == 1
    assert svc.omni.prompt_calls[0][1] == "auto transcript"
    meta = json.loads((svc.dir / "voices" / vid / "meta.json").read_text())
    assert meta["prompts"]["omnivoice"]["model_license"] == "CC-BY-NC"
    assert meta["transcript_source"] == "asr"

    r = svc.call(
        "POST",
        "/v1/audio/speech",
        json={
            "input": "Designed voice.",
            "engine": "omnivoice",
            "instruct": "female, low pitch, british accent",
            "word_timestamps": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["receipt"]["model_license"] == "CC-BY-NC"
    assert body["receipt"]["instruct"] == "female, low pitch, british accent"
    assert svc.omni.synth_calls[-1]["instruct"] == "female, low pitch, british accent"


# --- request validation ---------------------------------------------------


def test_instruct_rejected_on_chatterbox_and_with_a_cloned_voice(svc, monkeypatch):
    r = svc.call("POST", "/v1/audio/speech", json={"input": "Hi.", "instruct": "male, deep"})
    assert r.status_code == 400
    monkeypatch.setattr(server, "OMNIVOICE_LICENSE_ACCEPTED", True)
    vid = enroll(svc, transcript="hello").json()["voice_id"]
    r = svc.call(
        "POST",
        "/v1/audio/speech",
        json={"input": "Hi.", "engine": "omnivoice", "voice": vid, "instruct": "male"},
    )
    assert r.status_code == 400
    assert svc.omni.synth_calls == []


def test_unknown_engine_format_and_oversize_input(svc, monkeypatch):
    assert svc.call("POST", "/v1/audio/speech", json={"input": "x", "engine": "xtts"}).status_code == 400
    assert (
        svc.call("POST", "/v1/audio/speech", json={"input": "x", "response_format": "flac"}).status_code
        == 400
    )
    monkeypatch.setattr(server, "MAX_INPUT_CHARS", 10)
    assert svc.call("POST", "/v1/audio/speech", json={"input": "x" * 11}).status_code == 413
    assert svc.chatter.synth_calls == []


# --- formats, speed, chunking --------------------------------------------


@pytest.mark.parametrize(
    "fmt,mime,magic",
    [
        ("wav", "audio/wav", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WAVE"),
        ("mp3", "audio/mpeg", lambda b: b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)),
        ("opus", "audio/ogg", lambda b: b[:4] == b"OggS" and b"OpusHead" in b[:64]),
    ],
)
def test_format_matrix(svc, fmt, mime, magic, tmp_path):
    text = "Twenty characters!!!"  # 20 chars -> 1.0 s from the fake engine
    r = svc.call("POST", "/v1/audio/speech", json={"input": text, "response_format": fmt})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == mime
    assert magic(r.content)
    assert float(r.headers["X-TTS-Duration-Seconds"]) == pytest.approx(1.0, abs=0.01)
    assert probe_seconds(r.content, tmp_path) == pytest.approx(1.0, abs=0.08)


def test_word_timestamps_json_shape(svc):
    r = svc.call(
        "POST",
        "/v1/audio/speech",
        json={"input": "Read 42 pages, slowly.", "word_timestamps": True, "response_format": "opus"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert base64.b64decode(body["audio_base64"])[:4] == b"OggS"
    assert [w["word"] for w in body["words"]] == ["Read", "pages,", "slowly."]
    assert body["receipt"]["unaligned_words"] == ["42"]
    starts = [w["start"] for w in body["words"]]
    assert starts == sorted(starts)


def test_chatterbox_speed_is_applied_by_tempo(svc):
    text = "x" * 40  # 2.0 s from the fake engine
    r = svc.call("POST", "/v1/audio/speech", json={"input": text, "speed": 2.0, "response_format": "wav"})
    assert r.status_code == 200
    assert svc.chatter.synth_calls[-1]["speed"] == 1.0  # engine has no native speed
    assert float(r.headers["X-TTS-Duration-Seconds"]) == pytest.approx(1.0, abs=0.05)


def test_long_input_is_chunked_for_chatterbox(svc, monkeypatch):
    monkeypatch.setattr(server, "CHUNK_CHARS", 30)
    text = "First sentence here. Second sentence follows. Third one ends it."
    r = svc.call("POST", "/v1/audio/speech", json={"input": text, "response_format": "wav"})
    assert r.status_code == 200
    sent = [c["text"] for c in svc.chatter.synth_calls]
    assert len(sent) == 3 and " ".join(sent) == text


def test_split_for_engine_keeps_every_word_within_the_cap():
    text = "Short one. " + " ".join(["word"] * 40) + ". Tail!"
    chunks = server.split_for_engine(text, 50)
    assert all(len(c) <= 50 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_alignment_words_normalization():
    pairs = engines.alignment_words("Don’t stop, 2026 café 'quoted'")
    assert pairs == [
        ("Don’t", "don't"),
        ("stop,", "stop"),
        ("2026", ""),
        ("café", "cafe"),
        ("'quoted'", "quoted"),
    ]
