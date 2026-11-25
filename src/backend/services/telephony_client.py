"""Telephony client (#1650): call events from the telephony gateway by push.

The gateway (Asterisk + ARI) sits in the DMZ and may not open connections into
the LAN, so Renfield dials OUT and keeps one WebSocket open; the gateway pushes
events over it. Protocol (JSON text frames)::

    -> {"type": "hello", "epoch": "<id>", "resume_from": <seq>}
    <- {"type": "welcome", "instance": "...", "epoch": "<id>", "last_seq": <n>}
    <- {"type": "incoming_call", "seq": n, "call_id": "...", "did": "+49...", "caller": "+49..." | "anonymous"}
    <- {"type": "call_ended", "seq": n, "call_id": "...", "cause": "..."}
    <- {"type": "voicemail", "seq": n, "call_id": "...", "did": "...", "caller": "...", "duration_s": 23}

Guarantees:
- Resume: (epoch, last seq) are stored in Redis; after a reconnect the gateway
  replays what was missed. A new epoch (gateway restart) replays from 0.
- Exactly once: an announced event (``incoming_call``, ``voicemail``) is
  delivered only after claiming ``<type>:<call_id>`` in Redis (SET NX), so a
  replay never announces it twice.
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


class StateStore(Protocol):
    async def load(self) -> tuple[str, int]: ...
    async def save(self, epoch: str, seq: int) -> None: ...
    async def claim(self, call_id: str) -> bool: ...
    async def release(self, call_id: str) -> None: ...


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

    async def claim(self, call_id: str) -> bool:
        return bool(await self._redis.set(f"{CLAIM_PREFIX}{call_id}", "1", nx=True, ex=CLAIM_TTL_SECONDS))

    async def release(self, call_id: str) -> None:
        await self._redis.delete(f"{CLAIM_PREFIX}{call_id}")


def format_number(number: str) -> str:
    """Display form: German numbers nationally (``0170 1234567``), others as E.164."""
    if not number or number == "anonymous":
        return "Unbekannt"
    if number.startswith("+49") and len(number) > 5:
        national = "0" + number[3:]
        return f"{national[:4]} {national[4:]}" if national.startswith("01") else national
    return number


ANNOUNCED_TYPES = ("incoming_call", "voicemail")


def format_duration(seconds: object) -> str:
    try:
        total = max(int(float(seconds)), 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        total = 0
    return f"{total // 60}:{total % 60:02d}"


def build_notification(event: dict) -> dict:
    """Map an ``incoming_call``/``voicemail`` event to the recipient-independent
    NotificationService.process_webhook kwargs (recipient is added per user)."""
    caller = format_number(str(event.get("caller", "")))
    if event.get("type") == "voicemail":
        duration = format_duration(event.get("duration_s"))
        title, message = (
            f"Neue Nachricht von {caller}",
            f"Neue Nachricht auf dem Anrufbeantworter von {caller} ({duration})",
        )
    else:
        title, message = f"Anruf von {caller}", f"Eingehender Anruf von {caller}"
    data = {"call_id": event.get("call_id"), "did": event.get("did"), "caller": event.get("caller")}
    if event.get("type") == "voicemail":
        data["duration_s"] = event.get("duration_s")
    return {
        "event_type": f"telephony.{event.get('type')}",
        "title": title,
        "message": message,
        "urgency": "info",
        "tts": False,
        "data": data,
        "source": "telephony",
        "privacy": "personal",
        "dedup": False,  # exactly once per event already (call claim); a repeat call is a new call
    }


async def notify_users(event: dict, user_ids: list[int], process) -> int:
    """One personal notification per user. ``process`` = process_webhook.

    A user's feedback suppression raises ValueError — that skips only that user.
    Any other error propagates, so the call claim is released and the replay
    retries (users already notified may then get it twice — better than none).
    """
    base = build_notification(event)
    sent = 0
    for user_id in user_ids:
        try:
            await process(**base, target_user_id=user_id)
            sent += 1
        except ValueError as e:
            logger.debug("Telephony notification suppressed: %s", error_text_without_content(e))
    return sent


async def deliver_via_notification_service(event: dict) -> None:
    from sqlalchemy import select

    from models.database import User
    from services.database import AsyncSessionLocal
    from services.notification_service import NotificationService

    async with AsyncSessionLocal() as db:
        user_ids = list((await db.execute(select(User.id).where(User.is_active.is_(True)))).scalars())
        if not user_ids:
            logger.warning("Telephony event without any active user — nobody to notify")
            return
        await notify_users(event, user_ids, NotificationService(db).process_webhook)


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

    async def run(self) -> None:
        """Connect, consume, reconnect with backoff — until cancelled."""
        backoff = 1.0
        while True:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, closed connections, bad frames
                logger.warning(
                    "Telephony gateway connection lost (%s): %s", type(e).__name__, error_text_without_content(e)
                )
            finally:
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
            welcome = json.loads(await ws.recv())
            if welcome.get("type") != "welcome":
                raise ValueError("gateway did not send welcome")
            if welcome.get("epoch") != epoch:
                epoch, seq = welcome.get("epoch", ""), 0  # gateway restarted: replay comes from 0
                await self._store.save(epoch, seq)
            logger.info("Telephony gateway connected (instance=%s)", welcome.get("instance"))
            self.connected.set()
            async for raw in ws:
                event = json.loads(raw)
                event_seq = int(event.get("seq", 0))
                if event_seq <= seq:
                    continue
                await self._handle(event)
                seq = event_seq
                await self._store.save(epoch, seq)

    async def _handle(self, event: dict) -> None:
        if event.get("type") not in ANNOUNCED_TYPES:
            return  # call_ended etc.: nothing to announce
        call_id = str(event.get("call_id") or "")
        if not call_id:
            return
        claim = f"{event['type']}:{call_id}"
        if not await self._store.claim(claim):
            return
        try:
            await self._deliver(event)
        except Exception:
            await self._store.release(claim)  # let the replay after reconnect deliver it
            raise


async def run_telephony_client() -> None:
    """Entry point for the lifecycle task (only called when TELEPHONY_ENABLED)."""
    from services.redis_client import get_redis

    if not settings.telephony_gateway_url or not settings.telephony_gateway_token.get_secret_value():
        logger.error("TELEPHONY_ENABLED but TELEPHONY_GATEWAY_URL/TOKEN missing — telephony client not started")
        return
    ssl_ctx = None
    if settings.telephony_gateway_url.startswith("wss://"):
        if not settings.telephony_gateway_ca_pem:
            logger.error("TELEPHONY_GATEWAY_CA_PEM missing for wss:// — refusing to connect without a pinned cert")
            return
        ssl_ctx = ssl_context_for(settings.telephony_gateway_ca_pem)
    client = TelephonyClient(
        settings.telephony_gateway_url,
        settings.telephony_gateway_token.get_secret_value(),
        RedisStateStore(get_redis()),
        deliver_via_notification_service,
        ssl_ctx=ssl_ctx,
    )
    await client.run()
