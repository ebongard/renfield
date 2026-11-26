"""Contact points: phone / mobile / fax as attributes of KG entities (#1240).

Design: ``docs/design/kg-contact-points.md``. A number hangs on a person or an
organization, never on its own node. Every read goes through
``contact_points_circles_filter`` AND the entity filter. Every tier goes through
``services/contact_point_tier.py``.

Stage 1 writes what an owner enters by hand (``source_type='manual'``); the
document source (``services/document_contact_points.py``) writes
``source_type='document'``. vCard and chat come in later stages.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import phonenumbers
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    CONTACT_POINT_KIND_FAX,
    CONTACT_POINT_KIND_MOBILE,
    CONTACT_POINT_KIND_PHONE,
    CONTACT_POINT_KINDS,
    CONTACT_POINT_LABELS,
    CONTACT_POINT_SOURCE_MANUAL,
    TIER_PUBLIC,
    KGEntity,
    KGEntityContactPoint,
)
from services.contact_point_tier import contact_point_tier
from utils.config import settings

# The partial unique index on live (entity, kind, normalized) values.
LIVE_UNIQUE_INDEX = "uq_contact_points_live"

# A value read from a document names nobody while the same number is live on
# ANOTHER entity of the same owner: a tax advisor's or a property manager's number
# printed on letters for many issuers (design §14). Applied on every read; the
# rows stay, so the evidence is kept and the value comes back once it is unique.
DOCUMENT_AMBIGUITY_SQL = (
    "NOT (cp.source_type = 'document' AND EXISTS ("
    "SELECT 1 FROM kg_entity_contact_points o JOIN kg_entities oe ON oe.id = o.entity_id "
    "WHERE o.normalized = cp.normalized AND o.entity_id <> cp.entity_id "
    "AND o.valid_to IS NULL AND oe.is_active AND oe.canonical_id IS NULL "
    "AND o.user_id IS NOT DISTINCT FROM cp.user_id))"
)

# Longest value accepted verbatim; a phone number in any notation is far shorter.
MAX_VALUE_LENGTH = 64


class ContactPointError(ValueError):
    """A value that cannot be stored; ``code`` is stable for the API / UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_phone(value: str, region: str | None = None) -> tuple[str, str | None] | None:
    """``(E.164, detected kind)`` or None when ``value`` is no valid number.

    A value that does not normalize is never stored -- this is the guard that
    ``_is_valid_entity`` gives the graph (design §6). The detected kind is
    ``mobile`` for a mobile number, else None (the caller's kind stands).
    """
    if not value or len(value) > MAX_VALUE_LENGTH:
        return None
    try:
        parsed = phonenumbers.parse(value, region or settings.contact_points_default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    detected = (
        CONTACT_POINT_KIND_MOBILE
        if phonenumbers.number_type(parsed) == phonenumbers.PhoneNumberType.MOBILE
        else None
    )
    return e164, detected


def _visibility_sql(asker_id: int | None) -> tuple[str, dict]:
    """The two filters (value AND entity) every read applies; '' when auth is off."""
    if not settings.auth_enabled:
        return "", {}
    if asker_id is None:
        return (
            " AND cp.circle_tier = :cp_pub AND e.circle_tier = :cp_pub",
            {"cp_pub": TIER_PUBLIC},
        )
    from services.circle_sql import contact_points_circles_filter, kg_entities_circles_filter

    cp_clause, cp_params = contact_points_circles_filter(asker_id, alias="cp")
    e_clause, e_params = kg_entities_circles_filter(asker_id, alias="e")
    # Both helpers bind the same names with the same values (asker, public tier,
    # source table 'kg_entities'), so the merged dict is unambiguous.
    return f" AND ({cp_clause}) AND ({e_clause})", {**cp_params, **e_params}


@dataclass(frozen=True)
class ContactPointHit:
    point: KGEntityContactPoint
    entity_id: int
    entity_name: str
    entity_type: str


class ContactPointService:
    """CRUD + lookup for one request session."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- reads ---------------------------------------------------------------

    async def _hits(self, where: str, params: dict, asker_id: int | None, limit: int) -> list[ContactPointHit]:
        vis, vis_params = _visibility_sql(asker_id)
        rows = (await self.db.execute(
            text(
                "SELECT cp.id, e.id AS entity_id, e.name, e.entity_type "
                "FROM kg_entity_contact_points cp JOIN kg_entities e ON e.id = cp.entity_id "
                "WHERE cp.valid_to IS NULL AND e.is_active AND e.canonical_id IS NULL "
                f"AND {DOCUMENT_AMBIGUITY_SQL} AND {where}{vis} "
                # A document value ranks after every other source (never "confirmed").
                "ORDER BY e.name, cp.kind, (cp.source_type = 'document'), cp.id LIMIT :limit"
            ),
            {**params, **vis_params, "limit": int(limit)},
        )).all()
        if not rows:
            return []
        points = {
            p.id: p for p in (await self.db.execute(
                select(KGEntityContactPoint).where(KGEntityContactPoint.id.in_([r.id for r in rows]))
            )).scalars()
        }
        return [
            ContactPointHit(points[r.id], int(r.entity_id), r.name, r.entity_type)
            for r in rows if r.id in points
        ]

    async def list_for_entity(self, entity_id: int, asker_id: int | None) -> list[KGEntityContactPoint]:
        hits = await self._hits("cp.entity_id = :eid", {"eid": int(entity_id)}, asker_id, limit=200)
        return [h.point for h in hits]

    async def lookup_number(self, raw: str, asker_id: int | None, limit: int = 10) -> list[ContactPointHit]:
        """Reverse lookup "whose number is this?" -- circle-filtered, live values only."""
        normalized = normalize_phone(raw)
        if normalized is None:
            return []
        return await self._hits("cp.normalized = :n", {"n": normalized[0]}, asker_id, limit)

    async def find_for_name(
        self, name: str, asker_id: int | None, kind: str | None = None, limit: int = 20,
    ) -> list[ContactPointHit]:
        """Contact points of the entities named ``name`` (exact or alias, case-insensitive)."""
        where = (
            "(lower(e.name) = lower(:name) "
            " OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(e.surface_forms::jsonb) sf "
            "            WHERE lower(sf) = lower(:name)))"
        )
        params: dict = {"name": name.strip()}
        if kind:
            where += " AND cp.kind = :kind"
            params["kind"] = kind
        return await self._hits(where, params, asker_id, limit)

    async def get_visible(self, point_id: int, asker_id: int | None) -> KGEntityContactPoint | None:
        """The live point if the asker may see it AND its entity -- one row, both filters."""
        hits = await self._hits("cp.id = :pid", {"pid": int(point_id)}, asker_id, limit=1)
        return hits[0].point if hits else None

    # --- writes (the route checks the entity is visible AND owned) -------------

    async def add(
        self,
        entity: KGEntity,
        *,
        kind: str,
        value: str,
        label: str | None = None,
        actor_id: int | None = None,
        source_type: str = CONTACT_POINT_SOURCE_MANUAL,
        source_tier: int | None = None,
    ) -> KGEntityContactPoint:
        if kind not in CONTACT_POINT_KINDS:
            raise ContactPointError("invalid_kind", f"unknown kind {kind!r}")
        if label is not None and label not in CONTACT_POINT_LABELS:
            raise ContactPointError("invalid_label", f"unknown label {label!r}")
        value = (value or "").strip()
        normalized = normalize_phone(value)
        if normalized is None:
            raise ContactPointError("invalid_number", "not a valid phone number")
        e164, detected = normalized
        # A mobile number entered as "phone" is stored as mobile; an explicit
        # fax stays fax (the type check cannot tell a fax line).
        if kind == CONTACT_POINT_KIND_PHONE and detected == CONTACT_POINT_KIND_MOBILE:
            kind = CONTACT_POINT_KIND_MOBILE
        entity_tier = int(entity.circle_tier or 0)
        src_tier = entity_tier if source_tier is None else min(int(source_tier), TIER_PUBLIC)
        point = KGEntityContactPoint(
            entity_id=entity.id,
            user_id=entity.user_id,
            kind=kind,
            label=label,
            value=value,
            normalized=e164,
            source_type=source_type,
            source_tier=src_tier,
            circle_tier=contact_point_tier(entity_tier, src_tier),
            created_by_user_id=actor_id,
            valid_from=_now(),
        )
        # A SAVEPOINT, not a session rollback: a duplicate must not expire the
        # caller's other objects (the route's user, the entity).
        try:
            async with self.db.begin_nested():
                self.db.add(point)
                await self.db.flush()
        except IntegrityError as exc:
            # Only the live-value unique index is a duplicate; anything else (an
            # FK on an entity deleted meanwhile) is a real error, not a 409.
            if LIVE_UNIQUE_INDEX not in str(exc.orig):
                raise
            raise ContactPointError("duplicate", "the entity already has this value") from exc
        await self.db.commit()
        await self.db.refresh(point)
        return point

    async def update(
        self, point: KGEntityContactPoint, *, label: str | None = None, label_set: bool = False,
        source_tier: int | None = None,
    ) -> KGEntityContactPoint:
        """Change the label or LOWER/raise the value's own tier (never above its entity)."""
        if label_set:
            if label is not None and label not in CONTACT_POINT_LABELS:
                raise ContactPointError("invalid_label", f"unknown label {label!r}")
            point.label = label
        if source_tier is not None:
            entity = await self.db.get(KGEntity, point.entity_id)
            requested = min(int(source_tier), TIER_PUBLIC)
            # Only a value the owner entered may be opened again: a value from a
            # document / vCard / chat carries its SOURCE's tier, which only goes
            # down (contact_point_tier.py) -- an edit may lower it, never lift it.
            if point.source_type != CONTACT_POINT_SOURCE_MANUAL:
                requested = min(requested, int(point.source_tier))
            point.source_tier = requested
            point.circle_tier = contact_point_tier(int(entity.circle_tier or 0), point.source_tier)
        await self.db.commit()
        await self.db.refresh(point)
        return point

    async def delete(self, point: KGEntityContactPoint) -> None:
        """Hard delete: an owner removing a wrong or obsolete value."""
        await self.db.delete(point)
        await self.db.commit()


# --- entity lifecycle (called inside the KG service's own transaction) ---------

async def reparent_contact_points(db: AsyncSession, *, loser_id: int, winner_id: int) -> None:
    """Move the loser's live values to the survivor of a merge (no commit).

    A value the survivor already holds live ends on the loser's side
    (``superseded_by_id`` = the survivor's row) and hands over the LOWER source
    tier, so the merge never raises visibility. Ended rows move along too, for
    provenance. The caller recomputes the survivor's tiers afterwards.
    """
    params = {"w": int(winner_id), "l": int(loser_id), "now": _now()}
    await db.execute(
        text(
            "UPDATE kg_entity_contact_points w SET source_tier = LEAST(w.source_tier, l.source_tier) "
            "FROM kg_entity_contact_points l "
            "WHERE w.entity_id = :w AND l.entity_id = :l "
            "AND w.valid_to IS NULL AND l.valid_to IS NULL "
            "AND w.kind = l.kind AND w.normalized = l.normalized"
        ),
        params,
    )
    await db.execute(
        text(
            "UPDATE kg_entity_contact_points l SET valid_to = :now, superseded_by_id = w.id "
            "FROM kg_entity_contact_points w "
            "WHERE w.entity_id = :w AND l.entity_id = :l "
            "AND w.valid_to IS NULL AND l.valid_to IS NULL "
            "AND w.kind = l.kind AND w.normalized = l.normalized"
        ),
        params,
    )
    await db.execute(
        text("UPDATE kg_entity_contact_points SET entity_id = :w WHERE entity_id = :l"),
        params,
    )


async def delete_entity_contact_points(db: AsyncSession, entity_id: int) -> None:
    """Remove the values of a soft-deleted entity (no commit; design R5)."""
    await db.execute(
        text("DELETE FROM kg_entity_contact_points WHERE entity_id = :eid"),
        {"eid": int(entity_id)},
    )


__all__ = [
    "CONTACT_POINT_KIND_FAX",
    "ContactPointError",
    "ContactPointHit",
    "ContactPointService",
    "delete_entity_contact_points",
    "normalize_phone",
    "reparent_contact_points",
]
