"""
Whisper Prompt Builder — per-household initial_prompt for STT bias.

Phase B-3 of the voice pipeline plan. Builds a ~150-200 char free-text bias
string that gets passed as faster-whisper's `initial_prompt`. Helps the
decoder pick household-specific names (rooms, users, devices) and German
technical terms that the base model doesn't see often enough.

Resolution order:
1. Fire `build_whisper_initial_prompt` hook — first plugin (e.g. Reva) that
   returns a non-None string wins.
2. Fall back to the platform default: a fixed-structure prompt assembled
   from the DB (Sprecher, Raum, andere Personen, andere Räume, Geräte).

Caching: results are keyed on `(user_id, room_id, language)` and cached for
5 minutes. The household graph (rooms, user names) changes on the order of
weeks; 5 min is a sensible upper bound for staleness while still avoiding a
DB hit per utterance under multi-satellite bursts. Explicit invalidation on
rooms/users mutations is a follow-up — TTL-only is the v1 contract.

Future: per-user frequency-ranked vocabulary will plug in here once we have
a corpus of identified-speaker transcripts to mine. The hook system already
supports a plugin replacing the default prompt entirely; the eventual
vocabulary builder can register as a higher-priority handler.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from utils.hooks import run_hooks

# Maximum prompt length passed to faster-whisper. The decoder uses it as
# context for beam search; longer prompts dilute the bias and slow decoding.
# Empirically ~200 tokens is the sweet spot — measured in chars below.
_MAX_PROMPT_CHARS = 220
_CACHE_TTL_SECONDS = 300.0


@dataclass(slots=True)
class _CacheEntry:
    prompt: str | None
    expires_at: float


class WhisperPromptBuilder:
    """Builds and caches per-(user, room, language) STT bias prompts."""

    def __init__(self) -> None:
        self._cache: dict[tuple[int | None, int | None, str], _CacheEntry] = {}
        self._cache_hits = 0
        self._cache_misses = 0

    async def build(
        self,
        *,
        user_id: int | None,
        room_id: int | None,
        language: str,
        db_session=None,
    ) -> str | None:
        """Return a bias prompt for this request, or None if no context."""
        key = (user_id, room_id, language)
        now = time.monotonic()

        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > now:
            self._cache_hits += 1
            return cached.prompt
        self._cache_misses += 1

        prompt = await self._resolve(
            user_id=user_id, room_id=room_id, language=language, db_session=db_session
        )
        if prompt and len(prompt) > _MAX_PROMPT_CHARS:
            prompt = prompt[:_MAX_PROMPT_CHARS].rstrip()

        self._cache[key] = _CacheEntry(prompt=prompt, expires_at=now + _CACHE_TTL_SECONDS)
        return prompt

    def invalidate(self) -> None:
        """Drop the entire cache. Wire to room/user mutation events later."""
        self._cache.clear()

    def stats(self) -> dict:
        return {
            "size": len(self._cache),
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "ttl_seconds": _CACHE_TTL_SECONDS,
        }

    # --- internals ---

    async def _resolve(
        self,
        *,
        user_id: int | None,
        room_id: int | None,
        language: str,
        db_session,
    ) -> str | None:
        # Plugin precedence: any plugin (Reva, etc.) that returns a non-None
        # string takes the slot. Plugins receive full kwargs and can build
        # whatever bias makes sense for their domain.
        results = await run_hooks(
            "build_whisper_initial_prompt",
            user_id=user_id,
            room_id=room_id,
            language=language,
        )
        for result in results:
            if isinstance(result, str) and result.strip():
                return result.strip()

        # Platform default: assemble from DB
        if db_session is None:
            return None
        return await self._build_platform_default(
            user_id=user_id, room_id=room_id, language=language, db_session=db_session
        )

    async def _build_platform_default(
        self,
        *,
        user_id: int | None,
        room_id: int | None,
        language: str,
        db_session,
    ) -> str | None:
        """Assemble the default prompt from the rooms + users tables."""
        from sqlalchemy import select

        from ha_glue.models.database import Room
        from models.database import User

        speaker_name: str | None = None
        room_name: str | None = None
        other_users: list[str] = []
        other_rooms: list[str] = []

        try:
            user_result = await db_session.execute(select(User.id, User.first_name, User.username))
            for uid, first_name, username in user_result.all():
                display = (first_name or username or "").strip()
                if not display:
                    continue
                if uid == user_id:
                    speaker_name = display
                else:
                    other_users.append(display)

            room_result = await db_session.execute(select(Room.id, Room.name))
            for rid, name in room_result.all():
                if not name:
                    continue
                if rid == room_id:
                    room_name = name
                else:
                    other_rooms.append(name)
        except Exception as e:
            logger.debug(f"WhisperPromptBuilder: DB query failed, skipping bias: {e}")
            return None

        if not (speaker_name or room_name or other_users or other_rooms):
            return None

        # Localized labels. We only support de + en for now; unknown
        # languages get the German labels as a fallback (production default).
        labels = _LABELS.get(language, _LABELS["de"])
        parts: list[str] = []
        if speaker_name:
            parts.append(f"{labels['speaker']}: {speaker_name}.")
        if room_name:
            parts.append(f"{labels['room']}: {room_name}.")
        if other_users:
            parts.append(f"{labels['people']}: {', '.join(other_users)}.")
        if other_rooms:
            parts.append(f"{labels['rooms']}: {', '.join(other_rooms)}.")
        return " ".join(parts) if parts else None


_LABELS = {
    "de": {
        "speaker": "Sprecher",
        "room": "Raum",
        "people": "Personen",
        "rooms": "Räume",
    },
    "en": {
        "speaker": "Speaker",
        "room": "Room",
        "people": "People",
        "rooms": "Rooms",
    },
}


_instance: WhisperPromptBuilder | None = None


def get_whisper_prompt_builder() -> WhisperPromptBuilder:
    """Singleton accessor."""
    global _instance
    if _instance is None:
        _instance = WhisperPromptBuilder()
    return _instance


async def whisper_prompt_household_changed(
    *, kind: str, mutation: str
) -> None:
    """Hook handler for `household_graph_changed` — drops the prompt cache.

    Fire-and-forget. The cost of an extra DB roundtrip on the next STT call is
    cheap (5 ms) compared to keeping a stale "Personen" / "Räume" list around
    for up to 5 minutes after a rename or member change.
    """
    get_whisper_prompt_builder().invalidate()
    logger.debug(f"WhisperPromptBuilder cache invalidated ({kind} {mutation})")
    return None


async def resolve_first_speaker_from_room(
    *, room_id: int | None
) -> Optional[int]:
    """Return the first known user_id currently in `room_id`, or None.

    Uses the `resolve_room_occupants` hook (ha_glue's BLE-presence handler).
    First-utterance bias case: speaker recognition runs after STT today, but
    when room occupancy is known we can use it to seed the prompt before STT.
    """
    if room_id is None:
        return None
    results = await run_hooks("resolve_room_occupants", room_id=room_id)
    for result in results:
        if isinstance(result, list) and result:
            for uid in result:
                if isinstance(uid, int):
                    return uid
    return None
