"""#1568 through the real satellite loop: one room, one connection, several voices.

The satellite keeps the ROOM's history in memory for the whole connection, and
the room thread is shared by every member at tier 2. Here `satellite_websocket`
runs whole with the real `SatelliteManager`, the real speaker-to-person lookup
and the real conversation service on Postgres; only the edges are fakes
(socket, STT, LLM, TTS). Anna speaks, then Bert on the SAME connection: what
reaches the LLM for Bert holds none of Anna's tool results, and every stored
answer names the PERSON who asked -- never the device account.
"""
from __future__ import annotations

import base64
import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text

from ha_glue.api.websocket import satellite_handler as handler
from models.database import Conversation, Message
from services.conversation_service import ConversationService
from services.ollama_service import OllamaService
from tests.backend.dbrows import ensure_role, ensure_speaker, ensure_user
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.database]

DEVICE, ANNA, BERT = 61, 62, 63
SAT = "sat-shared"
ROOM_SESSION = f"satellite-{SAT}-{date.today().isoformat()}"
PREFIX = "[Aktionsergebnis"
A_SUMMARY = "mcp.paperless.search_documents → 1 Ergebnisse:\n  - id=4711, title=Befund Anna"
SPEAKERS = {DEVICE: (71, "haushalt-sat"), ANNA: (72, "anna-sat"), BERT: (73, "bert-sat")}


class _Socket:
    """Plays text frames into `satellite_websocket`, then disconnects."""

    def __init__(self, frames: list[dict], app):
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.client = MagicMock(host="10.0.0.7")
        self.app = app

    async def accept(self):
        return None

    async def receive(self):
        if self._frames:
            return {"type": "websocket.receive", "text": json.dumps(self._frames.pop(0))}
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, **_kw):
        return None


class _Ollama:
    """The real load/save path; the LLM calls are recorded fakes."""

    load_conversation_context = OllamaService.load_conversation_context
    save_message = OllamaService.save_message

    def __init__(self):
        self.histories: list[list[dict]] = []

    async def extract_ranked_intents(self, _text, room_context=None, conversation_history=None):
        return [{"intent": "general.conversation", "parameters": {}, "confidence": 1.0}]

    async def chat_stream(self, _prompt, history=None):
        self.histories.append([dict(m) for m in history or []])
        yield "Gern."


def _voice_turn(n: int) -> list[dict]:
    sid = f"s{n}"
    pcm = base64.b64encode(b"\0" * 32000).decode("ascii")
    return [
        {"type": "wakeword_detected", "keyword": "hey_renfield", "confidence": 0.9, "session_id": sid},
        {"type": "audio", "session_id": sid, "chunk": pcm, "sequence": 1},
        {"type": "audio_end", "session_id": sid, "reason": "silence"},
    ]


@pytest.fixture
async def room(db_session):
    """Device account owning the room thread at tier 2; Anna and Bert reach
    it. Anna's earlier answer in it carries her tool result."""
    await ensure_role(db_session)
    for uid, name in ((DEVICE, "haushalt-sat"), (ANNA, "anna-sat"), (BERT, "bert-sat")):
        user = await ensure_user(db_session, uid, username=name)
        spk_id, spk_name = SPEAKERS[uid]
        user.speaker_id = (await ensure_speaker(db_session, spk_id, spk_name)).id
        user.is_device_account = uid == DEVICE
    await db_session.commit()
    for member in (ANNA, BERT):
        await db_session.execute(text(
            "INSERT INTO circle_memberships "
            "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
            "VALUES (:o, :m, 'tier', '2', :o, NOW())"), {"o": DEVICE, "m": member})
    svc = ConversationService(db_session)
    await svc.save_message(ROOM_SESSION, "user", "Such meinen Befund", user_id=DEVICE, circle_tier=2)
    await svc.save_message(
        ROOM_SESSION, "assistant", "Gefunden.", user_id=DEVICE,
        metadata={"intent": "agent", "action_success": True,
                  "action_summary": A_SUMMARY, "asker_user_id": ANNA},
    )
    await db_session.commit()


