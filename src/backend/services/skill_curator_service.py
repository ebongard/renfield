"""
SkillCuratorService — periodic skill-corpus maintenance.

Self-learning Phase 4: long-running deployments accumulate skills.
After a few weeks the corpus has near-duplicates (the same procedure
extracted twice with slightly different titles) and stale rows (skills
that worked once and never matched again). The curator job:

  1. find_duplicate_pairs(user_id):
     For every pair of active skills owned by the user, compare their
     embeddings; pairs with cosine similarity >=
     ``settings.skill_curator_duplicate_threshold`` are flagged.
     Pgvector handles the SQL; no LLM in the hot path.

  2. merge_pair(loser, winner):
     Pick the "winner" as the skill with the higher success rate
     (ties broken by more recent last_used_at). Combine their triggers
     (deduped, capped). Bump the winner's version. Mark the loser
     ``is_active=False, merged_into_id=winner.id`` — kept for audit.

  3. archive_stale(user_id):
     Skills not used in ``skill_curator_stale_days`` days that have
     at least ``skill_curator_min_uses_to_consider_stale`` total calls
     AND a success_rate below ``skill_curator_stale_success_rate``
     get soft-archived (is_active=False, no merged_into_id since
     they're not duplicates). Pinned skills are exempt.

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
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ProceduralSkill, SKILL_SOURCE_SEED
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

        sql = text("""
            SELECT a.id AS id_a, b.id AS id_b,
                   1 - (a.embedding <=> b.embedding) AS similarity
            FROM procedural_skills a
            JOIN procedural_skills b
              ON a.id < b.id
             AND a.user_id = b.user_id
             AND a.embedding IS NOT NULL
             AND b.embedding IS NOT NULL
            WHERE a.user_id = :user_id
              AND a.is_active = TRUE
              AND b.is_active = TRUE
              AND a.source <> :seed
              AND b.source <> :seed
              AND (1 - (a.embedding <=> b.embedding)) >= :threshold
            ORDER BY similarity DESC
        """)
        rows = (await self.db.execute(sql, {
            "user_id": user_id,
            "seed": SKILL_SOURCE_SEED,
            "threshold": threshold,
        })).fetchall()

        # Choose winner per pair via the same metric used elsewhere:
        # higher success rate wins; tie-break on more-recent usage.
        out: list[DuplicatePair] = []
        for r in rows:
            winner_id, loser_id = await self._pick_winner(int(r.id_a), int(r.id_b))
            out.append(DuplicatePair(
                loser_id=loser_id, winner_id=winner_id,
                similarity=float(r.similarity),
            ))
        return out

    async def _pick_winner(self, id_a: int, id_b: int) -> tuple[int, int]:
        """Return (winner, loser). Higher success rate wins; ties broken
        by total usage; further ties broken by last_used_at."""
        skills = (await self.db.execute(
            select(ProceduralSkill).where(ProceduralSkill.id.in_([id_a, id_b]))
        )).scalars().all()
        if len(skills) != 2:
            # Shouldn't happen — caller already pulled them from the join
            return id_a, id_b

        def _rank_key(s: ProceduralSkill) -> tuple[float, int, datetime]:
            total = s.success_count + s.failure_count
            rate = (s.success_count / total) if total > 0 else 0.0
            last_used = s.last_used_at or s.created_at or datetime.min
            return (rate, total, last_used)

        a, b = skills
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
        until the first commits, then re-checks loser.is_active and
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
        if not loser.is_active or loser.merged_into_id is not None:
            await self.db.rollback()
            return
        if not winner.is_active or winner.merged_into_id is not None:
            await self.db.rollback()
            return

        # Re-derive combined triggers in case the winner's trigger list
        # changed since the pre-load (defensive — keeps the eventual
        # state consistent with what we observed at lock time, not the
        # stale pre-load snapshot).
        combined = self._combine_triggers(
            winner.trigger_examples or [],
            loser.trigger_examples or [],
        )

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
        loser.is_active = False
        loser.merged_into_id = winner.id
        loser.updated_at = datetime.now(UTC).replace(tzinfo=None)

        await self.db.commit()

        # Bust the has-any cache (record_outcome / route handlers do the
        # same on any active-state flip).
        SkillService._has_skills_cache.clear()

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
        """
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            days=settings.skill_curator_stale_days
        )
        min_uses = settings.skill_curator_min_uses_to_consider_stale
        max_rate = settings.skill_curator_stale_success_rate

        candidates = (await self.db.execute(
            select(ProceduralSkill).where(
                ProceduralSkill.user_id == user_id,
                ProceduralSkill.is_active.is_(True),
                ProceduralSkill.pinned.is_(False),
                # last_used_at IS NULL means never used — also stale.
                # (last_used_at < cutoff) OR (last_used_at IS NULL AND created_at < cutoff)
            )
        )).scalars().all()

        archived = 0
        for s in candidates:
            total = s.success_count + s.failure_count
            if total < min_uses:
                continue
            rate = s.success_count / total if total else 0.0
            if rate >= max_rate:
                continue
            last = s.last_used_at or s.created_at
            if last is None or last >= cutoff:
                continue

            s.is_active = False
            s.updated_at = datetime.now(UTC).replace(tzinfo=None)
            archived += 1
            logger.info(
                f"🧹 Curator archive: skill #{s.id} {s.title!r} "
                f"(rate {s.success_count}/{total}, last_used {last.isoformat()})"
            )

        if archived > 0:
            await self.db.commit()
            SkillService._has_skills_cache.clear()
        return archived

    # ============================================================ helper
    async def list_active_user_ids(self) -> list[int]:
        """Returns the user_ids that own at least one active non-seed
        skill — the curator scheduler iterates over this list rather
        than scanning every User row."""
        rows = (await self.db.execute(
            select(ProceduralSkill.user_id).where(
                ProceduralSkill.user_id.isnot(None),
                ProceduralSkill.is_active.is_(True),
                ProceduralSkill.source != SKILL_SOURCE_SEED,
            ).distinct()
        )).all()
        return [r[0] for r in rows if r[0] is not None]
