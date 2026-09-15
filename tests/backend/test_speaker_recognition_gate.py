"""Speaker-recognition privacy gate on the chat-WS voice path (F6).

A browser voice turn arrives at the chat WS with the voice-server's 192-dim
ECAPA embedding. Before this gate, ``chat_handler`` resolved it UNCONDITIONALLY
— with the default ``speaker_auto_enroll`` / ``speaker_continuous_learning``
that stored a voiceprint (biometric data, Art. 9 GDPR) and an
"Unbekannter Sprecher #N" row on EVERY voice turn, even on an instance that set
``SPEAKER_RECOGNITION_ENABLED=false``.

Pins:
- flag off → the chat handler never calls the resolver and never opens a DB
  session (so no Speaker / SpeakerEmbedding / SpeakerCandidate row can exist),
  while ``voice_originated`` still derives from the embedding;
- flag off → the resolver itself refuses before touching the session (defense
  in depth for any future caller);
- flag on → resolution runs exactly as before.
"""
from __future__ import annotations

import pytest

from utils.config import settings
from utils.voice_context import voice_originated

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_EMBEDDING = [0.1] * 192


class _RecordingSession:
    """Records every attribute access. The resolver wraps its DB work in a
    broad try/except, so a RAISING stub would be swallowed and the test would
    pass vacuously — recording is what proves the session was never touched."""

    def __init__(self):
        object.__setattr__(self, "accessed", [])

    def __getattr__(self, name):
        self.accessed.append(name)
        raise RuntimeError(f"session.{name} touched")


class _ExplodingSessionFactory:
    def __call__(self):
        raise AssertionError("AsyncSessionLocal opened while speaker recognition is off")


async def test_handler_skips_resolver_when_recognition_off(monkeypatch):
    import api.websocket.chat_handler as ch
    import services.speaker_resolver as resolver

    monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
    # Auto-enrol + continuous learning ON: the dangerous defaults must not matter.
    monkeypatch.setattr(settings, "speaker_auto_enroll", True)
    monkeypatch.setattr(settings, "speaker_continuous_learning", True)

    async def _must_not_run(*_a, **_kw):
        raise AssertionError("resolver called while speaker recognition is off")

    monkeypatch.setattr(resolver, "resolve_speaker_from_embedding", _must_not_run)
    monkeypatch.setattr(ch, "AsyncSessionLocal", _ExplodingSessionFactory())

    voice_originated.set(False)
    info = await ch._resolve_wire_speaker(_EMBEDDING, 2.5)

    assert info is None
    assert voice_originated.get() is True


async def test_handler_text_turn_is_not_voice_originated(monkeypatch):
    import api.websocket.chat_handler as ch

    monkeypatch.setattr(settings, "speaker_recognition_enabled", True)
    monkeypatch.setattr(ch, "AsyncSessionLocal", _ExplodingSessionFactory())

    voice_originated.set(True)  # stale value from a previous turn
    assert await ch._resolve_wire_speaker(None, None) is None
    assert voice_originated.get() is False


async def test_handler_resolves_when_recognition_on(monkeypatch):
    import api.websocket.chat_handler as ch
    import services.speaker_resolver as resolver

    monkeypatch.setattr(settings, "speaker_recognition_enabled", True)
    calls: list[tuple] = []
    expected = {
        "speaker_id": 7, "speaker_name": "Anna", "speaker_alias": None,
        "speaker_confidence": 0.9, "is_new_speaker": False,
    }

    async def _fake_resolve(session, embedding, *, audio_duration_s=None):
        calls.append((session, len(embedding), audio_duration_s))
        return expected

    class _Session:
        async def __aenter__(self):
            return "session"

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(resolver, "resolve_speaker_from_embedding", _fake_resolve)
    monkeypatch.setattr(ch, "AsyncSessionLocal", lambda: _Session())

    voice_originated.set(False)
    info = await ch._resolve_wire_speaker(_EMBEDDING, 3.0)

    assert info == expected
    assert calls == [("session", 192, 3.0)]
    assert voice_originated.get() is True


async def test_handler_resolver_failure_is_best_effort(monkeypatch):
    import api.websocket.chat_handler as ch
    import services.speaker_resolver as resolver

    monkeypatch.setattr(settings, "speaker_recognition_enabled", True)

    async def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    class _Session:
        async def __aenter__(self):
            return "session"

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(resolver, "resolve_speaker_from_embedding", _boom)
    monkeypatch.setattr(ch, "AsyncSessionLocal", lambda: _Session())

    assert await ch._resolve_wire_speaker(_EMBEDDING, 3.0) is None
    assert voice_originated.get() is True


async def test_resolver_refuses_without_touching_db_when_off(monkeypatch):
    from services.speaker_resolver import resolve_speaker_from_embedding

    monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
    monkeypatch.setattr(settings, "speaker_auto_enroll", True)
    monkeypatch.setattr(settings, "speaker_continuous_learning", True)

    session = _RecordingSession()
    info = await resolve_speaker_from_embedding(
        session, _EMBEDDING, audio_duration_s=5.0,
    )

    assert session.accessed == []
    assert info == {
        "speaker_id": None,
        "speaker_name": None,
        "speaker_alias": None,
        "speaker_confidence": 0.0,
        "is_new_speaker": False,
    }
