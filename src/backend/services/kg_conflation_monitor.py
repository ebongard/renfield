"""KG conflation tripwire — read-only early warning for forming magnet hubs.

The person-magnet bug (entity #11, 127 mentions) was a generic-description
embedding centroid that distinct people folded into. The fix disabled inline
embedding-match for persons, stripped generic descriptions, and de-magnetized
existing rows. Measurement showed non-person types are currently clean (0
distinct-name same-type pairs embed >= threshold), but the *mechanism* is
type-general: any type whose rows pick up a generic centroid could start folding.

This monitor is the tripwire. It runs the exact diagnostic — a per-user
halfvec self-join for **distinct-name, same-type, same-tier** entity pairs whose
cosine similarity is >= ``kg_conflation_monitor_threshold`` — and just *reports*
them (WARNING log + a Prometheus gauge). It NEVER mutates: a genuine duplicate is
the review-gated reconciler's job; this only surfaces "two things that should be
different look the same", which expected-0 makes a clean regression signal.

**Scope: NON-person entities only.** ``resolve_entity`` skips embedding-match for
persons unconditionally (gate on the multi-type set), because distinct person
names inherently embed >= the threshold name-only (measured: Jutta~Anna 0.894,
Jutta~Gaby 0.863). So a close person pair can NEVER fold via resolve — flagging it
would be permanent noise that keeps the gauge off zero. The tripwire therefore
excludes any entity that is person-typed (primary OR multi-type), matching the
set resolve protects, so the gauge stays a clean 0/non-0 signal for the types
where a fold can actually happen. (Person dedup is the reconciler's same-name
gate, not this tripwire.)

Read-only by construction. Postgres-only (halfvec); short-circuits to [] on the
sqlite shim. Mirrors ``KgReconcilerService`` structure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import EMBEDDING_DIMENSION
from utils.config import settings

logger = logging.getLogger("kg_conflation_monitor")


@dataclass
class ConflationPair:
    entity_type: str
    id_a: int
    name_a: str
    id_b: int
    name_b: str
    tier: int
    similarity: float


@dataclass
class ConflationReport:
    scanned_users: int = 0
    pairs: list[ConflationPair] = field(default_factory=list)


class KgConflationMonitor:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_active_user_ids(self) -> list[int]:
        rows = (await self.db.execute(text(
            "SELECT DISTINCT user_id FROM kg_entities "
            "WHERE is_active = true AND canonical_id IS NULL AND user_id IS NOT NULL"
        ))).fetchall()
        return [int(r[0]) for r in rows]

    async def scan_for_user(self, user_id: int) -> list[ConflationPair]:
        """Distinct-name, same-type, same-tier pairs embedding >= the threshold."""
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return []

        dim = EMBEDDING_DIMENSION
        sql = text(f"""
            SELECT a.entity_type AS etype, a.circle_tier AS tier,
                   a.id AS id_a, a.name AS name_a,
                   b.id AS id_b, b.name AS name_b,
                   1 - (a.embedding::halfvec({dim}) <=> b.embedding::halfvec({dim})) AS similarity
            FROM kg_entities a
            JOIN kg_entities b
              ON a.id < b.id
             AND a.user_id = b.user_id
             AND a.entity_type = b.entity_type
             AND a.circle_tier = b.circle_tier
             AND a.embedding IS NOT NULL
             AND b.embedding IS NOT NULL
             AND lower(a.name) <> lower(b.name)
            WHERE a.user_id = :uid
              AND a.is_active = true AND b.is_active = true
              AND a.canonical_id IS NULL AND b.canonical_id IS NULL
              -- NON-person only: persons skip embedding-match in resolve (their
              -- names inherently cluster >= threshold), so a close person pair
              -- can't fold and would be permanent noise. Mirror resolve's gate:
              -- exclude primary-person AND multi-type-person on either side.
              AND a.entity_type <> 'person' AND b.entity_type <> 'person'
              AND NOT (a.entity_types::jsonb @> '["person"]'::jsonb)
              AND NOT (b.entity_types::jsonb @> '["person"]'::jsonb)
              AND (1 - (a.embedding::halfvec({dim}) <=> b.embedding::halfvec({dim})))
                  >= :thresh
            ORDER BY similarity DESC
            LIMIT :cap
        """)
        rows = (await self.db.execute(sql, {
            "uid": user_id,
            "thresh": settings.kg_conflation_monitor_threshold,
            "cap": settings.kg_conflation_monitor_max_pairs,
        })).fetchall()
        return [
            ConflationPair(
                entity_type=r.etype, tier=int(r.tier or 0),
                id_a=int(r.id_a), name_a=r.name_a,
                id_b=int(r.id_b), name_b=r.name_b,
                similarity=round(float(r.similarity), 3),
            )
            for r in rows
        ]

    async def scan_all(self, user_id: int | None = None) -> ConflationReport:
        """Scan one user (if given) or every active user; log + count, never mutate."""
        uids = [user_id] if user_id is not None else await self.list_active_user_ids()
        rep = ConflationReport(scanned_users=len(uids))
        for uid in uids:
            try:
                pairs = await self.scan_for_user(uid)
            except Exception as e:  # noqa: BLE001 — one user's bad data must not stop the scan
                logger.warning("conflation scan failed for user %d: %s", uid, e)
                # All users share this session: a DB error leaves the txn in an
                # aborted state and every subsequent user would fail too, silently
                # zeroing the gauge — the worst failure for a tripwire. Roll back
                # so the scan continues cleanly (no-op on a read-only session).
                try:
                    await self.db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                continue
            for p in pairs:
                logger.warning(
                    "KG conflation tripwire (user %d): %s #%d %r ~ #%d %r tier=%d cosine=%.3f "
                    "(distinct names embed ≥ %.2f — possible forming magnet / mis-embedding)",
                    uid, p.entity_type, p.id_a, p.name_a, p.id_b, p.name_b, p.tier,
                    p.similarity, settings.kg_conflation_monitor_threshold,
                )
            rep.pairs.extend(pairs)

        try:
            from utils.metrics import set_kg_conflation_candidates
            set_kg_conflation_candidates(len(rep.pairs))
        except Exception:  # noqa: BLE001 — metrics are best-effort
            pass

        if rep.pairs:
            logger.warning("KG conflation tripwire: %d suspicious pair(s) across %d user(s)",
                           len(rep.pairs), rep.scanned_users)
        else:
            logger.info("KG conflation tripwire: clean (%d user(s) scanned)", rep.scanned_users)
        return rep
