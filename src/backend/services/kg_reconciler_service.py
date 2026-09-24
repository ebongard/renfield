"""KG entity reconciler (Structured Memory Phase 1, T5).

Periodic, per-user pass that catches near-duplicate entities born *after* both
spellings existed (the same-tier guard in resolve_entity deliberately creates a
fresh entity rather than fold across tiers, so duplicates accumulate and are
reconciled here). Mirrors SkillCuratorService: a halfvec embedding self-join
finds candidate pairs; the winner is the more-established row.

Policy (the safety core):
  - PERSON-GUARD (first gate): a person-involving pair whose names are UNRELATED
    is dropped entirely (no merge, no proposal). Distinct person names embed
    >= the candidate threshold by themselves, so embedding can't tell two people
    apart — persons only reconcile when names are related (equal or token-subset:
    "Alice" ⊆ "Alice B."). Mirrors resolve's person embedding-match skip. This is
    what makes the reconciler safe to enable; see _names_related. ONE exception
    (#876 field data): a TYPO pair — same tokens except one, that one differing
    by a single in-token edit, both spellings >= 4 chars (_names_near_typo) —
    survives as a REVIEW proposal (reason name_typo), never an auto-merge.
  - SAME tier AND similarity >= auto-merge threshold -> auto-merge via
    KnowledgeGraphService.merge_entities (which enforces tier=MIN etc.).
  - CROSS tier (could change visibility, D3) OR gray-zone (similar but below
    the auto bar, D10) OR name_typo -> a KgMergeProposal for owner review on
    /brain/review. Never silently merged. cross_tier takes precedence as the
    label (the visibility change is the invariant-bearing fact).

Idempotent: candidate pairs that already have a PENDING proposal are excluded
by the find query (and the proposals table carries a partial-unique guard), and
so are pairs the owner REJECTED — a rejection is a verdict the reconciler does
not re-litigate (the only way back is an explicit admin merge).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import selectinload

from models.database import (
    EMBEDDING_DIMENSION,
    KG_MERGE_PROPOSAL_APPROVED,
    KG_MERGE_PROPOSAL_PENDING,
    KG_MERGE_PROPOSAL_REJECTED,
    KG_MERGE_PROPOSAL_SUPERSEDED,
    KG_MERGE_REASON_CROSS_TIER,
    KG_MERGE_REASON_CROSS_TYPE,
    KG_MERGE_REASON_GRAY_ZONE,
    KG_MERGE_REASON_NAME_TOKENIZATION,
    KG_MERGE_REASON_NAME_TYPO,
    KGEntity,
    KgMergeProposal,
)
from services.knowledge_graph_service import KnowledgeGraphService
from utils.config import settings

# Fixed namespace key (classid) for the per-user reconciler advisory lock (#4).
# pg_advisory_lock keys are int4; the objid is the user_id.
_RECONCILER_LOCK_NS = 0x4B47  # "KG"


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _resolve_lock_engine(bind) -> AsyncEngine | None:
    """The AsyncEngine to open the dedicated advisory-lock connection on.

    Topology-dependent and the reason the reconciler crashed when first enabled
    in prod (run-unlocked / MissingGreenlet otherwise):
      * prod: ``AsyncSession`` is bound to an ``AsyncEngine`` (async_sessionmaker).
        Use it DIRECTLY — ``AsyncEngine.engine`` proxies to the *sync* Engine,
        whose ``.connect()`` returns a sync connection that explodes under
        ``async with`` (greenlet_spawn / 'NoneType' has no attribute 'cursor').
      * tests: ``AsyncSession`` is bound to an ``AsyncConnection`` (per-test
        connection fixture); its ``.engine`` IS the AsyncEngine.
      * sqlite shim / unknown: return None -> caller runs unlocked (safe: the
        single-instance daily scheduler won't collide per-user).
    """
    if isinstance(bind, AsyncEngine):
        return bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    return None


def _name_collision_low_signal(
    name_a: str | None, name_b: str | None,
    desc_a: str | None, desc_b: str | None,
) -> bool:
    """True when a candidate pair shares a name but lacks signal to tell them apart.

    Same normalized name + (either description empty OR identical descriptions) =>
    the embedding match is essentially a name match, which cannot distinguish two
    different real entities that happen to share a name. Such pairs must go to
    owner review, never auto-merge (Phase 3 P3-T2). When BOTH sides carry distinct
    non-empty descriptions the similarity is meaningful, so auto-merge stays allowed.
    """
    if _norm(name_a) != _norm(name_b):
        return False
    da, db = _norm(desc_a), _norm(desc_b)
    return (not da) or (not db) or (da == db)


def _is_person(etype: str | None, etypes_text: str | None) -> bool:
    """True if the entity is person-typed — primary OR in the multi-type set.

    ``etypes_text`` is ``entity_types::text`` (a JSON array literal like
    ``["organization", "person"]``), so a substring check on the quoted token is
    a deterministic, decoder-independent membership test.
    """
    return etype == "person" or (etypes_text is not None and '"person"' in etypes_text)


# The extraction's fallback bucket: `_build_entities` assigns "thing" when the
# model named no type at all. It is the ABSENCE of a type claim, so it must not
# make a pair "disjoint" — the single most common duplicate shape in an
# LLM-extracted graph is the same real thing extracted once as `thing` and once
# as `organization`/`concept`, and those names are often NOT token-related
# ("Fa. Müller" / "Müller GmbH"). Treating `thing` as a claim would drop exactly
# those pairs with no merge, no proposal and no row the owner could ever find.
_UNTYPED = {"thing"}


def _types_compatible(etype_a: str | None, etype_b: str | None) -> bool:
    """False only when the two entities' PRIMARY types disagree.

    Deliberately the scalar `entity_type`, not the `entity_types` superset. The
    superset only ever GROWS — ``merge_entities`` unions both sides into the
    survivor and every re-mention folds newly observed types in — so an
    overlap test disarms itself with exactly the usage this guard exists for:
    approve one legitimate mis-typed-duplicate fold and the survivor claims both
    types forever after, matching everything of either kind. The primary is
    stable: nothing writes it but an explicit owner edit (`update_entity`).
    It is also what the review UI compares, so view and service agree.

    An absent primary, or the `thing` bucket (see ``_UNTYPED``), is no claim at
    all and therefore no mismatch — the guard accuses, it never guesses.
    """
    pa, pb = _norm(etype_a), _norm(etype_b)
    if not pa or not pb:
        return True
    if pa in _UNTYPED or pb in _UNTYPED:
        return True
    return pa == pb


def _names_related(name_a: str | None, name_b: str | None) -> bool:
    """Two names are related iff equal or one's whitespace tokens subset the other.

    The person-guard's evidence (find_duplicate_pairs applies it to any pair with
    a person on either side): distinct person names embed >= the candidate
    threshold by themselves (measured: Jutta~Anna 0.894, Jutta~Gaby 0.863), so
    embedding similarity alone cannot tell two different people apart.
    "Alice" ⊆ "Alice B.", "Jutta" ⊆ "Jutta van den Bongard" -> related (likely the
    same entity, a surface-form variant). "Jutta" vs "Anna", "Anna Schmidt" vs
    "Anna Müller" -> unrelated. Empty on either side -> not related (can't tell).
    """
    ta, tb = set(_norm(name_a).split()), set(_norm(name_b).split())
    if not ta or not tb:
        return False
    return ta == tb or ta <= tb or tb <= ta


# Minimum token length for the typo test. A one-character difference in a short
# token is a different word, not a slip: "01" vs "02" (test accounts numbered by
# a trailing ordinal), "Jan" vs "Jen". Measured on the #876 field data: the six
# false pairs all differ in a 2-character ordinal, the one true pair in a
# 12-character surname.
_TYPO_MIN_TOKEN_LEN = 4


# CamelCase boundaries, as two ZERO-WIDTH lookarounds: they only insert a space,
# they never alter a character.
#   1. lower -> UPPER       "ProductOwner"  -> "Product Owner"
#   2. acronym boundary     "QAEngineer"    -> "QA Engineer"
# Rule 2 is easy to forget and carries half the cases ("XMLHttpRequest" without
# it becomes "XMLHttp Request"). Agreed with the reva instance 2026-09-24, which
# measured the field data this is built for.
#
# DIGITS ARE DELIBERATELY NOT A BOUNDARY (`[a-z]`, not `[a-z0-9]`): splitting
# them turns "E2E" into "E2 E", which in the measured corpus produced one
# nonsense pair and rescued nothing. `-` and `_` are not separators either —
# a hyphen carries meaning in this data ("RM27-10", "Product A - 1.2.4"), and
# splitting it would take apart exactly the version-number class that already
# merges too easily.
_CAMEL_LOWER_UPPER = re.compile(r"(?<=[a-z])(?=[A-Z])")
_CAMEL_ACRONYM = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def _split_camel(s: str | None) -> str:
    """Insert a space at every CamelCase boundary; otherwise leave the string alone."""
    return _CAMEL_ACRONYM.sub(" ", _CAMEL_LOWER_UPPER.sub(" ", s or ""))


def _names_related_after_split(name_a: str | None, name_b: str | None) -> bool:
    """True when two names become token-related once CamelCase is split apart.

    The gap both guards share: `_names_related` tokenizes on whitespace, so it
    cannot see across a tokenization difference — "Product Owner" against
    "ProductOwner" is a subset in neither direction, and the pair is dropped with
    no merge, no proposal and no row the owner could find. In the field that
    shape is almost always a MIS-TYPED ROLE ("ProductOwner", "SecurityEngineer",
    "QAEngineer" carried as `person`), which is exactly what the `cross_type`
    exception was built to collect and could not reach.

    This is a SEPARATE test, never a loosening of `_names_related` — that one is
    the person-guard's safety invariant, and `person_ok` reads it to open the
    AUTO-MERGE gate. A pair rescued here keeps `names_related=False`, so the
    gate stays shut; it may be reviewed, never silently folded.

    Already-related names are not tokenization variants (nothing to rescue).
    """
    if _names_related(name_a, name_b):
        return False
    return _names_related(_split_camel(name_a), _split_camel(name_b))


def _osa_distance_is_one(a: str, b: str) -> bool:
    """Optimal-string-alignment distance == 1: one substitution, insertion,
    deletion, or ADJACENT transposition. Names are short; a full DP is cheap."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    la, lb = len(a), len(b)
    prev2: list[int] | None = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]
                    and prev2 is not None):
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[lb] == 1


