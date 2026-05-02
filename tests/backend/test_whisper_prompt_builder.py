"""Tests for WhisperPromptBuilder (Phase B-3)."""
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

# Pre-mock optional native deps so module import doesn't drag in faster_whisper.
_missing_stubs = [
    "asyncpg", "faster_whisper", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "piper", "piper.voice",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import pytest

from services.whisper_prompt_builder import (
    WhisperPromptBuilder,
    get_whisper_prompt_builder,
    resolve_first_speaker_from_room,
)


def _make_db_session(users: list[tuple[int, str | None, str]], rooms: list[tuple[int, str]]):
    """A session whose `execute()` returns mock results matching select shape."""
    session = MagicMock()

    user_result = MagicMock()
    user_result.all.return_value = users
    room_result = MagicMock()
    room_result.all.return_value = rooms

    call_results = iter([user_result, room_result])
    session.execute = AsyncMock(side_effect=lambda *args, **kw: next(call_results))
    return session


@pytest.mark.unit
class TestPlatformDefaultBuilder:

    @pytest.mark.asyncio
    async def test_basic_prompt_with_user_and_room(self):
        builder = WhisperPromptBuilder()
        db = _make_db_session(
            users=[(1, "Eduard", "evdb"), (2, "Anna", "anna")],
            rooms=[(10, "Wohnzimmer"), (20, "Küche")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            prompt = await builder.build(user_id=1, room_id=10, language="de", db_session=db)

        assert "Sprecher: Eduard" in prompt
        assert "Raum: Wohnzimmer" in prompt
        assert "Personen: Anna" in prompt
        assert "Räume: Küche" in prompt

    @pytest.mark.asyncio
    async def test_prompt_omits_speaker_when_user_unknown(self):
        builder = WhisperPromptBuilder()
        db = _make_db_session(
            users=[(1, "Eduard", "evdb")],
            rooms=[(10, "Wohnzimmer")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            prompt = await builder.build(user_id=None, room_id=10, language="de", db_session=db)

        assert prompt is not None
        assert "Sprecher" not in prompt
        assert "Raum: Wohnzimmer" in prompt
        # The single user becomes part of "Personen" since user_id doesn't match
        assert "Personen: Eduard" in prompt

    @pytest.mark.asyncio
    async def test_english_localization(self):
        builder = WhisperPromptBuilder()
        db = _make_db_session(
            users=[(1, "Eduard", "evdb")],
            rooms=[(10, "Living Room")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            prompt = await builder.build(user_id=1, room_id=10, language="en", db_session=db)

        assert "Speaker: Eduard" in prompt
        assert "Room: Living Room" in prompt

    @pytest.mark.asyncio
    async def test_unknown_language_falls_back_to_german_labels(self):
        builder = WhisperPromptBuilder()
        db = _make_db_session(
            users=[(1, "Eduard", "evdb")],
            rooms=[(10, "Wohnzimmer")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            prompt = await builder.build(user_id=1, room_id=10, language="fr", db_session=db)

        assert "Sprecher: Eduard" in prompt

    @pytest.mark.asyncio
    async def test_returns_none_when_no_context_available(self):
        builder = WhisperPromptBuilder()
        db = _make_db_session(users=[], rooms=[])
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            prompt = await builder.build(user_id=None, room_id=None, language="de", db_session=db)

        assert prompt is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_db_session_and_no_plugin(self):
        builder = WhisperPromptBuilder()
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            prompt = await builder.build(user_id=1, room_id=10, language="de", db_session=None)

        assert prompt is None

    @pytest.mark.asyncio
    async def test_db_failure_returns_none_not_crash(self):
        """DB exceptions during prompt build must not break the calling pipeline."""
        builder = WhisperPromptBuilder()
        broken_session = MagicMock()
        broken_session.execute = AsyncMock(side_effect=RuntimeError("DB exploded"))
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            prompt = await builder.build(user_id=1, room_id=10, language="de", db_session=broken_session)

        assert prompt is None


@pytest.mark.unit
class TestPluginPrecedence:

    @pytest.mark.asyncio
    async def test_plugin_string_wins_over_default(self):
        """A non-None hook result short-circuits the platform default."""
        builder = WhisperPromptBuilder()
        with patch(
            "services.whisper_prompt_builder.run_hooks",
            AsyncMock(return_value=["Plugin-supplied bias prompt."]),
        ):
            prompt = await builder.build(user_id=1, room_id=10, language="de", db_session=None)

        assert prompt == "Plugin-supplied bias prompt."

    @pytest.mark.asyncio
    async def test_first_non_empty_plugin_wins(self):
        """Empty/whitespace plugin returns are skipped, first usable wins."""
        builder = WhisperPromptBuilder()
        with patch(
            "services.whisper_prompt_builder.run_hooks",
            AsyncMock(return_value=["  ", "real bias", "later bias"]),
        ):
            prompt = await builder.build(user_id=1, room_id=10, language="de", db_session=None)

        assert prompt == "real bias"

    @pytest.mark.asyncio
    async def test_plugin_returning_none_falls_through_to_default(self):
        builder = WhisperPromptBuilder()
        db = _make_db_session(
            users=[(1, "Eduard", "evdb")],
            rooms=[(10, "Wohnzimmer")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            prompt = await builder.build(user_id=1, room_id=10, language="de", db_session=db)

        assert prompt is not None
        assert "Sprecher: Eduard" in prompt


@pytest.mark.unit
class TestCacheBehavior:

    @pytest.mark.asyncio
    async def test_repeat_same_key_hits_cache(self):
        builder = WhisperPromptBuilder()
        db = _make_db_session(
            users=[(1, "Eduard", "evdb")],
            rooms=[(10, "Wohnzimmer")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            await builder.build(user_id=1, room_id=10, language="de", db_session=db)
            # Second call must NOT execute additional DB queries.
            db.execute.reset_mock()
            await builder.build(user_id=1, room_id=10, language="de", db_session=db)

        assert db.execute.call_count == 0
        stats = builder.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    @pytest.mark.asyncio
    async def test_different_keys_dont_share_cache(self):
        builder = WhisperPromptBuilder()
        db = _make_db_session(
            users=[(1, "Eduard", "evdb")],
            rooms=[(10, "Wohnzimmer"), (20, "Küche")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            await builder.build(user_id=1, room_id=10, language="de", db_session=db)

        # New session for the second call (the first iteration consumed the
        # mock's iterator) — different room_id triggers fresh resolution.
        db2 = _make_db_session(
            users=[(1, "Eduard", "evdb")],
            rooms=[(10, "Wohnzimmer"), (20, "Küche")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            await builder.build(user_id=1, room_id=20, language="de", db_session=db2)

        assert builder.stats()["misses"] == 2

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self):
        """Past the TTL the cache must reject the entry and re-resolve."""
        builder = WhisperPromptBuilder()
        db = _make_db_session(
            users=[(1, "Eduard", "evdb")],
            rooms=[(10, "Wohnzimmer")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            with patch("services.whisper_prompt_builder.time") as mock_time:
                mock_time.monotonic.return_value = 0.0
                await builder.build(user_id=1, room_id=10, language="de", db_session=db)

                # Jump past the 5-minute TTL
                mock_time.monotonic.return_value = 400.0
                # Need a fresh DB mock since the previous one's iterator is exhausted
                db2 = _make_db_session(
                    users=[(1, "Eduard", "evdb")],
                    rooms=[(10, "Wohnzimmer")],
                )
                await builder.build(user_id=1, room_id=10, language="de", db_session=db2)

        assert builder.stats()["misses"] == 2

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache(self):
        builder = WhisperPromptBuilder()
        db = _make_db_session(
            users=[(1, "Eduard", "evdb")],
            rooms=[(10, "Wohnzimmer")],
        )
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            await builder.build(user_id=1, room_id=10, language="de", db_session=db)

        builder.invalidate()
        assert builder.stats()["size"] == 0


@pytest.mark.unit
class TestPromptLength:

    @pytest.mark.asyncio
    async def test_long_prompt_truncated(self):
        """Plugin or DB-derived prompt over the cap is truncated, not rejected."""
        builder = WhisperPromptBuilder()
        long_prompt = "a" * 500
        with patch(
            "services.whisper_prompt_builder.run_hooks",
            AsyncMock(return_value=[long_prompt]),
        ):
            prompt = await builder.build(user_id=1, room_id=10, language="de", db_session=None)

        assert prompt is not None
        assert len(prompt) <= 220


@pytest.mark.unit
class TestHouseholdGraphChangedHook:

    @pytest.mark.asyncio
    async def test_invalidate_handler_clears_cache(self):
        """The handler that ships with the module clears the singleton's cache."""
        from services.whisper_prompt_builder import whisper_prompt_household_changed

        builder = get_whisper_prompt_builder()
        # Seed the cache so we have something to clear.
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=["seed"])):
            await builder.build(user_id=1, room_id=10, language="de", db_session=None)
        assert builder.stats()["size"] == 1

        await whisper_prompt_household_changed(kind="user", mutation="created")
        assert builder.stats()["size"] == 0

    @pytest.mark.asyncio
    async def test_handler_returns_none_for_fire_and_forget_contract(self):
        """Hook returns None so run_hooks() doesn't accumulate it as a result."""
        from services.whisper_prompt_builder import whisper_prompt_household_changed
        result = await whisper_prompt_household_changed(kind="room", mutation="updated")
        assert result is None


@pytest.mark.unit
class TestSingletonAccessor:

    def test_get_whisper_prompt_builder_returns_singleton(self):
        a = get_whisper_prompt_builder()
        b = get_whisper_prompt_builder()
        assert a is b


@pytest.mark.unit
class TestRoomOccupantsResolver:

    @pytest.mark.asyncio
    async def test_resolves_first_user_from_hook_result(self):
        with patch(
            "services.whisper_prompt_builder.run_hooks",
            AsyncMock(return_value=[[42, 7]]),
        ):
            uid = await resolve_first_speaker_from_room(room_id=10)
        assert uid == 42

    @pytest.mark.asyncio
    async def test_skips_empty_lists_in_hook_results(self):
        with patch(
            "services.whisper_prompt_builder.run_hooks",
            AsyncMock(return_value=[[], [99]]),
        ):
            uid = await resolve_first_speaker_from_room(room_id=10)
        assert uid == 99

    @pytest.mark.asyncio
    async def test_returns_none_when_room_id_missing(self):
        uid = await resolve_first_speaker_from_room(room_id=None)
        assert uid is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_handlers_resolved(self):
        with patch("services.whisper_prompt_builder.run_hooks", AsyncMock(return_value=[])):
            uid = await resolve_first_speaker_from_room(room_id=10)
        assert uid is None
