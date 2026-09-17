"""Spoken turns must reach the memory/KG extractors — but ONLY when the speaker
was recognized.

Gap this covers: `ha_glue/api/websocket/satellite_handler.py` persisted the turn
(`ollama.save_message`) and kept a 5-exchange in-memory history, but never ran
`post_message` hooks nor memory extraction. So anything said to a satellite was
forgotten, while the same sentence typed in the browser was remembered
(`api/websocket/chat_handler.py` spawns the extraction after the `done` frame).

Two properties are load-bearing here:

1. **Privacy boundary** — extraction runs ONLY for a turn with a real
   ``user_id`` (Speaker → User via ``User.speaker_id``). An unattributed voice
   turn must not become somebody's memory, and must not fire the ``post_message``
   hooks either (KG extraction + plugins are the same leak).
2. **Never on the critical path** — the spawn is fire-and-forget: it must not be
   awaited inline (a voice turn must not get slower) and a failing extractor must
   not break the turn or its TTS.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit]


def _memory_on(settings_mock, *, subsume: bool = False) -> None:
    settings_mock.memory_enabled = True
    settings_mock.memory_extraction_enabled = True
    settings_mock.memory_subsume_to_kg = subsume


# ---------------------------------------------------------------------------
# The satellite seam: privacy gate + fire-and-forget spawn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSatelliteExtractionGate:
    async def test_recognized_speaker_spawns_exactly_one_extraction(self):
        """A recognized speaker's turn schedules ONE extraction carrying the
        resolved user_id + the satellite conversation's session id."""
        from ha_glue.api.websocket import satellite_handler as sh

        calls: list[dict] = []

        async def _fake_extract(**kwargs):
            calls.append(kwargs)

        with patch("services.turn_extraction.extract_memories_background", _fake_extract), \
             patch.object(sh, "spawn_post_message_hooks", MagicMock()), \
             patch("services.turn_extraction.settings") as _st:
            _memory_on(_st)
            spawn = sh._spawn_satellite_extraction(
                user_text="Ich mag Espresso",
                response_text="Notiert.",
                user_id=42,
                session_id="sat-session-1",
                lang="de",
                action_success=None,
            )
            assert spawn is not None and spawn.task is not None
            await spawn.task

        assert len(calls) == 1, "exactly one extraction per recognized turn"
        assert calls[0]["user_id"] == 42
        assert calls[0]["session_id"] == "sat-session-1"
        assert calls[0]["lang"] == "de"
        assert calls[0]["user_message"] == "Ich mag Espresso"
        assert calls[0]["assistant_response"] == "Notiert."

    async def test_unrecognized_speaker_extracts_nothing(self):
        """PRIVACY: user_id=None (voice not attributed to a person) must extract
        NOTHING — no memory extraction and no post_message hooks (KG/plugins)."""
        from ha_glue.api.websocket import satellite_handler as sh

        spawned: list[str] = []

        def _fake_spawn_memory(**kwargs):
            spawned.append("memory")
            raise AssertionError("memory extraction must not be spawned")

        def _fake_spawn_hooks(**kwargs):
            spawned.append("hooks")
            raise AssertionError("post_message hooks must not be spawned")

        with patch.object(sh, "spawn_memory_extraction", _fake_spawn_memory), \
             patch.object(sh, "spawn_post_message_hooks", _fake_spawn_hooks):
            result = sh._spawn_satellite_extraction(
                user_text="Ich mag Espresso",
                response_text="Notiert.",
                user_id=None,
                session_id="sat-session-1",
                lang="de",
                action_success=None,
            )

        assert result is None
        assert spawned == [], "an unattributed voice turn must reach no extractor"

    async def test_raising_extractor_does_not_break_the_turn(self):
        """A failing extraction is swallowed — the spoken turn (and its TTS)
        must survive it."""
        from ha_glue.api.websocket import satellite_handler as sh

        def _boom(**kwargs):
            raise RuntimeError("extractor exploded")

        with patch.object(sh, "spawn_memory_extraction", _boom):
            # Must not raise.
            result = sh._spawn_satellite_extraction(
                user_text="u",
                response_text="a",
                user_id=7,
                session_id="s",
                lang="de",
                action_success=None,
            )
        assert result is None

    async def test_spawn_is_not_awaited_inline(self):
        """The extraction must be scheduled, not awaited: the helper returns
        BEFORE the extractor has run a single step."""
        from ha_glue.api.websocket import satellite_handler as sh

        started = asyncio.Event()
        released = asyncio.Event()

        async def _slow_extract(**kwargs):
            started.set()
            await released.wait()

        with patch("services.turn_extraction.extract_memories_background", _slow_extract), \
             patch.object(sh, "spawn_post_message_hooks", MagicMock()), \
             patch("services.turn_extraction.settings") as _st:
            _memory_on(_st)
            spawn = sh._spawn_satellite_extraction(
                user_text="u", response_text="a", user_id=7,
                session_id="s", lang="de", action_success=None,
            )
            # Nothing ran yet — create_task only schedules.
            assert not started.is_set(), "extraction must not run inline"
            assert not spawn.task.done()
            released.set()
            await spawn.task
            assert started.is_set()

    async def test_failed_action_turn_is_skipped_like_chat(self):
        """Same skip policy as the chat path: a turn whose tool action FAILED
        does not become memory (the error string would be re-injected as fact)."""
        from ha_glue.api.websocket import satellite_handler as sh

        calls: list[dict] = []

        async def _fake_extract(**kwargs):
            calls.append(kwargs)

        with patch("services.turn_extraction.extract_memories_background", _fake_extract), \
             patch.object(sh, "spawn_post_message_hooks", MagicMock()) as _hooks, \
             patch("services.turn_extraction.settings") as _st:
            _memory_on(_st)
            spawn = sh._spawn_satellite_extraction(
                user_text="mach das Licht an",
                response_text="Das konnte ich nicht ausführen",
                user_id=42,
                session_id="s",
                lang="de",
                action_success=False,
            )

        assert spawn is not None
        assert spawn.task is None, "failed-action turn must not extract memories"
        assert calls == []
        # KG + plugins are NOT starved by a failed-action turn (chat does the same).
        assert _hooks.call_count == 1


