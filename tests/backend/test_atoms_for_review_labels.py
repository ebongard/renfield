"""
Tests for `/api/circles/me/atoms-for-review` label resolution.

Regression guard: the endpoint used to return only `atom_id`, which
the Brain Review UI displayed as a raw UUID — a human cannot pick a
tier for an unlabeled hex string. The response now includes a
`title` + optional `preview` resolved from each atom's source row.

Covers:
- kg_node → entity.name + description
- kg_edge → "subject predicate object"
- kb_chunk → document.filename + chunk.content
- conversation_memory → "Memory · YYYY-MM-DD HH:MM" + content
- orphan source_id → "Unknown … (id)" fallback (no crash)
- mixed atom types → each gets its type-specific label, no bleed
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routes.circles import _resolve_review_labels


def _atom(atom_id: str, atom_type: str, source_table: str, source_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        atom_id=atom_id,
        atom_type=atom_type,
        source_table=source_table,
        source_id=source_id,
    )


def _scalars_returning(objs: list) -> MagicMock:
    """Mock a `db.execute` result whose .scalars().all() yields `objs`."""
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=objs)
    result.scalars = MagicMock(return_value=scalars)
    return result


class TestResolveReviewLabels:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_kg_node_resolves_to_entity_name_and_description(self):
        atom = _atom("a1", "kg_node", "kg_entities", "42")
        entity = SimpleNamespace(id=42, name="Katharinenstraße", description="Straße in Korschenbroich")

        db = MagicMock()
        db.execute = AsyncMock(return_value=_scalars_returning([entity]))

        out = await _resolve_review_labels(db, [atom])
        assert out == {"a1": ("Katharinenstraße", "Straße in Korschenbroich")}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_kg_edge_resolves_to_subject_predicate_object(self):
        atom = _atom("e1", "kg_edge", "kg_relations", "7")
        subj = SimpleNamespace(name="Heizung")
        obj = SimpleNamespace(name="Erdgas")
        relation = SimpleNamespace(id=7, predicate="verwendet", subject=subj, object=obj)

        db = MagicMock()
        db.execute = AsyncMock(return_value=_scalars_returning([relation]))

        out = await _resolve_review_labels(db, [atom])
        assert out == {"e1": ("Heizung verwendet Erdgas", None)}

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_kb_chunk_prefers_document_title_then_filename(self):
        atom_with_title = _atom("c1", "kb_chunk", "document_chunks", "10")
        atom_without_title = _atom("c2", "kb_chunk", "document_chunks", "11")

        doc_titled = SimpleNamespace(title="Nebenkostenabrechnung 2025", filename="NK_2025.pdf")
        doc_untitled = SimpleNamespace(title=None, filename="mysterium.pdf")
        chunks = [
            SimpleNamespace(id=10, content="Die Nebenkosten für 2025 belaufen sich auf...", document=doc_titled),
            SimpleNamespace(id=11, content="Kapitel 4: Betriebskosten", document=doc_untitled),
        ]

        db = MagicMock()
        db.execute = AsyncMock(return_value=_scalars_returning(chunks))

        out = await _resolve_review_labels(db, [atom_with_title, atom_without_title])
        # Titled → title wins
        assert out["c1"][0] == "Nebenkostenabrechnung 2025"
        assert out["c1"][1].startswith("Die Nebenkosten")
        # Untitled → falls back to filename
        assert out["c2"][0] == "mysterium.pdf"
        assert out["c2"][1] == "Kapitel 4: Betriebskosten"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_conversation_memory_uses_timestamp_prefix(self):
        atom = _atom("m1", "conversation_memory", "conversation_memories", "99")
        mem = SimpleNamespace(
            id=99,
            content="Max erwähnt, dass er morgen um 8 Uhr abgeholt werden möchte.",
            created_at=datetime(2026, 4, 19, 14, 30),
        )

        db = MagicMock()
        db.execute = AsyncMock(return_value=_scalars_returning([mem]))

        out = await _resolve_review_labels(db, [atom])
        title, preview = out["m1"]
        assert title.startswith("Memory · 2026-04-19")
        assert "Max erwähnt" in preview

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_orphan_source_id_falls_back_to_unknown(self):
        """Source row was deleted after the atom was written. Endpoint
        must still return a row — "Unknown entity (42)" — so the UI
        renders something instead of crashing."""
        atom = _atom("a1", "kg_node", "kg_entities", "42")

        db = MagicMock()
        db.execute = AsyncMock(return_value=_scalars_returning([]))  # no rows

        out = await _resolve_review_labels(db, [atom])
        assert out["a1"] == ("Unknown entity (42)", None)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_non_numeric_source_id_does_not_crash(self):
        """Defensive: a malformed atom whose source_id isn't an int
        should be labeled 'unknown' without triggering ValueError."""
        atom = _atom("a1", "kg_node", "kg_entities", "not-a-number")

        db = MagicMock()
        db.execute = AsyncMock(return_value=_scalars_returning([]))

        out = await _resolve_review_labels(db, [atom])
        assert out["a1"][0].startswith("Unknown entity")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_long_preview_is_truncated_with_ellipsis(self):
        atom = _atom("a1", "kg_node", "kg_entities", "1")
        long_desc = "x" * 500
        entity = SimpleNamespace(id=1, name="Test", description=long_desc)

        db = MagicMock()
        db.execute = AsyncMock(return_value=_scalars_returning([entity]))

        out = await _resolve_review_labels(db, [atom])
        _, preview = out["a1"]
        assert preview.endswith("…")
        assert len(preview) == 200

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_unknown_atom_type_gets_generic_fallback(self):
        """A future atom_type the resolver doesn't know about gets a
        generic 'Unknown xyz' label — forward-compat without UI crash."""
        atom = _atom("a1", "satellite_telemetry", "some_future_table", "1")
        db = MagicMock()
        db.execute = AsyncMock()  # won't be called — no matching bucket

        out = await _resolve_review_labels(db, [atom])
        assert out["a1"] == ("Unknown satellite_telemetry", None)
