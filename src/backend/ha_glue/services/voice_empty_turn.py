"""Why a voice turn produced no text (#1386).

A turn that ends without text used to end with one WARNING ("Empty
transcription") and a silent return to idle. 92 of 232 wake events in one
household week ended that way, and a false wake could not be told apart from
speech that was heard but not understood — the two need opposite repairs
(room-specific wake-word negatives vs. STT / feedback work).

This module classifies such a turn from data that already reaches the backend,
and records the verdict as a log line and a counter. It never sees the text
(there is none) and never logs audio — content-free by construction.

The kinds:

- ``no_audio``: the satellite sent ``audio_end`` with nothing buffered.
- ``stt_error``: the speech-to-text call failed. Until #1386 a voice-server
  failure came back as ``""`` and was counted as an empty transcription.
- ``vad_floor``: the device stopped on silence at the EARLIEST moment its VAD
  allows (grace period + silence window). Its VAD saw no speech after the grace
  period: the signature of a false wake. NOT proof of one: the VAD ignores the
  grace period, so a short command spoken entirely inside it ends at the floor
  too.
- ``vad_heard_speech``: stopped on silence, but later than the floor — the VAD
  kept the turn open on something it took for speech, and STT returned nothing.
  Unintelligible speech, or a non-speech sound loud enough for the VAD.
- ``cap_reached``: stopped at ``max_recording_seconds`` and still no text.
- ``button``: ended with the button.
- ``unknown_end``: no or an unknown turn-end reason (legacy clients, browser
  devices without the satellite's VAD).
"""

from __future__ import annotations

from loguru import logger

from utils.metrics import VOICE_EMPTY_TURN_KINDS, VOICE_EMPTY_TURN_SOURCES

#: Satellite capture chunk: 1280 samples at 16 kHz (`audio.chunk_size`).
SATELLITE_CHUNK_SECONDS = 0.08

#: The fleet's VAD end-of-turn settings. 🛑 A fleet setting has TWO sources
#: (`src/satellite/provisioning/group_vars/satellites.yml` and the ConfigMap in
#: `k8s/satellite-esszimmer.yaml`); `min_listening_seconds` is set in neither
#: and comes from the satellite's code default. A cross-check test in
#: `tests/satellite/` fails when they drift apart from these values.
FLEET_MIN_LISTENING_SECONDS = 2.0
FLEET_SILENCE_DURATION_MS = 1200

#: Opus packet length on the satellite (`audio/opus_codec.py`: 40 ms).
OPUS_PACKET_SECONDS = 0.04

#: 16 kHz mono S16_LE.
PCM_BYTES_PER_SECOND = 16000 * 2

#: The kinds above and the sources (a satellite, or a `/ws/device` client).
#: The vocabulary lives with the counter, which platform code owns.
EMPTY_TURN_KINDS = VOICE_EMPTY_TURN_KINDS
EMPTY_TURN_SOURCES = VOICE_EMPTY_TURN_SOURCES


def vad_floor_seconds(
    min_listening_seconds: float = FLEET_MIN_LISTENING_SECONDS,
    silence_duration_ms: int = FLEET_SILENCE_DURATION_MS,
    chunk_seconds: float = SATELLITE_CHUNK_SECONDS,
) -> float:
    """The shortest recording a satellite can end on silence with.

    Mirrors the chunk counting in `satellite.py`: silence is counted from the
    chunk at which `_recorded_chunks` reaches the grace period, so the first
    possible end is chunk `grace + silence - 1`. With the fleet values that is
    39 chunks = 3.12 s — exactly the "floor" length measured in #1386.
    """
    chunk_ms = chunk_seconds * 1000
    grace_chunks = int(min_listening_seconds * 1000 / chunk_ms)
    silence_chunks = int(silence_duration_ms / chunk_ms)
    return (grace_chunks + silence_chunks - 1) * chunk_seconds


SATELLITE_VAD_FLOOR_SECONDS = vad_floor_seconds()


def recorded_seconds(*, opus_packets: int = 0, pcm_bytes: int = 0) -> float:
    """Recorded audio length from what the backend buffered."""
    if opus_packets:
        return opus_packets * OPUS_PACKET_SECONDS
    return pcm_bytes / PCM_BYTES_PER_SECOND


def classify_empty_turn(
    *,
    turn_end_reason: str | None,
    audio_seconds: float,
    stt_failed: bool = False,
    floor_seconds: float | None = None,
) -> str:
    """Name why a turn produced no text. ``floor_seconds=None``: no VAD floor
    is known for this source, so a silence end is not split by length."""
    if stt_failed:
        return "stt_error"
    if audio_seconds <= 0:
        return "no_audio"
    if turn_end_reason == "silence":
        if floor_seconds is None:
            return "unknown_end"
        # Half a chunk of slack, deliberately not one: the length is a
        # multiple of the chunk, so half a chunk only absorbs float error.
        # One speech chunk at the first counted position already ends the
        # turn ONE chunk past the floor (3.20 s); a full chunk of slack would
        # file the shortest turn in which the VAD did hear speech as a floor.
        # An overrun chunk would need the satellite to send audio after
        # deciding to stop but before `audio_end`: the chunk that triggers the
        # end schedules `_end_listening` first, which flips the state before
        # any later chunk can be streamed; only a pending camera snapshot
        # (awaited before `audio_end`) together with an event-loop stall of
        # >= 80 ms could let a queued chunk through. The per-turn length in
        # the log line shows whether 3.20 s piles up in camera rooms.
        if audio_seconds <= floor_seconds + SATELLITE_CHUNK_SECONDS / 2:
            return "vad_floor"
        return "vad_heard_speech"
    if turn_end_reason == "timeout":
        return "cap_reached"
    if turn_end_reason == "button":
        return "button"
    return "unknown_end"


def record_empty_turn(
    *,
    source: str,
    session_id: str,
    room: str | None,
    turn_end_reason: str | None,
    audio_seconds: float,
    stt_failed: bool = False,
    floor_seconds: float | None = None,
) -> dict:
    """Classify, log and count one empty turn.

    Returns the content-free detail for the session's history event. Never
    raises: a metrics fault must not cost the turn its clean end.
    """
    kind = classify_empty_turn(
        turn_end_reason=turn_end_reason,
        audio_seconds=audio_seconds,
        stt_failed=stt_failed,
        floor_seconds=floor_seconds,
    )
    logger.warning(
        f"🔇 Empty voice turn ({source}) session={session_id} room={room or '-'} "
        f"kind={kind} turn_end={turn_end_reason or '-'} audio={audio_seconds:.2f}s"
    )
    try:
        from utils.metrics import record_voice_empty_turn

        record_voice_empty_turn(source, kind)
    except Exception as e:
        logger.warning(f"⚠️ Could not record empty voice turn: {e!r}")
    return {"empty_kind": kind, "audio_seconds": round(audio_seconds, 2)}
