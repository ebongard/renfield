"""Tests for speaker_vocabulary_service (Phase B-3 follow-up)."""
import sys
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

# Pre-mock optional native deps that the import chain pulls in.
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

from services.speaker_vocabulary_service import (
    _tokenize,
    capture_transcript,
    vocab_initial_prompt_handler,
)


@pytest.mark.unit
class TestTokenizer:

    def test_drops_short_tokens(self):
        tokens = _tokenize("Ja so ist das", "de")
        # "Ja", "so", "ist", "das" — all <= 3 chars or stopwords
        assert tokens == []

    def test_keeps_meaningful_de_words(self):
        tokens = _tokenize("Schalte das Licht im Wohnzimmer an", "de")
        assert "schalte" in tokens
        assert "licht" in tokens
        assert "wohnzimmer" in tokens

    def test_drops_german_stopwords(self):
        tokens = _tokenize("Der Hund läuft schnell", "de")
        assert "der" not in tokens
        assert "hund" in tokens

    def test_drops_english_stopwords(self):
        tokens = _tokenize("The dog runs fast", "en")
        assert "the" not in tokens
        assert "dog" in tokens

    def test_drops_pure_numbers(self):
        tokens = _tokenize("Frigate 12345 Konferenzraum 2026", "de")
        assert "frigate" in tokens
        assert "konferenzraum" in tokens
        # The regex requires a leading letter so pure digit runs are already
        # filtered at the regex level, not by the isdigit() branch.
        assert "12345" not in tokens
        assert "2026" not in tokens

    def test_lowercases(self):
        tokens = _tokenize("Frigate Paperless Treehouse", "de")
        assert tokens == ["frigate", "paperless", "treehouse"]

    def test_preserves_umlauts(self):
        tokens = _tokenize("Schlüssel Übergabe Mahnung", "de")
        assert "schlüssel" in tokens
        assert "übergabe" in tokens
        assert "mahnung" in tokens


@pytest.mark.unit
class TestCaptureTranscript:

    @pytest.mark.asyncio
    async def test_skips_new_speakers(self):
        """Auto-enrolled 'Unbekannter Sprecher' rows must not feed the corpus."""
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal") as mock_factory:
            await capture_transcript(
                speaker_id=42, text="hello", language="de", is_new_speaker=True
            )
        mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_empty_text(self):
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal") as mock_factory:
            await capture_transcript(
                speaker_id=42, text="   ", language="de", is_new_speaker=False
            )
        mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_user_linked(self):
        """Identified speaker but no User account → no corpus row written."""
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        session.add = MagicMock()
        session.commit = AsyncMock()

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal", return_value=ctx):
            await capture_transcript(
                speaker_id=42, text="something", language="de", is_new_speaker=False
            )
        session.add.assert_not_called()
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_appends_corpus_row_for_known_user(self):
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = 7  # user_id
        session.execute = AsyncMock(return_value=result)
        session.add = MagicMock()
        session.commit = AsyncMock()

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal", return_value=ctx):
            await capture_transcript(
                speaker_id=42, text="Schalte das Licht ein", language="de", is_new_speaker=False
            )

        session.add.assert_called_once()
        session.commit.assert_called_once()
        added = session.add.call_args[0][0]
        assert added.user_id == 7
        assert added.text == "Schalte das Licht ein"
        assert added.language == "de"
        assert added.circle_tier == 0

    @pytest.mark.asyncio
    async def test_swallows_db_errors(self):
        """capture_transcript must not raise — it's fire-and-forget."""
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("DB exploded"))
        ctx.__aexit__ = AsyncMock(return_value=None)
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal", return_value=ctx):
            # Must not raise
            await capture_transcript(
                speaker_id=42, text="hello", language="de", is_new_speaker=False
            )


@pytest.mark.unit
class TestVocabInitialPromptHandler:

    @pytest.mark.asyncio
    async def test_returns_none_when_user_id_is_none(self):
        result = await vocab_initial_prompt_handler(
            user_id=None, room_id=10, language="de"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_cold_start(self):
        """No vocab rows yet → None, platform default takes over."""
        session = MagicMock()
        empty_result = MagicMock()
        empty_result.all.return_value = []
        session.execute = AsyncMock(return_value=empty_result)

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal", return_value=ctx):
            result = await vocab_initial_prompt_handler(
                user_id=1, room_id=None, language="de"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_builds_prompt_with_user_name_and_terms(self):
        session = MagicMock()
        terms_result = MagicMock()
        terms_result.all.return_value = [("frigate",), ("paperless",), ("wohnzimmer",)]
        user_result = MagicMock()
        user_result.first.return_value = ("Eduard", "evdb")
        session.execute = AsyncMock(side_effect=[terms_result, user_result])

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal", return_value=ctx):
            result = await vocab_initial_prompt_handler(
                user_id=1, room_id=None, language="de"
            )
        assert result is not None
        assert "Sprecher: Eduard" in result
        assert "Häufige Begriffe: frigate, paperless, wohnzimmer" in result

    @pytest.mark.asyncio
    async def test_builds_english_prompt(self):
        session = MagicMock()
        terms_result = MagicMock()
        terms_result.all.return_value = [("docker",), ("cluster",)]
        user_result = MagicMock()
        user_result.first.return_value = ("Eduard", "evdb")
        session.execute = AsyncMock(side_effect=[terms_result, user_result])

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal", return_value=ctx):
            result = await vocab_initial_prompt_handler(
                user_id=1, room_id=None, language="en"
            )
        assert result is not None
        assert "Speaker: Eduard" in result
        assert "Frequent terms: docker, cluster" in result

    @pytest.mark.asyncio
    async def test_truncates_long_prompt(self):
        session = MagicMock()
        terms_result = MagicMock()
        # 30 long terms → easily > 220 chars
        long_terms = [(f"langeswort{i}",) for i in range(30)]
        terms_result.all.return_value = long_terms
        user_result = MagicMock()
        user_result.first.return_value = ("Eduard", "evdb")
        session.execute = AsyncMock(side_effect=[terms_result, user_result])

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=None)
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal", return_value=ctx):
            result = await vocab_initial_prompt_handler(
                user_id=1, room_id=None, language="de"
            )
        assert result is not None
        assert len(result) <= 220

    @pytest.mark.asyncio
    async def test_swallows_db_errors_returns_none(self):
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("DB exploded"))
        ctx.__aexit__ = AsyncMock(return_value=None)
        with patch("services.speaker_vocabulary_service.AsyncSessionLocal", return_value=ctx):
            result = await vocab_initial_prompt_handler(
                user_id=1, room_id=None, language="de"
            )
        assert result is None
