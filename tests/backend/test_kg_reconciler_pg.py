"""Postgres-only tests for KgReconcilerService (Structured Memory Phase 1, T5).

Real PG via ``pg_db_session`` (halfvec self-join + the proposals table). The
reconciler and merge_entities commit internally; under the rollback-isolated
fixture we patch commit/rollback -> flush. KnowledgeGraphService._get_embedding
is class-patched (auto-merge recomputes the survivor embedding).
"""
from __future__ import annotations

import math
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    EMBEDDING_DIMENSION,
    KG_MERGE_PROPOSAL_APPROVED,
    KG_MERGE_PROPOSAL_PENDING,
    KG_MERGE_PROPOSAL_REJECTED,
    KG_MERGE_PROPOSAL_SUPERSEDED,
    KG_MERGE_REASON_CROSS_TIER,
    KG_MERGE_REASON_CROSS_TYPE,
    KG_MERGE_REASON_NAME_TOKENIZATION,
    KGEntity,
    KgMergeProposal,
    Role,
    User,
)
from services.knowledge_graph_service import KnowledgeGraphService
from services.kg_reconciler_service import _RECONCILER_LOCK_NS, KgReconcilerService

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _unit(i: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIMENSION
    v[i % EMBEDDING_DIMENSION] = 1.0
    return v


def _gray() -> list[float]:
    # cosine ~0.9 vs _unit(0): in (candidate 0.85, auto 0.95) -> gray zone
    v = [0.0] * EMBEDDING_DIMENSION
    v[0] = 0.9
    v[1] = math.sqrt(1 - 0.81)
    return v


async def _make_user(db: AsyncSession, name: str) -> User:
    role = Role(name=f"{name}_role")
    db.add(role)
    await db.flush()
    u = User(username=name, email=f"{name}@ex.test", password_hash="x",
             role_id=role.id, is_active=True)
    db.add(u)
    await db.flush()
    return u


async def _entity(db, owner, name, *, tier=0, mention=1, emb=None, etype="person", desc=None) -> KGEntity:
    e = KGEntity(user_id=owner.id, name=name, entity_type=etype, circle_tier=tier,
                 mention_count=mention, embedding=emb, description=desc)
    db.add(e)
    await db.flush()
    return e


def _recon(db, monkeypatch) -> KgReconcilerService:
    monkeypatch.setattr(db, "commit", db.flush)
    monkeypatch.setattr(db, "rollback", db.flush)
    monkeypatch.setattr(KnowledgeGraphService, "_get_embedding",
                        AsyncMock(return_value=_unit(3)))
    return KgReconcilerService(db)


async def _count_pending(db, uid) -> int:
    return (await db.execute(text(
        "SELECT count(*) FROM kg_merge_proposals WHERE user_id = :u AND status = 'pending'"
    ), {"u": uid})).scalar_one()


class TestFindPairs:
    async def test_finds_similar_pair_and_picks_winner(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rec_find")
        # identical embeddings -> cosine ~1.0; b has more mentions -> winner
        await _entity(pg_db_session, owner, "Alice", mention=2, emb=_unit(5))
        big = await _entity(pg_db_session, owner, "Alice B.", mention=9, emb=_unit(5))
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert len(pairs) == 1
        assert pairs[0].winner_id == big.id  # more mentions wins


class TestRunForUser:
    async def test_same_tier_high_sim_auto_merges(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rec_auto")
        a = await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Alice B.", tier=0, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 1
        assert report.proposed == 0
        # loser tombstoned
        loser = a if b.mention_count >= a.mention_count else b
        tomb = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == loser.id)
        )).scalar_one()
        assert tomb.is_active is False and tomb.canonical_id is not None

    async def test_same_name_low_signal_proposes_not_merges(self, pg_db_session, monkeypatch):
        # P3-T2: two same-tier entities sharing a name with empty descriptions are
        # high-similarity but indistinguishable (could be two different people).
        # They must NOT auto-merge — route to review — else the memory bridge's
        # backfill would feed the Jutta/Anna conflation back through the reconciler.
        owner = await _make_user(pg_db_session, "rec_namecol")
        await _entity(pg_db_session, owner, "Anna", tier=0, mention=2, emb=_unit(6))
        await _entity(pg_db_session, owner, "Anna", tier=0, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 0   # blocked
        assert report.proposed == 1      # routed to owner review instead
        assert await _count_pending(pg_db_session, owner.id) == 1

    async def test_same_name_distinct_descriptions_still_auto_merges(self, pg_db_session, monkeypatch):
        # Control: same name BUT both sides carry distinct non-empty descriptions,
        # so the embedding similarity is meaningful — auto-merge stays allowed.
        owner = await _make_user(pg_db_session, "rec_namedesc")
        await _entity(pg_db_session, owner, "Anna", tier=0, mention=2, emb=_unit(6),
                      desc="meine Mutter, wohnt in Bonn")
        await _entity(pg_db_session, owner, "Anna", tier=0, mention=9, emb=_unit(6),
                      desc="Kollegin im Büro")
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 1
        assert report.proposed == 0

    async def test_cross_tier_proposes_not_merges(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rec_cross")
        a = await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Alice B.", tier=2, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 0
        assert report.proposed == 1
        assert await _count_pending(pg_db_session, owner.id) == 1
        # nothing merged — both still active
        for e in (a, b):
            row = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == e.id)
            )).scalar_one()
            assert row.is_active is True

    async def test_gray_zone_same_tier_proposes(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rec_gray")
        await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=_unit(0))
        await _entity(pg_db_session, owner, "Alice B.", tier=0, mention=9, emb=_gray())
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        # ~0.9 similarity: candidate but below auto (0.95) -> proposal, no merge
        assert report.auto_merged == 0
        assert report.proposed == 1

    async def test_backfill_embeds_null_entities_then_reconciles(self, pg_db_session, monkeypatch):
        # #6: entities born without an embedding are invisible to the self-join;
        # the pass backfills them first, so they become reconcilable same run.
        owner = await _make_user(pg_db_session, "rec_backfill")
        a = await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=None)
        b = await _entity(pg_db_session, owner, "Alice B.", tier=0, mention=9, emb=None)
        rec = _recon(pg_db_session, monkeypatch)  # _get_embedding -> _unit(3) for both

        # before backfill: NULL embeddings -> no candidate pairs at all
        assert await rec.find_duplicate_pairs(owner.id) == []

        report = await rec.run_for_user(owner.id)
        assert report.embedded_backfilled == 2
        # identical backfilled embedding -> same-tier high-sim pair -> auto-merge
        assert report.auto_merged == 1
        for e in (a, b):
            row = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == e.id)
            )).scalar_one()
            assert row.embedding is not None

    async def test_concurrent_run_skips_when_locked(self, pg_db_session, pg_async_engine, monkeypatch):
        # #4: a second overlapping run for the same user finds the per-user
        # advisory lock held and returns a no-op report instead of redoing work.
        owner = await _make_user(pg_db_session, "rec_lock")
        await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=_unit(6))
        await _entity(pg_db_session, owner, "Alice B.", tier=0, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        async with pg_async_engine.connect() as holder:
            got = (await holder.execute(
                text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                {"ns": _RECONCILER_LOCK_NS, "uid": owner.id},
            )).scalar()
            assert got is True
            try:
                report = await rec.run_for_user(owner.id)
                # lock held -> skipped despite an obvious same-tier dup pair
                assert report.candidates == 0
                assert report.auto_merged == 0
                assert any("skipped" in n for n in report.notes)
            finally:
                await holder.execute(
                    text("SELECT pg_advisory_unlock(:ns, :uid)"),
                    {"ns": _RECONCILER_LOCK_NS, "uid": owner.id},
                )

    async def test_idempotent_second_run_no_new_proposals(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rec_idem")
        await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=_unit(6))
        await _entity(pg_db_session, owner, "Alice B.", tier=2, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        r1 = await rec.run_for_user(owner.id)
        r2 = await rec.run_for_user(owner.id)
        assert r1.proposed == 1
        assert r2.proposed == 0  # pending proposal excludes the pair
        assert await _count_pending(pg_db_session, owner.id) == 1


class TestApproveReject:
    async def test_approve_merges_and_marks(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rec_appr")
        a = await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Alice B.", tier=2, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)
        await rec.run_for_user(owner.id)
        pid = (await pg_db_session.execute(
            select(KgMergeProposal.id).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()

        survivor = await rec.approve_proposal(pid, resolved_by=owner.id)
        assert survivor is not None and survivor.id == b.id
        prop = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == pid)
        )).scalar_one()
        assert prop.status == KG_MERGE_PROPOSAL_APPROVED
        loser = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == a.id)
        )).scalar_one()
        assert loser.is_active is False

    async def test_reject_marks_no_merge(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "rec_rej")
        a = await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=_unit(6))
        await _entity(pg_db_session, owner, "Alice B.", tier=2, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)
        await rec.run_for_user(owner.id)
        pid = (await pg_db_session.execute(
            select(KgMergeProposal.id).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()

        assert await rec.reject_proposal(pid, resolved_by=owner.id) is True
        prop = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == pid)
        )).scalar_one()
        assert prop.status == KG_MERGE_PROPOSAL_REJECTED
        loser = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == a.id)
        )).scalar_one()
        assert loser.is_active is True  # rejection does not merge

    async def test_overlapping_approve_marks_superseded(self, pg_db_session, monkeypatch):
        # #3: two pending proposals share entity b (b->a and c->b). Approving
        # the first tombstones b; approving the second is a no-op merge, so it
        # closes as SUPERSEDED rather than a misleading APPROVED.
        owner = await _make_user(pg_db_session, "rec_super")
        a = await _entity(pg_db_session, owner, "Alice", tier=0, mention=9, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Alice B.", tier=2, mention=2, emb=_unit(6))
        c = await _entity(pg_db_session, owner, "Alice C.", tier=2, mention=5, emb=_unit(6))
        p1 = KgMergeProposal(
            user_id=owner.id, loser_entity_id=b.id, winner_entity_id=a.id,
            similarity=0.9, loser_tier=2, winner_tier=0, reason=KG_MERGE_REASON_CROSS_TIER,
        )
        p2 = KgMergeProposal(
            user_id=owner.id, loser_entity_id=c.id, winner_entity_id=b.id,
            similarity=0.9, loser_tier=2, winner_tier=2, reason=KG_MERGE_REASON_CROSS_TIER,
        )
        pg_db_session.add_all([p1, p2])
        await pg_db_session.flush()
        rec = _recon(pg_db_session, monkeypatch)

        s1 = await rec.approve_proposal(p1.id, resolved_by=owner.id)
        assert s1 is not None and s1.id == a.id  # b merged into a

        s2 = await rec.approve_proposal(p2.id, resolved_by=owner.id)
        assert s2 is None  # winner b already tombstoned -> no-op

        prop2 = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == p2.id)
        )).scalar_one()
        assert prop2.status == KG_MERGE_PROPOSAL_SUPERSEDED
        # c was never touched by the no-op merge
        c_row = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == c.id)
        )).scalar_one()
        assert c_row.is_active is True and c_row.canonical_id is None

    async def test_single_user_mode_routes_user_none(self, pg_db_session, monkeypatch):
        # AUTH_ENABLED=false → require_permission yields user=None. The routes
        # must not crash on user.id and the operator sees/acts on everything.
        from api.routes.knowledge_graph import (
            list_merge_proposals,
            reject_merge_proposal,
            run_reconciler,
        )
        owner = await _make_user(pg_db_session, "rt_single")
        a = await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Alice B.", tier=2, mention=9, emb=_unit(6))
        p = KgMergeProposal(
            user_id=owner.id, loser_entity_id=a.id, winner_entity_id=b.id,
            similarity=0.9, loser_tier=0, winner_tier=2, reason=KG_MERGE_REASON_CROSS_TIER,
        )
        pg_db_session.add(p)
        await pg_db_session.flush()
        _recon(pg_db_session, monkeypatch)  # patch commit/rollback + embedding

        # list: user=None sees the pending proposal (no AttributeError on user.id)
        listed = await list_merge_proposals(db=pg_db_session, user=None)
        assert listed.total == 1
        assert listed.proposals[0].winner.id == b.id

        # reject: user=None resolves the owner's own queue
        rejected = await reject_merge_proposal(p.id, db=pg_db_session, user=None)
        assert rejected["status"] == "rejected"
        assert (await list_merge_proposals(db=pg_db_session, user=None)).total == 0

        # run: user=None aggregates over all active users (here: just `owner`).
        # The REJECTED (a,b) pair stays out (a rejection is the owner's verdict
        # and is not re-litigated); a fresh cross-tier pair proves the
        # aggregation path executed.
        c = await _entity(pg_db_session, owner, "Carla", tier=0, mention=2, emb=_unit(5))
        d = await _entity(pg_db_session, owner, "Carla D.", tier=2, mention=9, emb=_unit(5))
        report = await run_reconciler(db=pg_db_session, user=None)
        assert report.candidates == 1 and report.proposed == 1
        pending = (await pg_db_session.execute(
            select(KgMergeProposal).where(
                KgMergeProposal.user_id == owner.id,
                KgMergeProposal.status == KG_MERGE_PROPOSAL_PENDING,
            )
        )).scalars().all()
        assert len(pending) == 1
        assert {pending[0].loser_entity_id, pending[0].winner_entity_id} == {c.id, d.id}
        await pg_db_session.refresh(p)
        assert p.status == KG_MERGE_PROPOSAL_REJECTED  # the verdict stands

    async def test_approve_override_winner(self, pg_db_session, monkeypatch):
        # D2 survivor toggle: owner keeps the LESS-mentioned entity instead of
        # the reconciler's default (more-mentioned) winner.
        owner = await _make_user(pg_db_session, "rec_swap")
        a = await _entity(pg_db_session, owner, "Alice", tier=0, mention=2, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Alice B.", tier=2, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)
        await rec.run_for_user(owner.id)
        pid = (await pg_db_session.execute(
            select(KgMergeProposal.id).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()

        # default winner is b (more mentions); override to keep a
        survivor = await rec.approve_proposal(pid, resolved_by=owner.id, winner_id=a.id)
        assert survivor is not None and survivor.id == a.id
        b_row = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == b.id)
        )).scalar_one()
        assert b_row.is_active is False  # b became the loser