def _names_near_typo(name_a: str | None, name_b: str | None) -> bool:
    """Two names are a TYPO pair iff they have the same tokens in the same order
    except for exactly one token, and that token differs by a single in-token
    edit (OSA distance 1) with both spellings at least ``_TYPO_MIN_TOKEN_LEN``.

    This is the gap the token-subset test leaves (#876 field data, 2026-09-21:
    the real case had two characters transposed INSIDE the final token of a
    four-token name; the anonymised stand-in "…Lastname" / "…Lastnrame" is a
    one-character insertion — both are OSA distance 1). Such a pair is a subset
    in neither direction, so the person-guard dropped the single most common
    duplicate cause with no merge and no proposal. A typo pair is a REVIEW
    candidate only ("Anna Schmidt" vs "Anna Schmitt" may be two people); the
    caller must never auto-merge it. Names that are already related (equal /
    subset) are not typo pairs.
    """
    ta, tb = _norm(name_a).split(), _norm(name_b).split()
    if not ta or not tb or len(ta) != len(tb):
        return False
    diffs = [(x, y) for x, y in zip(ta, tb, strict=True) if x != y]
    if len(diffs) != 1:
        return False
    x, y = diffs[0]
    if min(len(x), len(y)) < _TYPO_MIN_TOKEN_LEN:
        return False
    return _osa_distance_is_one(x, y)


