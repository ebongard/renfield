"""
WebSocket handler for Satellite Voice Assistants (/ws/satellite endpoint).

This module handles:
- Raspberry Pi satellite registration and management
- Wake word detection and audio streaming
- Speech-to-text transcription with speaker recognition
- Intent extraction and action execution
- TTS response generation and routing
- OTA update progress tracking
"""

import asyncio
import json
from datetime import date
from typing import NamedTuple

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger

from ha_glue.services.opus_transport import (
    BinaryFrameError,
    parse_audio_frame,
)
from models.websocket_messages import WSErrorCode
from services.database import AsyncSessionLocal
from services.turn_extraction import (
    TurnExtractionSpawn,
    spawn_memory_extraction,
    spawn_post_message_hooks,
)
from services.wakeword_config_manager import get_wakeword_config_manager
from services.websocket_auth import WSAuthError, authenticate_websocket
from services.websocket_rate_limiter import get_connection_limiter, get_rate_limiter
from utils.config import settings
from ha_glue.utils.config import ha_glue_settings

from api.websocket.shared import get_whisper_service, send_ws_error

router = APIRouter()


async def _route_satellite_tts_output(
    satellite_manager,
    satellite,
    session_id: str,
    tts_audio: bytes
):
    """
    Route TTS audio for satellite devices to the best available output device.

    Similar to _route_tts_output but for the satellite WebSocket handler.
    """
    # If satellite has no room_id, fallback to satellite itself
    if not satellite or not satellite.room_id:
        logger.debug("Satellite has no room_id, using satellite for output")
        await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)
        return

    try:
        from ha_glue.services.audio_output_service import get_audio_output_service
        from ha_glue.services.output_routing_service import OutputRoutingService

        async with AsyncSessionLocal() as db_session:
            routing_service = OutputRoutingService(db_session)
            audio_output_service = get_audio_output_service()

            # Get the best audio output device for this room
            decision = await routing_service.get_audio_output_for_room(
                room_id=satellite.room_id,
                input_device_id=satellite.satellite_id
            )

            logger.info(f"🔊 Satellite output routing: {decision.reason} → {decision.target_type}:{decision.target_id}")

            if decision.output_device and not decision.fallback_to_input:
                # Use configured output device
                success = await audio_output_service.play_audio(
                    audio_bytes=tts_audio,
                    output_device=decision.output_device,
                    session_id=session_id
                )

                if not success:
                    # Fallback to satellite if output failed
                    logger.warning("Output device playback failed, falling back to satellite")
                    await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)
            else:
                # Fallback to satellite
                await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)

    except Exception as e:
        logger.error(f"❌ Satellite output routing failed: {e}, falling back to satellite")
        import traceback
        logger.error(traceback.format_exc())
        # Fallback to satellite on error
        await satellite_manager.send_tts_audio(session_id, tts_audio, is_final=True)


def _build_assistant_metadata(
    intent: dict | None,
    action_result: dict | None,
) -> dict:
    """Shape for both in-memory satellite history and DB save.

    ``action_success`` lets ``agent_service._build_agent_prompt`` mark prior
    failed tool turns with ``[VORHERIGE_FEHLGESCHLAGENE_AKTION]`` so a stale
    error string cannot masquerade as current state on the next turn.
    See #430 (web-chat path) and #431 (this satellite path).

    Returns a fresh dict each call — callers may mutate/annotate the result
    without affecting other sites.
    """
    return {
        "intent": intent.get("intent") if intent else None,
        "action_success": action_result.get("success") if action_result else None,
    }


def _spawn_satellite_extraction(
    *,
    user_text: str,
    response_text: str,
    user_id: int | None,
    session_id: str | None,
    lang: str,
    action_success: bool | None,
    is_device_account: bool = False,
) -> TurnExtractionSpawn | None:
    """Feed a completed SPOKEN turn to the same extractors the browser chat uses.

    Until this existed, a satellite turn was transcribed, answered and persisted
    — and then forgotten: the handler ran neither the ``post_message`` hooks (KG
    + plugins) nor memory extraction, so anything said out loud never became
    knowledge, while the same sentence typed in the browser did.

    **Privacy boundary — extraction only for a RECOGNIZED speaker.** ``user_id``
    is the Speaker → User resolution (``User.speaker_id``); when it is None the
    voice in the room is unattributed, and an unattributed utterance must not
    become somebody's memory (nor reach the KG/plugins via ``post_message``).
    So: no user, no extraction at all — deliberately, not as an oversight.

    **The device account is not a person either.** Once an unrecognised turn
    runs as ``SATELLITE_DEVICE_ACCOUNT`` it HAS a ``user_id``, and that user_id
    alone would re-open exactly the door the paragraph above closes: every voice
    in the room writing into one account's memory. ``is_device_account`` keeps
    it shut — a device reads and acts, it never remembers (D-4b).

    **Persistence boundary — only a turn that was actually WRITTEN.**
    ``session_id`` is the satellite conversation's DB session (assigned right
    after registration); the message-persistence block at the call site is gated
    on exactly it. Extracting from a turn that was never persisted would produce
    memories with ``session_id=None``, attached to no conversation — so an
    unpersisted turn is skipped as well. Rare in practice, not impossible.

    Fire-and-forget: the work is scheduled, never awaited, so the turn's TTS is
    not delayed; every failure is swallowed so a broken extractor cannot break
    the spoken turn.
    """
    if user_id is None:
        logger.debug(
            "📝 Satellite-Extraktion übersprungen: Sprecher nicht erkannt "
            f"(session={session_id})"
        )
        return None
    if is_device_account:
        logger.debug(
            "📝 Satellite-Extraktion übersprungen: Gerätekonto sammelt keine "
            f"Erinnerungen (session={session_id})"
        )
        return None
    if not session_id:
        logger.debug(
            "📝 Satellite-Extraktion übersprungen: Turn nicht persistiert "
            f"(user_id={user_id})"
        )
        return None
    try:
        spawn = spawn_memory_extraction(
            user_message=user_text,
            assistant_response=response_text,
            user_id=user_id,
            session_id=session_id,
            lang=lang,
            action_success=action_success,
        )
        # When subsume coordination is active the spawned coroutine dispatches
        # post_message itself — firing it here too would run KG extraction twice.
        if not spawn.owns_post_message:
            spawn_post_message_hooks(
                user_msg=user_text,
                assistant_msg=response_text,
                user_id=user_id,
                session_id=session_id,
                lang=lang,
            )
        return spawn
    except Exception as e:  # noqa: BLE001 — extraction must never break the turn
        logger.warning(f"⚠️ Satellite-Extraktion konnte nicht gestartet werden: {e}")
        return None


# Limiter key for the second-look budget. '#' cannot occur in an IP, and a
# satellite_id colliding with it would only share a budget with itself.
_SECOND_LOOK_SUFFIX = "#second-look"


def _live_session_frame(raw: dict, satellite_id: str, manager) -> str | None:
    """Classify a frame the rate limiter just refused (#1284).

    Returns ``"audio"`` or ``"audio_end"`` when the frame belongs to a session
    that is live AND owned by this satellite, else ``None``.

    Only ever called on the refusal path of a REGISTERED satellite, so an
    unregistered flooder is still turned away without its frames being parsed.
    The frame is parsed a second time on the normal path — deliberate: that
    costs one extra parse on a rare path instead of parsing every frame before
    the limiter has seen it.
    """
    raw_bytes = raw.get("bytes")
    if raw_bytes is not None:
        try:
            session_id, _, _ = parse_audio_frame(raw_bytes)
        except BinaryFrameError:
            return None
        kind = "audio"
    else:
        try:
            data = json.loads(raw.get("text") or "")
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("type") not in ("audio", "audio_end"):
            return None
        session_id = data.get("session_id")
        kind = data["type"]

    # isinstance: a hostile frame may carry an unhashable session_id.
    session = manager.sessions.get(session_id) if isinstance(session_id, str) else None
    if session is None or session.satellite_id != satellite_id:
        return None
    return kind


