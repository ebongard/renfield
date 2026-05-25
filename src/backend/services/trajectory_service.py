"""
TrajectoryService — capture + export full agent-turn traces.

Self-learning Phase 2: every agent turn that completes (success, partial,
or controlled abort) gets persisted as a row in ``agent_trajectories``
when ``settings.trajectory_capture_enabled=true``. Rows are exported as
JSONL via ``/api/trajectories/export`` for downstream LoRA fine-tuning
of the local model.

Three call sites:

  save(...) — invoked from ``_post_turn_skill_bookkeeping`` in the same
              fire-and-forget post-turn task. Single INSERT; never blocks
              the response path; failures are logged and swallowed.

  list(...) — admin-only paged listing, filter by user_id / outcome /
              date range.

  export_jsonl(...) — async-iterable yielding one JSON object per row.
              Streamed via FastAPI's StreamingResponse so a 50k-row
              corpus doesn't buffer in memory.

  purge_expired(...) — invoked by the cleanup scheduler. Deletes rows
              older than ``trajectory_retention_days`` that are NOT
              flagged for retention.

PII / consent: v1 ships ``redacted_payload`` as a separate nullable
column. Producers only fill ``raw_payload`` today; Phase 4 will add the
PII-scrubber that fills ``redacted_payload`` and gate the export route
on its presence.
"""
from __future__ import annotations

from collections.abc import AsyncIterable
from datetime import datetime, timedelta, UTC
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    AgentTrajectory,
    TRAJECTORY_OUTCOME_ABORT,
    TRAJECTORY_OUTCOME_SUCCESS,
    TRAJECTORY_OUTCOME_TOOL_FAIL,
)
from utils.config import settings


_OUTCOME_CHOICES = {
    TRAJECTORY_OUTCOME_SUCCESS,
    TRAJECTORY_OUTCOME_TOOL_FAIL,
    TRAJECTORY_OUTCOME_ABORT,
}

# Per-step content cap. A 4kB ceiling keeps a single attachment-heavy
# trace from bloating a row to multi-MB while leaving room for the LLM
# to see real tool output in the export. Centralized so the storage
# policy is reviewable as a unit alongside trajectory_max_per_user etc.
_STEP_CONTENT_MAX_CHARS = 4000


def _allowed_outcomes() -> set[str]:
    """Parse settings.trajectory_capture_outcomes once per call.

    Comma-separated; whitespace tolerant; unknown tokens are dropped.
    Empty string means "capture nothing", which is more useful as a
    runtime kill-switch than `trajectory_capture_enabled=false` because
    settings flips don't require restart only if read at call time —
    so we re-parse on every save (cheap string op).
    """
    raw = (settings.trajectory_capture_outcomes or "").strip()
    if not raw:
        return set()
    parsed = {tok.strip() for tok in raw.split(",")}
    return parsed & _OUTCOME_CHOICES


def outcome_from_steps(steps: list) -> str:
    """Derive the trajectory outcome label from an AgentContext.steps list.

    Conservative on the success classification: only count a tool_result
    as successful when `.success` is EXPLICITLY True. Missing or None
    (executor races, partially-evaluated results, tools that don't set
    the field at all) count as failures so we don't silently train the
    fine-tuning corpus on mislabeled positives.
    """
    has_final = any(getattr(s, "step_type", "") == "final_answer" for s in steps)
    has_error = any(getattr(s, "step_type", "") == "error" for s in steps)
    tool_results = [s for s in steps if getattr(s, "step_type", "") == "tool_result"]
    any_tool_fail = any(getattr(s, "success", None) is not True for s in tool_results)

    if not has_final:
        return TRAJECTORY_OUTCOME_ABORT
    if has_error or any_tool_fail:
        return TRAJECTORY_OUTCOME_TOOL_FAIL
    return TRAJECTORY_OUTCOME_SUCCESS


def serialize_steps(steps: list) -> list[dict]:
    """Render AgentStep dataclass instances as JSON-safe dicts.

    The agent context's blob_store / blob_meta sidecar is NOT included
    here — binary attachments shouldn't enter training data. We also
    truncate ``content`` to 4000 chars per step so a single attachment-
    heavy trace doesn't bloat a row to multi-MB.
    """
    out: list[dict] = []
    for s in steps:
        step_dict = {
            "step_number": getattr(s, "step_number", None),
            "step_type": getattr(s, "step_type", None),
            "tool": getattr(s, "tool", None),
            "parameters": getattr(s, "parameters", None),
            "reason": getattr(s, "reason", None),
            "success": getattr(s, "success", None),
        }
        content = getattr(s, "content", "") or ""
        if len(content) > _STEP_CONTENT_MAX_CHARS:
            content = content[:_STEP_CONTENT_MAX_CHARS] + "…[truncated]"
        step_dict["content"] = content
        out.append(step_dict)
    return out


