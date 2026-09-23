"""Re-home the household's data before the auth-on cutover (§4, P0 Nr. 8).

Under ``AUTH_ENABLED=false`` the household wrote everything onto one fallback
owner at tier 0, left typed conversations without an owner at all, and never
evaluated the access filters. Flipping the flag without re-homing the data first
leaves every member but the admin seeing nothing. This module does the re-homing;
`bin/backfill_household_tiers.py` is the CLI over it.

Runs while auth is still OFF, so it is INERT at the moment it runs: tiers and
memberships are not evaluated under auth-off. That is the point — the data is in
place before the switch, and `--revert` puts it back if the switch is deferred.

**Only atoms still at the fallback tier 0 are moved.** The household carries 56
hand-set tiers (2 × 1, 19 × 2, 35 × 4) and §4.1 says they stay as they are. There
is no "hand-set" flag, and there does not need to be one: the auth-off fallback
wrote tier 0 and nothing else, so a non-zero tier IS the fingerprint of a
deliberate choice. Never overwrite one.

A consequence worth stating plainly, because it makes the job much smaller than
§4.1's table suggests: of the four tier classes only ONE actually moves a row.
The knowledge graph goes 0 → 2. Documents, facts and memories are *already* at
tier 0 and their target is tier 0 — for them the cutover's effect comes from auth
being switched on, not from anything this script writes. The script still counts
them, so the dry-run shows the whole posture rather than only the deltas.

Every tier change goes through ``AtomService.update_tier`` — never a direct
UPDATE (`docs/CIRCLES.md` anti-patterns): it owns the cascade (entity → incident
relations, document → chunks → facts) and keeps ``atoms.policy`` and the
denormalized ``circle_tier`` columns from drifting apart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Tier targets per source class (§4.1). The knowledge graph becomes household
# knowledge; everything that carries a person's documents or words stays with
# the admin until they widen it one atom at a time.
TIER_HOUSEHOLD = 2
TIER_SELF = 0

_KG_ATOM_TYPES = ("kg_node", "kg_edge")
_ADMIN_ATOM_TYPES = ("kb_document", "document_fact", "conversation_memory")

# The dimension config a household member's circles row carries. Only the tier
# dimension is configured for households (enterprise adds tenant/project).
_HOUSEHOLD_DIMENSION_CONFIG: dict[str, Any] = {
    "tier": {
        "shape": "ladder",
        "values": ["self", "trusted", "household", "extended", "public"],
    }
}


@dataclass
class ClassResult:
    """What one class did, or would do. ``changed`` is the count that matters;
    ``examined`` is how many rows the class looked at, so a dry-run that reports
    0 changes can be told apart from one that found nothing to look at."""

    name: str
    examined: int = 0
    changed: int = 0
    skipped_handset: int = 0
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        parts = [f"{self.name}: {self.changed} von {self.examined}"]
        if self.skipped_handset:
            parts.append(f"{self.skipped_handset} handgesetzt (unberührt)")
        return " · ".join(parts + self.notes)


@dataclass
class MemberList:
    """Who the household is. Supplied by the operator, never guessed.

    ``family`` are the people who see each other's household-tier atoms.
    ``guests`` get no memberships at all (D-2d) and a capture default of tier 0.
    ``device_account_id`` is the satellite's identity; ``admin_id`` owns the
    legacy corpus.
    """

    admin_id: int
    family: list[int]
    guests: list[int] = field(default_factory=list)
    device_account_id: int | None = None


async def _atom_ids_at_fallback_tier(
    db: AsyncSession, owner_id: int, atom_types: tuple[str, ...]
) -> tuple[list[str], int]:
    """(atom ids still at tier 0, count of hand-set ones left alone).

    ``atoms.policy`` is JSON; the tier lives under the ``tier`` key. Read it as
    text and cast, the same idiom `circle_sql` uses — Postgres will not cast
    `json` straight to `integer`.
    """
    rows = (await db.execute(
        text(
            "SELECT atom_id, COALESCE((policy ->> 'tier')::int, 0) AS tier "
            "  FROM atoms "
            " WHERE owner_user_id = :owner AND atom_type = ANY(:types)"
        ),
        {"owner": owner_id, "types": list(atom_types)},
    )).all()
    at_fallback = [r[0] for r in rows if r[1] == TIER_SELF]
    handset = sum(1 for r in rows if r[1] != TIER_SELF)
    return at_fallback, handset


async def backfill_kg_tiers(
    db: AsyncSession, members: MemberList, *, dry_run: bool = True, revert: bool = False
) -> ClassResult:
    """The knowledge graph becomes household knowledge: tier 0 → 2 (§4.1).

    The ONE class that actually moves rows. `update_tier` cascades a node's tier
    onto its incident relations, so the edges follow their endpoints rather than
    being moved separately.
    """
    target = TIER_SELF if revert else TIER_HOUSEHOLD
    source = TIER_HOUSEHOLD if revert else TIER_SELF
    rows = (await db.execute(
        text(
            "SELECT atom_id FROM atoms "
            " WHERE owner_user_id = :owner AND atom_type = ANY(:types) "
            "   AND COALESCE((policy ->> 'tier')::int, 0) = :source"
        ),
        {"owner": members.admin_id, "types": list(_KG_ATOM_TYPES), "source": source},
    )).all()
    atom_ids = [r[0] for r in rows]
    _, handset = await _atom_ids_at_fallback_tier(db, members.admin_id, _KG_ATOM_TYPES)

    result = ClassResult(
        name="kg", examined=len(atom_ids), skipped_handset=0 if revert else handset
    )
    if dry_run:
        result.changed = len(atom_ids)
        result.notes.append(f"Stufe {source} → {target} (Probelauf)")
        return result

    from services.atom_service import AtomService

    svc = AtomService(db)
    for atom_id in atom_ids:
        # Per-row transaction (§4.1): a failure halfway leaves a consistent
        # prefix done, and a re-run picks up where it stopped — the selection
        # above only sees rows still at the source tier, so it is idempotent.
        await svc.update_tier(atom_id, {"tier": target})
        await db.commit()
        result.changed += 1
    result.notes.append(f"Stufe {source} → {target}")
    return result


async def report_admin_tiers(
    db: AsyncSession, members: MemberList, *, dry_run: bool = True, revert: bool = False
) -> ClassResult:
    """Documents, facts and memories stay with the admin at tier 0 (§4.1).

    Reports, never writes. Their target tier IS the fallback tier they already
    carry, so there is nothing to move: what changes for them at the cutover is
    that the filter starts being evaluated at all. Counted anyway so the dry-run
    shows the posture of the whole corpus instead of only its deltas — an
    operator reading "documents: 0" next to a class that moved thousands of rows
    should be able to see it is 0 because nothing was needed.
    """
    at_fallback, handset = await _atom_ids_at_fallback_tier(
        db, members.admin_id, _ADMIN_ATOM_TYPES
    )
    return ClassResult(
        name="admin-atome",
        examined=len(at_fallback) + handset,
        changed=0,
        skipped_handset=handset,
        notes=["bereits Stufe 0 — der Cutover wirkt durch das Flag, nicht durch dieses Skript"],
    )


async def backfill_null_relations(
    db: AsyncSession, members: MemberList, *, dry_run: bool = True, revert: bool = False
) -> ClassResult:
    """`kg_relations` with no owner (§4.2): unreachable in EVERY branch once auth
    is on — the owner branch compares against NULL and the membership branch
    keys `circle_owner_id` on it. They hang off the admin's entities, so they
    get the admin and the graph's tier."""
    if revert:
        rows = (await db.execute(
            text(
                "SELECT COUNT(*) FROM kg_relations "
                " WHERE user_id = :admin AND circle_tier = :tier"
            ),
            {"admin": members.admin_id, "tier": TIER_HOUSEHOLD},
        )).scalar_one()
        return ClassResult(
            name="null-relationen", examined=int(rows), changed=0,
            notes=["Rückweg nicht eindeutig: welche Zeilen vorher NULL waren, "
                   "ist nicht mehr unterscheidbar — bewusst nicht zurückgenommen"],
        )

    count = (await db.execute(
        text("SELECT COUNT(*) FROM kg_relations WHERE user_id IS NULL")
    )).scalar_one()
    result = ClassResult(name="null-relationen", examined=int(count))
    if dry_run:
        result.changed = int(count)
        result.notes.append(f"→ Eigentümer {members.admin_id}, Stufe {TIER_HOUSEHOLD}")
        return result
    await db.execute(
        text(
            "UPDATE kg_relations SET user_id = :admin, circle_tier = :tier "
            " WHERE user_id IS NULL"
        ),
        {"admin": members.admin_id, "tier": TIER_HOUSEHOLD},
    )
    await db.commit()
    result.changed = int(count)
    return result


