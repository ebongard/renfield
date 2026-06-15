"""Per-fact tier-override carry-over across re-extraction (Schicht A).

A re-ingest / re-OCR recreates the whole ``document_facts`` set from scratch
(new rows, new atoms). The carry-over logic in
``schicht_a_post_document_ingest_hook`` snapshots the prior OVERRIDDEN facts by
content identity and re-applies ``tier_overridden`` + the override tier to a
matching freshly-extracted fact, so a deliberate per-fact override survives a
re-extraction instead of silently reverting to the document tier.

These tests drive the real hook against an in-memory sqlite engine (the
carry-over path uses only ORM inserts + AtomService.create_with_source /
finalize_source_id + AtomPurgeService.purge — all sqlite-safe; it never touches
the Postgres-specific update_tier cascade). The hook opens its own
``AsyncSessionLocal()``, so we point that sessionmaker at the same StaticPool
engine (one shared connection) and stub the LLM extractor + title synthesis.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
]
import importlib as _importlib  # noqa: E402

for _mod in _missing_stubs:
    if _mod in sys.modules:
        continue
    try:
        _importlib.import_module(_mod)
    except Exception:  # noqa: BLE001
        sys.modules[_mod] = MagicMock()

from models.database import (  # noqa: E402
    ATOM_TYPE_KB_DOCUMENT,
    Atom,
    Base,
    Document,
    DocumentFact,
    Role,
    User,
)
from services.atom_service import AtomService  # noqa: E402
from services.schicht_a_extractor import (  # noqa: E402
    ExtractedFact,
    SchichtAResult,
    _fact_identity_key,
)


# ---------------------------------------------------------------------------
# Pure unit: the identity key
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestFactIdentityKey:
    def test_identifier_keys_on_normalized_value(self):
        """An identifier's stable anchor is normalized_value; letter-spacing
        / case in the raw value must not change the key."""
        k1 = _fact_identity_key(
            "identifier", "steuernummer", "114/5876/5293", "11 4 / 5 8 7 6 / 5 2 9 3",
        )
        k2 = _fact_identity_key(
            "identifier", "STEUERNUMMER", "114 / 5876 / 5293", "different raw",
        )
        assert k1 == k2

    def test_falls_back_to_value_when_no_normalized(self):
        k = _fact_identity_key("universal", "issuer", None, "Finanzamt Bonn")
        assert k == ("universal", "issuer", "finanzamtbonn")

    def test_distinct_kinds_do_not_collide(self):
        assert _fact_identity_key("obligation", "zahlung", None, "100 EUR") != \
            _fact_identity_key("obligation", "frist", None, "100 EUR")


# ---------------------------------------------------------------------------
# End-to-end: the hook
# ---------------------------------------------------------------------------
@pytest.fixture
async def carryover_engine(monkeypatch):
    """In-memory sqlite engine shared with the hook's own AsyncSessionLocal."""
    from sqlalchemy import event

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    # The carry-over correctness depends on the OLD fact rows being removed when
    # AtomPurgeService.purge deletes their atoms — that's an ON DELETE CASCADE FK
    # (document_facts.atom_id → atoms.atom_id). sqlite ignores FK constraints
    # unless PRAGMA foreign_keys=ON is set per connection.
    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # With FK enforcement ON, atoms.owner_user_id → users.id → roles.id must
    # resolve. Seed the minimal Role + User (id=1) the hook's atoms reference.
    async with maker() as db:
        db.add(Role(id=1, name="member", permissions=[]))
        await db.flush()
        db.add(User(id=1, username="owner", password_hash="x", role_id=1))
        await db.commit()
    # The hook does `from services.database import AsyncSessionLocal` at call
    # time, so patch it on the source module.
    monkeypatch.setattr("services.database.AsyncSessionLocal", maker, raising=False)

    yield engine, maker

    await engine.dispose()


async def _seed_doc_with_fact(
    maker, *, doc_tier: int, fact: ExtractedFact, fact_tier: int, overridden: bool,
):
    """Create a kb_document atom + Document + one DocumentFact at fact_tier.

    Returns (document_id, fact_id). Simulates a prior PATCH having set the
    override directly on the fact row (we deliberately do NOT call update_tier —
    that's the Postgres cascade path; here we only need the persisted state).
    """
    async with maker() as db:
        svc = AtomService(db)
        doc_atom = await svc.create_with_source(
            atom_type=ATOM_TYPE_KB_DOCUMENT, owner_user_id=1, tier=doc_tier,
        )
        doc = Document(
            filename="bescheid.pdf", file_path="/x/bescheid.pdf", file_type="pdf",
            status="completed", atom_id=doc_atom, circle_tier=doc_tier,
        )
        db.add(doc)
        await db.flush()
        await svc.finalize_source_id(doc_atom, doc.id)

        fact_atom = await svc.create_with_source(
            atom_type="document_fact", owner_user_id=1, tier=fact_tier,
        )
        row = DocumentFact(
            document_id=doc.id, category=fact.category, kind=fact.kind,
            value=fact.value, normalized_value=fact.normalized_value,
            excerpt=fact.excerpt, source=fact.source, legal_gate=fact.legal_gate,
            atom_id=fact_atom, circle_tier=fact_tier, tier_overridden=overridden,
        )
        db.add(row)
        await db.flush()
        await svc.finalize_source_id(fact_atom, row.id)
        await db.commit()
        return doc.id, row.id


def _patch_extractor(monkeypatch, facts: list[ExtractedFact]):
    """Stub SchichtAExtractor().extract → facts and the title LLM call."""
    async def _fake_extract(self, field_text, *, lang="de"):
        return SchichtAResult(facts=facts)

    monkeypatch.setattr(
        "services.schicht_a_extractor.SchichtAExtractor.extract", _fake_extract,
    )
    monkeypatch.setattr(
        "services.schicht_a_extractor.generate_document_title",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "services.schicht_a_extractor.settings.schicht_a_extraction_enabled", True,
    )