class TrajectoryService:
    """CRUD + export + cleanup for AgentTrajectory rows."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ============================================================== save
    async def save(
        self,
        *,
        user_id: int | None,
        conversation_id: int | None,
        user_message: str,
        steps: list,
        lang: str,
        tools_available: list[str] | None = None,
        used_skill_ids: list[int] | None = None,
        extracted_skill_id: int | None = None,
        token_count: int | None = None,
    ) -> AgentTrajectory | None:
        """Persist a single agent-turn trajectory.

        Returns the inserted row, or None if the outcome wasn't in the
        capture-set or the feature is disabled.
        """
        if not settings.trajectory_capture_enabled:
            return None

        outcome = outcome_from_steps(steps)
        allowed = _allowed_outcomes()
        if outcome not in allowed:
            return None

        tool_calls = [
            s for s in steps
            if getattr(s, "step_type", "") == "tool_call"
        ]
        tools_in_turn = [c.tool for c in tool_calls if getattr(c, "tool", None)]
        final_answer = next(
            (getattr(s, "content", "") for s in steps
             if getattr(s, "step_type", "") == "final_answer"),
            "",
        )

        raw_payload = {
            "schema_version": 1,
            "user_message": user_message,
            "lang": lang,
            "tools_available": list(tools_available or []),
            "steps": serialize_steps(steps),
            "final_answer": final_answer,
            "captured_at": datetime.now(UTC).isoformat(),
        }

        row = AgentTrajectory(
            user_id=user_id,
            conversation_id=conversation_id,
            raw_payload=raw_payload,
            outcome=outcome,
            tool_count=len(tool_calls),
            distinct_tool_count=len(set(tools_in_turn)),
            token_count=token_count,
            extracted_skill_id=extracted_skill_id,
            used_skill_ids=used_skill_ids or [],
            # Flag as "keep forever" when this turn produced a skill —
            # those are gold examples for the future fine-tuning corpus.
            flagged_for_retention=(extracted_skill_id is not None),
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)

        # Soft-cap per-user: drop oldest non-flagged when the user crosses
        # the cap. Anonymous turns (user_id IS NULL) share one bucket —
        # otherwise single-user/AUTH_ENABLED=false deployments would grow
        # the trajectory table unbounded between the daily cleanup ticks.
        #
        # Probabilistic: COUNT-then-DELETE every save would cost N round-
        # trips for N inserts even though only the last few near the cap
        # actually matter. We run the check on every Nth insert (keyed by
        # row.id, deterministic so test-harness behavior is predictable);
        # drift up to N rows over the cap is harmless because the cleanup
        # scheduler also prunes by retention.
        if (
            settings.trajectory_max_per_user > 0
            and row.id % settings.trajectory_cap_check_every == 0
        ):
            await self._enforce_per_user_cap(user_id)

        return row

    async def _enforce_per_user_cap(self, user_id: int | None) -> None:
        """Drop oldest non-flagged rows when this user is over cap.

        Two queries (COUNT + DELETE-with-subquery), down from three. The
        previous SELECT-then-DELETE materialized victim IDs in Python
        between statements; this version pushes the victim selection
        into a subquery the DELETE consumes directly, so the row IDs
        never round-trip. The COUNT stays out front because we need to
        short-circuit cleanly when under cap (the DELETE-with-subquery
        would still execute LIMIT 0, but COUNT is the cheaper gate).
        """
        cap = settings.trajectory_max_per_user
        # SQLAlchemy's bound `col == None_var` produces SQL `col = NULL`
        # which is UNKNOWN and matches no rows; use IS NULL explicitly.
        if user_id is None:
            user_filter = AgentTrajectory.user_id.is_(None)
        else:
            user_filter = AgentTrajectory.user_id == user_id

        count = (await self.db.execute(
            select(func.count(AgentTrajectory.id)).where(user_filter)
        )).scalar() or 0
        if count <= cap:
            return

        excess = count - cap
        victims_sq = (
            select(AgentTrajectory.id)
            .where(
                user_filter,
                AgentTrajectory.flagged_for_retention.is_(False),
            )
            .order_by(AgentTrajectory.created_at.asc())
            .limit(excess)
            .scalar_subquery()
        )
        result = await self.db.execute(
            delete(AgentTrajectory).where(AgentTrajectory.id.in_(victims_sq))
        )
        if result.rowcount:
            await self.db.commit()

    # ============================================================== read
    async def list_for_admin(
        self,
        *,
        user_id: int | None = None,
        outcome: str | None = None,
        flagged_only: bool = False,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentTrajectory]:
        stmt = select(AgentTrajectory)
        if user_id is not None:
            stmt = stmt.where(AgentTrajectory.user_id == user_id)
        if outcome:
            stmt = stmt.where(AgentTrajectory.outcome == outcome)
        if flagged_only:
            stmt = stmt.where(AgentTrajectory.flagged_for_retention.is_(True))
        if since is not None:
            stmt = stmt.where(AgentTrajectory.created_at >= since)
        stmt = (stmt.order_by(AgentTrajectory.created_at.desc())
                    .limit(limit).offset(offset))
        return list((await self.db.execute(stmt)).scalars().all())

    async def export_jsonl(
        self,
        *,
        outcome: str | None = None,
        since: datetime | None = None,
        flagged_only: bool = False,
        batch_size: int = 500,
        require_redacted: bool = False,
    ) -> AsyncIterable[dict[str, Any]]:
        """Yield trajectory rows as plain dicts suitable for json.dumps.

        Streamed in pages of ``batch_size`` so a 50k-row export doesn't
        load the corpus into memory. The caller (the route handler)
        wraps each dict in ``json.dumps(...) + "\\n"`` for line-delimited
        JSONL output.

        When ``require_redacted=True``, rows whose ``redacted_payload``
        is NULL are skipped — gate for downstream consumers that won't
        accept the raw payload. Phase 4 will flip this default.
        """
        last_id = 0
        while True:
            stmt = select(AgentTrajectory).where(AgentTrajectory.id > last_id)
            if outcome:
                stmt = stmt.where(AgentTrajectory.outcome == outcome)
            if since is not None:
                stmt = stmt.where(AgentTrajectory.created_at >= since)
            if flagged_only:
                stmt = stmt.where(AgentTrajectory.flagged_for_retention.is_(True))
            if require_redacted:
                stmt = stmt.where(AgentTrajectory.redacted_payload.isnot(None))
            stmt = stmt.order_by(AgentTrajectory.id.asc()).limit(batch_size)

            batch = list((await self.db.execute(stmt)).scalars().all())
            if not batch:
                return

            # Snapshot the data we need OUT of the ORM rows before
            # expunging — yield-as-plain-dicts means the consumer never
            # touches the ORM after we release these rows.
            for row in batch:
                payload = row.redacted_payload if require_redacted else row.raw_payload
                yield {
                    "id": row.id,
                    "user_id": row.user_id,
                    "outcome": row.outcome,
                    "tool_count": row.tool_count,
                    "distinct_tool_count": row.distinct_tool_count,
                    "token_count": row.token_count,
                    "extracted_skill_id": row.extracted_skill_id,
                    "used_skill_ids": row.used_skill_ids or [],
                    "flagged_for_retention": row.flagged_for_retention,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "trace": payload,
                }
            last_id = batch[-1].id

            # Bound the identity-map size — without this a 50k-row export
            # accumulates 50k ORM instances (each carrying a multi-KB JSON
            # payload) in memory, defeating the streaming design. Expunge
            # drops them from the identity map after we've handed off the
            # plain dicts.
            self.db.expunge_all()

    # ============================================================ cleanup
    async def purge_expired(self) -> int:
        """Delete rows older than ``trajectory_retention_days`` unless
        ``flagged_for_retention=True``. Returns the number of rows deleted.
        """
        if settings.trajectory_retention_days <= 0:
            return 0
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            days=settings.trajectory_retention_days
        )
        result = await self.db.execute(
            delete(AgentTrajectory).where(
                AgentTrajectory.created_at < cutoff,
                AgentTrajectory.flagged_for_retention.is_(False),
            )
        )
        await self.db.commit()
        deleted = result.rowcount or 0
        if deleted > 0:
            logger.info(
                f"🧹 Trajectory cleanup: removed {deleted} expired row(s) "
                f"older than {settings.trajectory_retention_days} days"
            )
        return deleted
