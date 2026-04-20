"""
Regression tests for the ConversationMemoryService -> MemoryRetrieval extraction
(Lane A3 of the second-brain-circles eng-review plan), updated for Lane C.

Lane C changes:
- The `_build_scope_filter` (scope/team_id-based) is replaced by
  `_memory_circles_filter` (circle_tier-based). Parity tests dropped because
  the legacy SQL is no longer issued anywhere.
- `circles_use_new_memory` is now a no-op flag — both ON and OFF route through
  MemoryRetrieval.
- `retrieve_for_prompt` no longer accepts `team_ids` (parked for v2 named
  circles).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.database import TIER_PUBLIC
from services.conversation_memory_service import ConversationMemoryService
from services.memory_retrieval import MemoryRetrieval


class TestMemoryCirclesFilter:
    """The new circle-tier filter helper used by every retrieval method."""

    @pytest.mark.unit
    def test_auth_disabled_bypasses_filter(self):
        # AUTH_ENABLED=false (single-user mode) → full bypass
        with patch("services.memory_retrieval.settings") as svc_settings:
            svc_settings.auth_enabled = False
            clause, params = MemoryRetrieval._memory_circles_filter(None)
        assert clause == "TRUE"
        assert params == {}

    @pytest.mark.unit
    def test_anonymous_caller_when_auth_on_only_sees_public(self):
        with patch("services.memory_retrieval.settings") as svc_settings:
            svc_settings.auth_enabled = True
            clause, params = MemoryRetrieval._memory_circles_filter(None)
        assert clause == "m.circle_tier = :asker_id_pub"
        assert params == {"asker_id_pub": TIER_PUBLIC}

    @pytest.mark.unit
    def test_authenticated_caller_uses_full_4_branch_or(self):
        with patch("services.memory_retrieval.settings") as svc_settings:
            svc_settings.auth_enabled = True
            clause, params = MemoryRetrieval._memory_circles_filter(42)
        assert "m.user_id = :asker_id" in clause
        assert "m.circle_tier = :asker_id_pub" in clause
        assert "atom_explicit_grants" in clause
        assert "a.source_table = 'conversation_memories'" in clause
        assert "circle_memberships" in clause
        assert params == {"asker_id": 42, "asker_id_pub": TIER_PUBLIC}


class TestRecencyScoreParity:
    """Both classes still expose _recency_score — verify they remain identical."""

    _DRIFT_TOL = 1e-6

    @pytest.mark.unit
    def test_none_created_at_returns_neutral(self):
        legacy = ConversationMemoryService._recency_score(None)
        new = MemoryRetrieval._recency_score(None)
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


class TestRouting:
    """ConversationMemoryService.retrieve_for_prompt always routes to MemoryRetrieval."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_retrieve_for_prompt_always_routes(self):
        db = MagicMock()
        service = ConversationMemoryService(db)
        sentinel = {"essential": [], "procedural": [], "semantic": [], "episodic": []}

        with patch(
            "services.memory_retrieval.MemoryRetrieval.retrieve_for_prompt",
            new=AsyncMock(return_value=sentinel),
        ) as ret_call:
            result = await service.retrieve_for_prompt(
                "query", user_id=42, budget_chars=2000,
            )

        ret_call.assert_called_once()
        call_kwargs = ret_call.call_args
        assert call_kwargs.kwargs.get("user_id") == 42
        assert call_kwargs.kwargs.get("budget_chars") == 2000
        assert result is sentinel


class TestMemoryRetrievalSurface:
    @pytest.mark.unit
    def test_required_methods_present(self):
        required = {
            "retrieve",
            "retrieve_essential",
            "retrieve_for_prompt",
            "_recency_score",
            "_memory_circles_filter",
            "_get_embedding",
            "_get_ollama_client",
        }
        actual = {name for name in dir(MemoryRetrieval) if not name.startswith("__")}
        missing = required - actual
        assert not missing, f"MemoryRetrieval is missing extracted methods: {missing}"