def _rate_verdict(raw: dict, rate_key: str, satellite_id: str | None, rate_limiter, manager) -> tuple[bool, str]:
    """Rate-limit one inbound frame; a refused frame of a LIVE turn gets a second look.

    A refused frame must not cost a running turn (#1284). Audio legitimately
    arrives in bursts — a Pi whose event loop stalled flushes its queued chunks
    at once — while its sustained rate is physically bounded (12.5 chunks/s), so
    it answers to the minute budget only. `audio_end` always passes: it is at
    most one per session, and dropping it strands the session in `listening`
    until the cleanup sweep discards the recording unanswered.
    """
    allowed, reason = rate_limiter.check(rate_key, record_violation=False)
    if allowed:
        return True, ""

    # The second look parses the frame, so it has a budget of its own: without
    # one, refused frames would be parsed without limit (a registered client
    # could buy a JSON parse of a ~1 MB frame per message). A legitimate turn
    # never gets near it — a satellite sends ~775 frames a minute in total.
    if satellite_id and rate_limiter.check(
        f"{rate_key}{_SECOND_LOOK_SUFFIX}", burst_ok=True, record_violation=False
    )[0]:
        frame_kind = _live_session_frame(raw, satellite_id, manager)
        if frame_kind == "audio_end":
            return True, ""
        if frame_kind == "audio":
            allowed, reason = rate_limiter.check(rate_key, burst_ok=True, record_violation=False)
            if allowed:
                return True, ""

    rate_limiter.record_violation(rate_key, reason)
    return False, reason


async def _reject_derostered_heartbeat(websocket, satellite_id: str, manager) -> bool:
    """Close a heartbeat connection whose satellite is no longer in the roster.

    Defence in depth behind `cleanup_stale`'s own close(). Without it the receive
    loop keeps answering `heartbeat_ack` after an eviction while
    `update_heartbeat` silently no-ops, so the device sees a healthy link, never
    re-registers (it only registers on connect) and is mute FOREVER.

    Returns True when the connection was closed and the receive loop must stop.
    A failing close is swallowed — the connection is being torn down either way,
    and raising here would kill the loop's own cleanup.
    """
    if manager.is_connected(satellite_id):
        return False
    logger.warning(
        f"🔌 Heartbeat von nicht-registriertem Satelliten {satellite_id} "
        "— Verbindung wird geschlossen, damit er sich neu anmeldet"
    )
    try:
        await websocket.close(code=1001, reason="re-register required")
    except Exception:  # noqa: BLE001
        pass
    return True


_anon_perms_warned = False


def _warn_once_on_unknown_grants(grants: list[str]) -> None:
    """Log unknown grant names ONCE. A typo (`ha.controll`, `mcp.homeasistant`)
    is otherwise a silent house-wide denial: the gate just says "permission
    denied" for a grant nobody ever had. Non-``mcp.`` entries must be real
    ``Permission`` values; ``mcp.<server>`` is a convention grant whose server
    name only exists once MCP is up, so it is checked for shape only."""
    global _anon_perms_warned
    if _anon_perms_warned:
        return
    _anon_perms_warned = True
    from models.permissions import Permission

    known = {p.value for p in Permission}
    unknown = [
        g for g in grants
        if not (g in known or (g.startswith("mcp.") and len(g) > 4))
    ]
    if unknown:
        logger.warning(
            f"⚠️ SATELLITE_ANONYMOUS_PERMISSIONS nennt unbekannte Rechte {unknown} — "
            f"sie greifen nie und wirken wie eine stille Verweigerung"
        )


def anonymous_permissions() -> list[str] | None:
    """The grant list an UNRECOGNISED satellite voice runs with, or None.

    None means "no permission model in effect" — the #690 fail-open every MCP
    and internal-tool gate honours so spoken commands work. That is right for a
    single-trust-domain household (`AUTH_ENABLED=false`) and wrong once auth is
    on, where it would hand any voice in the house every tool the agent has.

    Returns None (today's behaviour, byte-identical) when auth is off or the
    setting is empty; otherwise the parsed list. An empty LIST would deny
    everything, so a whitespace-only setting is treated as unset rather than as
    a lockout of the house.
    """
    if not settings.auth_enabled:
        return None
    grants = [p.strip() for p in settings.satellite_anonymous_permissions.split(",")]
    grants = [p for p in grants if p]
    if not grants:
        return None
    _warn_once_on_unknown_grants(grants)
    return grants


class AnonymousIdentity(NamedTuple):
    """Who an unrecognised satellite voice IS for one turn, and what it may do."""

    user_id: int | None
    permissions: list[str] | None
    is_device_account: bool


_device_account_warned = False


def _warn_once_about_device_account(reason: str) -> None:
    global _device_account_warned
    if _device_account_warned:
        return
    _device_account_warned = True
    logger.warning(
        f"⚠️ SATELLITE_DEVICE_ACCOUNT={settings.satellite_device_account!r}: {reason} — "
        f"unerkannte Stimmen werden bis zur Korrektur abgelehnt"
    )


