"""#446 — reaper for orphaned ``__pending__`` placeholder atoms.

``AtomService.create_with_source`` seeds an atoms row with a ``__pending__<uuid>``
placeholder ``source_id`` and finalizes it to the real PK in a later step. A crash
in between leaves the placeholder orphaned. ``reap_orphan_placeholder_atoms``
deletes such rows — but only those older than a floor, so an in-flight create is
never reaped.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from models.database import Atom as AtomModel
from services.atom_service import reap_orphan_placeholder_atoms


def _atom(atom_id: str, source_id: str, created_at: datetime) -> AtomModel:
    return AtomModel(
        atom_id=atom_id,
        atom_type="kb_document",
        source_table="documents",
        source_id=source_id,
        owner_user_id=1,
        policy={"tier": 0},
        created_at=created_at,
    )


@pytest.mark.database
@pytest.mark.asyncio
class TestReapOrphanPlaceholderAtoms:
    async def test_reaps_only_old_placeholders(self, db_session):
        now = datetime.now(UTC).replace(tzinfo=None)
        old = now - timedelta(hours=2)
        db_session.add_all([
            _atom("orphan-old", "__pending__orphan-old", old),        # reaped
            _atom("inflight-fresh", "__pending__inflight-fresh", now),  # kept: age floor
            _atom("real-old", "123", old),                             # kept: not a placeholder
        ])
        await db_session.commit()

        n = await reap_orphan_placeholder_atoms(db_session, older_than_seconds=3600)

        assert n == 1
        remaining = {
            a.atom_id
            for a in (await db_session.execute(select(AtomModel))).scalars().all()
        }
        assert remaining == {"inflight-fresh", "real-old"}

    async def test_no_placeholders_returns_zero(self, db_session):
        db_session.add(_atom("real-1", "42", datetime.now(UTC).replace(tzinfo=None)))
        await db_session.commit()

        assert await reap_orphan_placeholder_atoms(db_session, older_than_seconds=3600) == 0

    async def test_fresh_placeholder_not_reaped(self, db_session):
        """A placeholder younger than the floor (a legitimately in-flight create)
        is left untouched even though its source_id matches the pattern."""
        now = datetime.now(UTC).replace(tzinfo=None)
        db_session.add(_atom("inflight", "__pending__inflight", now))
        await db_session.commit()

        n = await reap_orphan_placeholder_atoms(db_session, older_than_seconds=3600)

        assert n == 0
        remaining = {
            a.atom_id
            for a in (await db_session.execute(select(AtomModel))).scalars().all()
        }
        assert remaining == {"inflight"}