@dataclass
class MergeCandidate:
    loser_id: int
    winner_id: int
    similarity: float
    loser_tier: int
    winner_tier: int
    # Same canonical name + weak disambiguating signal (a missing or identical
    # description on either side). The embedding similarity is then driven almost
    # entirely by the shared name, so two genuinely different people ("Anna" the
    # mother vs "Anna" the friend) look like a dupe. Never auto-merge these —
    # route to owner review — else the memory↔entity bridge's backfill would feed
    # the Jutta/Anna conflation back through the reconciler (Phase 3 P3-T2).
    block_auto_merge: bool = False
    # Person-guard defense-in-depth: whether the pair involves a person and whether
    # the names are related. find_duplicate_pairs already DROPS unrelated-name
    # person pairs, so a surviving person candidate is always name-related; the
    # auto-merge gate re-checks these (a no-op for current behavior) so a future
    # refactor or a person-detection miss can't silently merge two distinct people.
    is_person_pair: bool = False
    names_related: bool = True
    # Person pair whose names differ by one in-token edit (see _names_near_typo):
    # kept as a REVIEW candidate (reason name_typo), never auto-merged — the
    # gate above already refuses it via names_related=False; this is the label.
    name_typo: bool = False
    # The two sides claim DISJOINT types (a place and an organization). Such a
    # pair only reaches here with related names, which is the mis-TYPED-duplicate
    # shape; it is a review candidate, never an auto-merge (see the type-guard in
    # find_duplicate_pairs).
    cross_type: bool = False
    # The names only become token-related once CamelCase is split apart (see
    # _names_related_after_split). Review candidate only: `names_related` stays
    # False so the auto-merge gate refuses it, and block_auto_merge says so.
    name_tokenization: bool = False
    # Both sides actually state a primary type. `_types_compatible` is lenient by
    # design — an absent type is no evidence of a mismatch — but that leniency
    # belongs to the DROP decision, not to the decision to merge two rows
    # silently. An auto-merge requires positive evidence; a review proposal does
    # not. (`entity_type` is NOT NULL, so this is corruption-shaped, not a normal
    # path — which is exactly why it must not be the thing that widens the gate.)
    types_known: bool = True


@dataclass
class ClusterResolution:
    """Outcome of one owner decision over a whole name cluster."""
    merged: int = 0                 # entities folded into the survivor
    approved: int = 0               # pending pairs closed as approved
    rejected: int = 0               # pending pairs closed as rejected
    skipped_cross_tier: int = 0     # left individually decidable (visibility)
    skipped_cross_type: int = 0     # left individually decidable (disjoint types)
    skipped_unreachable: int = 0    # same tier + type, but not in the survivor's component
    notes: list[str] = field(default_factory=list)


@dataclass
class ReconcileReport:
    user_id: int
    candidates: int = 0
    # Pairs the find-time guards ATE. `candidates` counts what survived them, so
    # without these two a guard that is too greedy on some graph is invisible:
    # no row, no proposal, no log line. Both guards drop silently by design —
    # these make the silence measurable.
    dropped_person_guard: int = 0
    dropped_cross_type: int = 0
    auto_merged: int = 0
    proposed: int = 0
    embedded_backfilled: int = 0
    notes: list[str] = field(default_factory=list)


