"""Telephony client (#1650): call events from the telephony gateway by push.

The gateway (Asterisk + ARI) sits in the DMZ and may not open connections into
the LAN, so Renfield dials OUT and keeps one WebSocket open; the gateway pushes
events over it. Protocol (JSON text frames)::

    -> {"type": "hello", "epoch": "<id>", "resume_from": <seq>}
    <- {"type": "welcome", "instance": "...", "epoch": "<id>", "last_seq": <n>}
    <- {"type": "incoming_call", "seq": n, "call_id": "...", "did": "+49...", "caller": "+49..." | "anonymous"}
    <- {"type": "call_ended", "seq": n, "call_id": "...", "cause": "..."}
    <- {"type": "voicemail", "seq": n, "call_id": "...", "did": "...", "caller": "...", "duration_s": 23,
        "recording": "<basename>"}   # recording: the transcript job's handle (#1669)
    <- {"type": "fax_received", "seq": n, "call_id": "...", "did": "...", "caller": "...",
        "status": "ok" | "failed", "pages": 3, "fax": "<basename>"}   # #1677, only with TELEPHONY_FAX_ENABLED

Guarantees:
- Resume: (epoch, last seq) are stored in Redis; after a reconnect the gateway
  replays what was missed. A new epoch (gateway restart) replays from 0.
- At least once, deduplicated: an announced event (``incoming_call``,
  ``voicemail``) is marked ``<type>:<call_id>`` in Redis only AFTER it was
  delivered, and the resume position only advances after that. A replay skips
  marked events; a crash mid-delivery repeats the event rather than losing it.
  An event that fails ``MAX_ATTEMPTS`` times is skipped (logged) so it cannot
  block every later call.
- Trust boundary: the gateway sends numbers only. Recipients are decided here,
  never by the event: every active user of the instance gets a personal
  notification of their own (``target_user_id``) — no room announcement and no
  public broadcast to shared devices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import ssl
from collections.abc import Awaitable, Callable
from typing import Protocol

from utils.config import settings
from utils.log_safety import error_text_without_content

logger = logging.getLogger(__name__)

STATE_KEY = "telephony:resume"
CLAIM_PREFIX = "telephony:call:"
CLAIM_TTL_SECONDS = 24 * 3600
BACKOFF_MAX_SECONDS = 60.0
WELCOME_TIMEOUT_SECONDS = 10.0
MAX_ATTEMPTS = 3
MAX_SEQ_JUMP = 10_000  # a seq far beyond what the gateway announced is a bug or forgery
_NUMBER_RE = re.compile(r"^\+?[0-9]{1,20}$")


class StateStore(Protocol):
    async def load(self) -> tuple[str, int]: ...
    async def save(self, epoch: str, seq: int) -> None: ...
    async def is_done(self, key: str) -> bool: ...
    async def mark_done(self, key: str) -> None: ...


class RedisStateStore:
    """Resume position + per-call claims in the shared Redis."""

    def __init__(self, redis):
        self._redis = redis

    async def load(self) -> tuple[str, int]:
        raw = await self._redis.get(STATE_KEY)
        if not raw:
            return "", 0
        try:
            data = json.loads(raw)
            return str(data.get("epoch", "")), int(data.get("seq", 0))
        except (ValueError, TypeError):
            return "", 0

    async def save(self, epoch: str, seq: int) -> None:
        await self._redis.set(STATE_KEY, json.dumps({"epoch": epoch, "seq": seq}))

    async def is_done(self, key: str) -> bool:
        return bool(await self._redis.exists(f"{CLAIM_PREFIX}{key}"))

    async def mark_done(self, key: str) -> None:
        await self._redis.set(f"{CLAIM_PREFIX}{key}", "1", ex=CLAIM_TTL_SECONDS)


def format_number(number: str) -> str:
    """Display form: German numbers nationally without spaces (``01701234567``),
    others as E.164."""
    if not number or not _NUMBER_RE.match(number):
        return "Unbekannt"  # "anonymous", and anything from the DMZ that is not a plain number
    if number.startswith("+49") and len(number) > 5:
        return "0" + number[3:]
    return number


ANNOUNCED_TYPES = ("incoming_call", "voicemail")
FAX_TYPE = "fax_received"  # #1677: announced only with TELEPHONY_FAX_ENABLED (off = ignored as before)


def announced_types() -> tuple[str, ...]:
    return (*ANNOUNCED_TYPES, FAX_TYPE) if settings.telephony_fax_enabled else ANNOUNCED_TYPES


def _plain_number(value: object) -> str | None:
    text = str(value or "")
    return text if _NUMBER_RE.match(text) else None


def _seconds(value: object) -> int:
    try:
        return min(max(int(float(value)), 0), 24 * 3600)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0


def format_duration(seconds: object) -> str:
    total = _seconds(seconds)
    return f"{total // 60}:{total % 60:02d}"


def build_notification(event: dict, hint=None) -> dict:
    """Map an ``incoming_call``/``voicemail`` event to the NotificationService.process_webhook
    kwargs (recipient is added per user). ``hint`` = a ``caller_lookup.CallerHint`` for
    THIS recipient, or None -- None keeps the text byte-identical to before #1240."""
    caller = format_number(str(event.get("caller", "")))
    did = _plain_number(event.get("did"))
    # "Anruf von <Anrufer> an <angerufene Nummer>" -- the called number tells which
    # line rang (private, business, ...). Without a valid DID the "an" part is left out.
    to = f" an {format_number(did)}" if did else ""
    route = f"von {caller}{to}"
    long_route = route
    label = hint.label() if hint is not None else None
    if label:
        # A name is a HINT (CallerID is spoofable): the raw number always stands
        # beside it, the long text says where it comes from and that it is unchecked.
        origin = "laut Dokument" if hint.source == "document" else "laut Kontaktdaten"
        route = f"von {label}{to}"
        long_route = f"von {label} ({caller}; Name {origin}, nicht geprüft){to}"
    elif hint is not None and hint.place:
        route = long_route = f"von {caller} ({hint.place}){to}"
    if event.get("type") == "voicemail":
        duration = format_duration(event.get("duration_s"))
        title, message = (
            f"Neue Nachricht {route}",
            f"Neue Nachricht auf dem Anrufbeantworter {long_route} ({duration})",
        )
    elif event.get("type") == FAX_TYPE:
        title, message = _fax_text(event, route, long_route)
    else:
        title, message = f"Anruf {route}", f"Eingehender Anruf {long_route}"
    data = {
        "call_id": str(event.get("call_id", ""))[:64],
        "did": did,
        "caller": _plain_number(event.get("caller")),
    }
    if event.get("type") == "voicemail":
        data["duration_s"] = _seconds(event.get("duration_s"))
    if event.get("type") == FAX_TYPE:
        data["pages"] = _pages(event.get("pages"))
        data["fax_status"] = _fax_status(event)
        if isinstance(event.get("document_id"), int):
            data["document_id"] = event["document_id"]
    privacy = "personal"
    if label:
        # Snapshot of what this recipient was shown; never read back as identity.
        data["caller_name"] = label
        data["caller_name_source"] = hint.source
        # The name came through THIS recipient's circle filter. "personal" lets the
        # room delivery show it on a shared display to any household member in the
        # room; "confidential" = only while the recipient is alone (fail-closed).
        privacy = "confidential"
    return {
        "event_type": f"telephony.{event.get('type')}",
        "title": title,
        "message": message,
        "urgency": "info",
        "tts": False,
        "data": data,
        "source": "telephony",
        "privacy": privacy,
        "dedup": False,  # exactly once per event already (call claim); a repeat call is a new call
    }


