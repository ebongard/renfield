"""B.4.c thin-client tests for whisper_service + piper_service.

Verifies the wire-delegation paths activate when VOICE_SERVER_URL is
set and call voice_server_client.{stt,tts}. Speaker resolution flows
through speaker_resolver. In-process Whisper / Piper code is NOT
exercised here — that lives behind the legacy fallback path and has
its own tests.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest


def _patch_async_client(monkeypatch, handler):
    real_async_client = httpx.AsyncClient

    @asynccontextmanager
    async def fake_client(*_args, **_kwargs):
        async with real_async_client(transport=httpx.MockTransport(handler)) as c:
            yield c

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client(*args, **kwargs)

    from services import voice_server_client as mod
    monkeypatch.setattr(mod.httpx, "AsyncClient", _Factory())


@pytest.fixture(autouse=True)
def _voice_server_url(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "voice_server_url", "http://voice-server.test:8080")
    yield


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whisper_transcribe_bytes_delegates_to_voice_server(monkeypatch) -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"text": "wire-text", "language": "de", "audio_duration_s": 0.5})

    _patch_async_client(monkeypatch, handler)

    from services.whisper_service import WhisperService

    svc = WhisperService()
    text = await svc.transcribe_bytes(b"\0" * 100, filename="x.wav", language="de")
    assert text == "wire-text"
    assert captured["url"].endswith("/api/voice/stt")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whisper_with_speaker_passes_embedding_to_resolver(monkeypatch) -> None:
    """Wire embedding flows from voice-server STT into speaker_resolver."""
    received_embedding: list[list[float] | None] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": "spoken text",
                "language": "de",
                "audio_duration_s": 1.0,
                "speaker_embedding": [0.1] * 192,
            },
        )

    _patch_async_client(monkeypatch, handler)

    async def fake_resolver(_db, embedding):
        received_embedding.append(embedding)
        return {
            "speaker_id": 42,
            "speaker_name": "Test User",
            "speaker_alias": "test",
            "speaker_confidence": 0.9,
            "is_new_speaker": False,
        }

    monkeypatch.setattr(
        "services.speaker_resolver.resolve_speaker_from_embedding",
        fake_resolver,
    )

    # Force speaker_recognition_enabled
    from utils.config import settings
    monkeypatch.setattr(settings, "speaker_recognition_enabled", True)

    from services.whisper_service import WhisperService

    # Sentinel db_session — the wire path needs a non-None caller
    # session to opt-in to speaker resolution (mirrors the in-process
    # contract). The resolver itself receives a fresh session opened
    # internally, so the sentinel is never actually used for queries.
    svc = WhisperService()
    result = await svc.transcribe_bytes_with_speaker(
        b"\0" * 100,
        language="de",
        db_session=object(),
    )
    assert result["text"] == "spoken text"
    assert result["speaker_id"] == 42
    assert result["speaker_name"] == "Test User"
    assert len(received_embedding) == 1
    assert len(received_embedding[0]) == 192


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whisper_with_speaker_skips_resolution_when_db_session_none(monkeypatch) -> None:
    """db_session=None opt-out (mirrors in-process contract) suppresses speaker resolution."""
    resolver_called = []

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": "hello",
                "language": "de",
                "audio_duration_s": 1.0,
                "speaker_embedding": [0.1] * 192,
            },
        )

    _patch_async_client(monkeypatch, handler)

    async def fake_resolver(_db, _embedding):
        resolver_called.append(True)
        return {
            "speaker_id": 99,
            "speaker_name": "WRONG",
            "speaker_alias": "wrong",
            "speaker_confidence": 1.0,
            "is_new_speaker": True,
        }

    monkeypatch.setattr(
        "services.speaker_resolver.resolve_speaker_from_embedding",
        fake_resolver,
    )

    from utils.config import settings
    monkeypatch.setattr(settings, "speaker_recognition_enabled", True)

    from services.whisper_service import WhisperService

    svc = WhisperService()
    result = await svc.transcribe_bytes_with_speaker(
        b"\0" * 100,
        language="de",
        db_session=None,
    )
    assert result["text"] == "hello"
    assert result["speaker_id"] is None  # opt-out honored
    assert resolver_called == []          # resolver never called


@pytest.mark.unit
@pytest.mark.asyncio
async def test_piper_synthesize_to_bytes_delegates_to_voice_server(monkeypatch) -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, content=b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 200)

    _patch_async_client(monkeypatch, handler)

    from services.piper_service import PiperService

    svc = PiperService()
    audio = await svc.synthesize_to_bytes("Hallo Welt", language="de")
    assert audio.startswith(b"RIFF")
    assert captured["url"].endswith("/api/voice/tts")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_piper_cache_serves_repeated_text_without_wire_call(monkeypatch) -> None:
    """A cache hit short-circuits before the voice-server call."""
    call_count = {"n": 0}

    async def handler(_: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, content=b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 200)

    _patch_async_client(monkeypatch, handler)

    from services.piper_service import PiperService

    svc = PiperService()
    await svc.synthesize_to_bytes("same text", language="de")
    await svc.synthesize_to_bytes("same text", language="de")
    assert call_count["n"] == 1