class TestPersonGuard:
    async def test_distinct_name_persons_not_candidates_even_at_cosine_1(
        self, pg_db_session, monkeypatch
    ):
        # The vulnerability: two DIFFERENT people whose names cluster in embedding
        # space (here identical embeddings = cosine 1.0 >= auto threshold) must NOT
        # auto-merge and must NOT even be proposed — they're dropped as candidates.
        owner = await _make_user(pg_db_session, "rec_pg_distinct")
        await _entity(pg_db_session, owner, "Jutta", tier=0, mention=4, emb=_unit(6))
        await _entity(pg_db_session, owner, "Anna", tier=0, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert pairs == []  # unrelated person names -> not a candidate

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 0 and report.proposed == 0
        assert await _count_pending(pg_db_session, owner.id) == 0

    async def test_related_name_persons_still_reconcile(self, pg_db_session, monkeypatch):
        # "Jutta" ⊆ "Jutta van den Bongard" (token subset) -> a legitimate dedup
        # candidate; same-tier + cosine 1.0 -> auto-merge (the reconciler's purpose).
        owner = await _make_user(pg_db_session, "rec_pg_related")
        await _entity(pg_db_session, owner, "Jutta", tier=0, mention=2, emb=_unit(6))
        await _entity(pg_db_session, owner, "Jutta van den Bongard", tier=0, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 1

    async def test_distinct_name_nonpersons_unaffected(self, pg_db_session, monkeypatch):
        # The guard is person-scoped: two distinct-name organizations at cosine 1.0
        # same-tier still auto-merge (embedding IS meaningful for non-persons).
        owner = await _make_user(pg_db_session, "rec_pg_org")
        await _entity(pg_db_session, owner, "Acme GmbH", etype="organization",
                      tier=0, mention=2, emb=_unit(6))
        await _entity(pg_db_session, owner, "Globex AG", etype="organization",
                      tier=0, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 1

    async def test_multitype_person_distinct_names_excluded(self, pg_db_session, monkeypatch):
        # Primary 'organization' but person in the multi-type set -> person-guard
        # applies (mirrors resolve's seed_types gate); distinct names -> excluded.
        owner = await _make_user(pg_db_session, "rec_pg_multi")
        a = KGEntity(user_id=owner.id, name="Die Schmidts", entity_type="organization",
                     entity_types=["organization", "person"], circle_tier=0,
                     mention_count=2, embedding=_unit(6))
        b = KGEntity(user_id=owner.id, name="Die Müllers", entity_type="organization",
                     entity_types=["organization", "person"], circle_tier=0,
                     mention_count=9, embedding=_unit(6))
        pg_db_session.add_all([a, b])
        await pg_db_session.flush()
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert pairs == []

    async def test_typo_pair_is_proposed_never_auto_merged(self, pg_db_session, monkeypatch):
        # #876 field data: the same person under two spellings that differ by ONE
        # in-token edit (the real case: two characters transposed inside the
        # final token; the anonymised stand-in is an insertion). Not a token
        # subset, so the guard used to drop the pair with no path at all. Now: a
        # REVIEW proposal with reason name_typo — never an auto-merge, even at
        # cosine 1.0, same tier, and DISTINCT non-empty descriptions (the
        # low-signal name-collision rule is not what blocks it).
        from models.database import KG_MERGE_REASON_NAME_TYPO
        owner = await _make_user(pg_db_session, "rec_pg_typo")
        await _entity(pg_db_session, owner, "Firstname von der Lastname", tier=0,
                      mention=3085, emb=_unit(6), desc="Kollegin aus dem Vertrieb")
        await _entity(pg_db_session, owner, "Firstname von der Lastnrame", tier=0,
                      mention=134, emb=_unit(6), desc="Ansprechpartnerin Projekt X")
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert len(pairs) == 1
        assert pairs[0].name_typo is True and pairs[0].block_auto_merge is True
        assert pairs[0].names_related is False  # the auto-merge gate refuses it

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 0 and report.proposed == 1
        proposal = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()
        assert proposal.reason == KG_MERGE_REASON_NAME_TYPO

    async def test_rejected_pair_is_not_proposed_again(self, pg_db_session, monkeypatch):
        # A rejection is the owner's verdict: "two people". Before, only PENDING
        # proposals excluded a pair from the self-join, so a rejected pair came
        # back on the next daily run — for a class defined as "maybe two people"
        # that is structural queue noise. Now rejected pairs stay out.
        owner = await _make_user(pg_db_session, "rec_pg_rejected")
        await _entity(pg_db_session, owner, "Anna Schmidt", tier=0, mention=5, emb=_unit(6))
        await _entity(pg_db_session, owner, "Anna Schmitt", tier=0, mention=4, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        first = await rec.run_for_user(owner.id)
        assert first.proposed == 1
        pid = (await pg_db_session.execute(
            select(KgMergeProposal.id).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()
        assert await rec.reject_proposal(pid, resolved_by=owner.id) is True

        # (Here "Anna Schmidt" — inserted first, more mentions — is the winner,
        # i.e. the stored proposal is (loser=b, winner=a): this exercises the
        # REVERSED orientation of the NOT EXISTS; the single-user route test
        # covers the other one.)
        assert await rec.find_duplicate_pairs(owner.id) == []
        second = await rec.run_for_user(owner.id)
        assert second.proposed == 0 and second.auto_merged == 0
        assert await _count_pending(pg_db_session, owner.id) == 0

    async def test_propose_refuses_rejected_pair_if_find_bypassed(
        self, pg_db_session, monkeypatch
    ):
        # Second layer, tested on its own: even if the self-join handed a
        # rejected pair back (future refactor of the SQL), _propose must not
        # re-open it.
        owner = await _make_user(pg_db_session, "rec_pg_rejected_bypass")
        a = await _entity(pg_db_session, owner, "Anna Schmidt", tier=0, mention=5, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Anna Schmitt", tier=0, mention=4, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)
        assert (await rec.run_for_user(owner.id)).proposed == 1
        pid = (await pg_db_session.execute(
            select(KgMergeProposal.id).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()
        assert await rec.reject_proposal(pid, resolved_by=owner.id) is True

        from services.kg_reconciler_service import MergeCandidate
        cand = MergeCandidate(loser_id=b.id, winner_id=a.id, similarity=1.0,
                              loser_tier=0, winner_tier=0, block_auto_merge=True,
                              is_person_pair=True, names_related=False, name_typo=True)
        monkeypatch.setattr(rec, "find_duplicate_pairs", AsyncMock(return_value=[cand]))
        monkeypatch.setattr(rec, "backfill_missing_embeddings", AsyncMock(return_value=0))
        report = await rec.run_for_user(owner.id)
        assert report.proposed == 0 and report.auto_merged == 0
        assert await _count_pending(pg_db_session, owner.id) == 0

    async def test_org_typo_pair_still_auto_merges(self, pg_db_session, monkeypatch):
        # The typo exception is person-scoped. Two organisations one edit apart
        # at cosine 1.0, same tier, auto-merge exactly as before (embedding IS
        # meaningful for non-persons).
        owner = await _make_user(pg_db_session, "rec_pg_org_typo")
        await _entity(pg_db_session, owner, "Acme GmbH", etype="organization",
                      tier=0, mention=2, emb=_unit(6))
        await _entity(pg_db_session, owner, "Acme GmbG", etype="organization",
                      tier=0, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)
        pairs = await rec.find_duplicate_pairs(owner.id)
        assert len(pairs) == 1 and pairs[0].name_typo is False
        assert (await rec.run_for_user(owner.id)).auto_merged == 1

    async def test_cross_tier_typo_pair_keeps_cross_tier_reason(self, pg_db_session, monkeypatch):
        # Label precedence: the visibility change is the invariant-bearing fact,
        # and the card keys its warning + button de-emphasis on cross_tier.
        owner = await _make_user(pg_db_session, "rec_pg_typo_xtier")
        await _entity(pg_db_session, owner, "Firstname von der Lastname", tier=0,
                      mention=5, emb=_unit(6))
        await _entity(pg_db_session, owner, "Firstname von der Lastnrame", tier=2,
                      mention=4, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)
        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 0 and report.proposed == 1
        proposal = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()
        assert proposal.reason == KG_MERGE_REASON_CROSS_TIER

    async def test_numbered_test_accounts_are_still_dropped(self, pg_db_session, monkeypatch):
        # The other six measured pairs: distinct identities whose names differ
        # only in a trailing two-character ordinal. Below the token minimum →
        # not a typo pair → dropped as before (no proposal noise).
        owner = await _make_user(pg_db_session, "rec_pg_ordinal")
        await _entity(pg_db_session, owner, "Testkonto Alpha 01", tier=0, mention=2, emb=_unit(6))
        await _entity(pg_db_session, owner, "Testkonto Alpha 02", tier=0, mention=2, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)

        assert await rec.find_duplicate_pairs(owner.id) == []
        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 0 and report.proposed == 0

    async def test_auto_merge_gate_blocks_unrelated_person_if_find_bypassed(
        self, pg_db_session, monkeypatch
    ):
        # Defense in depth: even if a distinct-name person candidate reached the
        # loop (find-guard bypassed by a future refactor / detection miss), the
        # auto-merge gate must route it to a proposal, never a silent merge.
        from services.kg_reconciler_service import MergeCandidate
        owner = await _make_user(pg_db_session, "rec_pg_gate")
        a = await _entity(pg_db_session, owner, "Jutta", tier=0, mention=2, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Anna", tier=0, mention=9, emb=_unit(6))
        rec = _recon(pg_db_session, monkeypatch)
        cand = MergeCandidate(loser_id=a.id, winner_id=b.id, similarity=1.0,
                              loser_tier=0, winner_tier=0,
                              is_person_pair=True, names_related=False)
        monkeypatch.setattr(rec, "find_duplicate_pairs", AsyncMock(return_value=[cand]))
        monkeypatch.setattr(rec, "backfill_missing_embeddings", AsyncMock(return_value=0))

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 0 and report.proposed == 1


class TestLockEngineResolution:
    async def test_resolve_lock_engine_topologies(self, pg_async_engine):
        # Regression for the advisory-lock crash on first prod-enable: prod binds an
        # AsyncEngine (must be used directly — its .engine is the SYNC engine, which
        # explodes under `async with .connect()`); tests bind an AsyncConnection
        # (its .engine IS the AsyncEngine). Anything else -> None (unlocked).
        from services.kg_reconciler_service import _resolve_lock_engine

        assert _resolve_lock_engine(pg_async_engine) is pg_async_engine  # prod path
        async with pg_async_engine.connect() as conn:
            assert _resolve_lock_engine(conn) is pg_async_engine          # test path
        assert _resolve_lock_engine(object()) is None
        assert _resolve_lock_engine(None) is None


class TestResolveCluster:
    """One owner decision over a whole name cluster (review-queue cluster view).

    The queue is dominated by clusters of same-named entities with no
    description — measured on the live household 2026-09-24: 1 365 pending pairs
    over 952 entities in 199 name clusters. Pair-by-pair is the wrong unit; the
    owner judges the cluster once. The invariant that makes a bulk action safe is
    that ONLY same-tier pairs take part, so `tier = MIN` inside merge_entities
    can never shift visibility.
    """

    @staticmethod
    async def _proposal(db, owner, loser, winner, *, sim=0.9, reason="gray_zone"):
        p = KgMergeProposal(
            user_id=owner.id, loser_entity_id=loser.id, winner_entity_id=winner.id,
            similarity=sim, loser_tier=loser.circle_tier, winner_tier=winner.circle_tier,
            reason=reason, status=KG_MERGE_PROPOSAL_PENDING,
        )
        db.add(p)
        await db.flush()
        return p

    async def test_merge_folds_the_whole_cluster_into_the_survivor(
        self, pg_db_session, monkeypatch
    ):
        owner = await _make_user(pg_db_session, "clu_merge")
        a = await _entity(pg_db_session, owner, "Anna", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Anna", tier=2, mention=9, emb=_unit(6))
        c = await _entity(pg_db_session, owner, "Anna", tier=2, mention=3, emb=_unit(6))
        p1 = await self._proposal(pg_db_session, owner, a, b)
        p2 = await self._proposal(pg_db_session, owner, c, b)
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id, c.id],
            survivor_id=b.id, decision="merge", resolved_by=owner.id,
        )

        assert res.merged == 2
        assert res.approved == 2
        assert res.skipped_cross_tier == 0
        for e_id in (a.id, c.id):
            gone = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == e_id)
            )).scalar_one()
            assert gone.is_active is False
        kept = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == b.id)
        )).scalar_one()
        assert kept.is_active is True
        for pid in (p1.id, p2.id):
            prop = (await pg_db_session.execute(
                select(KgMergeProposal).where(KgMergeProposal.id == pid)
            )).scalar_one()
            assert prop.status == KG_MERGE_PROPOSAL_APPROVED

    async def test_cross_tier_pair_is_left_alone_and_reported(
        self, pg_db_session, monkeypatch
    ):
        """A pair that would change an atom's reach never rides along."""
        owner = await _make_user(pg_db_session, "clu_xtier")
        a = await _entity(pg_db_session, owner, "Jutta", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Jutta", tier=2, mention=9, emb=_unit(6))
        c = await _entity(pg_db_session, owner, "Jutta", tier=0, mention=4, emb=_unit(6))
        await self._proposal(pg_db_session, owner, a, b)
        px = await self._proposal(
            pg_db_session, owner, c, b, reason=KG_MERGE_REASON_CROSS_TIER
        )
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id, c.id],
            survivor_id=b.id, decision="merge", resolved_by=owner.id,
        )

        assert res.merged == 1               # only a folded in
        assert res.skipped_cross_tier == 1
        still_there = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == c.id)
        )).scalar_one()
        assert still_there.is_active is True
        assert still_there.circle_tier == 0  # untouched, no visibility shift
        prop = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == px.id)
        )).scalar_one()
        assert prop.status == KG_MERGE_PROPOSAL_PENDING

    async def test_reject_closes_the_pairs_and_merges_nothing(
        self, pg_db_session, monkeypatch
    ):
        owner = await _make_user(pg_db_session, "clu_rej")
        a = await _entity(pg_db_session, owner, "Anna", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Anna", tier=2, mention=9, emb=_unit(6))
        await self._proposal(pg_db_session, owner, a, b)
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id],
            survivor_id=None, decision="reject", resolved_by=owner.id,
        )

        assert (res.rejected, res.merged) == (1, 0)
        assert await _count_pending(pg_db_session, owner.id) == 0
        for e_id in (a.id, b.id):
            e = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == e_id)
            )).scalar_one()
            assert e.is_active is True

    async def test_an_entity_without_a_proposal_is_never_merged(
        self, pg_db_session, monkeypatch
    ):
        """The fold set comes from the PROPOSALS, not from the caller's list —
        otherwise this route would merge two arbitrary entities on request."""
        owner = await _make_user(pg_db_session, "clu_stow")
        a = await _entity(pg_db_session, owner, "Anna", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Anna", tier=2, mention=9, emb=_unit(6))
        stowaway = await _entity(pg_db_session, owner, "Berta", tier=2, mention=5, emb=_unit(7))
        await self._proposal(pg_db_session, owner, a, b)
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id, stowaway.id],
            survivor_id=b.id, decision="merge", resolved_by=owner.id,
        )

        assert res.merged == 1
        untouched = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == stowaway.id)
        )).scalar_one()
        assert untouched.is_active is True

    async def test_survivor_must_belong_to_the_cluster(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "clu_surv")
        a = await _entity(pg_db_session, owner, "Anna", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Anna", tier=2, mention=9, emb=_unit(6))
        outsider = await _entity(pg_db_session, owner, "Carla", tier=2, mention=5, emb=_unit(8))
        await self._proposal(pg_db_session, owner, a, b)
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id],
            survivor_id=outsider.id, decision="merge", resolved_by=owner.id,
        )

        assert (res.merged, res.approved) == (0, 0)
        assert res.notes
        assert await _count_pending(pg_db_session, owner.id) == 1

    async def test_only_the_survivors_component_is_folded(self, pg_db_session, monkeypatch):
        """Two disjoint components can EACH be same-tier and still sit at
        different tiers. Folding the union would apply `tier = MIN` across them
        and quietly narrow an atom's reach — the one thing this must never do."""
        owner = await _make_user(pg_db_session, "clu_comp")
        a = await _entity(pg_db_session, owner, "Anna", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Anna", tier=2, mention=9, emb=_unit(6))
        # ein zweites, UNVERBUNDENES Paar auf einer anderen Stufe
        c = await _entity(pg_db_session, owner, "Clara", tier=0, mention=2, emb=_unit(7))
        d = await _entity(pg_db_session, owner, "Clara", tier=0, mention=5, emb=_unit(7))
        await self._proposal(pg_db_session, owner, a, b)
        await self._proposal(pg_db_session, owner, c, d)
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id, c.id, d.id],
            survivor_id=b.id, decision="merge", resolved_by=owner.id,
        )

        assert res.merged == 1                      # nur a
        for e_id in (c.id, d.id):
            untouched = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == e_id)
            )).scalar_one()
            assert untouched.is_active is True
            assert untouched.circle_tier == 0       # keine Stufenverschiebung
        kept = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == b.id)
        )).scalar_one()
        assert kept.circle_tier == 2

    async def test_a_cross_tier_pair_survives_the_fold_as_a_real_question(
        self, pg_db_session, monkeypatch
    ):
        """The bulk action leaves cross-tier pairs pending — but a pair pointing
        at a tombstoned entity could never be exercised again. It is re-pointed
        at the survivor, so the owner's tier decision stays askable."""
        owner = await _make_user(pg_db_session, "clu_repoint")
        a = await _entity(pg_db_session, owner, "Jutta", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Jutta", tier=2, mention=9, emb=_unit(6))
        privat = await _entity(pg_db_session, owner, "Jutta", tier=0, mention=4, emb=_unit(6))
        await self._proposal(pg_db_session, owner, a, b)
        # haengt am SPAETER gefalteten a, nicht am Ueberlebenden
        px = await self._proposal(
            pg_db_session, owner, privat, a, reason=KG_MERGE_REASON_CROSS_TIER
        )
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id, privat.id],
            survivor_id=b.id, decision="merge", resolved_by=owner.id,
        )

        assert res.merged == 1
        moved = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == px.id)
        )).scalar_one()
        assert moved.status == KG_MERGE_PROPOSAL_PENDING
        assert moved.winner_entity_id == b.id       # zeigt jetzt auf den Ueberlebenden
        assert moved.loser_entity_id == privat.id

    async def test_a_pair_between_two_folded_entities_is_closed(
        self, pg_db_session, monkeypatch
    ):
        """Both sides went into the survivor: there is no question left."""
        owner = await _make_user(pg_db_session, "clu_both")
        a = await _entity(pg_db_session, owner, "Anna", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Anna", tier=2, mention=9, emb=_unit(6))
        c = await _entity(pg_db_session, owner, "Anna", tier=2, mention=3, emb=_unit(6))
        await self._proposal(pg_db_session, owner, a, b)
        await self._proposal(pg_db_session, owner, c, b)
        # a~c auf einer anderen Stufe waere cross_tier; hier gleichstufig, also
        # Teil des Clusters — und nach der Faltung gegenstandslos.
        extra = await self._proposal(pg_db_session, owner, a, c)
        rec = _recon(pg_db_session, monkeypatch)

        await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id, c.id],
            survivor_id=b.id, decision="merge", resolved_by=owner.id,
        )

        closed = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == extra.id)
        )).scalar_one()
        assert closed.status != KG_MERGE_PROPOSAL_PENDING

    async def test_another_owners_pairs_do_not_take_part(self, pg_db_session, monkeypatch):
        owner = await _make_user(pg_db_session, "clu_mine")
        other = await _make_user(pg_db_session, "clu_theirs")
        a = await _entity(pg_db_session, other, "Anna", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, other, "Anna", tier=2, mention=9, emb=_unit(6))
        await self._proposal(pg_db_session, other, a, b)
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id],
            survivor_id=b.id, decision="merge", resolved_by=owner.id,
        )

        assert (res.merged, res.approved, res.rejected) == (0, 0, 0)
        assert await _count_pending(pg_db_session, other.id) == 1

    async def test_an_unreachable_pair_is_not_reported_as_a_visibility_skip(
        self, pg_db_session, monkeypatch
    ):
        """Two disjoint components at the same tier: the far one is unreachable.

        It cleared the tier filter AND the type filter — calling it
        `skipped_cross_tier` tells the owner "different visibility", which is
        false; the pair sits at the survivor's own tier.
        """
        owner = await _make_user(pg_db_session, "clu_unreach")
        a = await _entity(pg_db_session, owner, "Anna", tier=2, mention=1, emb=_unit(6))
        b = await _entity(pg_db_session, owner, "Anna", tier=2, mention=9, emb=_unit(6))
        far1 = await _entity(pg_db_session, owner, "Anna", tier=2, mention=4, emb=_unit(6))
        far2 = await _entity(pg_db_session, owner, "Anna", tier=2, mention=3, emb=_unit(6))
        await self._proposal(pg_db_session, owner, a, b)
        await self._proposal(pg_db_session, owner, far1, far2)   # its own component
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[a.id, b.id, far1.id, far2.id],
            survivor_id=b.id, decision="merge", resolved_by=owner.id,
        )

        assert res.merged == 1                 # only a folded into b
        assert res.skipped_unreachable == 1    # the far pair, honestly labelled
        assert res.skipped_cross_tier == 0     # nothing here is a visibility skip
        assert res.skipped_cross_type == 0