def _pages(value: object) -> int:
    try:
        return min(max(int(value), 0), 10_000)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0


def _fax_status(event: dict) -> str:
    """``filed`` | ``not_filed`` (received, the filing failed) | ``failed`` (not received)."""
    if event.get("status") != "ok":
        return "failed"
    return "filed" if event.get("filed") else "not_filed"


def _fax_text(event: dict, route: str, long_route: str) -> tuple[str, str]:
    status = _fax_status(event)
    if status == "failed":
        return f"Fax {route} fehlgeschlagen", f"Ein Fax {long_route} konnte nicht empfangen werden"
    pages = _pages(event.get("pages"))
    count = f"{pages} Seite" if pages == 1 else f"{pages} Seiten"
    if status == "filed":
        kb = str(event.get("kb") or "").strip()[:64]
        where = f", abgelegt in „{kb}“" if kb else ""
        return f"Fax {route} ({count})", f"Fax {long_route} empfangen ({count}){where}"
    return (f"Fax {route} ({count})",
            f"Fax {long_route} empfangen ({count}), die Ablage ist fehlgeschlagen")


async def notify_users(event: dict, user_ids: list[int], process, resolve=None) -> int:
    """One personal notification per user. ``process`` = process_webhook.

    ``resolve(caller, user_id)`` (caller lookup, #1240) names the caller from what
    THAT user may see; it never raises (``caller_lookup.resolve_caller``), so a
    lookup problem can never turn into a retry and duplicate notifications.
    A user's feedback suppression raises ValueError — that skips only that user.
    Any other error propagates, so the event is retried (users already notified
    may then get it twice — accepted: rare, and better than none).
    """
    base = build_notification(event)
    caller = _plain_number(event.get("caller"))
    sent = 0
    for user_id in user_ids:
        kwargs = base
        if resolve is not None and caller:
            hint = await resolve(caller, user_id)
            if hint is not None:
                kwargs = build_notification(event, hint)
        try:
            await process(**kwargs, target_user_id=user_id)
            sent += 1
        except ValueError as e:
            logger.debug("Telephony notification suppressed: %s", error_text_without_content(e))
    return sent