async def _load_device_account() -> tuple[int, list[str]] | None:
    """The configured device account as (user_id, permissions), or None."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from models.database import User

    username = settings.satellite_device_account.strip()
    try:
        # `role` EAGER and everything inside the session: get_permissions()
        # reads that relationship, and an async session raises MissingGreenlet
        # on a lazy load — which the except below would turn into a silent
        # total denial of every anonymous turn.
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User)
                .options(selectinload(User.role))
                .where(User.username == username)
            )
            usr = result.scalar_one_or_none()
            if usr is None:
                _warn_once_about_device_account("kein Nutzer mit diesem Namen")
                return None
            if not usr.is_device_account:
                # A person's account is never borrowed by a device: without the
                # flag the room would write into that person's memory and
                # presence.
                _warn_once_about_device_account("der Nutzer ist kein Gerätekonto")
                return None
            # Re-arm the warning: a corrected setting (or a restored account)
            # must be able to warn again if it breaks a second time.
            global _device_account_warned
            _device_account_warned = False
            return usr.id, usr.get_permissions()
    except Exception as e:  # noqa: BLE001 — a DB hiccup must not fail open
        logger.warning(f"⚠️ Gerätekonto konnte nicht geladen werden: {e}")
        return None


async def room_history_owner_id() -> int | None:
    """Who OWNS a room history: the device account, or nobody (auth-on §8.1).

    Not the speaker, and deliberately not ``sat_user_id``. §8.1 rules out "the
    first recognised speaker" by name: that is adoption through the back door,
    and it would hand one member of the household — anybody holding `chat.own` —
    the right to delete the room's shared thread (``ConversationService.may_alter``
    answers to the owner). The recognised speaker still drives permissions,
    presence and per-turn memory extraction; ownership of the THREAD is a
    separate question with a separate answer.

    ``None`` when auth is off (one trust domain, nothing to own) or when no
    device account is configured. The caller must then NOT stamp tier 2: an
    ownerless row at tier 2 reaches nobody — every branch of the circle filter
    keys on the owner — so the shared thread would exist and be invisible to
    everyone, silently.
    """
    if not settings.auth_enabled:
        return None
    if not settings.satellite_device_account.strip():
        return None
    account = await _load_device_account()
    return account[0] if account else None


async def resolve_anonymous_identity() -> AnonymousIdentity:
    """Identity and grants for a turn whose speaker was not recognised (D-4a/D-4b).

    Three outcomes, in order:

    * **auth off** → ``(None, None, False)``: today's behaviour, byte-identical.
    * **a device account is configured** → that account's id and its role's
      permissions. The turn now HAS an identity: it reads as far as the device's
      circle memberships reach and acts within its role — which is why the role
      IS the grant set here and ``SATELLITE_ANONYMOUS_PERMISSIONS`` is only
      consulted on the no-device-account path. What the flag buys is the other
      half: a device must never accumulate a person's traces (no extraction, no
      presence — the gates read ``is_device_account``).
    * **no device account** → the anonymous grant list (or None when unset).

    A CONFIGURED but unresolvable device account denies (`[]`) instead of
    falling back: naming an account is an explicit statement about who an
    anonymous turn is, and a typo in it must be loud (a spoken refusal plus a
    warning), never a quiet widening.
    """
    if not settings.auth_enabled:
        return AnonymousIdentity(None, None, False)
    if not settings.satellite_device_account.strip():
        return AnonymousIdentity(None, anonymous_permissions(), False)
    account = await _load_device_account()
    if account is None:
        return AnonymousIdentity(None, [], False)
    return AnonymousIdentity(account[0], account[1], True)


def _handshake_identity_mismatch(auth_result: dict | None, satellite_id: str) -> bool:
    """True when the WS handshake authenticated a specific satellite (PSK
    strategy, ``auth_result["satellite_id"]``) and the register frame names a
    DIFFERENT one. A connection without a handshake identity (JWT, device
    token, auth-off) is never a mismatch — binding only applies where the
    handshake established an identity to bind to."""
    handshake_sat = (auth_result or {}).get("satellite_id")
    return handshake_sat is not None and satellite_id != handshake_sat


def _handshake_satisfies_enrollment(auth_result: dict | None, register_token: str | None) -> bool:
    """A PSK-authenticated handshake already proved THIS satellite's enrollment
    secret on THIS connection (identity bound by `_handshake_identity_mismatch`),
    so a register frame that carries NO token is accepted as enrolled — the
    operator provisions the secret once, not twice. A token that IS presented is
    still verified by the register gate (a wrong one rejects; divergence between
    the two config fields must stay loud, never tolerated)."""
    return (
        (auth_result or {}).get("auth_method") == "satellite_psk"
        and not register_token
    )


@router.websocket("/ws/satellite")
async def satellite_websocket(
    websocket: WebSocket,
    token: str = Query(None, description="Authentication token")
):
    """
    WebSocket endpoint for Raspberry Pi satellite voice assistants.

    Protocol v1.0:
    Satellite → Server:
        - {"type": "register", "satellite_id": str, "room": str, "capabilities": {...}}
        - {"type": "wakeword_detected", "keyword": str, "confidence": float, "session_id": str}
        - {"type": "audio", "chunk": str (base64), "sequence": int, "session_id": str}
        - {"type": "audio_end", "session_id": str, "reason": str}
        - {"type": "heartbeat", "status": str, "uptime_seconds": int}

    Server → Satellite:
        - {"type": "register_ack", "success": bool, "config": {...}, "protocol_version": str}
        - {"type": "state", "state": "idle|listening|processing|speaking"}
        - {"type": "transcription", "session_id": str, "text": str}
        - {"type": "action", "session_id": str, "intent": {...}, "success": bool}
        - {"type": "tts_audio", "session_id": str, "audio": str (base64), "is_final": bool}
        - {"type": "error", "code": str, "message": str}
    """
    # Extract client info
    ip_address = websocket.client.host if websocket.client else "unknown"

    # Check authentication if enabled
    # Connection limits FIRST: the PSK handshake below costs one bcrypt per
    # attempt (off the loop, but still CPU), so a flood must be refused before
    # any hashing — can_connect is a pure check, nothing is registered yet.
    connection_limiter = get_connection_limiter()
    can_connect, reason = connection_limiter.can_connect(ip_address, f"sat-pending-{ip_address}")
    if not can_connect:
        await websocket.close(code=4003, reason=reason)
        return

    # The ONLY endpoint that accepts the per-satellite enrollment PSK as the
    # handshake credential (D-4c) — every other WS endpoint refuses `sat.` tokens.
    auth_result = await authenticate_websocket(websocket, token, allow_satellite_psk=True)
    if not auth_result:
        await websocket.close(code=WSAuthError.UNAUTHORIZED, reason="Authentication required")
        return

    await websocket.accept()
    logger.info(f"📡 Satellite WebSocket connection established (IP: {ip_address})")

    # Access app state through websocket.app
    app = websocket.app

    from ha_glue.services.satellite_manager import SatelliteState, get_satellite_manager
    satellite_manager = get_satellite_manager()
    rate_limiter = get_rate_limiter()

    satellite_id = None

    # C1/C2 binary Opus transport. Set True at register when the satellite
    # requested opus AND the backend accepted it; then binary frames buffer
    # raw packets (backend does NOT decode — forwarded to the voice-server at
    # audio_end, design D6). A legacy JSON/PCM satellite leaves this False.
    opus_mode = False

    # Conversation history tracking for satellite (in-memory per connection)
    satellite_conversation_history: list[dict] = []
    satellite_history_loaded = False
    satellite_db_session_id = None  # Will be set after registration

    try:
        while True:
            raw = await websocket.receive()
            if raw.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(raw.get("code") or 1000)

            # Rate limiting (applies to text AND binary frames)
            rate_key = satellite_id if satellite_id else ip_address
            allowed, rate_reason = _rate_verdict(
                raw, rate_key, satellite_id, rate_limiter, satellite_manager
            )
            if not allowed:
                await send_ws_error(websocket, WSErrorCode.RATE_LIMITED, rate_reason)
                continue

            # C1/C2: binary audio frame (Opus). The backend does NOT decode —
            # it buffers the raw packets and forwards them to the voice-server
            # at audio_end, where decode belongs (media layer, design D6).
            raw_bytes = raw.get("bytes")
            if raw_bytes is not None:
                if not opus_mode:
                    await send_ws_error(
                        websocket, WSErrorCode.PROTOCOL_ERROR,
                        "binary audio frames were not negotiated (register with audio_codec=opus)",
                    )
                    continue
                if len(raw_bytes) > settings.ws_max_message_size:
                    await send_ws_error(
                        websocket, WSErrorCode.MESSAGE_TOO_LARGE,
                        f"Binary frame too large (max: {settings.ws_max_message_size} bytes)",
                    )
                    continue
                try:
                    bin_session_id, bin_sequence, opus_packets = parse_audio_frame(raw_bytes)
                except BinaryFrameError as e:
                    await send_ws_error(websocket, WSErrorCode.PROTOCOL_ERROR, str(e))
                    continue

                if not satellite_manager.has_session(bin_session_id):
                    await send_ws_error(
                        websocket, WSErrorCode.SESSION_ERROR,
                        f"unknown session {bin_session_id}",
                    )
                    continue

                success, error = satellite_manager.buffer_opus_packets(
                    bin_session_id, opus_packets, bin_sequence
                )
                if not success:
                    if "buffer full" in error.lower():
                        await satellite_manager.end_session(bin_session_id, reason="buffer_full")
                        await send_ws_error(websocket, WSErrorCode.BUFFER_FULL, error)
                    else:
                        await send_ws_error(websocket, WSErrorCode.SESSION_ERROR, error)
                continue

            # Text frame. A malformed or non-object frame is treated exactly as
            # the legacy receive_json() path did — it tore the connection down,
            # which is the satellite's self-heal (reconnect → fresh register +
            # config/IRK re-push). Keeping that means the flag-off path stays
            # byte-identical in behavior.
            text = raw.get("text")
            try:
                data = json.loads(text) if text is not None else None
            except json.JSONDecodeError:
                logger.warning(f"⚠️ Malformed JSON frame from {satellite_id or ip_address} — closing")
                break
            if not isinstance(data, dict):
                logger.warning(f"⚠️ Non-object frame from {satellite_id or ip_address} — closing")
                break

            msg_type = data.get("type", "")

            # Handle registration
            if msg_type == "register":
                satellite_id = data.get("satellite_id", "unknown")
                room = data.get("room", "Unknown Room")
                capabilities = data.get("capabilities", {})
                language = data.get("language", settings.default_language)
                version = data.get("version", "unknown")
                enrollment_psk = data.get("token")

                # Per-satellite enrollment gate (security review H1). Verify the
                # enrollment PSK against the `satellites` table. When enrollment is
                # disabled (default) this whole block is skipped, so the legacy
                # register path is byte-identical.
                #
                # FAIL CLOSED: if the authorization check raises (e.g. the DB is
                # unreachable / the pool is exhausted), we cannot prove this
                # connection is authorized AND cannot even read whether the fleet
                # is enforcing — so we REJECT rather than admit an unauthenticated
                # satellite. A fail-open here would let an attacker bypass
                # ENFORCING by inducing a DB error (review finding). Transient
                # blips are recoverable: the satellite's reconnect loop retries.
                # Handshake identity binding (household auth-on cutover, D-4c):
                # a connection authenticated with the per-satellite PSK at the
                # WS handshake carries that satellite_id; the register frame
                # must name the SAME id, else a device could authenticate as
                # itself and then register as another satellite. Checked BEFORE
                # the enrollment bcrypt below — cheap check first.
                if _handshake_identity_mismatch(auth_result, satellite_id):
                    logger.warning(
                        f"🚫 Satellite register rejected: handshake identity "
                        f"'{auth_result.get('satellite_id')}' but register frame claims '{satellite_id}'"
                    )
                    await send_ws_error(websocket, WSErrorCode.UNAUTHORIZED, "identity-mismatch")
                    await websocket.close(
                        code=WSAuthError.UNAUTHORIZED, reason="identity-mismatch"
                    )
                    return

                satellite_authenticated = False
                if settings.satellite_enrollment_enabled:
                    reject_reason: str | None = None
                    try:
                        from ha_glue.services.satellite_enrollment_service import (
                            authorize_register,
                            maybe_autoflip,
                        )

                        if _handshake_satisfies_enrollment(auth_result, enrollment_psk):
                            # Proven at the handshake on this very connection;
                            # last_authenticated_at was stamped there, so the
                            # auto-flip latch stays coherent.
                            async with AsyncSessionLocal() as enroll_db:
                                await maybe_autoflip(enroll_db)
                            satellite_authenticated = True
                        else:
                            async with AsyncSessionLocal() as enroll_db:
                                authz = await authorize_register(enroll_db, satellite_id, enrollment_psk)
                            satellite_authenticated = authz.authenticated
                            if authz.reject:
                                reject_reason = authz.reason
                    except Exception as e:
                        logger.error(f"⚠️ Satellite enrollment check errored (fail-closed): {e}")
                        satellite_authenticated = False
                        reject_reason = "enrollment-unavailable"
                    if reject_reason:
                        logger.warning(
                            f"🚫 Satellite register rejected for '{satellite_id}': {reject_reason}"
                        )
                        await send_ws_error(websocket, WSErrorCode.UNAUTHORIZED, reject_reason)
                        await websocket.close(
                            code=WSAuthError.UNAUTHORIZED, reason=reject_reason
                        )
                        return

                # C1 codec negotiation: the satellite advertises audio_codec
                # in its capabilities; the backend accepts opus only when the
                # fleet flag is on AND libopus is importable, else answers
                # pcm and the satellite keeps the legacy JSON path. A legacy
                # satellite (no audio_codec key) is untouched.
                negotiated_codec = "pcm"
                requested_codec = str(capabilities.get("audio_codec") or "pcm").lower()
                if requested_codec == "opus":
                    # Decode happens on the voice-server (design D6), so opus
                    # requires a voice-server to be configured — not backend
                    # opuslib. Without one, the in-process whisper path can't
                    # decode opus, so downgrade to pcm.
                    if settings.satellite_opus_enabled and settings.voice_server_url:
                        negotiated_codec = "opus"
                        opus_mode = True
                    else:
                        reason = (
                            "satellite_opus_enabled is off" if not settings.satellite_opus_enabled
                            else "no voice_server_url configured (opus decodes on the voice-server)"
                        )
                        logger.info(
                            f"🎛️ Satellite '{satellite_id}' requested opus → downgraded to pcm ({reason})"
                        )

                # Update connection limiter with actual satellite_id
                connection_limiter.add_connection(ip_address, satellite_id)

                success = await satellite_manager.register(
                    satellite_id=satellite_id,
                    room=room,
                    websocket=websocket,
                    capabilities=capabilities,
                    language=language,
                    version=version,
                    authenticated=satellite_authenticated,
                )
                if not success:
                    # register() refused (e.g. an unauthenticated connection
                    # tried to evict an enrolled incumbent). Close out.
                    logger.warning(f"🚫 Satellite registration refused for '{satellite_id}'")
                    await send_ws_error(
                        websocket, WSErrorCode.UNAUTHORIZED, "registration-refused"
                    )
                    await websocket.close(
                        code=WSAuthError.UNAUTHORIZED, reason="registration-refused"
                    )
                    return

                # Persist room assignment to database
                room_id = None
                if success and ha_glue_settings.rooms_auto_create_from_satellite:
                    try:
                        from ha_glue.services.room_service import RoomService

                        async with AsyncSessionLocal() as db_session:
                            room_service = RoomService(db_session)
                            db_room = await room_service.get_or_create_room_for_satellite(
                                satellite_id=satellite_id,
                                room_name=room,
                                auto_create=True
                            )
                            if db_room:
                                room_id = db_room.id
                                satellite_manager.set_room_id(satellite_id, room_id)
                                logger.info(f"📍 Satellite {satellite_id} linked to room '{db_room.name}' (id: {room_id})")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to persist room for satellite: {e}")

                # Generate daily DB session ID for conversation persistence
                satellite_db_session_id = f"satellite-{satellite_id}-{date.today().isoformat()}"
                logger.info(f"📚 Satellite DB session: {satellite_db_session_id}")

                # Load wake word config from config manager
                wakeword_config_manager = get_wakeword_config_manager()
                async with AsyncSessionLocal() as db_session:
                    wakeword_config = await wakeword_config_manager.get_config(db_session)

                # Subscribe to config updates with device info for tracking
                wakeword_config_manager.subscribe(
                    websocket=websocket,
                    device_id=satellite_id,
                    device_type="satellite"
                )

                # Ride the current target LED brightness in the ack so a
                # satellite reconnecting mid-night comes up already dimmed.
                from ha_glue.services.led_dimming_service import get_led_dimming_service

                register_ack = {
                    "type": "register_ack",
                    "success": success,
                    "config": wakeword_config.to_satellite_config(),
                    "room_id": room_id,
                    "protocol_version": settings.ws_protocol_version,
                    "model_download_url": "/api/settings/wakeword/models",
                    "led_brightness": get_led_dimming_service().get_current_led_brightness(),
                }
                # Only satellites that advertised a codec get the negotiation
                # answer — a legacy register keeps a byte-identical ack.
                if "audio_codec" in capabilities:
                    register_ack["audio_codec"] = negotiated_codec
                await websocket.send_json(register_ack)
                logger.info(f"📡 Satellite {satellite_id} registered from {room}")

                # Push known BLE + Classic BT MACs to satellite for presence scanning
                if ha_glue_settings.presence_enabled:
                    try:
                        from ha_glue.services.presence_service import get_presence_service
                        presence_svc = get_presence_service()
                        ble_macs = presence_svc.get_ble_macs()
                        if ble_macs:
                            await websocket.send_json({
                                "type": "ble_known_devices",
                                "devices": list(ble_macs),
                            })
                        classic_macs = presence_svc.get_classic_bt_macs()
                        if classic_macs:
                            await websocket.send_json({
                                "type": "classic_bt_known_devices",
                                "devices": list(classic_macs),
                            })
                        # Per-person IRKs for resolving rotating RPAs (iPhones).
                        # IRKs are location-tracking keys; gated per satellite
                        # (review H1). When enrollment is on, only a satellite
                        # that presented a valid PSK gets them; otherwise the
                        # legacy allowlist applies.
                        irks = presence_svc.irks_for_satellite(
                            satellite_id, is_enrolled_authenticated=satellite_authenticated
                        )
                        if irks:
                            await websocket.send_json({
                                "type": "ble_known_irks",
                                "irks": irks,
                            })
                        total = len(ble_macs) + len(classic_macs)
                        if total or irks:
                            logger.debug(f"Pushed {len(ble_macs)} BLE + {len(classic_macs)} Classic BT MACs "
                                         f"+ {len(irks)} IRK(s) to {satellite_id}")
                    except Exception as e:
                        logger.warning(f"Failed to push MACs/IRKs: {e}")

            # Handle config acknowledgment from satellite
            elif msg_type == "config_ack":
                ack_success = data.get("success", False)
                active_keywords = data.get("active_keywords", [])
                failed_keywords = data.get("failed_keywords", [])
                ack_error = data.get("error")

                wakeword_config_manager = get_wakeword_config_manager()
                wakeword_config_manager.handle_config_ack(
                    device_id=satellite_id,
                    success=ack_success,
                    active_keywords=active_keywords,
                    failed_keywords=failed_keywords,
                    error=ack_error,
                )

            # Handle wake word detection
            elif msg_type == "wakeword_detected":
                keyword = data.get("keyword", "unknown")
                confidence = data.get("confidence", 0.0)
                sat_id = data.get("satellite_id", satellite_id)
                # Use the session_id provided by the satellite (important for matching audio chunks)
                client_session_id = data.get("session_id")

                session_id = await satellite_manager.start_session(
                    satellite_id=sat_id,
                    keyword=keyword,
                    confidence=confidence,
                    session_id=client_session_id  # Use satellite's session ID
                )

                if session_id:
                    logger.info(f"🎙️ Wake word '{keyword}' detected by {sat_id}, session: {session_id}")
                else:
                    logger.warning(f"⚠️ Could not start session for {sat_id}")

            # Handle audio chunks
            elif msg_type == "audio":
                session_id = data.get("session_id")
                chunk_b64 = data.get("chunk", "")
                sequence = data.get("sequence", 0)

                if session_id and chunk_b64:
                    success, error = satellite_manager.buffer_audio(session_id, chunk_b64, sequence)
                    if not success:
                        # End session on buffer full to prevent further errors
                        if "buffer full" in error.lower():
                            await satellite_manager.end_session(session_id, reason="buffer_full")
                        await send_ws_error(websocket, WSErrorCode.BUFFER_FULL, error)

            # Handle end of audio
            elif msg_type == "audio_end":
                session_id = data.get("session_id")
                reason = data.get("reason", "unknown")
                image_b64 = data.get("image")  # Optional camera snapshot for visual queries

                if not session_id:
                    continue

                if image_b64:
                    logger.info(f"📸 Image received with audio_end ({len(image_b64)} chars base64)")
                logger.info(f"🔚 Audio ended for session {session_id} (reason: {reason})")

                # Update state to processing
                await satellite_manager.set_session_state(session_id, SatelliteState.PROCESSING)

                # Pull the buffered audio. Backend no longer decodes opus (moved
                # to the voice-server, design D6): an opus session buffered raw
                # packets → forward the packet blob; else it's legacy PCM.
                opus_blob = satellite_manager.get_opus_blob(session_id) if opus_mode else None
                audio_bytes = None if opus_blob is not None else satellite_manager.get_audio_buffer(session_id)

                if opus_blob is None and not audio_bytes:
                    logger.warning(f"⚠️ No audio buffered for session {session_id}")
                    await satellite_manager.end_session(session_id, reason="no_audio")
                    continue

                _size = len(opus_blob) if opus_blob is not None else len(audio_bytes)
                _kind = "opus (decode on voice-server)" if opus_blob is not None else "pcm"
                logger.info(f"🎵 Processing {_size} bytes of {_kind}")

                # Get satellite's configured language + room context
                satellite_info = satellite_manager.get_satellite_by_session(session_id)
                satellite_language = satellite_info.language if satellite_info else settings.default_language
                satellite_room_id = satellite_info.room_id if satellite_info else None
                logger.info(f"🌐 Using language: {satellite_language}")

                # Transcribe with Whisper (with speaker recognition)
                try:
                    whisper = get_whisper_service()
                    await asyncio.to_thread(whisper.load_model)  # No-op if already loaded

                    # Prepare the STT payload. Opus → forward the raw packet blob
                    # to the voice-server's /stt-opus (decode there). PCM → wrap
                    # in a WAV for the /stt (ffmpeg) path. Both yield the same
                    # {text, speaker_embedding} downstream.
                    if opus_blob is not None:
                        stt_audio = opus_blob
                        stt_filename = "satellite_audio.opus"
                        stt_is_opus = True
                    else:
                        import io
                        import wave
                        wav_buffer = io.BytesIO()
                        with wave.open(wav_buffer, 'wb') as wav_file:
                            wav_file.setnchannels(1)
                            wav_file.setsampwidth(2)  # 16-bit
                            wav_file.setframerate(16000)
                            wav_file.writeframes(audio_bytes)
                        stt_audio = wav_buffer.getvalue()
                        stt_filename = "satellite_audio.wav"
                        stt_is_opus = False
                        logger.info(f"📦 Created WAV: {len(stt_audio)} bytes")

                    # Transcribe with speaker recognition (if enabled)
                    speaker_name = None
                    speaker_alias = None
                    speaker_confidence = 0.0

                    # Build the per-request STT bias prompt. Caller-side context:
                    # the satellite's registered room_id (so the prompt is biased
                    # toward that room's name + occupants). If presence is healthy
                    # and someone is currently in this room, seed the speaker name
                    # too — this is the first-utterance bias path discussed in the
                    # B-3 plan since speaker recognition has not run yet.
                    from services.whisper_prompt_builder import (
                        get_whisper_prompt_builder,
                        resolve_first_speaker_from_room,
                    )

                    prompt_builder = get_whisper_prompt_builder()
                    initial_user_id = await resolve_first_speaker_from_room(room_id=satellite_room_id)

                    if settings.speaker_recognition_enabled:
                        async with AsyncSessionLocal() as db_session:
                            initial_prompt = await prompt_builder.build(
                                user_id=initial_user_id,
                                room_id=satellite_room_id,
                                language=satellite_language,
                                db_session=db_session,
                            )
                            result = await whisper.transcribe_bytes_with_speaker(
                                stt_audio,
                                filename=stt_filename,
                                db_session=db_session,
                                language=satellite_language,
                                initial_prompt=initial_prompt,
                                is_opus=stt_is_opus,
                            )
                            text = result.get("text", "")
                            speaker_name = result.get("speaker_name")
                            speaker_alias = result.get("speaker_alias")
                            speaker_confidence = result.get("speaker_confidence", 0.0)

                            if speaker_name:
                                logger.info(f"🎤 Satellite Sprecher erkannt: {speaker_name} (@{speaker_alias}) - Konfidenz: {speaker_confidence:.2f}")
                            else:
                                logger.info("🎤 Satellite Sprecher nicht erkannt")
                    else:
                        async with AsyncSessionLocal() as db_session:
                            initial_prompt = await prompt_builder.build(
                                user_id=initial_user_id,
                                room_id=satellite_room_id,
                                language=satellite_language,
                                db_session=db_session,
                            )
                        text = await whisper.transcribe_bytes(
                            stt_audio,
                            stt_filename,
                            language=satellite_language,
                            initial_prompt=initial_prompt,
                            is_opus=stt_is_opus,
                        )

                    if not text or not text.strip():
                        logger.warning(f"⚠️ Empty transcription for session {session_id}")
                        await satellite_manager.end_session(session_id, reason="empty_transcription")
                        continue

                    logger.info(f"📝 Transcription: '{text}'")
                    await satellite_manager.send_transcription(session_id, text)

                except Exception as e:
                    logger.error(f"❌ Whisper transcription failed: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                    await satellite_manager.end_session(session_id, reason="transcription_error")
                    continue

                # Process with Ollama (intent extraction + action)
                try:
                    ollama = app.state.ollama

                    # Resolve Speaker DB object for handoff + association
                    spk = None
                    if speaker_name:
                        try:
                            from sqlalchemy import select

                            from models.database import Speaker

                            async with AsyncSessionLocal() as _spk_db:
                                _spk_r = await _spk_db.execute(
                                    select(Speaker).where(Speaker.name == speaker_name)
                                )
                                spk = _spk_r.scalar_one_or_none()
                        except Exception as e:
                            logger.warning(f"⚠️ Speaker lookup for handoff failed: {e}")

                    # Conversation handoff: copy context from another satellite if speaker moved
                    if spk and satellite_db_session_id and not satellite_history_loaded:
                        try:
                            from services.conversation_handoff import (
                                emit_continued_handoff_frame,
                                try_handoff_context,
                            )
                            async with AsyncSessionLocal() as handoff_db:
                                handed_off = await try_handoff_context(
                                    spk.id, satellite_db_session_id, handoff_db
                                )
                                if handed_off:
                                    logger.info(f"🔄 Conversation handoff for speaker {speaker_name} to {satellite_db_session_id}")
                                    # Surface the "continued in {room}" chip in this room
                                    # (the speak-path usually wins the debounce on move-and-talk).
                                    _sat = satellite_manager.get_satellite(satellite_id)
                                    await emit_continued_handoff_frame(_sat.room if _sat else None)
                        except Exception as e:
                            logger.warning(f"⚠️ Conversation handoff failed: {e}")

                    # Load conversation history from DB if not already loaded (once per day)
                    if satellite_db_session_id and not satellite_history_loaded:
                        try:
                            async with AsyncSessionLocal() as db_session:
                                db_history = await ollama.load_conversation_context(
                                    satellite_db_session_id, db_session, max_messages=5
                                )
                                if db_history:
                                    satellite_conversation_history.extend(db_history)
                                    logger.info(f"📚 Satellite conversation history loaded: {len(db_history)} messages")
                                satellite_history_loaded = True
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to load satellite conversation history: {e}")

                    # Build room context for satellite
                    satellite = satellite_manager.get_satellite(satellite_id)
                    room_context = None
                    if satellite:
                        room_context = {
                            "room_name": satellite.room,
                            "room_id": satellite.room_id,
                            "device_type": "satellite",
                        }
                        if speaker_name:
                            room_context["speaker_name"] = speaker_name
                        if speaker_alias:
                            room_context["speaker_alias"] = speaker_alias

                        logger.info(f"🏠 Satellite room context: {satellite.room} (ID: {satellite.room_id})")

                    # Extract ranked intents with room context and conversation history
                    ranked_intents = await ollama.extract_ranked_intents(
                        text,
                        room_context=room_context,
                        conversation_history=satellite_conversation_history if satellite_conversation_history else None
                    )

                    # Fallback chain: try intents until one works
                    from services.action_executor import ActionExecutor
                    mcp_mgr = getattr(websocket.app.state, 'mcp_manager', None)
                    action_result = None
                    intent = None

                    # Load user permissions from speaker recognition
                    sat_user_permissions = None
                    sat_user_id = None
                    sat_is_device_account = False
                    if speaker_name and (settings.auth_enabled or ha_glue_settings.presence_enabled):
                        try:
                            from sqlalchemy import select
                            from sqlalchemy.orm import selectinload

                            from models.database import Speaker, User
                            async with AsyncSessionLocal() as perm_db:
                                spk_result = await perm_db.execute(
                                    select(Speaker).where(Speaker.name == speaker_name)
                                )
                                spk = spk_result.scalar_one_or_none()
                                if spk:
                                    # Speaker → User via User.speaker_id FK.
                                    # `role` EAGER: get_permissions() reads that
                                    # relationship, and an async session raises
                                    # MissingGreenlet on a lazy load — the
                                    # except below would swallow it and leave
                                    # the recognised speaker without id or
                                    # permissions (no presence, no extraction).
                                    usr_result = await perm_db.execute(
                                        select(User)
                                        .options(selectinload(User.role))
                                        .where(User.speaker_id == spk.id)
                                    )
                                    usr = usr_result.scalar_one_or_none()
                                    if usr:
                                        sat_user_permissions = usr.get_permissions()
                                        sat_user_id = usr.id
                                        # The flag is the contract wherever the
                                        # identity comes from: linking a speaker
                                        # to a device account must not smuggle
                                        # extraction and presence back in.
                                        sat_is_device_account = bool(
                                            usr.is_device_account
                                        )
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to load satellite user permissions: {e}")
                            if settings.auth_enabled:
                                # Recognised but unresolvable → deny, never fall
                                # back to the anonymous set (mirrors chat.py:137).
                                sat_user_permissions = []

                    # No recognised speaker (or none linked to an account): the
                    # turn runs as the device account if one is configured, else
                    # with the anonymous grant list — instead of the fail-open
                    # None. Nothing changes while auth is off; see
                    # resolve_anonymous_identity().
                    if sat_user_permissions is None:
                        anon = await resolve_anonymous_identity()
                        sat_user_id = anon.user_id
                        sat_user_permissions = anon.permissions
                        sat_is_device_account = anon.is_device_account
                        if sat_is_device_account:
                            logger.info(
                                f"🔐 Satelliten-Zug als Gerätekonto "
                                f"(user_id={sat_user_id}, "
                                f"{len(sat_user_permissions or [])} Rechte)"
                            )
                        elif sat_user_permissions is not None:
                            logger.info(
                                f"🔐 Anonymer Satelliten-Zug mit "
                                f"{len(sat_user_permissions)} Rechten"
                            )

                    # Who owns the ROOM history (§8.1): the device account, or
                    # nobody. Resolved once here because two places assign it —
                    # this association and the message save at the end of the
                    # turn — and they must not disagree. Never `sat_user_id`:
                    # that is the recognised PERSON, and handing them the thread
                    # is the adoption §8.1 rules out by name.
                    room_owner_id = await room_history_owner_id()
                    room_tier = 2 if room_owner_id is not None else 0

                    # Associate conversation with speaker (for handoff lookup).
                    # The SPEAKER is the recognised person; the OWNER is the
                    # device account. Two different questions, two values.
                    if spk and satellite_db_session_id:
                        try:
                            from services.conversation_service import ConversationService
                            async with AsyncSessionLocal() as assoc_db:
                                assoc_svc = ConversationService(assoc_db)
                                await assoc_svc.associate_speaker(
                                    satellite_db_session_id, spk.id, user_id=room_owner_id
                                )
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to associate speaker with conversation: {e}")

                    # Register voice presence if speaker was recognized. NEVER
                    # for a device account: presence says "this person is in
                    # this room", and a device is in its room by construction —
                    # booking it would put a person-shaped trace on an account
                    # that stands for every voice in the house (D-4b).
                    if (
                        sat_user_id
                        and not sat_is_device_account
                        and ha_glue_settings.presence_enabled
                        and satellite
                        and satellite.room_id
                    ):
                        try:
                            from ha_glue.services.presence_service import get_presence_service
                            presence_svc = get_presence_service()
                            await presence_svc.register_voice_presence(
                                user_id=sat_user_id,
                                room_id=satellite.room_id,
                                room_name=satellite.room,
                                satellite_id=satellite_id,
                            )
                        except Exception as e:
                            logger.warning(f"⚠️ Voice presence update failed: {e}")

                    # Which room this voice turn came from — lets work that finishes
                    # later (a scan) speak its outcome back here. Reset after the
                    # loop so it never leaks into the next turn on this socket.
                    from utils.voice_context import origin_room_id
                    _origin_room_token = origin_room_id.set(
                        satellite.room_id if satellite else None
                    )
                    for intent_candidate in ranked_intents:
                        intent_name = intent_candidate.get("intent", "general.conversation")
                        logger.info(f"🎯 Satellite versucht Intent: {intent_name} (confidence: {intent_candidate.get('confidence', 0):.2f})")

                        if intent_name == "general.conversation":
                            intent = intent_candidate
                            break

                        # The satellite's conversation session lets session-scoped
                        # tools reach it later — a scan started by voice reports its
                        # outcome into this conversation.
                        executor = ActionExecutor(
                            mcp_manager=mcp_mgr, session_id=satellite_db_session_id
                        )
                        candidate_result = await executor.execute(
                            intent_candidate, user_permissions=sat_user_permissions,
                            user_id=sat_user_id,
                        )

                        if candidate_result.get("success") and not candidate_result.get("empty_result"):
                            intent = intent_candidate
                            action_result = candidate_result
                            logger.info(f"⚡ Action result: {candidate_result.get('success')}")
                            await satellite_manager.send_action_result(
                                session_id, intent, candidate_result.get("success", False)
                            )
                            break

                        # A REFUSAL is not an empty result: trying the next intent
                        # would end in plain chat, and the model would answer the
                        # very question the permission gate just declined. Say so
                        # instead (the failure branch below renders the message).
                        if candidate_result.get("permission_denied"):
                            intent = intent_candidate
                            action_result = candidate_result
                            logger.info(f"🔒 Intent {intent_name} abgelehnt (Berechtigung)")
                            await satellite_manager.send_action_result(session_id, intent, False)
                            break

                        logger.info(f"⏭️ Intent {intent_name} leer, versuche nächsten...")
                    origin_room_id.reset(_origin_room_token)

                    # Fallback to conversation if no intent worked
                    if intent is None:
                        intent = {"intent": "general.conversation", "parameters": {}, "confidence": 1.0}

                    # Generate response (with conversation history for context)
                    # Use vision model if image is present and vision model is configured
                    use_vision = bool(image_b64 and settings.ollama_vision_model)
                    if use_vision:
                        logger.info(f"👁️ Using vision model: {settings.ollama_vision_model}")

                    response_text = ""
                    if action_result and action_result.get("success"):
                        result_info = action_result.get("message", "")
                        enhanced_prompt = f"""Der Nutzer hat gefragt: "{text}"
