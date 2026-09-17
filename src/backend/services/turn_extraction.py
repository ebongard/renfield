"""Background memory/KG extraction for a completed conversational turn.

ONE seam for every turn producer. It lived inline in
``api/websocket/chat_handler.py``, so only the BROWSER path ever fed the
extractors: a spoken turn over ``/ws/satellite`` was transcribed, answered,
persisted — and forgotten, because the satellite handler ran neither the
``post_message`` hooks nor memory extraction. Lifting the two coroutines plus
the spawn policy here lets the voice path reuse the exact same code instead of
growing a second, drifting copy.

Everything here is **fire-and-forget**: callers spawn AFTER the user already has
their answer (the chat `done` frame / the satellite's persisted turn), so no
producer's latency — spinner, TTS, wakeword re-arm — depends on an LLM
extraction round-trip. Failures are swallowed and logged; a turn never dies of a
failed extraction.

Privacy note for voice callers: this module does NOT decide who may be
remembered. The satellite path gates on a RECOGNIZED speaker before calling in
(see ``satellite_handler._spawn_satellite_extraction``) — an unattributed voice
turn must not become somebody's memory.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger

from services.database import AsyncSessionLocal
from utils.config import settings

# Prevent background tasks from being garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()


async def extract_memories_background(
    user_message: str,
    assistant_response: str,
    user_id: int | None,
    session_id: str | None,
    lang: str,
    captured_kg_subjects: set[str] | None = None,
) -> None:
    """Background task: extract and save memories from a conversation exchange.

    Always logs the extraction outcome (including 0 memories) so silent
    failures of the LLM extractor or guard short-circuits are visible in
    production logs. Without this, missing memories look identical to a
    bug-skipped trigger.

    ``captured_kg_subjects`` (Phase 3-subsume per-fact fix): when the caller
    runs the `post_message`/KG extraction FIRST in the same ordered
    background coroutine, the subject names the KG actually captured a relation
    for this turn are threaded here so the subsume gate is per-fact, not a
    subject-level proxy. None = uncoordinated → service falls back to the proxy.
    """
    logger.info(
        f"📝 Memory extraction starting (session={session_id}, user_id={user_id}, "
        f"user_msg_len={len(user_message)}, assistant_msg_len={len(assistant_response)})"
    )
    try:
        async with AsyncSessionLocal() as db:
            from services.conversation_memory_service import ConversationMemoryService
            service = ConversationMemoryService(db)
            memories = await service.extract_and_save(
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                session_id=session_id,
                lang=lang,
                captured_kg_subjects=captured_kg_subjects,
            )
            logger.info(
                f"📝 Memory extraction done: extracted={len(memories)} "
                f"(session={session_id})"
            )
            # Chat branching (Phase 2) race guard: a fork/switch may have landed
            # WHILE this background extraction ran, so re-derive is_active from the
            # conversation's CURRENT active leaf. Idempotent — whichever of
            # extraction/fork commits last re-fixes truth, so a memory written for
            # an abandoned turn can't linger active. Flag-gated → zero cost when
            # branching is off (the prod default).
            if session_id and settings.chat_branching_enabled:
                try:
                    from sqlalchemy import select

                    from models.database import Conversation as _Conv
                    from services.conversation_service import (
                        ConversationService as _CS,
                    )
                    _conv = (
                        await db.execute(
                            select(_Conv).where(_Conv.session_id == session_id)
                        )
                    ).scalar_one_or_none()
                    if _conv is not None:
                        _changed = await _CS(db).recompute_memory_activation(_conv)
                        if _changed:
                            await db.commit()
                except Exception as _ge:  # noqa: BLE001
                    logger.warning(
                        f"⚠️ Post-extraction branch recompute failed: {_ge}"
                    )
    except Exception as e:
        logger.warning(f"Memory extraction failed: {e}", exc_info=True)


async def extract_structured_background(
    user_message: str,
    assistant_response: str,
    user_id: int | None,
    session_id: str | None,
    lang: str,
) -> None:
    """Ordered background coroutine for the Phase 3-subsume coordination.

    Runs the `post_message` hooks FIRST (KG extraction + plugins like the twin),
    capturing the subject NAMES of the relations the KG actually saved this turn
    into a shared set, then runs memory extraction with that set so the subsume
    gate is per (subject, turn): a state/attribute fact about a subject for whom
    no relation was captured this turn is kept flat. (NOT truly per-fact — the
    set holds subject names, not (subject, object) pairs, so a same-turn same-
    subject state fact alongside an entity-object fact is still subsumed; see
    ConversationMemoryService._should_subsume_fact.) KG extraction runs exactly
    ONCE (in the hook); the set is the only cross-task signal — no double-extract.

    Stays entirely in the background (this coroutine is spawned AFTER the turn's
    answer is delivered), so re-sequencing KG-before-memory never delays the user
    response / TTS / wakeword. Used ONLY when subsume coordination is active;
    otherwise the two tasks stay independent + concurrent (legacy behavior,
    byte-identical).
    """
    from utils.hooks import run_hooks

    captured_kg_subjects: set[str] = set()
    # 1) post_message hooks first — KG populates the set. The hook reads it under
    #    the kwarg name `captured_subjects` (see kg_post_message_hook). Plugins
    #    (twin) ignore the extra kwarg (**kwargs). run_hooks never raises.
    await run_hooks(
        "post_message",
        user_msg=user_message,
        assistant_msg=assistant_response,
        user_id=user_id,
        session_id=session_id,
        lang=lang,
        captured_subjects=captured_kg_subjects,
    )
    # 2) memory extraction with the per-turn captured set as the subsume signal.
    await extract_memories_background(
        user_message=user_message,
        assistant_response=assistant_response,
        user_id=user_id,
        session_id=session_id,
        lang=lang,
        captured_kg_subjects=captured_kg_subjects,
    )


@dataclass(frozen=True)
class TurnExtractionSpawn:
    """Outcome of one spawn decision.

    ``task`` is None when the turn was skipped (flags off / empty answer /
    failed tool action). ``owns_post_message`` is True when the spawned
    coroutine dispatches the `post_message` hooks itself — the caller must then
    NOT spawn them again, or KG extraction would run twice for one turn.
    """

    task: asyncio.Task | None
    owns_post_message: bool


def spawn_memory_extraction(
    *,
    user_message: str,
    assistant_response: str,
    user_id: int | None,
    session_id: str | None,
    lang: str,
    action_success: bool | None,
) -> TurnExtractionSpawn:
    """Schedule this turn's memory extraction (never awaited by the caller).

    Skip policy (shared by every producer — one policy, not one per surface):
    memory flags off, an empty answer, or a turn whose tool action FAILED
    (``action_success is False``) → nothing is scheduled. Extracting memories
    from an error response would re-inject the error string into long-term
    memory as if it were a stable fact.

    With subsume active the ordered KG-then-memory coroutine runs instead, and
    it owns the `post_message` dispatch (see ``TurnExtractionSpawn``).
    """
    should_run = (
        settings.memory_enabled
        and settings.memory_extraction_enabled
        and bool(assistant_response)
        and action_success is not False
    )
    coordinate = bool(getattr(settings, "memory_subsume_to_kg", False))
    if not should_run:
        return TurnExtractionSpawn(task=None, owns_post_message=False)

    logger.debug(
        f"📝 Scheduling memory extraction (session={session_id}, "
        f"subsume_coordinate={coordinate})"
    )
    if coordinate:
        task = asyncio.create_task(
            extract_structured_background(
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                session_id=session_id,
                lang=lang,
            )
        )
    else:
        task = asyncio.create_task(
            extract_memories_background(
                user_message=user_message,
                assistant_response=assistant_response,
                user_id=user_id,
                session_id=session_id,
                lang=lang,
            )
        )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return TurnExtractionSpawn(task=task, owns_post_message=coordinate)


def spawn_post_message_hooks(
    *,
    user_msg: str,
    assistant_msg: str,
    user_id: int | None,
    session_id: str | None,
    lang: str,
) -> asyncio.Task:
    """Fire-and-forget `post_message` dispatch (KG extraction + plugins).

    Only for callers whose extraction spawn did NOT take ownership of it
    (``TurnExtractionSpawn.owns_post_message is False``) — otherwise KG
    extraction would run twice for a single turn. Still fired when memory
    extraction was skipped, so KG + plugins are not starved by a failed-action
    or empty-answer turn.
    """
    from utils.hooks import run_hooks

    task = asyncio.create_task(run_hooks(
        "post_message",
        user_msg=user_msg,
        assistant_msg=assistant_msg,
        user_id=user_id,
        session_id=session_id,
        lang=lang,
    ))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