async def backfill_kb_owner(
    db: AsyncSession, members: MemberList, *, dry_run: bool = True, revert: bool = False
) -> ClassResult:
    """`knowledge_bases.owner_id IS NULL` (§4.2). The document owner branch reads
    the KB owner (with an atom-owner fallback for null-KB rows), so an ownerless
    KB leaves its documents reachable only through that fallback."""
    if revert:
        return ClassResult(
            name="kb-eigentümer", examined=0, changed=0,
            notes=["Rückweg nicht eindeutig: welche KBs vorher eigentümerlos waren, "
                   "ist nicht mehr unterscheidbar — bewusst nicht zurückgenommen"],
        )
    count = (await db.execute(
        text("SELECT COUNT(*) FROM knowledge_bases WHERE owner_id IS NULL")
    )).scalar_one()
    result = ClassResult(name="kb-eigentümer", examined=int(count))
    if dry_run:
        result.changed = int(count)
        result.notes.append(f"→ Eigentümer {members.admin_id}")
        return result
    await db.execute(
        text("UPDATE knowledge_bases SET owner_id = :admin WHERE owner_id IS NULL"),
        {"admin": members.admin_id},
    )
    await db.commit()
    result.changed = int(count)
    return result


async def backfill_conversations(
    db: AsyncSession, members: MemberList, *, dry_run: bool = True, revert: bool = False
) -> ClassResult:
    """Ownerless conversations get an owner and an atom (§4.2, D-3).

    A conversation with a `speaker_id` whose speaker is linked to a user goes to
    THAT user; the rest go to the admin. Tier 0 for the existing corpus — a
    deliberate exception to §8.1's room-history rule, since nobody can say today
    which of these was a shared room thread and which was somebody's private
    chat. The owner raises individual ones afterwards.

    Each row gets its atoms entry as it gets its owner, which is the invariant
    `ConversationService.ensure_atom` keeps everywhere else.
    """
    if revert:
        return ClassResult(
            name="unterhaltungen", examined=0, changed=0,
            notes=["Rückweg nicht eindeutig: welche Zeilen vorher eigentümerlos waren, "
                   "ist nicht mehr unterscheidbar — bewusst nicht zurückgenommen"],
        )

    from models.database import Conversation
    from services.conversation_service import ConversationService

    rows = (await db.execute(
        text(
            "SELECT c.id, u.id AS linked_user "
            "  FROM conversations c "
            "  LEFT JOIN users u ON u.speaker_id = c.speaker_id "
            " WHERE c.user_id IS NULL"
        )
    )).all()
    by_speaker = sum(1 for r in rows if r[1] is not None)
    result = ClassResult(name="unterhaltungen", examined=len(rows))
    result.notes.append(
        f"{by_speaker} über Sprecher-Verknüpfung, {len(rows) - by_speaker} an "
        f"Eigentümer {members.admin_id}"
    )
    if dry_run:
        result.changed = len(rows)
        return result

    svc = ConversationService(db)
    for conv_id, linked_user in rows:
        owner = linked_user if linked_user is not None else members.admin_id
        conversation = await db.get(Conversation, conv_id)
        if conversation is None:  # vanished between the scan and here
            continue
        conversation.user_id = owner
        await db.flush()
        await svc.ensure_atom(conversation)
        await db.commit()
        result.changed += 1
    return result


