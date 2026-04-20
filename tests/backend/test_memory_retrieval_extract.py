"""
Regression tests for the ConversationMemoryService -> MemoryRetrieval extraction
(Lane A3 of the second-brain-circles eng-review plan).

Critical invariant: behaviour of ConversationMemoryService.retrieve,
retrieve_essential, and retrieve_for_prompt must be IDENTICAL whether
routed through the legacy inline code (flag off) or through the extracted
MemoryRetrieval module (flag on).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.conversation_memory_service import ConversationMemoryService
from services.memory_retrieval import MemoryRetrieval


class TestBuildScopeFilterParity:
    """Both classes must build identical SQL scope-filter clauses."""

    @pytest.mark.unit
    def test_user_only_scope(self):
        legacy = ConversationMemoryService._build_scope_filter(user_id=42, team_ids=None)
        new = MemoryRetrieval._build_scope_filter(user_id=42, team_ids=None)
        assert legacy == new
        assert "scope = 'global'" in new
        assert "user_id = :user_id" in new

    @pytest.mark.unit
    def test_user_plus_team_scope(self):
        legacy = ConversationMemoryService._build_scope_filter(user_id=42, team_ids=["t1", "t2"])
        new = MemoryRetrieval._build_scope_filter(user_id=42, team_ids=["t1", "t2"])
        assert legacy == new
        assert "scope = 'team'" in new
        assert "team_id IN :team_ids" in new

    @pytest.mark.unit
    def test_global_only_scope(self):
        legacy = ConversationMemoryService._build_scope_filter(user_id=None, team_ids=None)
        new = MemoryRetrieval._build_scope_filter(user_id=None, team_ids=None)
        assert legacy == new
        assert legacy == "AND (scope = 'global')"

    @pytest.mark.unit
    def test_team_only_scope_no_user(self):
        legacy = ConversationMemoryService._build_scope_filter(user_id=None, team_ids=["t1"])
        new = MemoryRetrieval._build_scope_filter(user_id=None, team_ids=["t1"])
        assert legacy == new


class TestRecencyScoreParity:
    """Both classes must compute identical exponential decay scores.

    Note: _recency_score calls datetime.now() internally, so back-to-back calls
    pick up microsecond-level clock drift. The two implementations are
    byte-equivalent code; the test uses pytest.approx with a tight tolerance
    to absorb that drift without hiding actual algorithm divergence.
    """

    # Tolerance absorbs back-to-back datetime.now() drift; loose enough for slow CI boxes.
    _DRIFT_TOL = 1e-6

    @pytest.mark.unit
    def test_none_created_at_returns_neutral(self):
        legacy = ConversationMemoryService._recency_score(None)
        new = MemoryRetrieval._recency_score(None)
        # No now() call when created_at is None -- exact equality is safe here
        assert legacy == new == 0.5

    @pytest.mark.unit
    def test_just_created_returns_near_one(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        legacy = ConversationMemoryService._recency_score(now)
        new = MemoryRetrieval._recency_score(now)
        assert legacy == pytest.approx(new, abs=self._DRIFT_TOL)
        assert legacy > 0.99

    @pytest.mark.unit
    def test_one_half_life_decays_to_half(self):
        old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=14)
        legacy = ConversationMemoryService._recency_score(old, half_life_days=14.0)
        new = MemoryRetrieval._recency_score(old, half_life_days=14.0)
        assert legacy == pytest.approx(new, abs=self._DRIFT_TOL)
        assert 0.49 < legacy < 0.51

    @pytest.mark.unit
    def test_very_old_decays_toward_zero(self):
        old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=365)
        legacy = ConversationMemoryService._recency_score(old, half_life_days=14.0)
        new = MemoryRetrieval._recency_score(old, half_life_days=14.0)
        assert legacy == pytest.approx(new, abs=self._DRIFT_TOL)
        assert legacy < 0.01

    @pytest.mark.unit
    def test_custom_half_life_used(self):
        old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7)
        legacy_short = ConversationMemoryService._recency_score(old, half_life_days=3.5)
        new_short = MemoryRetrieval._recency_score(old, half_life_days=3.5)
        assert legacy_short == pytest.approx(new_short, abs=self._DRIFT_TOL)
        assert 0.24 < legacy_short < 0.26


class TestFlagRouting:
    """retrieve / retrieve_essential / retrieve_for_prompt must delegate when the flag is on."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_retrieve_flag_on_routes_to_memory_retrieval(self):
        db = MagicMock()
        service = ConversationMemoryService(db)
        sentinel = [{"id": 1, "content": "from MemoryRetrieval", "similarity": 0.9}]

        with patch("services.conversation_memory_service.settings") as svc_settings, \
             patch(
                 "services.memory_retrieval.MemoryRetrieval.retrieve",
                 new=AsyncMock(return_value=sentinel),
             ) as ret_call:
            svc_settings.circles_use_new_memory = True
            result = await service.retrieve("query", user_id=42, limit=5, threshold=0.5)

        ret_call.assert_called_once()
        call_kwargs = ret_call.call_args
        assert call_kwargs.kwargs.get("user_id") == 42
        assert call_kwargs.kwargs.get("limit") == 5
        assert call_kwargs.kwargs.get("threshold") == 0.5
        assert result is sentinel

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_retrieve_flag_off_does_not_route(self):
        db = MagicMock()
        service = ConversationMemoryService(db)

        with patch("services.conversation_memory_service.settings") as svc_settings, \
             patch.object(
                 ConversationMemoryService, "_get_embedding",
                 new=AsyncMock(side_effect=RuntimeError("simulated embedding failure")),
             ), \
             patch(
                 "services.memory_retrieval.MemoryRetrieval.retrieve",
                 new=AsyncMock(return_value=[{"should": "not-be-called"}]),
             ) as ret_call:
            svc_settings.circles_use_new_memory = False
            svc_settings.memory_retrieval_limit = 3
            svc_settings.memory_retrieval_threshold = 0.7

            result = await service.retrieve("query", user_id=1)

        assert result == []
        ret_call.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_retrieve_essential_flag_on_routes(self):
        db = MagicMock()
        service = ConversationMemoryService(db)
        sentinel = [{"id": 1, "content": "essential", "similarity": 1.0}]

        with patch("services.conversation_memory_service.settings") as svc_settings, \
             patch(
                 "services.memory_retrieval.MemoryRetrieval.retrieve_essential",
                 new=AsyncMock(return_value=sentinel),
             ) as ret_call:
            svc_settings.circles_use_new_memory = True
            result = await service.retrieve_essential(user_id=42, limit=10)

        ret_call.assert_called_once()
        call_kwargs = ret_call.call_args
        assert call_kwargs.kwargs.get("user_id") == 42
        assert call_kwargs.kwargs.get("limit") == 10
        assert result is sentinel

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_retrieve_essential_flag_off_does_not_route(self):
        """With the flag off, retrieve_essential must NOT delegate to MemoryRetrieval."""
        db = MagicMock()
        # Make db.execute raise so the inline path fails fast; we only assert
        # MemoryRetrieval.retrieve_essential is NOT called when the flag is off.
        db.execute = AsyncMock(side_effect=RuntimeError("inline path entered"))
        service = ConversationMemoryService(db)

        with patch("services.conversation_memory_service.settings") as svc_settings, \
             patch(
                 "services.memory_retrieval.MemoryRetrieval.retrieve_essential",
                 new=AsyncMock(return_value=[{"should": "not-be-called"}]),
             ) as ret_call:
            svc_settings.circles_use_new_memory = False
            svc_settings.memory_essential_threshold = 0.85
            svc_settings.memory_retrieval_limit = 3

            with pytest.raises(RuntimeError, match="inline path entered"):
                await service.retrieve_essential(user_id=1)

        ret_call.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_retrieve_for_prompt_flag_on_routes(self):
        db = MagicMock()
        service = ConversationMemoryService(db)
        sentinel = {"essential": [], "procedural": [], "semantic": [], "episodic": []}

        with patch("services.conversation_memory_service.settings") as svc_settings, \
             patch(
                 "services.memory_retrieval.MemoryRetrieval.retrieve_for_prompt",
                 new=AsyncMock(return_value=sentinel),
             ) as ret_call:
            svc_settings.circles_use_new_memory = True
            result = await service.retrieve_for_prompt(
                "query", user_id=42, team_ids=["t1"], budget_chars=2000
            )

        ret_call.assert_called_once()
        call_kwargs = ret_call.call_args
        assert call_kwargs.kwargs.get("user_id") == 42
        assert call_kwargs.kwargs.get("team_ids") == ["t1"]
        assert call_kwargs.kwargs.get("budget_chars") == 2000
        assert result is sentinel

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_retrieve_for_prompt_flag_off_does_not_route(self):
        """With the flag off, retrieve_for_prompt must NOT delegate to MemoryRetrieval."""
        db = MagicMock()
        # The inline path calls retrieve_essential first; make IT delegate to a
        # raising stub so we exit fast. Critical assertion: retrieve_for_prompt
        # on MemoryRetrieval is NEVER reached when flag is off.
        service = ConversationMemoryService(db)

        with patch("services.conversation_memory_service.settings") as svc_settings, \
             patch.object(
                 ConversationMemoryService, "retrieve_essential",
                 new=AsyncMock(side_effect=RuntimeError("inline retrieve_essential entered")),
             ), \
             patch(
                 "services.memory_retrieval.MemoryRetrieval.retrieve_for_prompt",
                 new=AsyncMock(return_value={"should": ["not-be-called"]}),
             ) as ret_call:
            svc_settings.circles_use_new_memory = False
            svc_settings.memory_retrieval_budget_chars = 2000
            svc_settings.memory_episodic_enabled = False

            with pytest.raises(RuntimeError, match="inline retrieve_essential entered"):
                await service.retrieve_for_prompt("query", user_id=1)

        ret_call.assert_not_called()


class TestMemoryRetrievalSurface:
    @pytest.mark.unit
    def test_required_methods_present(self):
        required = {
            "retrieve",
            "retrieve_essential",
            "retrieve_for_prompt",
            "_recency_score",
            "_build_scope_filter",
            "_get_embedding",
            "_get_ollama_client",
        }
        actual = {name for name in dir(MemoryRetrieval) if not name.startswith("__")}
        missing = required - actual
        assert not missing, f"MemoryRetrieval is missing extracted methods: {missing}"