Die Aktion wurde ausgeführt: {result_info}
Gib eine kurze, natürliche Antwort. KEIN JSON, nur Text."""

                        async for chunk in ollama.chat_stream(enhanced_prompt, history=satellite_conversation_history):
                            response_text += chunk
                    elif action_result and action_result.get("permission_denied"):
                        # Spoken refusal — never the gate's technical message.
                        response_text = (
                            "Das darf ich nur für eine erkannte Stimme tun. "
                            "Sag es mir bitte noch einmal, wenn ich dich erkannt habe."
                            if satellite_language == "de" else
                            "I can only do that for a recognised voice. "
                            "Please ask again once I know who you are."
                        )
                    elif action_result and not action_result.get("success"):
                        response_text = f"Entschuldigung, das konnte ich nicht ausführen: {action_result.get('message')}"
                    elif use_vision:
                        # Visual query: use vision model with image
                        async for chunk in ollama.chat_stream_with_image(
                            text, image_b64, history=satellite_conversation_history,
                            lang=satellite_language
                        ):
                            response_text += chunk
                    else:
                        # Normal conversation (with history for follow-up questions)
                        async for chunk in ollama.chat_stream(text, history=satellite_conversation_history):
                            response_text += chunk

                    logger.info(f"💬 Response: '{response_text[:100]}...'")

                    # The ollama client's pydantic Message model silently drops unknown
                    # keys, so keeping ``metadata`` on the in-memory dict is safe — it
                    # never reaches the LLM, only the agent prompt builder and the DB.
                    assistant_metadata = _build_assistant_metadata(intent, action_result)

                    # Kiosk active-subsystem pulse (VOICE path — mirrors the web-chat
                    # emit in chat_handler). Without this a spoken "turn off the light"
                    # lights nothing on the kiosk; only typed chat commands pulsed. The
                    # executed intent name is already in ``mcp.<server>.<tool>`` /
                    # ``internal.<tool>`` form, so the same extractor maps it. A plain
                    # ``general.conversation`` turn maps to no subsystem → no-op push.
                    try:
                        from api.websocket.kiosk_data import (
                            broadcast_turn_activity,
                            extract_subsystems_used,
                        )

                        _intent_name = intent.get("intent") if intent else None
                        _subs = (
                            extract_subsystems_used([(_intent_name, action_result)])
                            if _intent_name
                            else []
                        )
                        await broadcast_turn_activity(
                            None,
                            _subs,
                            action_result.get("success") if action_result else None,
                        )
                    except Exception as e:  # noqa: BLE001 — never break the turn on a push
                        logger.debug(f"kiosk turn_activity (voice) broadcast failed: {e}")

                    # Update in-memory conversation history (keep max 5 exchanges = 10 messages)
                    satellite_conversation_history.append({"role": "user", "content": text})
                    satellite_conversation_history.append({
                        "role": "assistant",
                        "content": response_text,
                        "metadata": assistant_metadata,
                    })
                    if len(satellite_conversation_history) > 10:
                        satellite_conversation_history[:] = satellite_conversation_history[-10:]

                    # Persist messages to DB if we have a session ID
                    if satellite_db_session_id and response_text:
                        try:
                            async with AsyncSessionLocal() as db_session:
                                # A ROOM history, not a person's chat (§8.1):
                                # tier 2 so every member the device account
                                # reaches shares the one thread instead of each
                                # starting a context-less new one. Only used
                                # when this turn CREATES the conversation; an
                                # existing row keeps the tier it has.
                                #
                                # The OWNER is the device account, never
                                # `sat_user_id` (§8.1 rules that out by name:
                                # the recognised speaker would then be able to
                                # delete the room's shared thread). Without a
                                # device account there is nobody to own it, and
                                # tier 2 on an ownerless row reaches NO ONE —
                                # so the tier drops back to 0 rather than
                                # producing a shared thread nobody can see.
                                # (`room_owner_id` / `room_tier` are resolved
                                # once, above, next to the speaker association.)
                                await ollama.save_message(
                                    satellite_db_session_id, "user", text, db_session,
                                    metadata={
                                        "satellite_id": satellite_id,
                                        "room": satellite.room if satellite else None,
                                        "speaker": speaker_name
                                    },
                                    user_id=room_owner_id,
                                    circle_tier=room_tier,
                                )
                                await ollama.save_message(
                                    satellite_db_session_id, "assistant", response_text, db_session,
                                    metadata=assistant_metadata,
                                    user_id=room_owner_id,
                                    circle_tier=room_tier,
                                )
                                logger.debug(f"💾 Satellite messages saved to DB: {satellite_db_session_id}")
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to save satellite messages to DB: {e}")

                    # Background: memory + KG extraction for this spoken turn —
                    # the same seam the browser chat path uses, scheduled (never
                    # awaited) so the TTS below is not delayed. Runs ONLY when the
                    # speaker was recognized and never for the device account;
                    # see _spawn_satellite_extraction.
                    _spawn_satellite_extraction(
                        user_text=text,
                        response_text=response_text,
                        user_id=sat_user_id,
                        session_id=satellite_db_session_id,
                        lang=satellite_language,
                        action_success=assistant_metadata.get("action_success"),
                        is_device_account=sat_is_device_account,
                    )

                    # Generate TTS with satellite's language
                    from services.piper_service import get_piper_service
                    piper = get_piper_service()
                    tts_audio = await piper.synthesize_to_bytes(response_text, language=satellite_language)

                    if tts_audio:
                        # Route TTS to the best available output device
                        await _route_satellite_tts_output(
                            satellite_manager, satellite, session_id, tts_audio
                        )
                    else:
                        logger.warning(f"⚠️ TTS synthesis failed for session {session_id}")

                except Exception as e:
                    logger.error(f"❌ Processing failed: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

                # End session
                await satellite_manager.end_session(session_id, reason="completed")

            # Handle heartbeat with optional metrics
            elif msg_type == "heartbeat":
                if satellite_id:
                    if await _reject_derostered_heartbeat(
                        websocket, satellite_id, satellite_manager
                    ):
                        break
                    # Extract metrics and version from heartbeat if present
                    metrics = data.get("metrics")
                    version = data.get("version")
                    satellite_manager.update_heartbeat(satellite_id, metrics, version)
                    # Send heartbeat ack
                    await websocket.send_json({"type": "heartbeat_ack"})

            # Reply to an on-demand camera snapshot request (announce privacy check)
            elif msg_type == "snapshot_result":
                satellite_manager.resolve_snapshot(
                    data.get("request_id"), data.get("image")
                )

            # Reply to an on-demand Bluetooth discovery scan request
            elif msg_type == "bt_scan_result":
                satellite_manager.resolve_bt_scan(
                    data.get("request_id"),
                    data.get("devices", []),
                    data.get("error"),
                )

            # Reply to an on-demand IRK pairing capture request
            elif msg_type == "irk_capture_result":
                satellite_manager.resolve_irk_capture(
                    data.get("request_id"),
                    data.get("result"),
                    data.get("error"),
                )

            # Handle BLE presence scan results
            elif msg_type == "ble_presence":
                if satellite_id and ha_glue_settings.presence_enabled:
                    ble_devices = data.get("devices", [])
                    satellite = satellite_manager.get_satellite(satellite_id)
                    ble_room_id = satellite.room_id if satellite else None

                    from ha_glue.services.presence_service import get_presence_service
                    presence_svc = get_presence_service()
                    await presence_svc.process_ble_report(
                        satellite_id=satellite_id,
                        room_id=ble_room_id,
                        devices=ble_devices,
                        room_name=satellite.room if satellite else None,
                    )

            # Handle OTA update progress
            elif msg_type == "update_progress":
                if satellite_id:
                    stage = data.get("stage", "unknown")
                    progress = data.get("progress", 0)
                    message = data.get("message", "")
                    logger.info(f"📥 Update progress from {satellite_id}: {stage} ({progress}%) - {message}")

                    satellite_manager.apply_update_progress(
                        satellite_id, stage=stage, progress=progress, message=message
                    )

            # Handle OTA update complete
            elif msg_type == "update_complete":
                if satellite_id:
                    from ha_glue.services.satellite_manager import UpdateStatus
                    success = data.get("success", False)
                    old_version = data.get("old_version", "unknown")
                    new_version = data.get("new_version", "unknown")

                    if success:
                        logger.info(f"✅ Satellite {satellite_id} updated: {old_version} → {new_version}")
                        satellite_manager.set_update_status(
                            satellite_id,
                            UpdateStatus.COMPLETED,
                            stage="completed",
                            progress=100
                        )
                        # Through the manager, so the value is bounded like
                        # every other device-supplied field.
                        satellite_manager.set_version(satellite_id, new_version)
                    else:
                        error = data.get("error", "Unknown error")
                        logger.error(f"❌ Satellite {satellite_id} update failed: {error}")
                        satellite_manager.set_update_status(
                            satellite_id,
                            UpdateStatus.FAILED,
                            stage="failed",
                            progress=0,
                            error=error
                        )

            # Handle OTA update failed
            elif msg_type == "update_failed":
                if satellite_id:
                    from ha_glue.services.satellite_manager import UpdateStatus
                    stage = data.get("stage", "unknown")
                    error = data.get("error", "Unknown error")
                    rolled_back = data.get("rolled_back", False)
                    logger.error(f"❌ Satellite {satellite_id} update failed at {stage}: {error} (rolled_back: {rolled_back})")
                    satellite_manager.set_update_status(
                        satellite_id,
                        UpdateStatus.FAILED,
                        stage=stage,
                        progress=0,
                        error=error
                    )

    except WebSocketDisconnect:
        logger.info(f"👋 Satellite WebSocket disconnected: {satellite_id}")
    except Exception as e:
        logger.error(f"❌ Satellite WebSocket error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        # Clean up connection limiter
        if satellite_id and ip_address:
            connection_limiter.remove_connection(ip_address, satellite_id)

        if satellite_id:
            # Mark satellite offline in database
            try:
                from ha_glue.services.room_service import RoomService

                async with AsyncSessionLocal() as db_session:
                    room_service = RoomService(db_session)
                    await room_service.set_satellite_online(satellite_id, False)
            except Exception as e:
                logger.warning(f"⚠️ Failed to mark satellite offline: {e}")

            # Unsubscribe from wake word config updates
            wakeword_config_manager = get_wakeword_config_manager()
            wakeword_config_manager.unsubscribe(websocket)

            await satellite_manager.unregister(satellite_id, websocket=websocket)