async def recipient_user_ids(db) -> list[int]:
    """Active human accounts. Device accounts (satellites, kiosk) are users too — never recipients."""
    from sqlalchemy import select

    from models.database import User

    query = select(User.id).where(User.is_active.is_(True), User.is_device_account.is_(False)).order_by(User.id)
    return list((await db.execute(query)).scalars())


async def deliver_via_notification_service(event: dict) -> None:
    from services.database import AsyncSessionLocal
    from services.notification_service import NotificationService

    if event.get("type") == FAX_TYPE:
        # #1677: one configured owner, not every user; filing first, then the notice.
        from services.telephony_fax import handle_event

        await handle_event(event)
        return

    async with AsyncSessionLocal() as db:
        user_ids = await recipient_user_ids(db)
        if not user_ids:
            logger.warning("Telephony event without any active user — nobody to notify")
            return
        resolve = None
        if settings.caller_lookup_enabled and settings.contact_points_enabled:
            from services.caller_lookup import resolve_caller as resolve
        await notify_users(event, user_ids, NotificationService(db).process_webhook, resolve)
    if event.get("type") == "voicemail" and settings.voicemail_transcript_enabled:
        # #1669: after the notifications, before the event is marked done. Never
        # raises -- a queue problem must not retry the event and notify twice.
        from services.voicemail_transcript import enqueue

        await enqueue(event)