@pytest.fixture
def run_room(monkeypatch, room, own_session_to_test_db):
    """Run the given speakers' turns on ONE satellite connection."""
    from ha_glue.services import satellite_manager as manager_mod
    from ha_glue.utils.config import ha_glue_settings

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "satellite_device_account", "haushalt-sat")
    monkeypatch.setattr(settings, "speaker_recognition_enabled", True)
    monkeypatch.setattr(settings, "satellite_enrollment_enabled", False)
    monkeypatch.setattr(ha_glue_settings, "rooms_auto_create_from_satellite", False)
    monkeypatch.setattr(ha_glue_settings, "presence_enabled", False)

    mgr = manager_mod.SatelliteManager()
    monkeypatch.setattr(manager_mod, "get_satellite_manager", lambda: mgr)
    limiter = MagicMock()
    limiter.can_connect.return_value = (True, "")
    monkeypatch.setattr(handler, "get_connection_limiter", lambda: limiter)
    rate = MagicMock()
    rate.check.return_value = (True, "")
    monkeypatch.setattr(handler, "get_rate_limiter", lambda: rate)
    monkeypatch.setattr(handler, "authenticate_websocket", AsyncMock(return_value={"sub": "t"}))
    wakeword = MagicMock()
    wakeword.get_config = AsyncMock(return_value=MagicMock(to_satellite_config=lambda: {}))
    monkeypatch.setattr(handler, "get_wakeword_config_manager", lambda: wakeword)
    monkeypatch.setattr(handler, "_spawn_satellite_extraction", lambda **_kw: None)

    builder = MagicMock()
    builder.build = AsyncMock(return_value="")
    monkeypatch.setattr("services.whisper_prompt_builder.get_whisper_prompt_builder", lambda: builder)
    monkeypatch.setattr(
        "services.whisper_prompt_builder.resolve_first_speaker_from_room", AsyncMock(return_value=None),
    )
    piper = MagicMock()
    piper.synthesize_to_bytes = AsyncMock(return_value=b"")
    monkeypatch.setattr("services.piper_service.get_piper_service", lambda: piper)

    monkeypatch.setattr(settings, "voice_auth_min_confidence", 0.7)

    async def _run(speakers: list[int | tuple[int, float]]) -> _Ollama:
        """Each entry a speaker, or (speaker, recognition confidence); 0.9 by default."""
        turns = [s if isinstance(s, tuple) else (s, 0.9) for s in speakers]
        whisper = MagicMock()
        whisper.transcribe_bytes_with_speaker = AsyncMock(side_effect=[
            {"text": "Was gibt es Neues?", "speaker_name": SPEAKERS[uid][1],
             "speaker_alias": None, "speaker_confidence": conf}
            for uid, conf in turns
        ])
        monkeypatch.setattr(handler, "get_whisper_service", lambda: whisper)
        ollama = _Ollama()
        app = MagicMock()
        app.state.ollama = ollama
        frames = [{"type": "register", "satellite_id": SAT, "room": "Küche", "capabilities": {}}]
        for n in range(len(speakers)):
            frames += _voice_turn(n + 1)
        await handler.satellite_websocket(_Socket(frames, app), token=None)
        assert len(ollama.histories) == len(speakers), "every turn reached the LLM"
        return ollama

    return _run


async def _stored_askers(db_session) -> list:
    db_session.expire_all()
    conv = (await db_session.execute(
        select(Conversation).where(Conversation.session_id == ROOM_SESSION))).scalar_one()
    metas = (await db_session.execute(
        select(Message.message_metadata)
        .where(Message.conversation_id == conv.id, Message.role == "assistant")
        .order_by(Message.id))).scalars().all()
    return [(m or {}).get("asker_user_id") for m in metas]


def _carries_anna(history: list[dict]) -> bool:
    return any(
        PREFIX in (m.get("content") or "") or "4711" in (m.get("content") or "")
        or "4711" in str((m.get("metadata") or {}).get("action_summary", ""))
        for m in history
    )


async def test_the_next_voice_on_the_connection_gets_none_of_the_askers_results(
    run_room, db_session,
):
    ollama = await run_room([ANNA, BERT])
    anna, bert = ollama.histories

    assert _carries_anna(anna), "precondition: Anna's own turn sees her result"
    assert any("Gefunden." in (m.get("content") or "") for m in bert), "Bert shares the thread"
    assert not _carries_anna(bert), "Bert's prompt carries Anna's tool result"

    assert await _stored_askers(db_session) == [ANNA, ANNA, BERT]


async def test_the_device_accounts_voice_is_no_asker(run_room, db_session):
    """A speaker linked to the device account stands for every voice in the
    house: no asker recorded, and no member's result in its prompt."""
    ollama = await run_room([DEVICE])
    [device] = ollama.histories
    assert not _carries_anna(device)
    assert await _stored_askers(db_session) == [ANNA, None]


async def test_below_the_identity_floor_the_speaker_counts_as_unknown(run_room, db_session):
    """The prompt identity needs the browser's voice-promotion floor: at 0.6
    Anna is an unknown voice (no asker, none of her own results); at 0.9 on
    the same connection she gets them back."""
    ollama = await run_room([(ANNA, 0.6), (ANNA, 0.9)])
    weak, sure = ollama.histories
    assert not _carries_anna(weak)
    assert _carries_anna(sure)
    assert await _stored_askers(db_session) == [ANNA, None, ANNA]
