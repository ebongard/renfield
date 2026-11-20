"""A spoken filler on a slow satellite voice turn (#1576).

While a voice turn's action runs (Paperless, Home Assistant, search) and its
answer is phrased, the satellite says nothing; a slow turn reads as "did it
hear me?". After `SATELLITE_FILLER_AFTER_SECONDS` the satellite says a fixed
phrase ("Moment, ich schaue nach.") -- deterministic, in code, never an LLM
instruction.

Invariants:
* **At most one filler per turn, none when the work ends first.** `stop()`
  is called once the answer text is ready; a timer that has not fired by then
  never sends.
* **Never overlapping the answer.** The filler goes out with
  `is_final=False`, and only to a satellite that advertises the
  `tts_queue` capability (1.4.22): it plays clips one after the other. An
  older satellite DROPS a second clip while one plays -- that would be the
  answer -- so it never gets a filler.
* **One sender at a time on the socket.** The turn's own sends inside the
  window go through `exclusive()`; `stop()` takes the same lock, so a filler
  is never cancelled halfway through its frame.
* **The satellite itself, always.** Not routed to a room speaker: the device
  one spoke to acknowledges, and a DLNA/HA player stays free for the answer.
* **Only a language with a voice of its own** (`PIPER_VOICES`). A phrase in
  the fallback voice of another language would be worse than silence.
* A filler that fails (synthesis, send) costs the filler, never the turn.

The phrase is synthesised through `piper.synthesize_to_bytes`, whose LRU
cache holds it after the first time; `warm()` renders it ahead of the first
turn.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from loguru import logger

from utils.log_safety import error_text_without_content

#: The phrase per language. Short, so it is over before most answers are ready.
PHRASES: dict[str, str] = {
    "de": "Moment, ich schaue nach.",
    "en": "One moment, let me check.",
    "it": "Un momento, controllo.",
}


def filler_phrase(language: str | None) -> str | None:
    """The phrase for `language`, or `None` when it has none or no own voice."""
    from utils.config import settings

    lang = (language or settings.default_language or "").lower()
    if lang not in PHRASES or lang not in settings.piper_voice_map:
        return None
    return PHRASES[lang]


def filler_wanted(satellite_info: object | None, language: str | None) -> str | None:
    """The phrase to use on this turn, or `None` for no filler at all.

    Off unless `SATELLITE_FILLER_ENABLED`; only for a device that queues
    clips; only for a language with a voice of its own.
    """
    from ha_glue.utils.config import ha_glue_settings

    if not ha_glue_settings.satellite_filler_enabled or satellite_info is None:
        return None
    caps = getattr(satellite_info, "capabilities", None)
    if not getattr(caps, "tts_queue", False):
        return None
    return filler_phrase(language)


async def warm(language: str | None) -> None:
    """Render the phrase for `language` into the TTS cache. Best effort."""
    phrase = filler_phrase(language)
    if phrase is None:
        return
    try:
        from services.piper_service import get_piper_service

        await get_piper_service().synthesize_to_bytes(phrase, language=language)
    except Exception as e:  # a cold cache only costs the first filler time
        logger.warning(f"Satellite filler warm-up failed: {error_text_without_content(e)}")


#: Strong references to running warm-ups (a bare task can be collected mid-run).
_warming: set[asyncio.Task] = set()


def warm_in_background(satellite_info: object | None, language: str | None) -> None:
    """On registration: render the phrase ahead of the first slow turn.

    Only where a filler would be used at all, so the switch off costs nothing.
    """
    if filler_wanted(satellite_info, language) is None:
        return
    task = asyncio.create_task(warm(language))
    _warming.add(task)
    task.add_done_callback(_warming.discard)


class SatelliteFiller:
    """One turn's filler timer. `start()`, then `stop()` when the answer is ready."""

    def __init__(
        self,
        *,
        after_seconds: float,
        render: Callable[[], Awaitable[bytes | None]],
        send: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._after = after_seconds
        self._render = render
        self._send = send
        self._done = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        #: Whether the filler went out (for the log and the tests).
        self.sent = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self._done.wait(), self._after)
            return  # the work ended first
        except TimeoutError:
            pass
        try:
            audio = await self._render()
            if not audio:
                return
            async with self._lock:
                if self._done.is_set():
                    return  # the answer was ready while the phrase rendered
                await self._send(audio)
                self.sent = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Satellite filler not sent: {error_text_without_content(e)}")

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Hold the socket for one of the turn's own sends inside the window."""
        async with self._lock:
            yield

    async def stop(self) -> None:
        """End the window. Waits for a filler that is being SENT, never cuts it.

        After the lock is taken the timer is either before its send or past
        it: the cancellation then lands in the wait or the rendering.
        """
        async with self._lock:
            self._done.set()
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@contextlib.asynccontextmanager
async def nothing_exclusive() -> AsyncIterator[None]:
    """`exclusive()` for a turn without a filler."""
    yield