class TestTypeGuard:
    """A place must never be folded into the company seated in it.

    Field shape, xidra graph 2026-09-24: place "Korschenbroich" ~ organization
    "X-Idra Systems GmbH" at cosine 0.895 — both descriptions come out of the
    same letterhead, so the embedding says "same thing" and is simply wrong.
    Four such pairs were pending; one of them inside a cluster would have pulled
    the whole company component into the town.

    Every entity here carries the SAME embedding (cosine 1.0, above the 0.95
    auto bar) and the same tier, and both sides carry DISTINCT descriptions so
    the name-collision brake (_name_collision_low_signal) cannot be what stops
    the merge. Without the type guard each of these auto-merges.
    """

    @staticmethod
    async def _proposal(db, owner, loser, winner, *, sim=0.9, reason="gray_zone"):
        p = KgMergeProposal(
            user_id=owner.id, loser_entity_id=loser.id, winner_entity_id=winner.id,
            similarity=sim, loser_tier=loser.circle_tier, winner_tier=winner.circle_tier,
            reason=reason, status=KG_MERGE_PROPOSAL_PENDING,
        )
        db.add(p)
        await db.flush()
        return p

    async def test_place_and_organization_with_unrelated_names_is_no_candidate(
        self, pg_db_session, monkeypatch
    ):
        owner = await _make_user(pg_db_session, "tg_field")
        town = await _entity(pg_db_session, owner, "Korschenbroich", tier=2, mention=4,
                             emb=_unit(6), etype="place", desc="Stadt am Niederrhein")
        firm = await _entity(pg_db_session, owner, "X-Idra Systems GmbH", tier=2,
                             mention=9, emb=_unit(6), etype="organization",
                             desc="Softwarehaus")
        rec = _recon(pg_db_session, monkeypatch)

        assert await rec.find_duplicate_pairs(owner.id) == []

        report = await rec.run_for_user(owner.id)
        assert (report.auto_merged, report.proposed) == (0, 0)
        assert await _count_pending(pg_db_session, owner.id) == 0
        for e_id in (town.id, firm.id):
            row = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == e_id)
            )).scalar_one()
            assert row.is_active is True
            assert row.canonical_id is None

    async def test_same_name_different_type_survives_as_review_only(
        self, pg_db_session, monkeypatch
    ):
        """The mis-TYPED duplicate — the one shape a disjoint type may be.

        Both live graphs hold such a fold (person "Pontresina" -> place
        "Pontresina"). It must still reach the owner, and it must never be
        auto-merged: which type is right is a human call.
        """
        owner = await _make_user(pg_db_session, "tg_mistyped")
        a = await _entity(pg_db_session, owner, "Pontresina", tier=2, mention=1,
                          emb=_unit(6), etype="person", desc="im Reisebericht erwähnt")
        b = await _entity(pg_db_session, owner, "Pontresina", tier=2, mention=9,
                          emb=_unit(6), etype="place", desc="Gemeinde im Engadin")
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert len(pairs) == 1
        assert pairs[0].cross_type is True
        assert pairs[0].block_auto_merge is True

        report = await rec.run_for_user(owner.id)
        assert (report.auto_merged, report.proposed) == (0, 1)
        prop = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()
        assert prop.reason == KG_MERGE_REASON_CROSS_TYPE
        for e_id in (a.id, b.id):
            row = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == e_id)
            )).scalar_one()
            assert row.is_active is True

    async def test_subset_name_across_types_is_a_review_candidate_too(
        self, pg_db_session, monkeypatch
    ):
        """A subset name across two REAL type claims: review, never auto-merge.

        The live field pair (organization "Publikationsplattform" ⊆ thing
        "Publikationsplattform der …") is deliberately not the fixture here:
        `thing` is the no-type bucket and disarms the guard entirely
        (``_UNTYPED``), so that pair flows through as it always did. Two claimed
        types are what the exception is actually about.
        """
        owner = await _make_user(pg_db_session, "tg_subset")
        await _entity(pg_db_session, owner, "Publikationsplattform", tier=2, mention=1,
                      emb=_unit(6), etype="organization", desc="Kurzform")
        await _entity(pg_db_session, owner, "Publikationsplattform der Bundesanzeiger",
                      tier=2, mention=9, emb=_unit(6), etype="concept", desc="Langform")
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert (report.auto_merged, report.proposed) == (0, 1)
        assert report.dropped_cross_type == 0

    async def test_same_type_still_auto_merges(self, pg_db_session, monkeypatch):
        """The guard must not be a blanket brake on the queue."""
        owner = await _make_user(pg_db_session, "tg_control")
        await _entity(pg_db_session, owner, "X-Idra Systems GmbH", tier=2, mention=1,
                      emb=_unit(6), etype="organization", desc="Softwarehaus")
        await _entity(pg_db_session, owner, "X-idra Systems GmbH", tier=2, mention=9,
                      emb=_unit(6), etype="organization", desc="Auftraggeber")
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.auto_merged == 1

    async def test_cluster_fold_leaves_a_cross_type_pair_alone(
        self, pg_db_session, monkeypatch
    ):
        """The view's half proved on the backend: a bulk fold never crosses types.

        Two spellings of the company plus the town, all same-tier, all tied by
        pending pairs — the shape the owner saw in /brain/review. Only the two
        company rows fold; the town stays, and its pair stays pending.
        """
        owner = await _make_user(pg_db_session, "tg_cluster")
        f1 = await _entity(pg_db_session, owner, "X-Idra Systems GmbH", tier=2,
                           mention=1, emb=_unit(6), etype="organization")
        f2 = await _entity(pg_db_session, owner, "X-idra Systems GmbH", tier=2,
                           mention=9, emb=_unit(6), etype="organization")
        town = await _entity(pg_db_session, owner, "Korschenbroich", tier=2,
                             mention=4, emb=_unit(6), etype="place")
        await self._proposal(pg_db_session, owner, f1, f2)
        px = await self._proposal(pg_db_session, owner, town, f2)
        rec = _recon(pg_db_session, monkeypatch)

        res = await rec.resolve_cluster(
            user_id=owner.id, entity_ids=[f1.id, f2.id, town.id],
            survivor_id=f2.id, decision="merge", resolved_by=owner.id,
        )

        assert res.merged == 1
        assert res.skipped_cross_type == 1
        assert res.skipped_cross_tier == 0
        still = (await pg_db_session.execute(
            select(KGEntity).where(KGEntity.id == town.id)
        )).scalar_one()
        assert still.is_active is True
        assert still.entity_type == "place"
        prop = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.id == px.id)
        )).scalar_one()
        assert prop.status == KG_MERGE_PROPOSAL_PENDING

    async def test_thing_vs_organization_still_reaches_the_owner(
        self, pg_db_session, monkeypatch
    ):
        """The guard must not eat the commonest LLM duplicate shape.

        `thing` is the extraction's no-type fallback. Two unrelated-looking
        names, one bucketed as `thing` and one as `organization`, are the same
        firm more often than not — before the guard that was a `gray_zone`
        proposal the owner could decide, and it has to stay one.
        """
        owner = await _make_user(pg_db_session, "tg_thing")
        await _entity(pg_db_session, owner, "Fa. Beispiel", tier=2, mention=1,
                      emb=_unit(6), etype="thing", desc="aus einer Rechnung")
        await _entity(pg_db_session, owner, "Beispiel GmbH", tier=2, mention=9,
                      emb=_unit(6), etype="organization", desc="Lieferant")
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.dropped_cross_type == 0
        assert report.auto_merged + report.proposed == 1

    async def test_a_typo_pair_survives_a_type_mismatch(
        self, pg_db_session, monkeypatch
    ):
        """The type guard must not cancel the #876 typo exception.

        A typo pair is `related=False` by construction, so a bare
        `cross_type and not related` drop would swallow every person mis-extracted
        under another type — the exact case that exception exists for.
        """
        owner = await _make_user(pg_db_session, "tg_typo")
        await _entity(pg_db_session, owner, "Anna Schmitt", tier=2, mention=1,
                      emb=_unit(6), etype="person", desc="aus einem Protokoll")
        await _entity(pg_db_session, owner, "Anna Schmidt", tier=2, mention=9,
                      emb=_unit(6), etype="concept", desc="falsch typisiert")
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.dropped_cross_type == 0
        assert (report.auto_merged, report.proposed) == (0, 1)

    async def test_the_guards_count_what_they_eat(self, pg_db_session, monkeypatch):
        """`candidates` counts SURVIVORS, so a greedy guard is otherwise invisible."""
        owner = await _make_user(pg_db_session, "tg_counters")
        # dropped by the type guard: disjoint types, unrelated names
        await _entity(pg_db_session, owner, "Korschenbroich", tier=2, mention=4,
                      emb=_unit(6), etype="place", desc="Stadt")
        await _entity(pg_db_session, owner, "Beispiel GmbH", tier=2, mention=9,
                      emb=_unit(6), etype="organization", desc="Firma")
        # dropped by the person guard: two different people, same embedding
        await _entity(pg_db_session, owner, "Jutta", tier=2, mention=3,
                      emb=_unit(7), etype="person", desc="Nachbarin")
        await _entity(pg_db_session, owner, "Gaby", tier=2, mention=5,
                      emb=_unit(7), etype="person", desc="Kollegin")
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert report.candidates == 0          # nothing survived — and that is the point
        assert report.dropped_cross_type == 1
        assert report.dropped_person_guard == 1

    async def test_a_missing_primary_type_does_not_buy_an_auto_merge(
        self, pg_db_session, monkeypatch
    ):
        """Leniency belongs to the DROP, never to the silent merge.

        `_types_compatible` treats an absent type as "no evidence of a mismatch",
        which is right when deciding whether to throw a pair away. It must not
        also be what lets two rows merge with nobody looking: an entity with an
        empty `entity_type` would otherwise be auto-merge-compatible with
        everything at >= the auto threshold.
        """
        owner = await _make_user(pg_db_session, "tg_notype")
        await _entity(pg_db_session, owner, "Beispiel GmbH", tier=2, mention=1,
                      emb=_unit(6), etype="", desc="ohne Typ")
        await _entity(pg_db_session, owner, "Beispiel GmbH", tier=2, mention=9,
                      emb=_unit(6), etype="organization", desc="Lieferant")
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert len(pairs) == 1
        assert pairs[0].types_known is False

        report = await rec.run_for_user(owner.id)
        assert (report.auto_merged, report.proposed) == (0, 1)