class KgReconcilerService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_active_user_ids(self) -> list[int]:
        rows = (await self.db.execute(text(
            "SELECT DISTINCT user_id FROM kg_entities "
            "WHERE is_active = true AND canonical_id IS NULL AND user_id IS NOT NULL"
        ))).fetchall()
        return [int(r[0]) for r in rows]

    async def find_duplicate_pairs(
        self, user_id: int, report: ReconcileReport | None = None,
    ) -> list[MergeCandidate]:
        """Embedding self-join over the user's live canonical entities.

        sqlite has no halfvec — short-circuits to [] there so the rest of the
        pipeline can still be exercised on the shim.

        ``report``, when given, collects how many pairs each find-time guard
        dropped (they drop silently by design; the counters make that visible).
        """
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return []

        dim = EMBEDDING_DIMENSION
        cap = max(settings.kg_reconciler_max_per_run * 2, 2)
        sql = text(f"""
            SELECT a.id AS id_a, b.id AS id_b,
                   a.circle_tier AS tier_a, b.circle_tier AS tier_b,
                   a.mention_count AS mc_a, b.mention_count AS mc_b,
                   a.first_seen_at AS fs_a, b.first_seen_at AS fs_b,
                   a.name AS name_a, b.name AS name_b,
                   a.description AS desc_a, b.description AS desc_b,
                   a.entity_type AS etype_a, b.entity_type AS etype_b,
                   a.entity_types::text AS etypes_a, b.entity_types::text AS etypes_b,
                   1 - (a.embedding::halfvec({dim}) <=> b.embedding::halfvec({dim})) AS similarity
            FROM kg_entities a
            JOIN kg_entities b
              ON a.id < b.id
             AND a.user_id = b.user_id
             AND a.embedding IS NOT NULL
             AND b.embedding IS NOT NULL
            WHERE a.user_id = :uid
              AND a.is_active = true AND b.is_active = true
              AND a.canonical_id IS NULL AND b.canonical_id IS NULL
              -- Notes (Phase 4B) resolve by EXACT title only (resolve stores an
              -- embedding but never matches on it); two distinct-titled notes must
              -- never auto-merge/propose. Exclude note-typed entities from candidacy.
              AND a.entity_type <> 'note' AND b.entity_type <> 'note'
              AND (1 - (a.embedding::halfvec({dim}) <=> b.embedding::halfvec({dim}))) >= :cand
              -- A pair with an open proposal is not re-proposed; neither is a pair
              -- the owner already REJECTED — a rejection is a verdict, re-asking it
              -- every run is queue noise (name_typo pairs are "maybe two people"
              -- by definition, so their rejection rate is structurally high).
              AND NOT EXISTS (
                  SELECT 1 FROM kg_merge_proposals p
                  WHERE p.status IN (:pending, :rejected)
                    AND ((p.loser_entity_id = a.id AND p.winner_entity_id = b.id)
                      OR (p.loser_entity_id = b.id AND p.winner_entity_id = a.id)))
            ORDER BY similarity DESC
            LIMIT :cap
        """)
        rows = (await self.db.execute(sql, {
            "uid": user_id,
            "cand": settings.kg_reconciler_candidate_threshold,
            "pending": KG_MERGE_PROPOSAL_PENDING,
            "rejected": KG_MERGE_PROPOSAL_REJECTED,
            "cap": cap,
        })).fetchall()

        out: list[MergeCandidate] = []
        for r in rows:
            is_person = (_is_person(r.etype_a, r.etypes_a)
                         or _is_person(r.etype_b, r.etypes_b))
            related = _names_related(r.name_a, r.name_b)
            # Person-guard: drop person-involving pairs whose names are unrelated
            # (distinct people whose names merely cluster in embedding space). No
            # auto-merge, no proposal. The one exception is a TYPO pair (one
            # in-token edit, see _names_near_typo): it survives as a review
            # proposal only — names_related stays False, so the auto-merge gate
            # refuses it, and block_auto_merge says so explicitly.
            typo = bool(is_person and not related and _names_near_typo(r.name_a, r.name_b))
            # The second gap `_names_related` leaves: a name written as one word.
            # It has to be answered HERE, at the person guard — the mis-typed
            # roles this rescues ("ProductOwner" carried as `person`) die on this
            # line, long before the type guard below could look at them.
            tok = bool(not related and not typo
                       and _names_related_after_split(r.name_a, r.name_b))
            if is_person and not related and not typo and not tok:
                if report is not None:
                    report.dropped_person_guard += 1
                continue
            # TYPE-GUARD: two different PRIMARY types is a different KIND of
            # thing, and embedding similarity cannot say so — a town and the
            # company seated in it are described out of the same documents, so
            # they embed well above the candidate threshold (measured on the
            # xidra graph 2026-09-24: place "Korschenbroich" ~ organization
            # "X-Idra Systems GmbH" at 0.895, four such pairs pending). Drop the
            # pair unless the NAMES are related, which is the one shape where a
            # foreign type means a MIS-TYPED duplicate rather than two different
            # things (field data: person "Pontresina" -> place "Pontresina").
            # Those survive as REVIEW candidates only: which type is right is a
            # human call, so no auto-merge.
            cross_type = not _types_compatible(r.etype_a, r.etype_b)
            # `not typo` matters: a typo pair is `related=False` by construction,
            # so without it this line would quietly cancel the #876 exception the
            # person-guard above just granted (a person mis-extracted as another
            # type, one in-token edit apart, is exactly the case worth reviewing).
            if cross_type and not related and not typo and not tok:
                if report is not None:
                    report.dropped_cross_type += 1
                continue
            # Winner = the more-established row: higher mention_count, tie-break
            # on the OLDER first_seen_at (smaller timestamp).
            a_key = (int(r.mc_a or 1), -(r.fs_a.timestamp() if r.fs_a else 0.0))
            b_key = (int(r.mc_b or 1), -(r.fs_b.timestamp() if r.fs_b else 0.0))
            if a_key >= b_key:
                winner_id, winner_tier = int(r.id_a), int(r.tier_a or 0)
                loser_id, loser_tier = int(r.id_b), int(r.tier_b or 0)
            else:
                winner_id, winner_tier = int(r.id_b), int(r.tier_b or 0)
                loser_id, loser_tier = int(r.id_a), int(r.tier_a or 0)
            out.append(MergeCandidate(
                loser_id=loser_id, winner_id=winner_id,
                similarity=float(r.similarity),
                loser_tier=loser_tier, winner_tier=winner_tier,
                block_auto_merge=typo or cross_type or tok or _name_collision_low_signal(
                    r.name_a, r.name_b, r.desc_a, r.desc_b,
                ),
                is_person_pair=is_person,
                names_related=related,
                name_typo=typo,
                cross_type=cross_type,
                name_tokenization=tok,
                types_known=bool(_norm(r.etype_a) and _norm(r.etype_b)),
            ))
        return out

    async def _propose(self, user_id: int, c: MergeCandidate) -> bool:
        """Create a PENDING proposal unless the pair is already open or was
        REJECTED by the owner (a verdict the reconciler does not re-litigate)."""
        existing = (await self.db.execute(
            select(KgMergeProposal.id).where(
                KgMergeProposal.status.in_(
                    [KG_MERGE_PROPOSAL_PENDING, KG_MERGE_PROPOSAL_REJECTED]
                ),
                KgMergeProposal.loser_entity_id.in_([c.loser_id, c.winner_id]),
                KgMergeProposal.winner_entity_id.in_([c.loser_id, c.winner_id]),
            )
        )).first()
        if existing:
            return False
        if c.loser_tier != c.winner_tier:
            reason = KG_MERGE_REASON_CROSS_TIER
        elif c.cross_type:
            reason = KG_MERGE_REASON_CROSS_TYPE
        elif c.name_typo:
            reason = KG_MERGE_REASON_NAME_TYPO
        elif c.name_tokenization:
            reason = KG_MERGE_REASON_NAME_TOKENIZATION
        else:
            reason = KG_MERGE_REASON_GRAY_ZONE
        self.db.add(KgMergeProposal(
            user_id=user_id,
            loser_entity_id=c.loser_id,
            winner_entity_id=c.winner_id,
            similarity=c.similarity,
            loser_tier=c.loser_tier,
            winner_tier=c.winner_tier,
            reason=reason,
        ))
        await self.db.flush()
        return True

    async def backfill_missing_embeddings(self, user_id: int) -> int:
        """Embed live entities that have no vector yet (#6).

        ``find_duplicate_pairs`` requires ``embedding IS NOT NULL`` on both
        sides, so an entity created before its embedding was computed (or whose
        embed call failed) is invisible to the self-join forever. Re-embed a
        bounded batch at the top of each pass so those entities become
        reconcilable. Best-effort: a failed embed leaves the row NULL for the
        next pass. Postgres-only (the sqlite shim has no vector column).
        """
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return 0
        cap = settings.kg_reconciler_embed_backfill_per_run
        if cap <= 0:
            return 0
        rows = (await self.db.execute(
            select(KGEntity).where(
                KGEntity.user_id == user_id,
                KGEntity.is_active.is_(True),
                KGEntity.canonical_id.is_(None),
                KGEntity.embedding.is_(None),
            ).limit(cap)
        )).scalars().all()
        if not rows:
            return 0
        kg = KnowledgeGraphService(self.db)
        n = 0
        for ent in rows:
            try:
                emb = await kg._get_embedding(
                    KnowledgeGraphService._embed_input(ent.name, ent.description)
                )
                if emb:
                    ent.embedding = emb
                    n += 1
            except Exception as e:  # noqa: BLE001 — leave NULL, retry next pass
                logger.warning(
                    f"KG reconciler: embed backfill failed for #{ent.id} {ent.name!r}: {e}"
                )
        if n:
            await self.db.commit()
        return n

    async def run_for_user(self, user_id: int) -> ReconcileReport:
        """One reconciler pass for a user, serialized per-user (idempotent).

        Wrapped in a non-blocking per-user advisory lock (#4): two overlapping
        runs for the same user must not redo each other's work — the second
        caller finds the lock held and returns a no-op report. The lock lives on
        a DEDICATED connection (a fresh connection off the AsyncEngine, see
        ``_resolve_lock_engine``) because merge_entities commits mid-pass, which
        can return self.db's own connection to the pool; a session-level lock
        taken on self.db would not survive that.
        """
        report = ReconcileReport(user_id=user_id)
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return await self._reconcile_pass(user_id, report)

        lock_engine = _resolve_lock_engine(self.db.bind)
        if lock_engine is None:  # no async connectable — run unlocked (safe fallback)
            return await self._reconcile_pass(user_id, report)

        async with lock_engine.connect() as lock_conn:
            got = (await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                {"ns": _RECONCILER_LOCK_NS, "uid": user_id},
            )).scalar()
            if not got:
                report.notes.append(
                    "skipped: another reconciler run holds this user's lock"
                )
                return report
            try:
                return await self._reconcile_pass(user_id, report)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:ns, :uid)"),
                    {"ns": _RECONCILER_LOCK_NS, "uid": user_id},
                )

    async def _reconcile_pass(self, user_id: int, report: ReconcileReport) -> ReconcileReport:
        """The actual work of one pass: embed-backfill, find, auto-merge/propose."""
        report.embedded_backfilled = await self.backfill_missing_embeddings(user_id)
        pairs = await self.find_duplicate_pairs(user_id, report)
        report.candidates = len(pairs)

        auto_t = settings.kg_reconciler_auto_merge_threshold
        cap = settings.kg_reconciler_max_per_run
        touched: set[int] = set()
        for c in pairs[:cap]:
            if c.loser_id in touched or c.winner_id in touched:
                continue  # transitive-cluster guard
            try:
                # Person pairs may only auto-merge when names are related (defense
                # in depth behind the find-time drop): a distinct-name person pair
                # must never silently merge two different people.
                person_ok = (not c.is_person_pair) or c.names_related
                # A disjoint-type pair is review-only by construction
                # (block_auto_merge is already set); spelt out here so a future
                # change to that flag cannot silently fold a place into a company.
                if (c.loser_tier == c.winner_tier and c.similarity >= auto_t
                        and not c.block_auto_merge and not c.cross_type
                        and not c.name_tokenization
                        and c.types_known and person_ok):
                    kg = KnowledgeGraphService(self.db)
                    res = await kg.merge_entities(c.loser_id, c.winner_id)
                    if res is not None:
                        # track BOTH sides: a survivor must not be re-merged into
                        # a third node later in this batch on stale (pre-merge)
                        # pair data (transitive-cluster guard).
                        touched.add(c.loser_id)
                        touched.add(c.winner_id)
                        report.auto_merged += 1
                elif await self._propose(user_id, c):
                    touched.add(c.loser_id)
                    touched.add(c.winner_id)
                    report.proposed += 1
            except Exception as e:  # noqa: BLE001
                report.notes.append(
                    f"reconcile failed loser={c.loser_id} winner={c.winner_id}: {e}"
                )

        await self.db.commit()
        if (report.auto_merged or report.proposed or report.embedded_backfilled
                or report.dropped_person_guard or report.dropped_cross_type):
            logger.info(
                f"🔗 KG reconciler user={user_id}: auto_merged={report.auto_merged}, "
                f"proposed={report.proposed}, candidates={report.candidates}, "
                f"embedded_backfilled={report.embedded_backfilled}, "
                f"dropped_person_guard={report.dropped_person_guard}, "
                f"dropped_cross_type={report.dropped_cross_type}"
            )
        return report

    async def approve_proposal(
        self,
        proposal_id: int,
        resolved_by: int | None = None,
        winner_id: int | None = None,
    ) -> KGEntity | None:
        """Apply a pending proposal: merge loser -> winner, mark approved.

        ``winner_id`` lets the owner override which side survives (D2 survivor
        toggle): pass the entity id to keep. It must be one of the proposal's two
        entities; the other becomes the loser. Defaults to the stored winner.

        Returns the surviving entity, or None if the proposal is missing/already
        resolved or the merge was a no-op.
        """
        p = (await self.db.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == proposal_id)
        )).scalar_one_or_none()
        if p is None or p.status != KG_MERGE_PROPOSAL_PENDING:
            return None
        pair = {p.loser_entity_id, p.winner_entity_id}
        if winner_id is not None and winner_id in pair:
            keep = winner_id
        else:
            keep = p.winner_entity_id  # default / invalid override -> stored winner
        drop = (pair - {keep}).pop()
        survivor = await KnowledgeGraphService(self.db).merge_entities(drop, keep)
        # merge_entities commits; re-load the proposal in the fresh txn to mark it.
        p = (await self.db.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == proposal_id)
        )).scalar_one_or_none()
        # Only this caller may resolve a still-PENDING proposal; if a concurrent
        # approve already resolved it, leave its verdict intact (#3).
        if p is not None and p.status == KG_MERGE_PROPOSAL_PENDING:
            from models.database import (
                KG_MERGE_PROPOSAL_APPROVED,
                KG_MERGE_PROPOSAL_SUPERSEDED,
            )
            # survivor is None => one side was already merged/tombstoned by an
            # overlapping approve; the merge was a no-op. Close as superseded
            # rather than a misleading "approved" (owner sees nothing changed).
            p.status = (
                KG_MERGE_PROPOSAL_APPROVED if survivor is not None
                else KG_MERGE_PROPOSAL_SUPERSEDED
            )
            p.resolved_at = datetime.now(UTC).replace(tzinfo=None)
            p.resolved_by_user_id = resolved_by
            await self.db.commit()
        return survivor

    async def resolve_cluster(
        self,
        *,
        user_id: int | None,
        entity_ids: list[int],
        survivor_id: int | None,
        decision: str,
        resolved_by: int | None = None,
    ) -> ClusterResolution:
        """Resolve a whole NAME CLUSTER in one owner decision.

        The review queue is dominated by clusters of same-named entities with no
        description to tell them apart (measured 2026-09-24: 1 365 pairs over 952
        entities in 199 name clusters). Deciding those pair by pair is the wrong
        unit — the owner judges "these are all the same thing" once.

        INVARIANT: only SAME-TIER pairs take part. A cross-tier pair changes an
        atom's reach, which is the one thing a bulk action must never do silently
        (D3) — those stay individually decidable and are counted in
        ``skipped_cross_tier``. Because every folded pair is same-tier, the
        ``tier = MIN`` rule inside ``merge_entities`` is a no-op here: no
        visibility can shift.

        SECOND INVARIANT: only TYPE-COMPATIBLE pairs take part. Folding a place
        into an organization rewrites what the entity IS. Those are counted in
        ``skipped_cross_type`` and likewise stay individually decidable. Note
        where this bites: the review UI builds its components on primary-type
        EQUALITY, which is transitive, so a cluster it submits can never contain
        such a pair. This bar is for the ROUTE — ``entity_ids`` is caller-supplied
        and need not come from a cluster card at all.

        The fold set is derived from the PROPOSALS, not from ``entity_ids``: an
        entity the caller names but that no pending same-tier proposal ties into
        the cluster is never merged. Otherwise this route would be a way to merge
        two arbitrary entities without a proposal behind it.
        """
        res = ClusterResolution()
        if decision not in ("merge", "reject"):
            res.notes.append(f"unknown decision: {decision}")
            return res
        ids = {int(i) for i in entity_ids}
        if len(ids) < 2:
            res.notes.append("a cluster needs at least two entities")
            return res

        q = (
            select(KgMergeProposal)
            .options(
                selectinload(KgMergeProposal.loser),
                selectinload(KgMergeProposal.winner),
            )
            .where(
                KgMergeProposal.status == KG_MERGE_PROPOSAL_PENDING,
                KgMergeProposal.loser_entity_id.in_(ids),
                KgMergeProposal.winner_entity_id.in_(ids),
            )
        )
        if user_id is not None:
            q = q.where(KgMergeProposal.user_id == user_id)
        pairs = list((await self.db.execute(q)).scalars().all())

        foldable: list[KgMergeProposal] = []
        for p in pairs:
            # Compare the LIVE tiers, not the ones stored when the pair was
            # proposed — a tier may have moved since, in either direction.
            lt = (p.loser.circle_tier if p.loser else None) or 0
            wt = (p.winner.circle_tier if p.winner else None) or 0
            if lt != wt:
                res.skipped_cross_tier += 1
                continue
            # A disjoint-type pair is not bulk-decidable either: folding a place
            # into an organization rewrites what the entity IS, and one such edge
            # inside a component would drag the whole component across the type
            # boundary. It stays an individual decision, exactly like cross-tier.
            if not _types_compatible(
                p.loser.entity_type if p.loser else None,
                p.winner.entity_type if p.winner else None,
            ):
                res.skipped_cross_type += 1
                continue
            foldable.append(p)

        if not foldable:
            res.notes.append("no foldable pending pair in this cluster")
            return res

        now = datetime.now(UTC).replace(tzinfo=None)

        if decision == "reject":
            for p in foldable:
                p.status = KG_MERGE_PROPOSAL_REJECTED
                p.resolved_at = now
                p.resolved_by_user_id = resolved_by
                res.rejected += 1
            await self.db.commit()
            return res

        if survivor_id is None:
            res.notes.append("merge needs a survivor")
            return res
        keep = int(survivor_id)

        # The fold set is the CONNECTED COMPONENT of the survivor, not the union
        # of every same-tier pair in the request. Two disjoint components can
        # each be same-tier internally and still sit at DIFFERENT tiers — folding
        # both into one survivor would apply `tier = MIN` across them and quietly
        # narrow an atom's reach, the exact thing this method promises never to
        # do. So: walk out from the survivor, and take only what is reachable.
        adjacency: dict[int, set[int]] = {}
        by_edge: dict[tuple[int, int], list[KgMergeProposal]] = {}
        for p in foldable:
            a, b = int(p.loser_entity_id), int(p.winner_entity_id)
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
            by_edge.setdefault((min(a, b), max(a, b)), []).append(p)
        if keep not in adjacency:
            res.notes.append("survivor must be one of the cluster's proposed entities")
            return res
        component: set[int] = set()
        frontier = [keep]
        while frontier:
            node = frontier.pop()
            if node in component:
                continue
            component.add(node)
            frontier.extend(adjacency.get(node, ()))

        # Belt and braces: every member must sit at the survivor's tier. The
        # edges say so pairwise; this says so for the whole component, so a
        # future change to the pair filter cannot reopen the hole above.
        tiers = {
            (p.loser.circle_tier or 0)
            for p in foldable
            if int(p.loser_entity_id) in component
        } | {
            (p.winner.circle_tier or 0)
            for p in foldable
            if int(p.winner_entity_id) in component
        }
        if len(tiers) > 1:
            res.notes.append("cluster spans more than one tier — refusing")
            return res

        in_component = [
            p for p in foldable
            if int(p.loser_entity_id) in component and int(p.winner_entity_id) in component
        ]
        # Ids BEFORE the folds: merge_entities rolls back on its bail paths, and a
        # rollback expires every persistent object in the session — touching
        # `p.id` afterwards would lazy-load on an AsyncSession and raise.
        pair_ids = [int(p.id) for p in in_component]
        # NOT a visibility skip: these pairs cleared both filters and were left
        # out only because they do not reach the survivor. Reporting them as
        # `skipped_cross_tier` told the owner "different visibility", which is
        # simply false — they have the survivor's tier by construction.
        res.skipped_unreachable += len(foldable) - len(in_component)

        kg = KnowledgeGraphService(self.db)
        folded: set[int] = set()
        for drop in sorted(component - {keep}):
            # merge_entities commits per fold and is idempotent on an
            # already-tombstoned loser (returns None).
            if await kg.merge_entities(drop, keep) is not None:
                res.merged += 1
                folded.add(drop)

        # Close the component's pairs. Every one of them said "these two are the
        # same"; after the fold that statement holds for all of them, so the
        # verdict is `approved` — `superseded` stays reserved for the concurrent
        # race in approve_proposal. `populate_existing` because the session keeps
        # objects alive across commits (expire_on_commit=False): without it this
        # would read the stale in-memory status and overwrite a verdict another
        # request reached in the meantime.
        reload_q = (
            select(KgMergeProposal)
            .where(KgMergeProposal.id.in_(pair_ids))
            .execution_options(populate_existing=True)
        )
        for p in (await self.db.execute(reload_q)).scalars().all():
            if p.status != KG_MERGE_PROPOSAL_PENDING:
                continue
            p.status = KG_MERGE_PROPOSAL_APPROVED
            p.resolved_at = now
            p.resolved_by_user_id = resolved_by
            res.approved += 1
        await self._repoint_after_fold(
            user_id=user_id, folded=folded, survivor_id=keep, now=now,
        )
        await self.db.commit()
        logger.info(
            f"🔗 KG cluster resolved user={user_id} survivor={keep}: "
            f"merged={res.merged}, approved={res.approved}, "
            f"cross_tier_left={res.skipped_cross_tier}"
        )
        return res

    async def _repoint_after_fold(
        self,
        *,
        user_id: int | None,
        folded: set[int],
        survivor_id: int,
        now: datetime,
    ) -> None:
        """Keep the promise that a cross-tier pair stays individually decidable.

        A fold tombstones entities. A still-pending proposal pointing at one of
        them — typically the CROSS-TIER pair the bulk action deliberately left
        alone — would become un-exercisable: approving it merges into a
        tombstone, which is a no-op, so the owner's "does this belong at that
        tier" decision could never be taken. Re-point such a proposal at the
        survivor instead, so it stays a real question about a real pair.

        When the survivor already has a pending proposal with that partner, the
        re-pointed one would be a duplicate: close it as superseded.
        """
        if not folded:
            return
        q = select(KgMergeProposal).where(
            KgMergeProposal.status == KG_MERGE_PROPOSAL_PENDING,
        ).execution_options(populate_existing=True)
        if user_id is not None:
            q = q.where(KgMergeProposal.user_id == user_id)
        open_pairs = list((await self.db.execute(q)).scalars().all())

        def edge(p: KgMergeProposal) -> tuple[int, int]:
            a, b = int(p.loser_entity_id), int(p.winner_entity_id)
            return (min(a, b), max(a, b))

        existing = {edge(p) for p in open_pairs}
        for p in open_pairs:
            lid, wid = int(p.loser_entity_id), int(p.winner_entity_id)
            if lid not in folded and wid not in folded:
                continue
            partner = wid if lid in folded else lid
            if partner in folded or partner == survivor_id:
                # Both sides went into the survivor — the pair has no question
                # left to ask.
                p.status = KG_MERGE_PROPOSAL_SUPERSEDED
                p.resolved_at = now
                continue
            new_edge = (min(partner, survivor_id), max(partner, survivor_id))
            if new_edge in existing:
                p.status = KG_MERGE_PROPOSAL_SUPERSEDED
                p.resolved_at = now
                continue
            existing.add(new_edge)
            # Keep loser/winner orientation meaningful: the survivor is the
            # established side by construction.
            p.loser_entity_id = partner
            p.winner_entity_id = survivor_id

    async def reject_proposal(self, proposal_id: int, resolved_by: int | None = None) -> bool:
        p = (await self.db.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == proposal_id)
        )).scalar_one_or_none()
        if p is None or p.status != KG_MERGE_PROPOSAL_PENDING:
            return False
        from models.database import KG_MERGE_PROPOSAL_REJECTED
        p.status = KG_MERGE_PROPOSAL_REJECTED
        p.resolved_at = datetime.now(UTC).replace(tzinfo=None)
        p.resolved_by_user_id = resolved_by
        await self.db.commit()
        return True
