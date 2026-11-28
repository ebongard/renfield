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
- per-user limit ``TELEPHONY_CALL_PER_HOUR`` (Redis, survives restarts), code calls included; one call per
  user at a time (lock), code calls to one number capped per day, 6-digit code, attempts counted atomically;
- the pending call (owner, purpose, text) lives in Redis under the call id; ``call_result``
  only ever notifies that owner, and is removed only after the notice went out (a replay then finds nothing).
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
ATTEMPTS_PREFIX = "telephony:verifyatt:"
TARGET_PREFIX = "telephony:verifytgt:"
LOCK_PREFIX = "telephony:calllock:"
LOCK_SECONDS = 180  # longer than ring + speech; released early by the call's result
CODE_DIGITS = 6
VERIFY_PER_TARGET_PER_DAY = 3  # code calls to one number, whoever asks
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
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            pcm = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        raise CallError("Die Ansage konnte nicht erzeugt werden.", "audio_format") from None
    if width != 2 or channels not in (1, 2) or not pcm:
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

    if not enabled():
        raise CallError("Anrufe sind nicht eingeschaltet.", "disabled")
    to = callable_number(number)
    if to is None:
        raise CallError("Diese Nummer kann ich nicht anrufen.", "target")
    redis = get_redis()
    lock = f"{LOCK_PREFIX}{user_id}"
    # One call per user at a time: a repeated tool call (retry, re-plan) never rings twice.
    if not await redis.set(lock, "1", ex=LOCK_SECONDS, nx=True):
        raise CallError("Ich rufe dich gerade schon an.", "in_flight")
    call_id = uuid.uuid4().hex
    try:
        if not await _within_rate(redis, user_id):
            raise CallError("Ich habe dich in dieser Stunde schon oft genug angerufen.", "rate")
        await _send(redis, user_id, to, text, purpose, call_id)
    except BaseException:
        await redis.delete(lock)
        raise
    logger.info("Outbound call %s queued (purpose=%s)", call_id[:8], purpose)
    return Placed(call_id)


async def _send(redis, user_id: int, to: str, text: str, purpose: str, call_id: str) -> None:
    from services.telephony_client import GatewayUnavailable, send_command

    audio = await synthesize(f"Hier ist Renfield. {text}")
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
    key = f"{PENDING_PREFIX}{call_id}"
    raw = await redis.get(key)
    if raw is None:
        return  # not ours, expired, or a replay of a result already handled
    pending = json.loads(raw)
    await redis.delete(f"{LOCK_PREFIX}{int(pending['user_id'])}")
    logger.info("Outbound call %s: %s", call_id[:8], status if status in _RESULT_TEXT or status == "answered" else "?")
    if status == "answered":
        await redis.delete(key)
        return
    what = _RESULT_TEXT.get(status, "fehlgeschlagen")
    if pending.get("purpose") == "verify":
        title, message = (
            "Bestätigungsanruf " + what,
            "Bitte trage die Nummer erneut ein, um einen neuen Code zu bekommen.",
        )
    else:
        title = "Anruf von Renfield " + what
        message = str(pending.get("text") or "")[: settings.telephony_call_max_chars] or title
    await _notify(int(pending["user_id"]), title, message)
    await redis.delete(key)  # only now: a failed notice leaves it for the replay


async def _notify(user_id: int, title: str, message: str) -> None:
    from services.database import AsyncSessionLocal
    from services.notification_service import NotificationService

    async with AsyncSessionLocal() as db:
        try:
            await NotificationService(db).process_webhook(
                event_type="telephony.call_result",
                title=title,
                message=message,
                urgency="info",
                source="telephony",
                privacy="personal",
                target_user_id=user_id,
                dedup=False,
                tts=False,  # never read a missed call's message aloud in a room
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
    """Ring ``number`` and speak a 6-digit code; the number is stored only on ``confirm_verification``."""
    from services.redis_client import get_redis

    to = callable_number(number)
    if to is None:
        raise CallError("Diese Nummer kann ich nicht anrufen (nur deutsche Mobil- oder Festnetznummern).", "target")
    redis = get_redis()
    target = f"{TARGET_PREFIX}{hashlib.sha256(to.encode()).hexdigest()[:32]}:{int(time.time() // 86400)}"
    count = await redis.incr(target)
    if count == 1:
        await redis.expire(target, 90000)
    if count > VERIFY_PER_TARGET_PER_DAY:
        raise CallError("Diese Nummer wurde heute schon oft genug angerufen.", "target_limit")
    code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    await cancel_verification(user_id)
    await redis.set(
        f"{VERIFY_PREFIX}{user_id}",
        json.dumps({"number": to, "code": _code_hash(user_id, code)}),
        ex=VERIFY_TTL_SECONDS,
    )
    try:
        await place_call(user_id=user_id, number=to, text=_spoken(code), purpose="verify")
    except BaseException:
        await cancel_verification(user_id)
        raise


async def cancel_verification(user_id: int) -> None:
    from services.redis_client import get_redis

    redis = get_redis()
    await redis.delete(f"{VERIFY_PREFIX}{user_id}")
    await redis.delete(f"{ATTEMPTS_PREFIX}{user_id}")


async def pending_verification(user_id: int) -> str | None:
    from services.redis_client import get_redis

    raw = await get_redis().get(f"{VERIFY_PREFIX}{user_id}")
    return json.loads(raw)["number"] if raw else None


async def confirm_verification(user_id: int, code: str) -> str | None:
    """The confirmed E.164 number, or None (wrong code; after VERIFY_MAX_ATTEMPTS the attempt is void).

    The attempt is counted atomically (INCR) BEFORE the compare, so parallel guesses share the budget.
    """
    from services.redis_client import get_redis

    redis = get_redis()
    key = f"{VERIFY_PREFIX}{user_id}"
    raw = await redis.get(key)
    if raw is None:
        return None
    attempts_key = f"{ATTEMPTS_PREFIX}{user_id}"
    attempt = await redis.incr(attempts_key)
    if attempt == 1:
        await redis.expire(attempts_key, VERIFY_TTL_SECONDS)
    if attempt > VERIFY_MAX_ATTEMPTS:
        await cancel_verification(user_id)
        return None
    state = json.loads(raw)
    if hmac.compare_digest(state["code"], _code_hash(user_id, (code or "").strip())):
        await cancel_verification(user_id)
        return state["number"]
    if attempt >= VERIFY_MAX_ATTEMPTS:
        await cancel_verification(user_id)
    return None