class TestNameTokenization:
    """The four acceptance conditions agreed with the reva instance 2026-09-24.

    Its production graph holds the one pair in three graphs that this rescues —
    `concept "Product Owner"` ~ `person "ProductOwner"` at 0.9030 — plus six
    near-misses that must stay invisible. The negative controls carry more weight
    than the positive one: a far too generous change passes the positive case
    just as well.
    """

    async def test_a_mis_typed_role_reaches_review(self, pg_db_session, monkeypatch):
        """The positive case. Dropped by the PERSON guard today, before the type
        guard could ever look at it."""
        owner = await _make_user(pg_db_session, "tok_role")
        a = await _entity(pg_db_session, owner, "Product Owner", tier=2, mention=4,
                          emb=_unit(0), etype="concept", desc="Rolle im Projekt")
        b = await _entity(pg_db_session, owner, "ProductOwner", tier=2, mention=9,
                          emb=_gray(), etype="person", desc="als Person extrahiert")
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert len(pairs) == 1
        assert pairs[0].name_tokenization is True
        assert pairs[0].names_related is False     # the safety flag is untouched
        assert pairs[0].block_auto_merge is True

        report = await rec.run_for_user(owner.id)
        assert (report.auto_merged, report.proposed) == (0, 1)
        assert report.dropped_person_guard == 0
        prop = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()
        # The LABEL is cross_type, not name_tokenization: this pair is BOTH, and
        # the agreed precedence (cross_tier > cross_type > name_typo >
        # name_tokenization > gray_zone) gives the type mismatch the label. That
        # is the right answer for the owner — "one of these is mis-typed" is the
        # actionable fact; how the names are written is only how it got missed.
        # `name_tokenization` therefore surfaces on SAME-type pairs; see
        # test_the_label_shows_on_a_same_type_pair below.
        assert prop.reason == KG_MERGE_REASON_CROSS_TYPE
        for e_id in (a.id, b.id):
            row = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == e_id)
            )).scalar_one()
            assert row.is_active is True

    async def test_never_auto_merges_even_at_cosine_one(self, pg_db_session, monkeypatch):
        """`person_ok` must stay shut. This is the whole reason the rescue is a
        SEPARATE test instead of a loosening of `_names_related`."""
        owner = await _make_user(pg_db_session, "tok_noauto")
        await _entity(pg_db_session, owner, "Security", tier=2, mention=4,
                      emb=_unit(6), etype="person", desc="Rolle")
        await _entity(pg_db_session, owner, "SecurityEngineer", tier=2, mention=9,
                      emb=_unit(6), etype="person", desc="Zustaendigkeit")
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert pairs[0].similarity >= 0.99          # far above the auto bar
        assert pairs[0].names_related is False      # … and the gate reads THIS
        report = await rec.run_for_user(owner.id)
        assert (report.auto_merged, report.proposed) == (0, 1)

    async def test_the_label_shows_on_a_same_type_pair(self, pg_db_session, monkeypatch):
        """Where `name_tokenization` is actually the most specific thing to say.

        A cross-TYPE tokenization pair is labelled `cross_type` (that mismatch is
        the actionable fact). On a same-type pair the tokenization IS the reason,
        and the owner would otherwise be told only "similar but uncertain".
        """
        owner = await _make_user(pg_db_session, "tok_label")
        await _entity(pg_db_session, owner, "Security", tier=2, mention=4,
                      emb=_unit(0), etype="person", desc="Rolle")
        await _entity(pg_db_session, owner, "SecurityEngineer", tier=2, mention=9,
                      emb=_gray(), etype="person", desc="Zustaendigkeit")
        rec = _recon(pg_db_session, monkeypatch)

        report = await rec.run_for_user(owner.id)
        assert (report.auto_merged, report.proposed) == (0, 1)
        prop = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()
        assert prop.reason == KG_MERGE_REASON_NAME_TOKENIZATION

    async def test_below_the_candidate_threshold_stays_invisible(
        self, pg_db_session, monkeypatch
    ):
        """Negative control, and the one that matters most: `similarity >= :cand`
        is a WHERE clause in the SQL, so the pair is never fetched and the rescue
        never runs. reva's `person 'BackendEngineer'` ~ `thing 'backend'` sits at
        0.6025 with the SAME owner — it tests the similarity filter in isolation,
        without the owner condition helping."""
        owner = await _make_user(pg_db_session, "tok_farapart")
        await _entity(pg_db_session, owner, "backend", tier=2, mention=4,
                      emb=_unit(1), etype="thing", desc="System")
        await _entity(pg_db_session, owner, "BackendEngineer", tier=2, mention=9,
                      emb=_unit(2), etype="person", desc="Rolle")   # orthogonal → 0.0
        rec = _recon(pg_db_session, monkeypatch)

        assert await rec.find_duplicate_pairs(owner.id) == []
        report = await rec.run_for_user(owner.id)
        assert (report.auto_merged, report.proposed) == (0, 0)
        assert report.dropped_person_guard == 0     # not dropped — never fetched

    async def test_a_different_owner_is_never_a_candidate(
        self, pg_db_session, monkeypatch
    ):
        """`a.user_id = b.user_id` sits in the JOIN, next to the similarity filter.
        Five of reva's six near-misses are excluded by it. Both of us had
        overlooked this condition while reasoning about the guards."""
        mine = await _make_user(pg_db_session, "tok_mine")
        theirs = await _make_user(pg_db_session, "tok_theirs")
        await _entity(pg_db_session, mine, "Product Owner", tier=2, mention=4,
                      emb=_unit(6), etype="concept")
        await _entity(pg_db_session, theirs, "ProductOwner", tier=2, mention=9,
                      emb=_unit(6), etype="person")
        rec = _recon(pg_db_session, monkeypatch)

        assert await rec.find_duplicate_pairs(mine.id) == []
        assert await rec.find_duplicate_pairs(theirs.id) == []

    async def test_the_rescue_creates_no_new_candidates(
        self, pg_db_session, monkeypatch
    ):
        """reva's fourth condition. The rescue runs in Python AFTER the self-join,
        so it can only keep pairs the SQL already fetched — never add one. A
        regression here would mean the normalization slipped in front of the
        similarity filter."""
        owner = await _make_user(pg_db_session, "tok_nonew")
        # one fetched pair (cosine ~0.9) …
        await _entity(pg_db_session, owner, "Product Owner", tier=2, mention=4,
                      emb=_unit(0), etype="concept", desc="Rolle")
        await _entity(pg_db_session, owner, "ProductOwner", tier=2, mention=9,
                      emb=_gray(), etype="person", desc="Person")
        # … and two that tokenize into each other but are orthogonal, so the SQL
        # never hands them over however related their names are.
        await _entity(pg_db_session, owner, "Billing Engine", tier=2, mention=2,
                      emb=_unit(3), etype="thing", desc="A")
        await _entity(pg_db_session, owner, "BillingEngine", tier=2, mention=1,
                      emb=_unit(4), etype="thing", desc="B")
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert len(pairs) == 1
        assert pairs[0].name_tokenization is True

    async def test_a_same_type_tokenization_pair_is_demoted_out_of_auto_merge(
        self, pg_db_session, monkeypatch
    ):
        """The rescue's SECOND effect, deliberate and easy to miss.

        A same-type non-person pair is gated by no guard — `related` is never
        consulted for it — so "Billing Engine" ~ "BillingEngine" is already a
        candidate, and ABOVE the auto bar with distinct descriptions it would be
        folded with nobody looking. Setting `block_auto_merge` for tokenization
        variants demotes that case to review.

        Whether any real pair crosses the bar is an empirical question, and on
        all three measured graphs none does today — the reva pair this fixture is
        named after sits at 0.8770 and is already a proposal there. The fixture
        puts it at cosine 1.0 on purpose: the mechanism has to be exercised even
        where no production corpus reaches it.

        That is wanted, not collateral: tokenization-relatedness is a weak
        signal. The same rule that pairs "Billing Engine" with "BillingEngine"
        also pairs "Release" with "HelmRelease" and "Payment" with
        "PaymentGateway-Timeout" (measured on the reva graph) — none of which
        may be folded with nobody looking.
        """
        owner = await _make_user(pg_db_session, "tok_demote")
        a = await _entity(pg_db_session, owner, "Billing Engine", tier=2, mention=2,
                          emb=_unit(6), etype="thing", desc="Abrechnung")
        b = await _entity(pg_db_session, owner, "BillingEngine", tier=2, mention=9,
                          emb=_unit(6), etype="thing", desc="Komponente")
        rec = _recon(pg_db_session, monkeypatch)

        pairs = await rec.find_duplicate_pairs(owner.id)
        assert len(pairs) == 1
        assert pairs[0].similarity >= 0.99          # above the auto bar …
        assert pairs[0].name_tokenization is True   # … but a tokenization variant
        assert pairs[0].block_auto_merge is True

        report = await rec.run_for_user(owner.id)
        assert (report.auto_merged, report.proposed) == (0, 1)
        prop = (await pg_db_session.execute(
            select(KgMergeProposal).where(KgMergeProposal.user_id == owner.id)
        )).scalar_one()
        assert prop.reason == KG_MERGE_REASON_NAME_TOKENIZATION
        for e_id in (a.id, b.id):
            row = (await pg_db_session.execute(
                select(KGEntity).where(KGEntity.id == e_id)
            )).scalar_one()
            assert row.is_active is True
