"""Outbound calls (#1683, Telefonie Phase 4): Renfield calls the user's OWN number.

Flow: ``place_call(user_id, text)`` -> Piper speaks "Hier ist Renfield. <text>" -> WAV 8 kHz
mono 16 bit -> gateway command ``place_call`` {id, to, audio_b64} over the existing WebSocket
(``telephony_client``) -> Asterisk rings, plays, hangs up -> event ``call_result`` {id, status}.
A call that was not answered (or could not be placed) falls back to a personal notification
with the message, so nothing is lost.

Trust and cost boundaries (the gateway enforces its own on top: German targets only, fixed
sender, one call at a time, hourly/daily limits):
- the number is NEVER taken from a tool argument: only ``users.call_number``, which is set
  only after a code call to that number was confirmed (``start_verification`` /
  ``confirm_verification``) -- nobody can make Renfield ring someone else's phone;
- per-user limit ``TELEPHONY_CALL_PER_HOUR`` (Redis, survives restarts), code calls included;
- the pending call (owner, purpose, text) lives in Redis under the call id; ``call_result``
  only ever notifies that owner, and a replayed result finds nothing (idempotent).
Logs carry neither the number nor the text.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import secrets
import time
import uuid
import wave
from dataclasses import dataclass

from utils.config import settings

logger = logging.getLogger(__name__)

CALL_RESULT_TYPE = "call_result"
PENDING_PREFIX = "telephony:outcall:"
VERIFY_PREFIX = "telephony:verify:"
RATE_PREFIX = "telephony:callrl:"
PENDING_TTL_SECONDS = 3600
VERIFY_TTL_SECONDS = 900
VERIFY_MAX_ATTEMPTS = 5
SAMPLE_RATE = 8000
MAX_AUDIO_SECONDS = 58.0  # the gateway refuses > 60 s
REPEAT_IF_UNDER_SECONDS = 25.0
LEAD_IN_SECONDS = 1.0  # the first second after picking up is often lost


class CallError(Exception):
    """A call could not be placed; ``reason`` is a short German sentence for the user."""

    def __init__(self, reason: str, code: str):
        super().__init__(code)
        self.reason = reason
        self.code = code


def enabled() -> bool:
    return bool(settings.telephony_outbound_enabled and settings.telephony_enabled)


# --------------------------------------------------------------------------- numbers


def callable_number(value: str) -> str | None:
    """E.164 of a German mobile or landline number, the same rule as the gateway; else None."""
    from services.contact_points import normalize_phone

    parsed = normalize_phone((value or "").strip(), "DE")
    if parsed is None:
        return None
    e164 = parsed[0]
    if not e164.startswith("+49"):
        return None
    rest = e164[3:]
    if rest[:1] == "1":
        return e164 if rest[:2] in ("15", "16", "17") else None
    if rest[:3] in ("700", "800", "900"):
        return None
    return e164 if rest[:1] in "23456789" else None


# --------------------------------------------------------------------------- audio


def _resample(pcm: bytes, rate: int) -> bytes:
    if rate == SAMPLE_RATE:
        return pcm
    from math import gcd

    import numpy as np
    from scipy.signal import resample_poly

    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    g = gcd(SAMPLE_RATE, rate)
    y = resample_poly(x, SAMPLE_RATE // g, rate // g)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def phone_wav(wav_bytes: bytes) -> bytes:
    """Any mono/stereo 16-bit WAV -> 8 kHz mono 16 bit, with a lead-in, repeated when short."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        pcm = w.readframes(w.getnframes())
    if width != 2:
        raise CallError("Die Ansage konnte nicht erzeugt werden.", "audio_format")
    if channels == 2:
        import numpy as np

        pcm = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2).mean(axis=1).astype("<i2").tobytes()
    speech = _resample(pcm, rate)
    pause = b"\x00\x00" * int(SAMPLE_RATE * LEAD_IN_SECONDS)
    body = pause + speech
    if len(speech) / 2 / SAMPLE_RATE < REPEAT_IF_UNDER_SECONDS:
        body += pause + speech
    body = body[: int(MAX_AUDIO_SECONDS * SAMPLE_RATE) * 2]
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(body)
    return out.getvalue()


async def synthesize(text: str) -> bytes:
    from services.piper_service import get_piper_service

    wav = await get_piper_service().synthesize_to_bytes(text, "de")
    if not wav:
        raise CallError("Die Ansage konnte nicht erzeugt werden.", "tts")
    return phone_wav(wav)


# --------------------------------------------------------------------------- placing


async def _within_rate(redis, user_id: int) -> bool:
    key = f"{RATE_PREFIX}{user_id}:{int(time.time() // 3600)}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 3700)
    return count <= settings.telephony_call_per_hour


@dataclass
class Placed:
    call_id: str


