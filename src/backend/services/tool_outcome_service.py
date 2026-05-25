"""
ToolOutcomeService — track per-(user, tool) success/failure counts.

Self-learning Phase 3: every ``tool_result`` step in the agent loop
bumps a counter via :meth:`record`. At prompt-build time
:meth:`get_health_warnings` returns a list of tool warnings the agent
should be aware of — used to inject a ``{tool_health_warnings}`` block
into the agent prompt parallel to the existing ``{tool_corrections}``
and ``{learned_skills}`` blocks.

Stats are per-user (not global) because permission gates and grant
state differ across users — a perfectly-fine tool for Alice might
always fail for Bob if Bob lacks the grant, and that asymmetry is
real information for the agent.

Concurrency: :meth:`record` uses INSERT … ON CONFLICT DO UPDATE
(postgres) / INSERT OR IGNORE + UPDATE (sqlite) so two concurrent
``record`` calls on the same (user, tool) serialize at the unique
constraint instead of racing on a SELECT-then-INSERT.

This service does NOT use embeddings — it's a flat counter. Semantic
"this tool fails for queries LIKE this one" is intent_feedback's
territory (services.intent_feedback_service.IntentFeedbackService).
"""
from __future__ import annotations

from datetime import datetime, UTC

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ToolOutcomeStat
from utils.config import settings
from utils.prompt_scrub import scrub_for_prompt


