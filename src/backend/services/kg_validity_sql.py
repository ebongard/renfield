"""
Validity filter for kg_relations — #875 Stufe 1 (docs/design/kg-bitemporal-edges.md).

One place that defines "this relation holds at time T", in the two shapes the
KG read paths use: a raw-SQL clause for the ``text()`` queries (kg_retrieval,
graph_expansion) and a list of ORM conditions for the ``select(KGRelation)``
queries. Same idea as ``circle_sql``: six files and ~20 read sites are too many
to keep consistent by discipline, so the predicate lives here and
``tests/backend/test_kg_validity_sites.py`` fails if a live-relation query
filters ``is_active`` without it.

**Off = byte-identical.** With ``settings.kg_validity_filter_enabled`` False
(the default) both helpers return the EMPTY value — ``("", {})`` and ``[]`` —
so every caller's SQL is exactly what it was before the columns existed. That
is how Stufe 1 is verified (§11: "Golden-SQL … byte-identisch").

**On changes no result in Stufe 1.** Nothing sets ``valid_to`` yet, so
``valid_to IS NULL`` holds for every row. The switch only arms the filter for
the Stufe 2 detector, which needs its own explicit go.

Timestamps are naive UTC, matching ``models.database._utcnow``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from utils.config import settings

#: Bind-parameter name for the raw-SQL clause. Prefixed so it cannot collide
#: with a caller's own ``:as_of`` or the circle filter's parameters.
AS_OF_PARAM = "kg_validity_as_of"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def enabled() -> bool:
    return bool(settings.kg_validity_filter_enabled)


def live_clause(alias: str = "r", as_of: datetime | None = None) -> tuple[str, dict[str, Any]]:
    """``(" AND (<alias>.valid_to IS NULL OR <alias>.valid_to > :p)", params)``.

    Starts with ``" AND "`` so a caller appends it right after its existing
    ``<alias>.is_active = true`` condition. Returns ``("", {})`` when the filter
    is off — append unconditionally, the SQL then stays byte-identical.
    """
    if not enabled():
        return "", {}
    return (
        f" AND ({alias}.valid_to IS NULL OR {alias}.valid_to > :{AS_OF_PARAM})",
        {AS_OF_PARAM: as_of or _now()},
    )


def live_conditions(model: Any = None, as_of: datetime | None = None) -> list:
    """ORM conditions for ``select(...).where(*live_conditions())``.

    ``model`` is ``KGRelation`` or an ``aliased(KGRelation)``. Returns ``[]``
    when the filter is off — ``.where(*[])`` adds nothing, so the compiled SQL
    is unchanged.
    """
    if not enabled():
        return []
    from sqlalchemy import or_

    if model is None:
        from models.database import KGRelation

        model = KGRelation
    moment = as_of or _now()
    return [or_(model.valid_to.is_(None), model.valid_to > moment)]
