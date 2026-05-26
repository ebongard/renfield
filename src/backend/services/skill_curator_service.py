"""
SkillCuratorService — periodic skill-corpus maintenance.

Self-learning Phase 4: long-running deployments accumulate skills.
After a few weeks the corpus has near-duplicates (the same procedure
extracted twice with slightly different titles) and stale rows (skills
that worked once and never matched again). The curator job:

  1. find_duplicate_pairs(user_id):
     For every pair of *approved* skills owned by the user, compare
     their embeddings; pairs with cosine similarity >=
     ``settings.skill_curator_duplicate_threshold`` are flagged.
     Pgvector handles the SQL; no LLM in the hot path. Draft/rejected
     rows are out of scope — the curator's job is to consolidate the
     active corpus, not to police pending review.

  2. merge_pair(loser, winner):
     Pick the "winner" as the skill with the higher success rate
     (ties broken by more recent last_used_at). Combine their triggers
     (deduped, capped). Bump the winner's version. Mark the loser
     ``status='archived', merged_into_id=winner.id`` — kept for audit.

  3. archive_stale(user_id):
     Approved skills not used in ``skill_curator_stale_days`` days that
     have at least ``skill_curator_min_uses_to_consider_stale`` total
     calls AND a success_rate below ``skill_curator_stale_success_rate``
     get archived (status='archived', no merged_into_id since they're
     not duplicates). Pinned skills are exempt.

  4. run_curator(triggered_by_user_id, run_type):
     Top-level orchestrator. Writes a SkillCuratorRun audit row at
     start (status='running') and updates it on completion with the
     aggregated counters. Used by both the lifecycle scheduler
     (run_type='scheduled') and the AdminCuratorPage "Run Now" button
     (run_type='manual', triggered_by_user_id set).

The whole job runs per-user (a household's curator runs N times, once
per active user). This keeps the duplicate pairs naturally scoped to
each user's skill graph instead of cross-contaminating private
procedures.

NO LLM use: v1 ships pure-embedding deduplication. A future v2 could
invoke an LLM to merge body_md content semantically; today we just
concat trigger sets and let the winner's body win.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from loguru import logger
from sqlalchemy import case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    CURATOR_RUN_STATUS_FAILED,
    CURATOR_RUN_STATUS_PARTIAL,
    CURATOR_RUN_STATUS_RUNNING,
    CURATOR_RUN_STATUS_SUCCESS,
    CURATOR_RUN_TYPE_MANUAL,
    CURATOR_RUN_TYPE_SCHEDULED,
    EMBEDDING_DIMENSION,
    ProceduralSkill,
    SKILL_SOURCE_SEED,
    SKILL_STATUS_APPROVED,
    SKILL_STATUS_ARCHIVED,
    SkillCuratorRun,
)
from services.skill_service import SkillService
from utils.config import settings


@dataclass
class DuplicatePair:
    loser_id: int
    winner_id: int
    similarity: float


@dataclass
class CuratorReport:
    user_id: int | None
    duplicates_found: int
    merges_applied: int
    stale_archived: int
    notes: list[str]


class SkillCuratorService:
    """Periodic maintenance over the per-user skill corpus."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ============================================================ public
    async def run_for_user(self, user_id: int) -> CuratorReport:
        """End-to-end curator pass for a single user. Idempotent.

        Order matters: dedupe BEFORE stale-archive so we don't archive
        the loser of a pending merge by accident.
        """
        report = CuratorReport(
            user_id=user_id,
            duplicates_found=0,
            merges_applied=0,
            stale_archived=0,
            notes=[],
        )

        pairs = await self.find_duplicate_pairs(user_id=user_id)
        report.duplicates_found = len(pairs)

        cap = settings.skill_curator_max_merges_per_run
        merged_ids: set[int] = set()
        for pair in pairs[:cap]:
            # Skip if either side was already merged in this run — a
            # transitive duplicate cluster {A,B,C} where A~B and B~C
            # would otherwise double-merge B.
            if pair.loser_id in merged_ids or pair.winner_id in merged_ids:
                continue
            try:
                await self.merge_pair(pair.loser_id, pair.winner_id)
                merged_ids.add(pair.loser_id)
                report.merges_applied += 1
            except Exception as e:  # noqa: BLE001
                report.notes.append(
                    f"merge failed loser={pair.loser_id} winner={pair.winner_id}: {e}"
                )

        try:
            archived = await self.archive_stale(user_id=user_id)
            report.stale_archived = archived
        except Exception as e:  # noqa: BLE001
            report.notes.append(f"stale archive failed: {e}")

        if report.merges_applied or report.stale_archived:
            logger.info(
                f"🧹 Curator user={user_id}: merged={report.merges_applied}, "
                f"stale_archived={report.stale_archived}, "
                f"pairs_seen={report.duplicates_found}"
            )
        return report

    # ============================================================ dedupe
    async def find_duplicate_pairs(self, user_id: int) -> list[DuplicatePair]:
        """Return user-owned skill pairs above the similarity threshold.

        SQL self-join on the embedding column with the pgvector cosine
        operator. We restrict to a.id < b.id so each pair appears once,
        and exclude seeds (cross-user public rows have no per-user owner
        to merge under).

        sqlite test harness has no pgvector — this method short-circuits
        to [] there so tests against the rest of the pipeline still run.
        """
        threshold = settings.skill_curator_duplicate_threshold
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return []

        # Over-fetch 2x the merge cap so run_for_user has headroom for
        # transitive-cluster skips (where one pair shares a side with a
        # pair already merged this pass). Without the LIMIT, an N-skill
        # user gets an unbounded O(N²) sort even though
        # max_merges_per_run (default 20) caps what we'll act on.
        #
        # halfvec cast on BOTH sides of the cosine op is mandatory — the
        # HNSW index in pc20260523 was built with `halfvec_cosine_ops`
        # (regular `vector` type caps at 2000 dims, production runs
        # 2560-dim qwen3-embedding:4b). Without the cast on both columns
        # the planner can't use the index and falls back to a seq-scan +
        # per-pair cosine on the raw 2560-dim vectors. Same pattern used
        # in skill_service.find_similar.
        fetch_cap = max(settings.skill_curator_max_merges_per_run * 2, 2)
        dim = EMBEDDING_DIMENSION
        sql = text(f"""
            SELECT a.id AS id_a, b.id AS id_b,
                   1 - (a.embedding::halfvec({dim}) <=> b.embedding::halfvec({dim})) AS similarity
            FROM procedural_skills a
            JOIN procedural_skills b
              ON a.id < b.id
             AND a.user_id = b.user_id
             AND a.embedding IS NOT NULL
             AND b.embedding IS NOT NULL
            WHERE a.user_id = :user_id
              AND a.status = :approved
              AND b.status = :approved
              AND a.source <> :seed
              AND b.source <> :seed
              AND (1 - (a.embedding::halfvec({dim}) <=> b.embedding::halfvec({dim}))) >= :threshold
            ORDER BY similarity DESC
            LIMIT :fetch_cap
        """)
        rows = (await self.db.execute(sql, {
            "user_id": user_id,
            "approved": SKILL_STATUS_APPROVED,
            "seed": SKILL_SOURCE_SEED,
            "threshold": threshold,
            "fetch_cap": fetch_cap,
        })).fetchall()

        if not rows:
            return []

        # Bulk-load every skill referenced by any pair in ONE round-trip,
        # then dispatch _pick_winner from the in-memory dict. Previous
        # implementation issued a separate SELECT inside _pick_winner per
        # pair (N+1 against the same table the self-join already scanned),
        # so a curator pass that found 40 pairs paid 40 sequential
        # round-trips before the merge work even started.
        ref_ids: set[int] = set()
        for r in rows:
            ref_ids.add(int(r.id_a))
            ref_ids.add(int(r.id_b))
        loaded = (await self.db.execute(
            select(ProceduralSkill).where(ProceduralSkill.id.in_(ref_ids))
        )).scalars().all()
        by_id: dict[int, ProceduralSkill] = {s.id: s for s in loaded}

        # Choose winner per pair via the same metric used elsewhere:
        # higher success rate wins; tie-break on more-recent usage.
        out: list[DuplicatePair] = []
        for r in rows:
            winner_id, loser_id = self._pick_winner_from_cache(
                int(r.id_a), int(r.id_b), by_id,
            )
            out.append(DuplicatePair(
                loser_id=loser_id, winner_id=winner_id,
                similarity=float(r.similarity),
            ))
        return out

    @classmethod
    def _pick_winner_from_cache(
        cls,
        id_a: int,
        id_b: int,
        by_id: dict[int, ProceduralSkill],
    ) -> tuple[int, int]:
        """Return (winner, loser) using already-loaded skill rows.

        Higher success rate wins; ties broken by total usage; further
        ties broken by last_used_at. If either id is missing from the
        dict (shouldn't happen — caller bulk-loaded both), preserve the
        (a, b) input order so the caller is no worse off than before.
        """
        a = by_id.get(id_a)
        b = by_id.get(id_b)
        if a is None or b is None:
            return id_a, id_b

        def _rank_key(s: ProceduralSkill) -> tuple[float, int, datetime]:
            total = s.success_count + s.failure_count
            rate = (s.success_count / total) if total > 0 else 0.0
            last_used = s.last_used_at or s.created_at or datetime.min
            return (rate, total, last_used)

        if _rank_key(a) >= _rank_key(b):
            return a.id, b.id
        return b.id, a.id

    # Trigger-count cap on a merged skill — matches the create-schema
    # cap so a re-validation via SkillCreateRequest never 422s.
    _TRIGGER_CAP = 10

    async def merge_pair(self, loser_id: int, winner_id: int) -> None:
        """Combine triggers, archive the loser, bump the winner's version.

        Concurrency safety: a second curator pass (scheduler + manual
        /curator/run can overlap) could otherwise re-merge the same pair
        and double-add the loser's outcome counters. We use
        SELECT ... FOR UPDATE on both rows so the second writer blocks
        until the first commits, then re-checks loser.status and
        winner.merged_into_id and skips the row if either has already
        been mutated by the first pass.

        Embedding is computed BEFORE any row lock so a slow or hung
        Ollama endpoint doesn't hold procedural_skills locks for the
        duration of the embed call. Brief race window: a concurrent
        PATCH that arrives between the embed and the lock-acquire could
        change the winner's body_md; the curator's embedding then doesn't
        reflect the PATCH. The PATCH path always re-embeds itself when
        the body changes, so the staleness is corrected on the next
        write either way.
        """
        # Load both rows without locking just to read title/body for the
        # embedding input. These reads are independent of the mutation
        # transaction.
        loser_preview = (await self.db.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == loser_id)
        )).scalar_one_or_none()
        winner_preview = (await self.db.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == winner_id)
        )).scalar_one_or_none()
        if loser_preview is None or winner_preview is None:
            return

        # Pre-compute the merged trigger set + embedding OUTSIDE any row lock.
        combined = self._combine_triggers(
            winner_preview.trigger_examples or [],
            loser_preview.trigger_examples or [],
        )
        svc = SkillService(self.db)
        new_emb = await svc.compute_embedding_for(
            winner_preview.title, combined, winner_preview.body_md,
        )

        # Now re-load both rows WITH row locks and apply the mutation.
        # If a concurrent writer already merged this pair, bail.
        loser = (await self.db.execute(
            select(ProceduralSkill)
            .where(ProceduralSkill.id == loser_id)
            .with_for_update()
        )).scalar_one_or_none()
        winner = (await self.db.execute(
            select(ProceduralSkill)
            .where(ProceduralSkill.id == winner_id)
            .with_for_update()
        )).scalar_one_or_none()
        if loser is None or winner is None:
            await self.db.rollback()
            return

        # Concurrent-curator guard: if the loser was archived between
        # the pre-load and the lock acquire, another pass already merged
        # it — skip cleanly. Same idea for the winner having been merged
        # into a third row.
        if loser.status != SKILL_STATUS_APPROVED or loser.merged_into_id is not None:
            await self.db.rollback()
            return
        if winner.status != SKILL_STATUS_APPROVED or winner.merged_into_id is not None:
            await self.db.rollback()
            return

        # Use the pre-lock `combined` triggers + `new_emb` together —
        # both were derived from the same pre-lock snapshot, so the
        # embedding is consistent with the trigger list we're about to
        # persist. The previous code re-derived `combined` post-lock
        # (defensive against a PATCH landing in the brief window between
        # pre-load and lock-acquire) but kept the pre-lock embedding,
        # producing a row whose triggers didn't match the vector indexed
        # for it. Concurrent PATCH races on the winner are rare and will
        # be corrected on the next PATCH (which always re-embeds) or
        # next curator pass.

        # Carry over the loser's outcome counts. This biases the winner
        # toward "skill at this concept has X total invocations" — a
        # better signal than treating each duplicate as independent.
        winner.trigger_examples = combined
        winner.success_count += loser.success_count
        winner.failure_count += loser.failure_count
        winner.version += 1
        if new_emb is not None:
            winner.embedding = new_emb

        # Archive the loser.
        loser.status = SKILL_STATUS_ARCHIVED
        loser.merged_into_id = winner.id
        loser.updated_at = datetime.now(UTC).replace(tzinfo=None)

        await self.db.commit()

        # Bust the has-any cache (record_outcome / route handlers do the
        # same on any active-state flip).
        SkillService.invalidate_has_skills_cache()

        logger.info(
            f"🧹 Curator merge: skill #{loser.id} -> #{winner.id} "
            f"(combined {len(combined)} triggers; loser {loser.title!r})"
        )

    @classmethod
    def _combine_triggers(
        cls, winner_triggers: list[str], loser_triggers: list[str],
    ) -> list[str]:
        """Merge two trigger lists, dedup case-insensitively, cap at
        _TRIGGER_CAP entries.

        Cap-application is FIRST on the winner's existing list (so a
        legacy row that already exceeded the cap gets trimmed back to
        the cap before we even look at the loser's contribution) THEN
        on the merged total. Without that pre-trim, a winner already
        at len=12 would only append one loser trigger before the
        `>= 10` check fires, producing a final len=13 — violating the
        documented cap.
        """
        out: list[str] = []
        seen: set[str] = set()
        for t in (winner_triggers or []) + (loser_triggers or []):
            if not t:
                continue
            key = t.strip().lower()
            if not key or key in seen:
                continue
            out.append(t)
            seen.add(key)
            if len(out) >= cls._TRIGGER_CAP:
                break
        return out

    # ============================================================ stale
    async def archive_stale(self, user_id: int) -> int:
        """Soft-archive skills past the retention threshold AND with a
        poor success rate. Returns count archived.

        Pinned skills are never archived. Skills with too few usages
        are also exempt — they may simply not have been tested yet,
        not "stale".

        Filter is pushed into SQL (single UPDATE) so a user with
        hundreds of skills doesn't pay full-row materialization +
        Python-side iteration. The success-rate predicate uses
        ``success_count::float / total`` with a CASE guard to avoid the
        zero-division branch — total is also gated by min_uses (>=1) so
        the divide-by-zero case is structurally impossible, but the
        CASE keeps the SQL portable to test-harness sqlite which
        evaluates the predicate even when min_uses guards short-circuit.
        """
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            days=settings.skill_curator_stale_days
        )
        min_uses = settings.skill_curator_min_uses_to_consider_stale
        max_rate = settings.skill_curator_stale_success_rate
        now = datetime.now(UTC).replace(tzinfo=None)

        total_expr = ProceduralSkill.success_count + ProceduralSkill.failure_count
        rate_expr = case(
            (total_expr > 0,
             ProceduralSkill.success_count * 1.0 / total_expr),
            else_=0.0,
        )
        last_expr = func.coalesce(
            ProceduralSkill.last_used_at, ProceduralSkill.created_at,
        )

        stmt = (
            update(ProceduralSkill)
            .where(
                ProceduralSkill.user_id == user_id,
                ProceduralSkill.status == SKILL_STATUS_APPROVED,
                ProceduralSkill.pinned.is_(False),
                total_expr >= min_uses,
                rate_expr < max_rate,
                last_expr < cutoff,
            )
            .values(status=SKILL_STATUS_ARCHIVED, updated_at=now)
        )
        result = await self.db.execute(stmt)
        archived = int(result.rowcount or 0)

        if archived > 0:
            await self.db.commit()
            SkillService.invalidate_has_skills_cache()
            logger.info(
                f"🧹 Curator archive: {archived} skill(s) "
                f"user={user_id} (stale_days={settings.skill_curator_stale_days})"
            )
        return archived

    # ============================================================ helper
    async def list_active_user_ids(self) -> list[int]:
        """Returns the user_ids that own at least one approved non-seed
        skill — the curator scheduler iterates over this list rather
        than scanning every User row."""
        rows = (await self.db.execute(
            select(ProceduralSkill.user_id).where(
                ProceduralSkill.user_id.isnot(None),
                ProceduralSkill.status == SKILL_STATUS_APPROVED,
                ProceduralSkill.source != SKILL_SOURCE_SEED,
            ).distinct()
        )).all()
        return [r[0] for r in rows if r[0] is not None]

    # ====================================================== orchestrator
    async def run_curator(
        self,
        *,
        triggered_by_user_id: int | None = None,
        run_type: str = CURATOR_RUN_TYPE_SCHEDULED,
    ) -> SkillCuratorRun:
        """Top-level entry point — writes a SkillCuratorRun audit row.

        Runs ``run_for_user`` for every user that owns at least one
        approved non-seed skill, aggregates the counters, and flips the
        audit row from ``running`` to ``success``/``partial``/``failed``
        depending on outcome.

        The scheduler in lifecycle.py calls this with
        ``run_type='scheduled'`` and ``triggered_by_user_id=None``. The
        AdminCuratorPage "Run Now" button calls this from POST
        /api/curator/run with ``run_type='manual'`` and the admin user id.

        A row in ``running`` state older than ~10 minutes is treated as
        ``failed`` in the admin UI — the run was almost certainly killed
        mid-execution (pod restart, crash). We don't try to mark the row
        ourselves because the very thing that killed us would prevent
        the UPDATE.
        """
        if run_type not in (CURATOR_RUN_TYPE_SCHEDULED, CURATOR_RUN_TYPE_MANUAL):
            raise ValueError(f"Invalid curator run_type: {run_type!r}")

        started = datetime.now(UTC).replace(tzinfo=None)
        run_row = SkillCuratorRun(
            started_at=started,
            run_type=run_type,
            triggered_by_user_id=triggered_by_user_id,
            status=CURATOR_RUN_STATUS_RUNNING,
        )
        self.db.add(run_row)
        await self.db.commit()
        await self.db.refresh(run_row)

        user_ids = await self.list_active_user_ids()
        totals = {
            "skills_examined": 0,
            "duplicate_pairs_found": 0,
            "duplicate_pairs_merged": 0,
            "stale_skills_archived": 0,
        }
        notes: list[str] = []

        for uid in user_ids:
            try:
                report = await self.run_for_user(uid)
                totals["duplicate_pairs_found"] += report.duplicates_found
                totals["duplicate_pairs_merged"] += report.merges_applied
                totals["stale_skills_archived"] += report.stale_archived
                notes.extend(report.notes)
            except Exception as e:  # noqa: BLE001
                notes.append(f"user={uid} run_for_user failed: {e}")

        # ``skills_examined`` = approved skills considered (rough count
        # for the audit display; precision is a follow-up if we add a
        # per-user metric).
        examined = (await self.db.execute(
            select(func.count(ProceduralSkill.id)).where(
                ProceduralSkill.status == SKILL_STATUS_APPROVED,
                ProceduralSkill.source != SKILL_SOURCE_SEED,
            )
        )).scalar() or 0
        totals["skills_examined"] = int(examined)

        finished = datetime.now(UTC).replace(tzinfo=None)
        duration = (finished - started).total_seconds()

        if notes and totals["duplicate_pairs_merged"] == 0 and totals["stale_skills_archived"] == 0:
            final_status = CURATOR_RUN_STATUS_FAILED
        elif notes:
            final_status = CURATOR_RUN_STATUS_PARTIAL
        else:
            final_status = CURATOR_RUN_STATUS_SUCCESS

        run_row.finished_at = finished
        run_row.duration_seconds = round(duration, 3)
        run_row.status = final_status
        run_row.skills_examined = totals["skills_examined"]
        run_row.duplicate_pairs_found = totals["duplicate_pairs_found"]
        run_row.duplicate_pairs_merged = totals["duplicate_pairs_merged"]
        run_row.stale_skills_archived = totals["stale_skills_archived"]
        if notes:
            # Keep the error_message bounded — admin UI displays the
            # first ~500 chars. Newline-joined for legibility.
            run_row.error_message = "\n".join(notes)[:2000]

        await self.db.commit()

        logger.info(
            f"🧹 Curator run #{run_row.id} ({run_type}) finished: "
            f"status={final_status}, merged={totals['duplicate_pairs_merged']}, "
            f"archived={totals['stale_skills_archived']}, "
            f"users={len(user_ids)}, duration={duration:.2f}s"
        )
        return run_row