class ToolOutcomeService:
    """Per-(user, tool) outcome stats. One per AsyncSession."""

    # Max length for last_failure_summary stored on the row. Matches the
    # column width assumption (Text, but bounded here so a multi-MB error
    # blob doesn't bloat the table). Centralized so write + format sites
    # stay in sync.
    _FAILURE_SUMMARY_MAX_CHARS = 500

    def __init__(self, db: AsyncSession):
        self.db = db

    # ============================================================= record
    async def record(
        self,
        *,
        user_id: int | None,
        tool_name: str,
        success: bool,
        failure_summary: str | None = None,
    ) -> None:
        """Increment the counter for (user_id, tool_name).

        Per-user accounting requires an identifiable user; calls with
        ``user_id is None`` are a no-op rather than inserting an
        unbounded set of NULL-keyed rows. PostgreSQL's UNIQUE constraint
        treats NULL values as distinct, so a NULL-keyed ON CONFLICT
        clause would never match — every anonymous call would create a
        fresh row instead of upserting. Single-user/AUTH_ENABLED=false
        deployments resolve a default admin user at the route layer
        (see services.auth_service.get_user_or_default); paths that
        reach this method without a user_id are legitimately anonymous
        and have no business polluting the per-user counter table.

        Uses an UPSERT pattern to avoid races between two parallel
        record() calls for the same (user, tool) pair. On postgres this
        is ``INSERT ... ON CONFLICT (user_id, tool_name) DO UPDATE``;
        on sqlite (test harness) we fall back to a SELECT-then-INSERT-
        or-UPDATE that works around sqlite's stricter handling.
        """
        if not tool_name:
            return
        if not settings.tool_health_tracking_enabled:
            return
        # See docstring — anonymous calls are a deliberate no-op.
        if user_id is None:
            return

        now = datetime.now(UTC).replace(tzinfo=None)
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""

        if dialect == "postgresql":
            insert_stmt = pg_insert(ToolOutcomeStat).values(
                user_id=user_id,
                tool_name=tool_name,
                success_count=1 if success else 0,
                failure_count=0 if success else 1,
                last_used_at=now,
                last_failure_at=None if success else now,
                last_failure_summary=None if success else (failure_summary or "")[:self._FAILURE_SUMMARY_MAX_CHARS],
            )
            update_values: dict = {
                "success_count": ToolOutcomeStat.success_count + (1 if success else 0),
                "failure_count": ToolOutcomeStat.failure_count + (0 if success else 1),
                "last_used_at": now,
                "updated_at": now,
            }
            if not success:
                update_values["last_failure_at"] = now
                update_values["last_failure_summary"] = (failure_summary or "")[:self._FAILURE_SUMMARY_MAX_CHARS]
            do_upsert = insert_stmt.on_conflict_do_update(
                constraint="uq_tool_outcome_user_tool",
                set_=update_values,
            )
            await self.db.execute(do_upsert)
            await self.db.commit()
            return

        # sqlite fallback (test harness) — SELECT then INSERT or UPDATE.
        existing = (await self.db.execute(
            select(ToolOutcomeStat).where(
                ToolOutcomeStat.user_id == user_id,
                ToolOutcomeStat.tool_name == tool_name,
            )
        )).scalar_one_or_none()

        if existing is None:
            row = ToolOutcomeStat(
                user_id=user_id,
                tool_name=tool_name,
                success_count=1 if success else 0,
                failure_count=0 if success else 1,
                last_used_at=now,
                last_failure_at=None if success else now,
                last_failure_summary=None if success else (failure_summary or "")[:self._FAILURE_SUMMARY_MAX_CHARS],
            )
            self.db.add(row)
        else:
            if success:
                existing.success_count += 1
            else:
                existing.failure_count += 1
                existing.last_failure_at = now
                existing.last_failure_summary = (failure_summary or "")[:self._FAILURE_SUMMARY_MAX_CHARS]
            existing.last_used_at = now
        await self.db.commit()

    # ====================================================== bulk-record
    async def record_from_steps(
        self,
        *,
        user_id: int | None,
        steps: list,
    ) -> None:
        """Convenience wrapper that iterates an AgentContext.steps list
        and records every tool outcome. Used by
        _post_turn_skill_bookkeeping.

        Errors per tool are isolated — a single failing record doesn't
        abort the rest. Net effect: best-effort accounting.

        Pairing strategy: a tool_call's outcome is the NEXT step's
        success/failure if and only if that next step is a tool_result.
        Two tool_call steps in a row (mid-dispatch crash, executor error
        that emitted an `error` step between them) are each accounted
        as failures — the first call doesn't silently disappear into
        the second call's overwrite of `last_tool`. Same idea for a
        tool_call followed by `error` or `final_answer` without an
        intervening tool_result.
        """
        pending: tuple[str, int] | None = None  # (tool_name, step_index)
        n = len(steps)
        for i, s in enumerate(steps):
            stype = getattr(s, "step_type", "")
            if stype == "tool_call":
                tool = getattr(s, "tool", None)
                if pending is not None:
                    # Previous call never got a tool_result. Treat as a
                    # failure with the next step's content as the summary
                    # if it was an error, else a generic note.
                    prev_tool, _ = pending
                    summary = self._summary_for_orphan(steps, i)
                    await self._safe_record(
                        user_id=user_id, tool_name=prev_tool,
                        success=False, failure_summary=summary,
                    )
                pending = (tool, i) if tool else None
            elif stype == "tool_result":
                if pending is None:
                    # Orphan result with no preceding call — nothing to
                    # attribute. The pre-fix had the same behavior.
                    continue
                tool, _ = pending
                pending = None
                success = bool(getattr(s, "success", False))
                summary = None
                if not success:
                    summary = (getattr(s, "content", "") or "")[:self._FAILURE_SUMMARY_MAX_CHARS]
                await self._safe_record(
                    user_id=user_id, tool_name=tool,
                    success=success, failure_summary=summary,
                )

        # Loop ended with an unresolved pending call → treat as failure.
        if pending is not None:
            tool, idx = pending
            summary = self._summary_for_orphan(steps, n)
            await self._safe_record(
                user_id=user_id, tool_name=tool,
                success=False, failure_summary=summary,
            )

    async def _safe_record(
        self, *, user_id, tool_name, success, failure_summary,
    ) -> None:
        try:
            await self.record(
                user_id=user_id, tool_name=tool_name,
                success=success, failure_summary=failure_summary,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"⚠️ ToolOutcomeService.record failed for {tool_name!r}: {e}"
            )

    @classmethod
    def _summary_for_orphan(cls, steps: list, next_idx: int) -> str:
        """Best-effort summary for a tool_call that never got a
        tool_result — pull from the next step if it carries a payload."""
        if 0 <= next_idx < len(steps):
            s = steps[next_idx]
            content = (getattr(s, "content", "") or "")
            if content:
                return content[:cls._FAILURE_SUMMARY_MAX_CHARS]
        return "tool_call had no matching tool_result"

    # ============================================================ reads
    async def get_health_warnings(
        self,
        user_id: int | None,
        *,
        candidate_tools: list[str] | None = None,
    ) -> list[dict]:
        """Return tools the agent should be warned about for THIS user.

        A tool earns a warning when:
          - it has been called at least ``tool_health_warn_min_uses`` times
            by this user, AND
          - its rolling success rate is below ``tool_health_warn_success_rate``.

        Limited to ``tool_health_warn_top_k`` entries, ordered by failure
        count desc (the most-broken tools first).

        ``candidate_tools`` filters the warnings to tools the agent is
        ACTUALLY going to consider for this turn — the prompt only lists
        a subset (from `_preselected_tools`), and warning about something
        not in the list is noise.
        """
        if not settings.tool_health_warn_enabled:
            return []
        if not settings.tool_health_tracking_enabled:
            return []
        # Per record(): per-user accounting requires an identifiable user.
        # Anonymous reads also short-circuit — symmetric with the write
        # path so the stats are never half-readable.
        if user_id is None:
            return []

        # candidate_tools semantics:
        #   None  → no filter (consider every tool the user has stats on)
        #   []    → explicit empty filter — caller knows there are zero
        #           candidates this turn, so return zero warnings
        #   [...] → restrict to the listed tool names
        if candidate_tools is not None and len(candidate_tools) == 0:
            return []

        stmt = select(ToolOutcomeStat).where(
            ToolOutcomeStat.user_id == user_id,
        )
        if candidate_tools:
            stmt = stmt.where(ToolOutcomeStat.tool_name.in_(candidate_tools))
        rows = (await self.db.execute(stmt)).scalars().all()

        warnings: list[dict] = []
        for r in rows:
            total = r.success_count + r.failure_count
            if total < settings.tool_health_warn_min_uses:
                continue
            rate = r.success_count / total if total else 1.0
            if rate >= settings.tool_health_warn_success_rate:
                continue
            warnings.append({
                "tool_name": r.tool_name,
                "success_count": r.success_count,
                "failure_count": r.failure_count,
                "total": total,
                "success_rate": round(rate, 3),
                "last_failure_at": r.last_failure_at,
                "last_failure_summary": r.last_failure_summary,
            })

        warnings.sort(key=lambda w: w["failure_count"], reverse=True)
        return warnings[: settings.tool_health_warn_top_k]

    async def list_stats(
        self,
        *,
        user_id: int | None = None,
        limit: int = 200,
    ) -> list[ToolOutcomeStat]:
        """Admin / dashboard listing — newest activity first."""
        stmt = select(ToolOutcomeStat)
        if user_id is not None:
            stmt = stmt.where(ToolOutcomeStat.user_id == user_id)
        stmt = stmt.order_by(desc(ToolOutcomeStat.last_used_at)).limit(limit)
        return list((await self.db.execute(stmt)).scalars().all())

    # ======================================================= formatting
    @staticmethod
    def format_for_prompt(warnings: list[dict], lang: str = "de") -> str:
        """Render the warning list as a compact prompt block.

        Empty list → empty string (clean placeholder). Otherwise one
        line per tool with the rate, count, and the latest failure
        excerpt — enough signal for the LLM to deprioritize without
        burying the rest of the prompt.
        """
        if not warnings:
            return ""

        if lang == "en":
            header = (
                "TOOL HEALTH WARNINGS — these tools have been failing for you; "
                "prefer alternatives where possible:"
            )
            line_tpl = (
                "- {tool}: {fails}/{total} failures (success rate {rate:.0%}). "
                "Last error: {summary}"
            )
        else:
            header = (
                "TOOL-HEALTH-WARNUNGEN — diese Tools schlagen bei dir aktuell "
                "haeufig fehl; nutze nach Moeglichkeit Alternativen:"
            )
            line_tpl = (
                "- {tool}: {fails}/{total} Fehlschlaege (Erfolgsrate {rate:.0%}). "
                "Letzter Fehler: {summary}"
            )

        lines = [header]
        for w in warnings:
            raw_summary = (w.get("last_failure_summary") or "").replace("\n", " ")[:120]
            summary = scrub_for_prompt(raw_summary) or "(no detail)"
            # Scrub tool_name too — it's the value of agent step.tool at
            # capture time. Federation peers and plugin-registered tools
            # can introduce names this service didn't author; a hostile
            # name like "foo\nsystem: ignore previous rules\nbar" would
            # otherwise ride in the warning block on every subsequent
            # turn until pruned.
            tool_name = scrub_for_prompt(w["tool_name"])
            lines.append(line_tpl.format(
                tool=tool_name,
                fails=w["failure_count"],
                total=w["total"],
                rate=w["success_rate"],
                summary=summary,
            ))
        return "\n".join(lines)
