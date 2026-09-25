"""Contract fixtures for Kokoro's opt-in English captioned response."""

import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

spec = importlib.util.spec_from_file_location("kokoro_server", Path(__file__).parents[1] / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class Audio:
    def __init__(self, samples):
        self.samples = samples

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.samples


def result(samples, *tokens):
    return SimpleNamespace(audio=Audio(np.asarray(samples, dtype="float32")), tokens=tokens)


def token(word, start=None, end=None):
    return SimpleNamespace(text=word, start_ts=start, end_ts=end)


@pytest.fixture
def service(monkeypatch, tmp_path):
    # The fixture exercises route/cache semantics with deterministic synthesis;
    # native thread scheduling is outside this contract.
    async def inline(fn, *args):
        return fn(*args)

    monkeypatch.setattr(server.asyncio, "to_thread", inline)
    monkeypatch.setattr(server, "_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(server, "_cache", server.LocalLRUCache(str(tmp_path), 1_000_000))
    monkeypatch.setattr(server, "_MODEL_VERSION", "kokoro-0.9.4+fixture")

    class Client:
        def post(self, path, json):
            async def send():
                transport = httpx.ASGITransport(app=server.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.post(path, json=json)

            return asyncio.run(send())

    return Client()


def test_words_are_seconds_in_chunk_order_and_audio_is_exact(service, monkeypatch):
    one = np.full(24000, 0.2, dtype="float32")
    two = np.full(12000, -0.2, dtype="float32")
    calls = []

    def pipeline(text, voice, speed):
        calls.append((text, voice, speed))
        yield result(one, token("Hello", 0.125, 0.5), token(","), token("world", 0.55, 0.9))
        yield result(two, token("!"), token("Again", 0.05, 0.4), token(""))

    monkeypatch.setattr(server, "_get_pipeline", lambda lang: pipeline)
    payload = {"input": "Hello, world! Again", "voice": "am_michael", "response_format": "wav", "speed": 1.5}
    response = service.post("/v1/audio/speech/captioned", json=payload)
    assert response.status_code == 200
    assert response.json()["words"] == [
        {"word": "Hello", "start": 0.125, "end": 0.5},
        {"word": "world", "start": 0.55, "end": 0.9},
        {"word": "Again", "start": 1.05, "end": 1.4},
    ]
    assert base64.b64decode(response.json()["audio_base64"]) == server._encode(
        np.concatenate((one, two)), "wav"
    )
    assert response.headers["x-tts-duration-seconds"] == "1.5"
    assert response.headers["x-tts-cache-hit"] == "0"
    assert response.headers["x-tts-chars"] == str(len(payload["input"]))
    assert response.headers["x-tts-model-version"] == "kokoro-0.9.4+fixture"
    assert len(response.headers["x-tts-cache-key"]) == 64
    assert calls == [(payload["input"], "am_michael", 1.5)]

    # A disk-backed hit reuses both the exact bytes and their timing sidecar.
    monkeypatch.setattr(server, "_cache", server.LocalLRUCache(server._cache.dir, 1_000_000))
    hit = service.post("/v1/audio/speech/captioned", json=payload)
    assert hit.json() == response.json()
    assert hit.headers["x-tts-cache-hit"] == "1"
    assert hit.headers["x-tts-cache-key"] == response.headers["x-tts-cache-key"]
    assert len(calls) == 1

    monkeypatch.setattr(server, "_synthesize", lambda *args: np.concatenate((one, two)))
    raw = service.post("/v1/audio/speech", json=payload)
    assert raw.content == base64.b64decode(response.json()["audio_base64"])
    assert raw.headers["content-type"] == "audio/wav"


def test_distinct_speeds_and_corrupt_pair_regenerate(service, monkeypatch):
    calls = []

    def pipeline(text, voice, speed):
        calls.append(speed)
        size = 12000 if speed == 1.0001 else 6000
        yield result(np.full(size, speed / 2, dtype="float32"), token("Go", 0.0, size / 24000))

    monkeypatch.setattr(server, "_get_pipeline", lambda lang: pipeline)
    body = {"input": "Go", "response_format": "wav", "speed": 1.0001}
    first = service.post("/v1/audio/speech/captioned", json=body)
    second = service.post("/v1/audio/speech/captioned", json={**body, "speed": 1.0002})
    assert first.status_code == second.status_code == 200
    assert first.headers["x-tts-cache-key"] != second.headers["x-tts-cache-key"]
    assert first.json()["words"][0]["end"] == 0.5
    assert second.json()["words"][0]["end"] == 0.25

    key = first.headers["x-tts-cache-key"]
    Path(server._cache.dir, key).write_bytes(b"wrong audio")
    repaired = service.post("/v1/audio/speech/captioned", json=body)
    assert repaired.status_code == 200
    assert repaired.headers["x-tts-cache-hit"] == "0"
    assert repaired.json() == first.json()
    sidecar = Path(server._cache.dir, key + ".words")
    metadata = json.loads(sidecar.read_text())
    metadata["words"][0]["end"] = 900.0
    sidecar.write_text(json.dumps(metadata))
    repaired_metadata = service.post("/v1/audio/speech/captioned", json=body)
    assert repaired_metadata.headers["x-tts-cache-hit"] == "0"
    assert repaired_metadata.json() == first.json()
    assert calls == [1.0001, 1.0002, 1.0001, 1.0001]


def test_speed_boundaries_use_model_times_and_emitted_samples(service, monkeypatch):
    def pipeline(text, voice, speed):
        samples = np.zeros(int(24000 / speed), dtype="float32")
        yield result(samples, token("Go", 0.0, len(samples) / 24000))

    monkeypatch.setattr(server, "_get_pipeline", lambda lang: pipeline)
    for speed, seconds in ((0.5, 2.0), (1.0, 1.0), (2.0, 0.5)):
        response = service.post("/v1/audio/speech/captioned", json={
            "input": "Go", "response_format": "wav", "speed": speed,
        })
        assert response.status_code == 200
        assert response.json()["words"] == [{"word": "Go", "start": 0.0, "end": seconds}]
        assert response.headers["x-tts-duration-seconds"] == str(seconds)


@pytest.mark.parametrize("tokens", [
    [token("Go", None, 0.2)],
    [token("Go", 0.1, 0.7)],
    [token("Go", 0.3, 0.2)],
    [token("first", 0.1, 0.3), token("second", 0.2, 0.4)],
])
def test_invalid_alignment_fails_closed(service, monkeypatch, tokens):
    monkeypatch.setattr(server, "_get_pipeline", lambda lang: lambda *args, **kwargs: iter([
        result(np.zeros(12000, dtype="float32"), *tokens)
    ]))
    response = service.post("/v1/audio/speech/captioned", json={"input": "Go", "response_format": "wav"})
    assert response.status_code == 503


def test_empty_and_punctuation_return_explicit_no_words_error(service, monkeypatch):
    calls = []

    def pipeline(text, voice, speed):
        calls.append(text)
        if text:
            yield result(np.zeros(2400, dtype="float32"), token("!"), token(" "))

    monkeypatch.setattr(server, "_get_pipeline", lambda lang: pipeline)
    for text in ("", "!"):
        response = service.post("/v1/audio/speech/captioned", json={"input": text, "response_format": "wav"})
        assert response.status_code == 422
        assert response.json() == {"detail": "input has no alignable spoken words"}
    assert calls == ["", "!"]
    assert server._cache.stats()["entries"] == 0


def test_legacy_empty_word_cache_cannot_turn_422_into_success(service, monkeypatch):
    key = server.captioned_cache_key("!", "am_michael", "wav", 1.0, "a")
    server._cache.put_captioned(key, b"old audio", 0.1, [])
    calls = []

    def pipeline(text, voice, speed):
        calls.append(text)
        yield result(np.zeros(2400, dtype="float32"), token("!"))

    monkeypatch.setattr(server, "_get_pipeline", lambda lang: pipeline)
    response = service.post("/v1/audio/speech/captioned", json={"input": "!", "response_format": "wav"})
    assert response.status_code == 422
    assert response.json() == {"detail": "input has no alignable spoken words"}
    assert calls == ["!"]


def test_mandarin_is_explicitly_unsupported_and_raw_route_is_audio(service, monkeypatch):
    response = service.post("/v1/audio/speech/captioned", json={"input": "你好", "voice": "zf_xiaoxiao"})
    assert response.status_code == 422
    assert "English" in response.json()["detail"]
    monkeypatch.setattr(server, "_synthesize", lambda *args: np.zeros(2400, dtype="float32"))
    raw = service.post("/v1/audio/speech", json={"input": "Go", "response_format": "wav"})
    assert raw.status_code == 200
    assert raw.content.startswith(b"RIFF")
    assert raw.headers["content-type"] == "audio/wav"
    assert raw.headers["x-tts-cache-hit"] == "0"
