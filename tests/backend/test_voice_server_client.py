"""Tests for services.voice_server_client — B.4.b HTTP wrapper.

Pytest + httpx MockTransport. Validates URL composition, multipart shape,
auth header, and the error path when voice_server returns non-2xx.
"""

from __future__ import annotations

import io
import wave
from contextlib import asynccontextmanager

import httpx
import pytest


def _make_silence_wav(duration_s: float = 0.5, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(duration_s * sr))
    return buf.getvalue()


def _patch_async_client(monkeypatch, handler):
    """Replace httpx.AsyncClient inside voice_server_client with one that
    routes through a MockTransport. The real AsyncClient is captured
    BEFORE patching to avoid recursion when our fake instantiates it."""
    from services import voice_server_client as mod

    real_async_client = httpx.AsyncClient

    @asynccontextmanager
    async def fake_client(*_args, **_kwargs):
        async with real_async_client(transport=httpx.MockTransport(handler)) as c:
            yield c

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client(*args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Factory())


@pytest.fixture(autouse=True)
def _voice_server_url(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "voice_server_url", "http://voice-server.test:8080")
    yield


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stt_posts_multipart_with_bearer(monkeypatch) -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"text": "hallo", "language": "de", "audio_duration_s": 0.5})

    _patch_async_client(monkeypatch, handler)

    from services.voice_server_client import stt

    result = await stt(
        _make_silence_wav(),
        filename="test.wav",
        content_type="audio/wav",
        language="de",
        auth_token="dummy.jwt.token",
    )
    assert result["text"] == "hallo"
    assert captured["url"] == "http://voice-server.test:8080/api/voice/stt"
    assert captured["auth"] == "Bearer dummy.jwt.token"
    assert captured["content_type"].startswith("multipart/form-data")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stt_sends_x_voice_client_when_configured(monkeypatch) -> None:
    # Registry auth: the shared voice-server routes verification by client id.
    from utils.config import settings
    monkeypatch.setattr(settings, "voice_client_id", "reva")

    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["client"] = request.headers.get("x-voice-client")
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"text": "x", "language": "de", "audio_duration_s": 0.1})

    _patch_async_client(monkeypatch, handler)
    from services.voice_server_client import stt
    await stt(_make_silence_wav(), auth_token="tok")
    assert captured["client"] == "reva"
    assert captured["auth"] == "Bearer tok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stt_omits_x_voice_client_when_unset(monkeypatch) -> None:
    # Legacy single-tenant (local/callback) voice-servers must be unaffected.
    from utils.config import settings
    monkeypatch.setattr(settings, "voice_client_id", "")

    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["has_client"] = "x-voice-client" in request.headers
        return httpx.Response(200, json={"text": "x", "language": "de", "audio_duration_s": 0.1})

    _patch_async_client(monkeypatch, handler)
    from services.voice_server_client import stt
    await stt(_make_silence_wav(), auth_token="tok")
    assert captured["has_client"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stt_raises_on_non_2xx(monkeypatch) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model loading")

    _patch_async_client(monkeypatch, handler)

    from services.voice_server_client import VoiceServerError, stt

    with pytest.raises(VoiceServerError, match="503"):
        await stt(b"\0" * 100, auth_token="t")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tts_returns_wav_bytes(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 200
        return httpx.Response(200, content=body)

    _patch_async_client(monkeypatch, handler)

    from services.voice_server_client import tts

    audio = await tts("hallo", language="de", auth_token="t")
    assert audio.startswith(b"RIFF")
    assert len(audio) > 100


@pytest.mark.unit
@pytest.mark.asyncio
async def test_voice_server_unconfigured_raises(monkeypatch) -> None:
    from utils.config import settings

    monkeypatch.setattr(settings, "voice_server_url", None)

    from services.voice_server_client import VoiceServerError, stt

    with pytest.raises(VoiceServerError, match="not configured"):
        await stt(b"\0", auth_token="t")
