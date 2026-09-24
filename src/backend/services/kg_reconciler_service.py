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
    KG_MERGE_REASON_CROSS_TIER,
    KG_MERGE_REASON_GRAY_ZONE,
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


@dataclass
class ClusterResolution:
    """Outcome of one owner decision over a whole name cluster."""
    merged: int = 0                 # entities folded into the survivor
    approved: int = 0               # pending pairs closed as approved
    rejected: int = 0               # pending pairs closed as rejected
    skipped_cross_tier: int = 0     # left individually decidable (visibility)
    notes: list[str] = field(default_factory=list)


@dataclass
class ReconcileReport:
    user_id: int
    candidates: int = 0
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

    async def find_duplicate_pairs(self, user_id: int) -> list[MergeCandidate]:
        """Embedding self-join over the user's live canonical entities.

        sqlite has no halfvec — short-circuits to [] there so the rest of the
        pipeline can still be exercised on the shim.
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
            if is_person and not related and not typo:
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
                block_auto_merge=typo or _name_collision_low_signal(
                    r.name_a, r.name_b, r.desc_a, r.desc_b,
                ),
                is_person_pair=is_person,
                names_related=related,
                name_typo=typo,
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
        elif c.name_typo:
            reason = KG_MERGE_REASON_NAME_TYPO
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
        pairs = await self.find_duplicate_pairs(user_id)
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
                if (c.loser_tier == c.winner_tier and c.similarity >= auto_t
                        and not c.block_auto_merge and person_ok):
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
        if report.auto_merged or report.proposed or report.embedded_backfilled:
            logger.info(
                f"🔗 KG reconciler user={user_id}: auto_merged={report.auto_merged}, "
                f"proposed={report.proposed}, candidates={report.candidates}, "
                f"embedded_backfilled={report.embedded_backfilled}"
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

        same_tier: list[KgMergeProposal] = []
        for p in pairs:
            # Compare the LIVE tiers, not the ones stored when the pair was
            # proposed — a tier may have moved since, in either direction.
            lt = (p.loser.circle_tier if p.loser else None) or 0
            wt = (p.winner.circle_tier if p.winner else None) or 0
            if lt == wt:
                same_tier.append(p)
            else:
                res.skipped_cross_tier += 1

        if not same_tier:
            res.notes.append("no same-tier pending pair in this cluster")
            return res

        now = datetime.now(UTC).replace(tzinfo=None)

        if decision == "reject":
            for p in same_tier:
                p.status = KG_MERGE_PROPOSAL_REJECTED
                p.resolved_at = now
                p.resolved_by_user_id = resolved_by
                res.rejected += 1
            await self.db.commit()
            return res

        connected: set[int] = set()
        for p in same_tier:
            connected.add(int(p.loser_entity_id))
            connected.add(int(p.winner_entity_id))
        if survivor_id is None or int(survivor_id) not in connected:
            res.notes.append("survivor must be one of the cluster's proposed entities")
            return res
        keep = int(survivor_id)

        kg = KnowledgeGraphService(self.db)
        for drop in sorted(connected - {keep}):
            # merge_entities commits per fold and is idempotent on an
            # already-tombstoned loser (returns None).
            if await kg.merge_entities(drop, keep) is not None:
                res.merged += 1

        # Close the cluster's pairs. Every one of them said "these two are the
        # same"; after the fold that statement holds for all of them, so the
        # verdict is `approved` — `superseded` stays reserved for the concurrent
        # race in approve_proposal.
        reload_q = select(KgMergeProposal).where(
            KgMergeProposal.id.in_([p.id for p in same_tier])
        )
        for p in (await self.db.execute(reload_q)).scalars().all():
            if p.status != KG_MERGE_PROPOSAL_PENDING:
                continue
            p.status = KG_MERGE_PROPOSAL_APPROVED
            p.resolved_at = now
            p.resolved_by_user_id = resolved_by
            res.approved += 1
        await self.db.commit()
        logger.info(
            f"🔗 KG cluster resolved user={user_id} survivor={keep}: "
            f"merged={res.merged}, approved={res.approved}, "
            f"cross_tier_left={res.skipped_cross_tier}"
        )
        return res

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
