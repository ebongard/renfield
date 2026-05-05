"""WebSocket /ws/voice handler — full Phase B protocol.

Client→Server (text JSON unless noted):
  - session_start { codec, sample_rate?, channels? }   first message after WS open
  - <binary frame>                                     audio chunk in announced codec
  - stt_flush                                          finalize STT (alt. to VAD-stop)
  - tts_request { request_id, text, language?, voice? }
  - cancel { request_id }                              reserved (Phase B.next)
  - ping                                               keepalive

Server→Client:
  - partial_transcript { text, confidence }
  - final_transcript { text, language, speaker_embedding[192], audio_duration_s }
  - <binary WAV with 24-byte RFWA header>              one frame per TTS sentence
  - tts_done { request_id }
  - error { code, message, request_id? }
  - pong

See VOICE_PIPELINE_DESIGN.md § "WebSocket protocol" for the full table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import numpy as np
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from voice_server.auth import AuthError, authenticate
from voice_server.services.audio_decoder import AudioDecoder
from voice_server.services.stt_service import STTService
from voice_server.services.tts_service import TTSService
from voice_server.services.speaker_service import SpeakerService

logger = logging.getLogger(__name__)
router = APIRouter()

# How often to run incremental STT while audio is streaming. Whisper-medium
# runs in 0.3-0.5x realtime on the 4060 Ti, so polling every ~700 ms keeps
# the GPU busy without thrashing.
PARTIAL_INTERVAL_S = 0.7

# Hard ceiling on a single utterance. Beyond this we force-flush even if
# the client never signals stt_flush. Prevents pathological memory growth.
MAX_UTTERANCE_S = 60


@dataclass
class SessionState:
    user_id: str
    codec: str | None = None
    decoder: AudioDecoder | None = None
    audio_pcm: list[np.ndarray] = field(default_factory=list)
    last_partial_at: float = 0.0
    last_partial_text: str = ""
    started_at: float = 0.0


def _accumulated_seconds(state: SessionState) -> float:
    total = sum(a.size for a in state.audio_pcm)
    return total / 16000.0


async def _send_json(ws: WebSocket, payload: dict) -> None:
    await ws.send_text(json.dumps(payload, separators=(",", ":")))


async def _send_error(ws: WebSocket, code: str, message: str, request_id: str | None = None) -> None:
    payload = {"type": "error", "code": code, "message": message}
    if request_id:
        payload["request_id"] = request_id
    try:
        await _send_json(ws, payload)
    except Exception:
        pass


async def _maybe_emit_partial(ws: WebSocket, state: SessionState, stt: STTService) -> None:
    """If enough time has passed since the last partial, run STT and emit a partial_transcript."""
    now = time.monotonic()
    if now - state.last_partial_at < PARTIAL_INTERVAL_S:
        return
    if not state.audio_pcm:
        return

    state.last_partial_at = now
    audio = np.concatenate(state.audio_pcm)
    if audio.size < 16000 * 0.3:  # less than 300 ms — too short to bother
        return

    try:
        last_seg_text = ""
        last_conf = 0.0
        async for seg in stt.transcribe_stream(audio):
            last_seg_text = seg.text
            last_conf = seg.confidence
        # Concatenate all segment texts for the cumulative partial
        # (faster-whisper's segment iterator finalizes them so we just
        # take the latest as a heuristic — partials are advisory anyway).
        if last_seg_text and last_seg_text != state.last_partial_text:
            state.last_partial_text = last_seg_text
            await _send_json(ws, {
                "type": "partial_transcript",
                "text": last_seg_text,
                "confidence": last_conf,
            })
    except Exception as e:
        logger.warning("partial STT failed: %s", e)


async def _finalize(
    ws: WebSocket,
    state: SessionState,
    stt: STTService,
    speaker: SpeakerService,
) -> None:
    """Drain the decoder, run final STT + speaker embed, emit final_transcript."""
    if state.decoder is not None:
        tail = await state.decoder.flush()
        if tail.size:
            state.audio_pcm.append(tail)

    if not state.audio_pcm:
        return

    audio = np.concatenate(state.audio_pcm)
    duration_s = float(audio.size) / 16000.0

    final_text = ""
    language = "de"
    try:
        async for seg in stt.transcribe_stream(audio):
            final_text = (final_text + " " + seg.text).strip()
        # faster-whisper exposes detected language via info — we already
        # passed language hint, so reflect it back.
    except Exception as e:
        logger.error("final STT failed: %s", e)
        await _send_error(ws, "stt_failed", str(e))
        state.audio_pcm.clear()
        return

    embedding: list[float] | None = None
    try:
        emb = await speaker.embed(audio)
        embedding = emb.tolist()
    except Exception as e:
        logger.warning("speaker embed failed: %s", e)
        await _send_error(ws, "speaker_extract_failed", str(e))
        # Spec: speaker_extract_failed must NOT abort the transcript.
        embedding = None

    payload: dict = {
        "type": "final_transcript",
        "text": final_text,
        "language": language,
        "audio_duration_s": duration_s,
    }
    if embedding is not None:
        payload["speaker_embedding"] = embedding

    await _send_json(ws, payload)
    state.audio_pcm.clear()
    state.last_partial_text = ""


async def _handle_tts_request(
    ws: WebSocket,
    msg: dict,
    tts: TTSService,
) -> None:
    request_id_raw = msg.get("request_id")
    text = msg.get("text", "")
    language = msg.get("language")

    if not request_id_raw or not text:
        await _send_error(ws, "invalid_audio", "tts_request missing request_id or text")
        return
    try:
        request_id = uuid.UUID(request_id_raw)
    except (ValueError, AttributeError):
        await _send_error(ws, "invalid_audio", "request_id is not a UUID", request_id_raw)
        return

    try:
        async for frame in tts.stream_sentences(text, request_id, language=language):
            await ws.send_bytes(frame)
        await _send_json(ws, {"type": "tts_done", "request_id": str(request_id)})
    except FileNotFoundError as e:
        await _send_error(ws, "model_unavailable", str(e), str(request_id))
    except Exception as e:
        logger.exception("tts failed for %s", request_id)
        await _send_error(ws, "tts_failed", str(e), str(request_id))


@router.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket, token: str = Query(...)) -> None:
    try:
        payload = await authenticate(token)
    except AuthError as e:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=str(e))
        return

    user_id = str(payload.get("sub") or payload.get("user_id") or "unknown")
    await websocket.accept()
    logger.info("voice session opened user=%s", user_id)

    stt: STTService = websocket.app.state.stt
    tts: TTSService = websocket.app.state.tts
    speaker: SpeakerService = websocket.app.state.speaker

    state = SessionState(user_id=user_id, started_at=time.monotonic())

    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break

            if "bytes" in msg and msg["bytes"] is not None:
                if state.decoder is None:
                    await _send_error(ws=websocket, code="invalid_audio",
                                       message="binary frame before session_start")
                    continue
                try:
                    await state.decoder.push(msg["bytes"])
                    pcm = await state.decoder.take_pcm()
                    if pcm.size:
                        state.audio_pcm.append(pcm)
                    await _maybe_emit_partial(websocket, state, stt)
                    if _accumulated_seconds(state) >= MAX_UTTERANCE_S:
                        logger.warning("voice session %s hit MAX_UTTERANCE_S, force-finalizing", user_id)
                        await _finalize(websocket, state, stt, speaker)
                except Exception as e:
                    logger.exception("audio chunk error")
                    await _send_error(ws=websocket, code="invalid_audio", message=str(e))
                continue

            if "text" not in msg or msg["text"] is None:
                continue

            try:
                data = json.loads(msg["text"])
            except json.JSONDecodeError:
                await _send_error(ws=websocket, code="invalid_audio", message="bad json")
                continue

            mtype = data.get("type")

            if mtype == "session_start":
                codec = data.get("codec")
                try:
                    state.decoder = AudioDecoder(codec)
                    await state.decoder.start()
                    state.codec = codec
                    await _send_json(websocket, {"type": "session_ready"})
                except ValueError as e:
                    await _send_error(ws=websocket, code="invalid_audio", message=str(e))

            elif mtype == "stt_flush":
                await _finalize(websocket, state, stt, speaker)

            elif mtype == "tts_request":
                await _handle_tts_request(websocket, data, tts)

            elif mtype == "cancel":
                # Phase B.next — barge-in. For now, ack only.
                await _send_json(websocket, {
                    "type": "tts_done",
                    "request_id": data.get("request_id"),
                })

            elif mtype == "ping":
                await _send_json(websocket, {"type": "pong"})

            else:
                await _send_error(ws=websocket, code="invalid_audio",
                                   message=f"unknown type: {mtype}")

    except WebSocketDisconnect:
        logger.info("voice session disconnected user=%s", user_id)
    except Exception:
        logger.exception("voice session crashed user=%s", user_id)
    finally:
        if state.decoder is not None:
            try:
                await state.decoder.close()
            except Exception:
                pass
