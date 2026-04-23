"""
Unit tests for paperless_example_retriever — the PR 3 retrieval layer
that pulls past confirm-diffs by similarity to the current doc_text.

Pure-unit, heavy mocking. The retriever's contract is intentionally
forgiving (silent fallback on every failure mode), so most of the
coverage here is about asserting that errors are swallowed and the
caller can keep going with seed-only prompts.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.paperless_example_retriever import (
    embed_doc_text,
    fetch_relevant_examples,
)


# ---------------------------------------------------------------------------
# fetch_relevant_examples — input guards
# ---------------------------------------------------------------------------


class TestInputGuards:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_blank_doc_text_returns_empty(self):
        assert await fetch_relevant_examples("") == []
        assert await fetch_relevant_examples("   \n\t  ") == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_zero_limit_returns_empty(self):
        assert await fetch_relevant_examples("real text", limit=0) == []
        assert await fetch_relevant_examples("real text", limit=-1) == []


# ---------------------------------------------------------------------------
# fetch_relevant_examples — embed failure
# ---------------------------------------------------------------------------


class TestEmbedFailure:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_embed_exception_returns_empty_silent(self):
        """Embed call raises → caller gets []. No exception bubbles up,
        because the seed prompt must keep working when Ollama is down."""
        with patch(
            "services.paperless_example_retriever._embed_doc_text",
            AsyncMock(side_effect=RuntimeError("ollama down")),
        ):
            result = await fetch_relevant_examples("doc text")
        assert result == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_embed_returns_none_returns_empty(self):
        with patch(
            "services.paperless_example_retriever._embed_doc_text",
            AsyncMock(return_value=None),
        ):
            result = await fetch_relevant_examples("doc text")
        assert result == []


# ---------------------------------------------------------------------------
# fetch_relevant_examples — DB query
# ---------------------------------------------------------------------------


class TestDBQuery:
    def _make_session_factory(self, rows):
        """Build an AsyncSessionLocal-shaped factory whose execute()
        returns the given rows. We don't go through the real ORM —
        callers consume .scalars().all() and that's it."""
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=rows)
        result = MagicMock()
        result.scalars = MagicMock(return_value=scalars)

        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        def _factory():
            return session
        return _factory

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_rows_with_expected_shape(self):
        row = SimpleNamespace(
            doc_text="Stadtwerke Rechnung 2025",
            llm_output={"correspondent": "Telekom"},
            user_approved={"correspondent": "Deutsche Telekom"},
            source="confirm_diff",
        )
        with patch(
            "services.paperless_example_retriever._embed_doc_text",
            AsyncMock(return_value=[0.1] * 8),
        ):
            with patch(
                "services.database.AsyncSessionLocal",
                self._make_session_factory([row]),
            ):
                result = await fetch_relevant_examples("Stadtwerke")

        assert len(result) == 1
        assert result[0]["doc_text"] == "Stadtwerke Rechnung 2025"
        assert result[0]["llm_output"]["correspondent"] == "Telekom"
        assert result[0]["user_approved"]["correspondent"] == "Deutsche Telekom"
        assert result[0]["source"] == "confirm_diff"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_db_exception_returns_empty_silent(self):
        """DB failure → caller gets []. Same fallback rule as embed."""
        broken_session = AsyncMock()
        broken_session.execute = AsyncMock(side_effect=RuntimeError("db gone"))
        broken_session.__aenter__ = AsyncMock(return_value=broken_session)
        broken_session.__aexit__ = AsyncMock(return_value=None)

        def _factory():
            return broken_session

        with patch(
            "services.paperless_example_retriever._embed_doc_text",
            AsyncMock(return_value=[0.1] * 8),
        ):
            with patch(
                "services.database.AsyncSessionLocal",
                _factory,
            ):
                result = await fetch_relevant_examples("Stadtwerke")
        assert result == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_empty_table_returns_empty(self):
        with patch(
            "services.paperless_example_retriever._embed_doc_text",
            AsyncMock(return_value=[0.1] * 8),
        ):
            with patch(
                "services.database.AsyncSessionLocal",
                self._make_session_factory([]),
            ):
                result = await fetch_relevant_examples("Stadtwerke")
        assert result == []


# ---------------------------------------------------------------------------
# embed_doc_text public wrapper
# ---------------------------------------------------------------------------


class TestEmbedDocTextPublic:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_blank_text_returns_none(self):
        assert await embed_doc_text("") is None
        assert await embed_doc_text("   ") is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_swallows_exceptions_returns_none(self):
        """Caller (commit tool) persists the row anyway when this
        returns None — must NEVER raise."""
        with patch(
            "services.paperless_example_retriever._embed_doc_text",
            AsyncMock(side_effect=RuntimeError("ollama down")),
        ):
            assert await embed_doc_text("real text") is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_embedding_on_success(self):
        with patch(
            "services.paperless_example_retriever._embed_doc_text",
            AsyncMock(return_value=[0.1, 0.2, 0.3]),
        ):
            result = await embed_doc_text("real text")
        assert result == [0.1, 0.2, 0.3]