async def backfill_pairing_remnant(
    db: AsyncSession, members: MemberList, *, dry_run: bool = True, revert: bool = False
) -> ClassResult:
    """Delete the stale self-membership `(admin, admin, tier)` (§4.3).

    `pairing_service._upsert_circle_membership` wrote it with a PEER's remote
    user id, which in a single-user household collides with the local admin's
    own id. It is not a household membership and it must not be read as one —
    under auth-on it would make the admin a member of their own circles, which
    is exactly the federation collision `peer_scoped` exists to prevent.

    Not reverted: recreating a row that was written by mistake is not a rollback.
    """
    if revert:
        return ClassResult(
            name="kopplungs-rest", examined=0, changed=0,
            notes=["nicht zurückgenommen — die Zeile war ein Kopplungs-Artefakt, "
                   "kein Zustand, den man wiederherstellen möchte"],
        )
    count = (await db.execute(
        text(
            "SELECT COUNT(*) FROM circle_memberships "
            " WHERE circle_owner_id = :a AND member_user_id = :a AND dimension = 'tier'"
        ),
        {"a": members.admin_id},
    )).scalar_one()
    result = ClassResult(name="kopplungs-rest", examined=int(count))
    if dry_run:
        result.changed = int(count)
        return result
    await db.execute(
        text(
            "DELETE FROM circle_memberships "
            " WHERE circle_owner_id = :a AND member_user_id = :a AND dimension = 'tier'"
        ),
        {"a": members.admin_id},
    )
    await db.commit()
    result.changed = int(count)
    return result


