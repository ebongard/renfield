"""``embed=false`` on /api/voice/stt: no voiceprint is COMPUTED (renfield #1669).

A phone caller's voice must not become a voiceprint. Leaving the embedding out of
the reply is not enough -- the speaker model must never run. ``embedded`` tells
the client what happened, so an older server that ignores the field is caught.
"""
from __future__ import annotations

import io
import shutil
import struct

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voice_server.api import rest_voice


class _Seg:
    def __init__(self, text):
        self.text = text


class FakeSTT:
    last_language = "de"

    async def transcribe_stream(self, pcm, language=None):
        yield _Seg("Hallo, bitte rufen Sie zurück.")


class FakeSpeaker:
    def __init__(self):
        self.calls = 0

    async def embed(self, pcm):
        self.calls += 1
        return np.zeros(192, dtype=np.float32)


def _wav(seconds: float = 0.5) -> bytes:
    n = int(16000 * seconds)
    data = (np.sin(np.arange(n) / 8.0) * 3000).astype("<i2").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16
    ) + b"data" + struct.pack("<I", len(data))
    return header + data


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(rest_voice.router)
    app.dependency_overrides[rest_voice._require_token] = lambda: {}
    app.state.stt = FakeSTT()
    app.state.speaker = FakeSpeaker()
    return TestClient(app), app.state.speaker


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@needs_ffmpeg
def test_embed_false_never_runs_the_speaker_model(client):
    http, speaker = client
    r = http.post(
        "/api/voice/stt",
        files={"audio": ("m.wav", io.BytesIO(_wav()), "audio/wav")},
        data={"language": "de", "embed": "false"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "Hallo, bitte rufen Sie zurück."
    assert body["speaker_embedding"] is None
    assert body["embedded"] is False
    assert speaker.calls == 0


@pytest.fixture
def no_decode(monkeypatch):
    """Skip the media decode so the privacy tests run everywhere (no ffmpeg/libopus)."""
    pcm = np.zeros(16000, dtype=np.float32)

    async def fake_upload(audio):
        return pcm

    monkeypatch.setattr(rest_voice, "decode_upload_to_pcm", fake_upload)
    monkeypatch.setattr(rest_voice, "decode_opus_packets_to_pcm", lambda blob: pcm)


def _post(http, path, data=None):
    return http.post(path, files={"audio": ("a.bin", io.BytesIO(b"x" * 64), "application/octet-stream")}, data=data)


@pytest.mark.parametrize("path", ["/api/voice/stt", "/api/voice/stt-opus"])
@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", " false "])
def test_every_false_spelling_skips_the_model(client, no_decode, path, value):
    http, speaker = client
    r = _post(http, path, {"embed": value})
    assert r.status_code == 200, r.text
    assert (r.json()["embedded"], r.json()["speaker_embedding"], speaker.calls) == (False, None, 0)


@pytest.mark.parametrize("path", ["/api/voice/stt", "/api/voice/stt-opus"])
@pytest.mark.parametrize("value", ["", "maybe", "nein", "2"])
def test_an_unclear_value_is_refused_not_defaulted(client, no_decode, path, value):
    # FastAPI would read an empty bool Form field as "missing" -> default True:
    # a voiceprint the caller explicitly did not want. Refuse instead.
    http, speaker = client
    r = _post(http, path, {"embed": value})
    assert r.status_code == 422, r.text
    assert speaker.calls == 0


@pytest.mark.parametrize("path", ["/api/voice/stt", "/api/voice/stt-opus"])
def test_absent_field_and_true_still_embed(client, no_decode, path):
    http, speaker = client
    assert _post(http, path).json()["embedded"] is True
    assert _post(http, path, {"embed": "true"}).json()["embedded"] is True
    assert speaker.calls == 2


@needs_ffmpeg
def test_default_still_embeds(client):
    http, speaker = client
    r = http.post("/api/voice/stt", files={"audio": ("m.wav", io.BytesIO(_wav()), "audio/wav")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["embedded"] is True and len(body["speaker_embedding"]) == 192
    assert speaker.calls == 1


async def test_tail_skips_the_model_and_reports_it():
    class Req:
        class app:
            class state:
                stt = FakeSTT()
                speaker = FakeSpeaker()

    pcm = np.zeros(16000, dtype=np.float32)
    out = await rest_voice._transcribe_and_embed(Req, pcm, "de", embed=False)
    assert (out.embedded, out.speaker_embedding, Req.app.state.speaker.calls) == (False, None, 0)


async def test_a_failed_embed_reports_not_embedded():
    class Boom(FakeSpeaker):
        async def embed(self, pcm):
            raise RuntimeError("model down")

    class Req:
        class app:
            class state:
                stt = FakeSTT()
                speaker = Boom()

    out = await rest_voice._transcribe_and_embed(Req, np.zeros(16000, dtype=np.float32), "de")
    assert out.embedded is False and out.speaker_embedding is None