def ssl_context_for(ca_pem: str) -> ssl.SSLContext:
    """Trust exactly the gateway's own (self-signed) certificate; hostname/IP is checked."""
    ctx = ssl.create_default_context(cadata=ca_pem)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class TelephonyClient:
    _sleep = staticmethod(asyncio.sleep)  # seam for tests (fast backoff)

    def __init__(
        self,
        url: str,
        token: str,
        store: StateStore,
        deliver: Callable[[dict], Awaitable[None]],
        ssl_ctx: ssl.SSLContext | None = None,
        connect: Callable | None = None,
    ):
        if connect is None:
            from websockets.asyncio.client import connect as ws_connect

            connect = ws_connect
        self._url = url
        self._token = token
        self._store = store
        self._deliver = deliver
        self._ssl = ssl_ctx
        self._connect = connect
        self.connected = asyncio.Event()
        self._attempts: dict[str, int] = {}

    async def run(self) -> None:
        """Connect, consume, reconnect with backoff — until cancelled."""
        backoff = 1.0
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, closed connections, failed deliveries
                logger.warning(
                    "Telephony gateway connection lost (%s): %s", type(e).__name__, error_text_without_content(e)
                )
            finally:
                if self.connected.is_set():
                    backoff = 1.0  # the session was healthy: start the backoff over
                self.connected.clear()
            await self._sleep(backoff + random.uniform(0, backoff / 4))
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)

    async def _session(self) -> None:
        kwargs = {"additional_headers": {"Authorization": f"Bearer {self._token}"}, "ping_interval": 20}
        if self._ssl is not None:
            kwargs["ssl"] = self._ssl
        async with self._connect(self._url, **kwargs) as ws:
            epoch, seq = await self._store.load()
            await ws.send(json.dumps({"type": "hello", "epoch": epoch, "resume_from": seq}))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), WELCOME_TIMEOUT_SECONDS))
            if welcome.get("type") != "welcome":
                raise ValueError("gateway did not send welcome")
            if welcome.get("epoch") != epoch:
                epoch, seq = welcome.get("epoch", ""), 0  # gateway restarted: replay comes from 0
                await self._store.save(epoch, seq)
            try:
                ceiling = max(int(welcome.get("last_seq", 0)), seq)
            except (TypeError, ValueError):
                ceiling = seq
            logger.info("Telephony gateway connected (instance=%s)", welcome.get("instance"))
            self.connected.set()
            async for raw in ws:
                try:
                    event = json.loads(raw)
                    event_seq = int(event["seq"])
                except (ValueError, TypeError, KeyError):
                    # Skip, don't reconnect: the replay would hand us the same frame again
                    # and every later call would be stuck behind it.
                    logger.warning("Telephony gateway sent a malformed frame — skipped")
                    continue
                if event_seq <= seq:
                    continue
                if event_seq > ceiling + MAX_SEQ_JUMP:
                    logger.warning("Telephony gateway sent an implausible seq — frame skipped")
                    continue
                if not await self._handle(event):
                    raise RuntimeError("delivery failed — reconnect and retry")
                seq, ceiling = event_seq, max(ceiling, event_seq)
                await self._store.save(epoch, seq)

    async def _handle(self, event: dict) -> bool:
        """True = done with this event (delivered, skipped or nothing to do); False = retry."""
        if event.get("type") not in announced_types():
            return True  # call_ended etc.: nothing to announce
        call_id = str(event.get("call_id") or "")[:64]
        if not call_id:
            return True
        key = f"{event['type']}:{call_id}"
        if await self._store.is_done(key):
            return True
        try:
            await self._deliver(event)
        except Exception as e:
            attempts = self._attempts.get(key, 0) + 1
            self._attempts[key] = attempts
            if attempts < MAX_ATTEMPTS:
                logger.warning("Telephony delivery failed (%d/%d): %s", attempts, MAX_ATTEMPTS, type(e).__name__)
                return False
            logger.error("Telephony event dropped after %d failed deliveries: %s", attempts, type(e).__name__)
        self._attempts.pop(key, None)
        await self._store.mark_done(key)
        return True


def _is_loopback(url: str) -> bool:
    import ipaddress
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def run_telephony_client() -> None:
    """Entry point for the lifecycle task (only called when TELEPHONY_ENABLED)."""
    from services.redis_client import get_redis

    if not settings.telephony_gateway_url or not settings.telephony_gateway_token.get_secret_value():
        logger.error("TELEPHONY_ENABLED but TELEPHONY_GATEWAY_URL/TOKEN missing — telephony client not started")
        return
    ssl_ctx = None
    if not settings.telephony_gateway_url.startswith("wss://") and not _is_loopback(settings.telephony_gateway_url):
        logger.error("TELEPHONY_GATEWAY_URL must be wss:// (plain ws:// only to localhost) — not started")
        return
    if settings.telephony_gateway_url.startswith("wss://"):
        if not settings.telephony_gateway_ca_pem:
            logger.error("TELEPHONY_GATEWAY_CA_PEM missing for wss:// — refusing to connect without a pinned cert")
            return
        try:
            ssl_ctx = ssl_context_for(settings.telephony_gateway_ca_pem)
        except (ssl.SSLError, ValueError) as e:
            logger.error("TELEPHONY_GATEWAY_CA_PEM is not a valid certificate (%s) — not started", type(e).__name__)
            return
    client = TelephonyClient(
        settings.telephony_gateway_url,
        settings.telephony_gateway_token.get_secret_value(),
        RedisStateStore(get_redis()),
        deliver_via_notification_service,
        ssl_ctx=ssl_ctx,
    )
    await client.run()
