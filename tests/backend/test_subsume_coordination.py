"""Phase 3-subsume per-fact fix — coordination seam (KG hook + chat handler).

The per-fact subsume gate needs the two previously-uncoordinated background
extractors to share state: the chat handler runs the ``post_message`` hooks
FIRST (KG extraction), capturing the subject NAMES of the relations the KG
actually saved this turn into a mutable set, then runs memory extraction with
that set. These tests cover the seam without a DB / LLM:

  * ``kg_post_message_hook`` populates a passed ``captured_subjects`` set with
    the subject names of saved relations (and is a no-op without it / on error).
  * ``_extract_structured_background`` orders KG-then-memory and threads the
    same set from the hook into memory extraction.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Ent:
    def __init__(self, id_: int, name: str):
        self.id = id_
        self.name = name


class _Rel:
    def __init__(self, subject_id: int):
        self.subject_id = subject_id


class TestKgHookCapturesSubjects:
    async def test_populates_captured_subjects_from_saved_relations(self):
        from services import knowledge_graph_service as kgs

        entities = [_Ent(1, "Anna"), _Ent(2, "Bonn"), _Ent(3, "Tom")]
        relations = [_Rel(subject_id=1), _Rel(subject_id=3)]

        class _FakeSvc:
            def __init__(self, db):
                pass

            async def extract_and_save(self, *a, **k):
                return entities, relations

        captured: set[str] = set()
        with patch.object(kgs, "KnowledgeGraphService", _FakeSvc), \
             patch("services.database.AsyncSessionLocal", _fake_session_local()):
            await kgs.kg_post_message_hook(
                "u", "a", user_id=7, session_id="s",
                captured_subjects=captured, lang="de",
            )
        # Subjects of the two saved relations (lowercased), object 'Bonn' excluded.
        assert captured == {"anna", "tom"}

    async def test_no_capture_arg_is_noop(self):
        from services import knowledge_graph_service as kgs

        class _FakeSvc:
            def __init__(self, db):
                pass

            async def extract_and_save(self, *a, **k):
                return [_Ent(1, "Anna")], [_Rel(subject_id=1)]

        with patch.object(kgs, "KnowledgeGraphService", _FakeSvc), \
             patch("services.database.AsyncSessionLocal", _fake_session_local()):
            # No captured_subjects passed — must not raise.
            await kgs.kg_post_message_hook("u", "a", user_id=7, session_id="s")

    async def test_extract_failure_leaves_set_empty(self):
        from services import knowledge_graph_service as kgs

        class _FakeSvc:
            def __init__(self, db):
                pass

            async def extract_and_save(self, *a, **k):
                raise RuntimeError("LLM down")

        captured: set[str] = set()
        with patch.object(kgs, "KnowledgeGraphService", _FakeSvc), \
             patch("services.database.AsyncSessionLocal", _fake_session_local()):
            await kgs.kg_post_message_hook(
                "u", "a", user_id=7, captured_subjects=captured,
            )
        assert captured == set()  # fail-safe: no subjects captured -> facts kept flat


class TestStructuredBackgroundOrdering:
    async def test_runs_kg_first_then_memory_with_captured_set(self):
        """The ordered coroutine runs post_message hooks first (KG populates the
        captured set) then memory extraction WITH that exact set."""
        from api.websocket import chat_handler as ch

        order: list[str] = []
        seen_captured: dict[str, object] = {}

        async def _fake_run_hooks(event, **kwargs):
            assert event == "post_message"
            order.append("kg")
            cs = kwargs.get("captured_subjects")
            assert cs is not None
            cs.add("anna")  # simulate KG capturing a relation for Anna this turn

        async def _fake_mem(*a, captured_kg_subjects=None, **k):
            order.append("mem")
            seen_captured["set"] = captured_kg_subjects

        # run_hooks is imported INSIDE the coroutine (`from utils.hooks import run_hooks`)
        with patch("utils.hooks.run_hooks", _fake_run_hooks), \
             patch.object(ch, "_extract_memories_background", _fake_mem):
            await ch._extract_structured_background(
                user_message="u", assistant_response="a",
                user_id=7, session_id="s", lang="de",
            )

        assert order == ["kg", "mem"], "KG must run before memory extraction"
        # The SAME set the hook populated is threaded into memory extraction.
        assert seen_captured["set"] == {"anna"}


def _fake_session_local():
    """Build an async-context-manager factory standing in for AsyncSessionLocal."""
    class _CM:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    def _factory():
        return _CM()

    return _factory