def _membership_pairs(members: MemberList) -> list[tuple[int, int, int]]:
    """Every `(circle_owner, member, tier)` row the household needs (§4.3).

    Three groups, and the third is a CORRECTION to §4.3 as written:

    1. family × family at tier 2 — each member sees the others' household-tier
       atoms. Guests get none (D-2d).
    2. the device account inside the ADMIN's circle at tier 2 (D-2e) — so an
       anonymous room turn reads the household graph and whatever the admin
       holds at tier 2, and NOT the other members' tier-2 memories.
    3. **each family member inside the DEVICE's circle at tier 2.** §4.3 only
       specified direction 2, which is the device READING. A room history
       (§8.1) is OWNED by the device account, and the filter keys
       `circle_owner_id` on the ROW's owner — so without this direction the
       kitchen thread reaches nobody but the admin and P0 Nr. 6 has no effect in
       service. §4.3 predates §8.1; this is the reconciliation.
       What it opens is narrow: a device account owns essentially only the room
       conversations, because extraction and presence are barred for device
       accounts (D-4b). It does not touch D-2e, which governs what the device
       reads, not what is read at it.
    """
    pairs: list[tuple[int, int, int]] = []
    for owner in members.family:
        for member in members.family:
            if owner != member:
                pairs.append((owner, member, TIER_HOUSEHOLD))
    if members.device_account_id is not None:
        pairs.append((members.admin_id, members.device_account_id, TIER_HOUSEHOLD))
        for member in members.family:
            if member != members.device_account_id:
                pairs.append((members.device_account_id, member, TIER_HOUSEHOLD))
    return pairs


