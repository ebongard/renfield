"""#875 Stufe 1 — services/kg_validity_sql.py.

The byte-identity guarantee of Stufe 1 rests on one property: with the switch
off, both helpers return the EMPTY value, so every caller appends nothing.
The golden tests below compile real statements both ways and check that the
only difference is the validity predicate.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from models.database import KGRelation
from services import kg_validity_sql
from utils.config import settings


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setattr(settings, "kg_validity_filter_enabled", False)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "kg_validity_filter_enabled", True)


@pytest.mark.unit
class TestOff:
    def test_default_is_off(self):
        from utils.config import Settings

        assert Settings.model_fields["kg_validity_filter_enabled"].default is False

    def test_clause_is_empty(self, off):
        assert kg_validity_sql.live_clause("r") == ("", {})

    def test_conditions_are_empty(self, off):
        assert kg_validity_sql.live_conditions(KGRelation) == []


@pytest.mark.unit
class TestOn:
    def test_clause_shape(self, on):
        clause, params = kg_validity_sql.live_clause("rel")
        assert clause == (
            " AND (rel.valid_to IS NULL OR rel.valid_to > :kg_validity_as_of)"
        )
        assert set(params) == {kg_validity_sql.AS_OF_PARAM}
        assert params[kg_validity_sql.AS_OF_PARAM].tzinfo is None  # naive UTC, like _utcnow

    def test_as_of_is_passed_through(self, on):
        moment = datetime(2026, 3, 1, 12, 0)
        _, params = kg_validity_sql.live_clause("r", as_of=moment)
        assert params[kg_validity_sql.AS_OF_PARAM] == moment

    def test_conditions_compile_to_the_same_predicate(self, on):
        (cond,) = kg_validity_sql.live_conditions(KGRelation)
        sql = str(cond.compile(dialect=postgresql.dialect()))
        assert "kg_relations.valid_to IS NULL" in sql
        assert "kg_relations.valid_to >" in sql


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


@pytest.mark.unit
class TestGoldenSql:
    """§11: flag off ⇒ the SQL a consumer sends is byte-identical to before."""

    def _consumer_stmt(self):
        # The shape every ORM read site uses: is_active, then *live_conditions().
        return select(KGRelation.id).where(
            KGRelation.is_active == True,  # noqa: E712
            *kg_validity_sql.live_conditions(KGRelation),
            KGRelation.subject_id == 1,
        )

    def _baseline_stmt(self):
        return select(KGRelation.id).where(
            KGRelation.is_active == True,  # noqa: E712
            KGRelation.subject_id == 1,
        )

    def test_orm_off_is_byte_identical(self, off):
        assert _compile(self._consumer_stmt()) == _compile(self._baseline_stmt())

    def test_orm_on_differs_only_by_the_predicate(self, on):
        with_filter = _compile(self._consumer_stmt())
        assert with_filter != _compile(self._baseline_stmt())
        assert "valid_to" in with_filter

    def test_raw_off_is_byte_identical(self, off):
        clause, params = kg_validity_sql.live_clause("r")
        template = "SELECT r.id FROM kg_relations r WHERE r.is_active = true{v} AND r.x = 1"
        assert template.format(v=clause) == template.format(v="")
        assert params == {}