@pytest.mark.database
@pytest.mark.asyncio
class TestOverrideCarryOver:
    async def test_override_survives_reextraction(self, carryover_engine, monkeypatch):
        """A public (tier 4) per-fact override on a private (tier 0) doc must
        survive a re-extraction that recreates the fact."""
        from services.schicht_a_extractor import schicht_a_post_document_ingest_hook

        _engine, maker = carryover_engine
        issuer = ExtractedFact(
            category="universal", kind="issuer", value="Finanzamt Bonn",
            normalized_value=None, source="llm",
        )
        doc_id, _fid = await _seed_doc_with_fact(
            maker, doc_tier=0, fact=issuer, fact_tier=4, overridden=True,
        )

        # Re-extraction: the same issuer fact is produced again.
        _patch_extractor(monkeypatch, [issuer])
        await schicht_a_post_document_ingest_hook(
            chunks=["x"], document_id=doc_id, user_id=1, field_text="some text",
        )

        async with maker() as db:
            rows = (await db.execute(
                select(DocumentFact).where(DocumentFact.document_id == doc_id)
            )).scalars().all()
            assert len(rows) == 1
            assert rows[0].tier_overridden is True
            assert rows[0].circle_tier == 4   # override carried, NOT doc tier 0
            # the wrapping atom's policy must agree
            atom = (await db.execute(
                select(Atom).where(Atom.atom_id == rows[0].atom_id)
            )).scalar_one()
            assert atom.policy == {"tier": 4}

    async def test_non_overridden_fact_follows_doc_tier(self, carryover_engine, monkeypatch):
        """A fact that was NOT overridden must come back at the doc tier after
        re-extraction (no spurious sticky bit)."""
        from services.schicht_a_extractor import schicht_a_post_document_ingest_hook

        _engine, maker = carryover_engine
        steuernr = ExtractedFact(
            category="identifier", kind="steuernummer", value="114/5876/5293",
            normalized_value="114/5876/5293", source="deterministic",
        )
        doc_id, _fid = await _seed_doc_with_fact(
            maker, doc_tier=2, fact=steuernr, fact_tier=2, overridden=False,
        )

        _patch_extractor(monkeypatch, [steuernr])
        await schicht_a_post_document_ingest_hook(
            chunks=["x"], document_id=doc_id, user_id=1, field_text="text",
        )

        async with maker() as db:
            rows = (await db.execute(
                select(DocumentFact).where(DocumentFact.document_id == doc_id)
            )).scalars().all()
            assert len(rows) == 1
            assert rows[0].tier_overridden is False
            assert rows[0].circle_tier == 2

    async def test_drifted_fact_does_not_carry_override(self, carryover_engine, monkeypatch):
        """If the re-extracted fact's content no longer matches the prior
        override key, it reverts to the doc tier (fail-safe)."""
        from services.schicht_a_extractor import schicht_a_post_document_ingest_hook

        _engine, maker = carryover_engine
        old = ExtractedFact(
            category="universal", kind="issuer", value="Finanzamt Bonn",
            normalized_value=None, source="llm",
        )
        doc_id, _fid = await _seed_doc_with_fact(
            maker, doc_tier=0, fact=old, fact_tier=4, overridden=True,
        )

        # Re-extraction produces a DIFFERENT issuer summary (drift).
        drifted = ExtractedFact(
            category="universal", kind="issuer", value="Stadtkasse Köln",
            normalized_value=None, source="llm",
        )
        _patch_extractor(monkeypatch, [drifted])
        await schicht_a_post_document_ingest_hook(
            chunks=["x"], document_id=doc_id, user_id=1, field_text="text",
        )

        async with maker() as db:
            rows = (await db.execute(
                select(DocumentFact).where(DocumentFact.document_id == doc_id)
            )).scalars().all()
            assert len(rows) == 1
            assert rows[0].value == "Stadtkasse Köln"
            assert rows[0].tier_overridden is False
            assert rows[0].circle_tier == 0   # reverted to doc tier (fail-safe)

    async def test_reset_clears_override_so_it_does_not_carry(self, carryover_engine, monkeypatch):
        """After reset_fact_tier clears the override, a later re-extraction must
        NOT resurrect it (reset still works end-to-end)."""
        from services.schicht_a_extractor import schicht_a_post_document_ingest_hook

        _engine, maker = carryover_engine
        issuer = ExtractedFact(
            category="universal", kind="issuer", value="Finanzamt Bonn",
            normalized_value=None, source="llm",
        )
        doc_id, fid = await _seed_doc_with_fact(
            maker, doc_tier=0, fact=issuer, fact_tier=4, overridden=True,
        )

        # Simulate reset: clear the override + restore to doc tier on the row.
        # (reset_fact_tier itself delegates to the Postgres update_tier cascade;
        # here we assert the carry-over snapshot only honors STILL-overridden
        # rows — a reset row's content must not re-acquire the override.)
        async with maker() as db:
            row = (await db.execute(
                select(DocumentFact).where(DocumentFact.id == fid)
            )).scalar_one()
            row.tier_overridden = False
            row.circle_tier = 0
            await db.commit()

        _patch_extractor(monkeypatch, [issuer])
        await schicht_a_post_document_ingest_hook(
            chunks=["x"], document_id=doc_id, user_id=1, field_text="text",
        )

        async with maker() as db:
            rows = (await db.execute(
                select(DocumentFact).where(DocumentFact.document_id == doc_id)
            )).scalars().all()
            assert len(rows) == 1
            assert rows[0].tier_overridden is False
            assert rows[0].circle_tier == 0
