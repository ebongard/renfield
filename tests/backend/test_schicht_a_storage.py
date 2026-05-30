"""Schicht A fact storage — atom round-trip + kb_document→facts tier cascade.

Round-trip runs on the sqlite ``db_session`` fixture (DocumentFact is in
Base.metadata). The tier cascade is Postgres SQL (``json_build_object``), so it's
asserted at the SQL-text level against a mocked session — the same pattern the KG
cascade uses in test_kg_circle_tier.py.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_missing_stubs = ["asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
                  "speechbrain.inference", "speechbrain.inference.speaker",
                  "openwakeword", "openwakeword.model"]
import importlib as _importlib
for _mod in _missing_stubs:
    if _mod in sys.modules:
        continue
    try:
        _importlib.import_module(_mod)
    except Exception:  # noqa: BLE001
        sys.modules[_mod] = MagicMock()

from sqlalchemy import select  # noqa: E402

from models.database import (  # noqa: E402
    ATOM_TYPE_DOCUMENT_FACT,
    ATOM_TYPE_KB_DOCUMENT,
    Atom,
    DocumentFact,
)
from services.atom_service import AtomService  # noqa: E402


@pytest.mark.asyncio
class TestFactAtomRoundTrip:
    async def test_create_fact_as_atom_and_read_back(self, db_session):
        """create_with_source → DocumentFact insert → finalize_source_id yields a
        fact wrapped by an atom carrying the document's tier."""
        svc = AtomService(db_session)
        atom_id = await svc.create_with_source(
            atom_type=ATOM_TYPE_DOCUMENT_FACT, owner_user_id=1, tier=2,
        )
        fact = DocumentFact(
            document_id=1, category="identifier", kind="steuernummer",
            value="114/5876/5293", normalized_value="114/5876/5293",
            atom_id=atom_id, circle_tier=2, source="deterministic", legal_gate=False,
        )
        db_session.add(fact)
        await db_session.flush()
        await svc.finalize_source_id(atom_id, fact.id)

        atom = (await db_session.execute(
            select(Atom).where(Atom.atom_id == atom_id)
        )).scalar_one()
        assert atom.atom_type == ATOM_TYPE_DOCUMENT_FACT
        assert atom.source_table == "document_facts"
        assert atom.source_id == str(fact.id)      # placeholder finalized
        assert atom.policy == {"tier": 2}

        roundtrip = (await db_session.execute(
            select(DocumentFact).where(DocumentFact.atom_id == atom_id)
        )).scalar_one()
        assert roundtrip.normalized_value == "114/5876/5293"
        assert roundtrip.circle_tier == 2


@pytest.mark.asyncio
class TestTierCascade:
    async def test_kb_document_tier_change_cascades_to_facts(self):
        """update_tier on a kb_document atom must also UPDATE document_facts for
        that document (facts follow the doc's tier, like chunks). Asserted on the
        emitted SQL — the cascade is Postgres-specific."""
        session = MagicMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        session.flush = AsyncMock()

        atom_orm = MagicMock()
        atom_orm.atom_type = ATOM_TYPE_KB_DOCUMENT
        atom_orm.source_table = "documents"
        atom_orm.source_id = "42"
        # First execute() (the select for the atom) returns our atom; the rest
        # are the cascade UPDATEs we want to inspect.
        first = MagicMock()
        first.scalar_one_or_none = MagicMock(return_value=atom_orm)
        session.execute = AsyncMock(side_effect=[first, AsyncMock(), AsyncMock(),
                                                 AsyncMock(), AsyncMock()])

        svc = AtomService(session)
        svc.resolver = MagicMock()
        svc.resolver.invalidate_for_atom = MagicMock()

        await svc.update_tier("atom-x", {"tier": 3})

        emitted = " ".join(
            str(call.args[0]) for call in session.execute.call_args_list
        )
        assert "UPDATE document_facts SET circle_tier" in emitted
        assert "WHERE document_id = :doc_id" in emitted
        # and the document_chunks cascade is still there (not regressed)
        assert "UPDATE document_chunks SET circle_tier" in emitted