async def backfill_memberships(
    db: AsyncSession, members: MemberList, *, dry_run: bool = True, revert: bool = False
) -> ClassResult:
    """Create (or remove) the household's circle memberships + capture defaults.

    Idempotent: a pair that already exists is left alone, so a re-run adds
    nothing twice and never lowers a tier somebody raised by hand.
    """
    pairs = _membership_pairs(members)
    result = ClassResult(name="mitgliedschaften", examined=len(pairs))

    existing = {
        (r[0], r[1])
        for r in (await db.execute(
            text(
                "SELECT circle_owner_id, member_user_id FROM circle_memberships "
                " WHERE dimension = 'tier'"
            )
        )).all()
    }
    if revert:
        removable = [p for p in pairs if (p[0], p[1]) in existing]
        result.changed = len(removable)
        if not dry_run:
            for owner, member, _tier in removable:
                await db.execute(
                    text(
                        "DELETE FROM circle_memberships "
                        " WHERE circle_owner_id = :o AND member_user_id = :m "
                        "   AND dimension = 'tier'"
                    ),
                    {"o": owner, "m": member},
                )
            await db.commit()
        return result

    missing = [p for p in pairs if (p[0], p[1]) not in existing]
    result.changed = len(missing)
    result.notes.append(f"{len(pairs) - len(missing)} bestanden bereits")
    if dry_run:
        return result

    for owner, member, tier in missing:
        await db.execute(
            text(
                "INSERT INTO circle_memberships "
                "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
                "VALUES (:o, :m, 'tier', :v, :admin, NOW())"
            ),
            {"o": owner, "m": member, "v": str(tier), "admin": members.admin_id},
        )
    await db.commit()
    return result


async def backfill_capture_policy(
    db: AsyncSession, members: MemberList, *, dry_run: bool = True, revert: bool = False
) -> ClassResult:
    """Capture default per account (D-2c): family tier 2, guests tier 0.

    What a NEW atom is written at. A family member's turns become household
    knowledge by default; a guest's stay private. Idempotent and never lowers an
    existing explicit policy — an operator who already set one meant it.
    """
    from models.database import Circle

    wanted = {uid: TIER_HOUSEHOLD for uid in members.family}
    wanted.update({uid: TIER_SELF for uid in members.guests})
    result = ClassResult(name="erfassungsvorgabe", examined=len(wanted))
    if revert:
        result.notes.append(
            "nicht zurückgenommen — die Vorgabe sagt, wie NEUE Atome entstehen; "
            "sie zurückzudrehen verändert nichts Bestehendes"
        )
        return result

    for uid, tier in wanted.items():
        row = (await db.execute(
            select(Circle).where(Circle.owner_user_id == uid)
        )).scalar_one_or_none()
        if row is not None:
            continue  # an existing policy is somebody's decision, not a default
        result.changed += 1
        if dry_run:
            continue
        db.add(Circle(
            owner_user_id=uid,
            dimension_config=dict(_HOUSEHOLD_DIMENSION_CONFIG),
            default_capture_policy={"tier": tier},
        ))
    if not dry_run:
        await db.commit()
    return result


CLASSES: dict[str, Any] = {
    "kg": backfill_kg_tiers,
    "admin-atome": report_admin_tiers,
    "null-relationen": backfill_null_relations,
    "kb-eigentümer": backfill_kb_owner,
    "unterhaltungen": backfill_conversations,
    "kopplungs-rest": backfill_pairing_remnant,
    "mitgliedschaften": backfill_memberships,
    "erfassungsvorgabe": backfill_capture_policy,
}


async def run_backfill(
    db: AsyncSession,
    members: MemberList,
    *,
    only: list[str] | None = None,
    dry_run: bool = True,
    revert: bool = False,
) -> list[ClassResult]:
    """Run the selected classes in order and return one result per class.

    Order matters in one place: `unterhaltungen` gives rows an owner, and
    `mitgliedschaften` decides who reaches them. Running memberships first would
    still be correct — the filter reads both at query time — but the dry-run
    reads better in the order the data acquires meaning.
    """
    names = only or list(CLASSES)
    unknown = [n for n in names if n not in CLASSES]
    if unknown:
        raise ValueError(f"Unbekannte Klasse(n): {unknown}. Bekannt: {list(CLASSES)}")
    results = []
    for name in names:
        logger.info(f"▶ {name} ({'Probelauf' if dry_run else 'Schreiblauf'})")
        results.append(
            await CLASSES[name](db, members, dry_run=dry_run, revert=revert)
        )
    return results
