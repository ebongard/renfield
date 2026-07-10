"""Tests for the shared atom-ownership guard load_owned_atom (#445).

The single ownership chokepoint for atom-mutating routes. Verifies it returns
the row for the owner, and raises a UNIFORM 404 (never 403) for both
not-found and not-owner — the existence-oracle defense the per-route checks
had, now in one reviewed place.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.routes.atoms import load_owned_atom


def _db_returning(atom_orm):
    """A mock AsyncSession whose execute(...).scalar_one_or_none() yields atom_orm."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=atom_orm)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _atom(owner_id: int):
    a = MagicMock()
    a.owner_user_id = owner_id
    return a


def _user(uid: int):
    u = MagicMock()
    u.id = uid
    return u


class TestLoadOwnedAtom:
    @pytest.mark.unit
    async def test_returns_row_for_owner(self):
        atom = _atom(7)
        db = _db_returning(atom)
        got = await load_owned_atom(db, "atom-1", _user(7))
        assert got is atom

    @pytest.mark.unit
    async def test_404_when_not_found(self):
        db = _db_returning(None)
        with pytest.raises(HTTPException) as exc:
            await load_owned_atom(db, "atom-1", _user(7))
        assert exc.value.status_code == 404

    @pytest.mark.unit
    async def test_404_uniform_when_not_owner(self):
        # Not 403 — an attacker must not be able to tell "exists but not yours"
        # from "does not exist".
        db = _db_returning(_atom(999))
        with pytest.raises(HTTPException) as exc:
            await load_owned_atom(db, "atom-1", _user(7))
        assert exc.value.status_code == 404

    @pytest.mark.unit
    async def test_custom_not_found_detail(self):
        db = _db_returning(None)
        with pytest.raises(HTTPException) as exc:
            await load_owned_atom(db, "atom-1", _user(7), not_found_detail="Fact not found")
        assert exc.value.detail == "Fact not found"

    @pytest.mark.unit
    async def test_for_update_path_returns_owner_row(self):
        # for_update just adds .with_for_update() to the stmt; execute is mocked,
        # so this asserts the branch builds + returns without error.
        atom = _atom(3)
        db = _db_returning(atom)
        got = await load_owned_atom(db, "atom-1", _user(3), for_update=True)
        assert got is atom