class TestSatelliteHandlerWiring:
    """The seam is useless if the WS loop never calls it — assert the wiring
    statically (the loop itself needs a live socket + whisper + ollama)."""

    def test_websocket_loop_calls_the_extraction_seam(self):
        import ha_glue.api.websocket.satellite_handler as sh

        tree = ast.parse(Path(sh.__file__).read_text())
        target = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
             and n.name == "satellite_websocket"),
            None,
        )
        assert target is not None, "satellite_websocket not found"
        called = {
            n.func.id
            for n in ast.walk(target)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "_spawn_satellite_extraction" in called, (
            "the satellite turn must hand off to the extraction seam — without "
            "this call nothing said to a satellite is ever remembered"
        )


# ---------------------------------------------------------------------------
# The shared seam itself (used by BOTH chat and satellite)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSharedSpawnPolicy:
    async def test_subsume_on_uses_the_coordinated_coroutine(self):
        """With subsume active the ordered KG-then-memory coroutine runs and
        OWNS the post_message dispatch (KG must run exactly once)."""
        from services import turn_extraction as te

        seen: list[str] = []

        async def _fake_structured(**kwargs):
            seen.append("structured")

        with patch.object(te, "extract_structured_background", _fake_structured), \
             patch.object(te, "settings") as _st:
            _memory_on(_st, subsume=True)
            spawn = te.spawn_memory_extraction(
                user_message="u", assistant_response="a", user_id=1,
                session_id="s", lang="de", action_success=None,
            )
            await spawn.task

        assert seen == ["structured"]
        assert spawn.owns_post_message is True

    async def test_memory_flags_off_schedules_nothing(self):
        from services import turn_extraction as te

        with patch.object(te, "settings") as _st:
            _memory_on(_st)
            _st.memory_enabled = False
            spawn = te.spawn_memory_extraction(
                user_message="u", assistant_response="a", user_id=1,
                session_id="s", lang="de", action_success=None,
            )
        assert spawn.task is None
        assert spawn.owns_post_message is False

    async def test_empty_response_schedules_nothing(self):
        from services import turn_extraction as te

        with patch.object(te, "settings") as _st:
            _memory_on(_st)
            spawn = te.spawn_memory_extraction(
                user_message="u", assistant_response="", user_id=1,
                session_id="s", lang="de", action_success=None,
            )
        assert spawn.task is None

    async def test_spawned_task_is_kept_referenced(self):
        """GC guard: a bare create_task result can be collected mid-flight."""
        from services import turn_extraction as te

        released = asyncio.Event()

        async def _slow(**kwargs):
            await released.wait()

        with patch.object(te, "extract_memories_background", _slow), \
             patch.object(te, "settings") as _st:
            _memory_on(_st)
            spawn = te.spawn_memory_extraction(
                user_message="u", assistant_response="a", user_id=1,
                session_id="s", lang="de", action_success=None,
            )
            assert spawn.task in te._background_tasks
            released.set()
            await spawn.task
            # done_callback discards it again
            assert spawn.task not in te._background_tasks


class TestChatHandlerStillUsesTheSameCode:
    """The refactor lifted the two extraction coroutines out of chat_handler
    into the shared module — chat_handler must reference the SAME objects, so
    the browser path cannot drift from the voice path."""

    def test_chat_handler_reexports_shared_extractors(self):
        from api.websocket import chat_handler as ch
        from services import turn_extraction as te

        assert ch._extract_memories_background is te.extract_memories_background
        assert ch._extract_structured_background is te.extract_structured_background