async def place_call(*, user_id: int, number: str, text: str, purpose: str = "message") -> Placed:
    """Send one call to the gateway. Raises CallError (nothing was sent) on any refusal."""
    from services.redis_client import get_redis
    from services.telephony_client import GatewayUnavailable, send_command

    if not enabled():
        raise CallError("Anrufe sind nicht eingeschaltet.", "disabled")
    to = callable_number(number)
    if to is None:
        raise CallError("Diese Nummer kann ich nicht anrufen.", "target")
    redis = get_redis()
    if not await _within_rate(redis, user_id):
        raise CallError("Ich habe dich in dieser Stunde schon oft genug angerufen.", "rate")
    audio = await synthesize(f"Hier ist Renfield. {text}")
    call_id = uuid.uuid4().hex
    await redis.set(
        f"{PENDING_PREFIX}{call_id}",
        json.dumps({"user_id": user_id, "purpose": purpose, "text": text if purpose == "message" else ""}),
        ex=PENDING_TTL_SECONDS,
    )
    command = {"type": "place_call", "id": call_id, "to": to, "audio_b64": base64.b64encode(audio).decode()}
    try:
        await send_command(command)
    except GatewayUnavailable:
        await redis.delete(f"{PENDING_PREFIX}{call_id}")
        raise CallError("Das Telefonie-Gateway ist gerade nicht erreichbar.", "gateway") from None
    logger.info("Outbound call %s queued (purpose=%s)", call_id[:8], purpose)
    return Placed(call_id)


# --------------------------------------------------------------------------- results

_RESULT_TEXT = {
    "no_answer": "nicht angenommen",
    "busy": "besetzt",
    "failed": "fehlgeschlagen",
    "rejected": "nicht möglich",
}


async def handle_call_result(event: dict) -> None:
    """``call_result`` from the gateway. Never raises (the event stream must go on)."""
    try:
        await _handle_call_result(event)
    except Exception as e:
        logger.warning("Outbound call result not handled: %s", type(e).__name__)


async def _handle_call_result(event: dict) -> None:
    from services.redis_client import get_redis

    call_id = str(event.get("id") or "")
    if len(call_id) != 32:
        return
    status = str(event.get("status") or "")
    redis = get_redis()
    raw = await redis.getdel(f"{PENDING_PREFIX}{call_id}")
    if raw is None:
        return  # not ours, expired, or a replay of a result already handled
    pending = json.loads(raw)
    logger.info("Outbound call %s: %s", call_id[:8], status if status in _RESULT_TEXT or status == "answered" else "?")
    if status == "answered":
        return
    what = _RESULT_TEXT.get(status, "fehlgeschlagen")
    if pending.get("purpose") == "verify":
        title, message = "Bestätigungsanruf " + what, "Bitte trage die Nummer erneut ein, um einen neuen Code zu bekommen."
    else:
        title = "Anruf von Renfield " + what
        message = str(pending.get("text") or "")[: settings.telephony_call_max_chars] or title
    await _notify(int(pending["user_id"]), title, message)


async def _notify(user_id: int, title: str, message: str) -> None:
    from services.database import AsyncSessionLocal
    from services.notification_service import NotificationService

    async with AsyncSessionLocal() as db:
        try:
            await NotificationService(db).process_webhook(
                event_type="telephony.call_result", title=title, message=message, urgency="info",
                source="telephony", privacy="personal", target_user_id=user_id, dedup=False,
            )
        except ValueError:
            pass  # suppressed by the user's feedback


# --------------------------------------------------------------------------- verification


def _code_hash(user_id: int, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{code}".encode()).hexdigest()


def _spoken(code: str) -> str:
    digits = ", ".join(code)
    return f"Dein Bestätigungscode lautet: {digits}. Ich wiederhole: {digits}."


async def start_verification(user_id: int, number: str) -> None:
    """Ring ``number`` and speak a 4-digit code; the number is stored only on ``confirm_verification``."""
    from services.redis_client import get_redis

    to = callable_number(number)
    if to is None:
        raise CallError("Diese Nummer kann ich nicht anrufen (nur deutsche Mobil- oder Festnetznummern).", "target")
    code = f"{secrets.randbelow(10000):04d}"
    redis = get_redis()
    await redis.set(
        f"{VERIFY_PREFIX}{user_id}",
        json.dumps({"number": to, "code": _code_hash(user_id, code), "attempts": 0}),
        ex=VERIFY_TTL_SECONDS,
    )
    try:
        await place_call(user_id=user_id, number=to, text=_spoken(code), purpose="verify")
    except CallError:
        await redis.delete(f"{VERIFY_PREFIX}{user_id}")
        raise


async def pending_verification(user_id: int) -> str | None:
    from services.redis_client import get_redis

    raw = await get_redis().get(f"{VERIFY_PREFIX}{user_id}")
    return json.loads(raw)["number"] if raw else None


async def confirm_verification(user_id: int, code: str) -> str | None:
    """The confirmed E.164 number, or None (wrong code; after VERIFY_MAX_ATTEMPTS the attempt is void)."""
    from services.redis_client import get_redis

    redis = get_redis()
    key = f"{VERIFY_PREFIX}{user_id}"
    raw = await redis.get(key)
    if raw is None:
        return None
    state = json.loads(raw)
    if hmac.compare_digest(state["code"], _code_hash(user_id, (code or "").strip())):
        await redis.delete(key)
        return state["number"]
    state["attempts"] = int(state.get("attempts", 0)) + 1
    if state["attempts"] >= VERIFY_MAX_ATTEMPTS:
        await redis.delete(key)
    else:
        ttl = await redis.ttl(key)
        await redis.set(key, json.dumps(state), ex=max(1, ttl if ttl and ttl > 0 else VERIFY_TTL_SECONDS))
    return None
