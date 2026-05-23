"""
SkillService — CRUD + similarity retrieval for ProceduralSkill atoms.

Mirror of IntentFeedbackService's shape:
  - embed text via the same Ollama client every other Lane-A service uses
  - store with a denormalized circle_tier on the source row
  - retrieve with pgvector cosine similarity, threshold-gated

Two write paths:

  create_auto_extracted(...)  — SkillExtractor calls this from the agent
                                 loop's post-turn background task. Creates
                                 the procedural_skills row AND the atom
                                 registry entry (so circle filters apply).

  load_seed(...)              — SkillSeedLoader calls this at boot for
                                 every .md file in seed_skills/. NO atom
                                 registry entry — seed skills are
                                 system-owned (user_id=NULL, tier=4 public)
                                 and bypass the atoms table on purpose:
                                 they have no per-user owner, so there is
                                 no atom policy to enforce.

One read path:

  find_similar(message, asker_id) — used by agent_service._build_agent_prompt
                                     to inject the top-K skills as procedural
                                     memory. SQL filters to (a) user's own
                                     skills + (b) public seed skills. Tier
                                     filter via circle_sql.circle_filter
                                     once we add procedural_skills to the
                                     circle_sql tier_clause builder; v1
                                     uses the simpler user_id OR tier=4 OR
                                     explicit grant filter inline here.

One outcome path:

  record_outcome(skill_id, success) — agent loop bumps success_count or
                                       failure_count after a turn that
                                       used the skill. Auto-deactivates
                                       below threshold per
                                       settings.skill_auto_demote_*.
"""
from __future__ import annotations

import time
from datetime import datetime, UTC

from loguru import logger
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    ATOM_TYPE_PROCEDURAL_SKILL,
    ProceduralSkill,
    SKILL_SOURCE_AUTO_EXTRACTED,
    SKILL_SOURCE_SEED,
    SKILL_SOURCE_USER_CREATED,
    TIER_PUBLIC,
    TIER_SELF,
)
from services.atom_service import AtomService
from services.atom_types import Atom as AtomDTO
from utils.config import settings
from utils.llm_client import get_embed_client


class SkillService:
    """Procedural-skill CRUD + similarity retrieval. One per AsyncSession."""

    # Process-wide cache so we don't re-query "do we have any skills at all?"
    # on every turn — same trick IntentFeedbackService uses.
    _has_skills_cache: dict[str, tuple[bool, float]] = {}
    _HAS_CACHE_TTL = 30.0

    def __init__(self, db: AsyncSession):
        self.db = db
        self._embed_client = None

    # ------------------------------------------------------------------ embed
    async def _get_embed_client(self):
        if self._embed_client is None:
            self._embed_client = get_embed_client()
        return self._embed_client

    async def _embed(self, text_input: str) -> list[float] | None:
        try:
            client = await self._get_embed_client()
            resp = await client.embeddings(
                model=settings.ollama_embed_model,
                prompt=text_input,
            )
            return list(resp.embedding) if resp and resp.embedding else None
        except Exception as e:
            logger.warning(f"⚠️ Skill embedding failed: {e}")
            return None

    def _embedding_input(
        self,
        title: str,
        trigger_examples: list[str],
        body_md: str,
    ) -> str:
        """The text we actually embed.

        Concat title + triggers + first 200 chars of body — triggers carry
        the user-facing phrasing variants that drive similarity matches,
        the title disambiguates, the body gives a tie-breaker.
        """
        parts = [title.strip()]
        if trigger_examples:
            parts.extend(t.strip() for t in trigger_examples if t)
        if body_md:
            parts.append(body_md.strip()[:200])
        return "\n".join(p for p in parts if p)

    # ------------------------------------------------------------------ has-any
    async def has_any_skills(self, scope_key: str = "global") -> bool:
        """Fast existence check used by the agent prompt builder to short-
        circuit the similarity query when no skills exist."""
        now = time.time()
        cached = self._has_skills_cache.get(scope_key)
        if cached and (now - cached[1]) < self._HAS_CACHE_TTL:
            return cached[0]

        result = await self.db.execute(
            select(func.count(ProceduralSkill.id)).where(
                ProceduralSkill.is_active.is_(True)
            )
        )
        count = result.scalar() or 0
        SkillService._has_skills_cache[scope_key] = (count > 0, now)
        return count > 0

    # ============================================================== writes
    async def create_auto_extracted(
        self,
        *,
        user_id: int,
        title: str,
        body_md: str,
        trigger_examples: list[str],
        tool_sequence: list[str],
        learned_from_conversation_id: int | None = None,
        circle_tier: int = TIER_SELF,
    ) -> ProceduralSkill:
        """Create a skill from an agent-turn extraction.

        Registers an atoms row via AtomService so circle-tier filters and
        explicit grants apply uniformly with all other atom types.
        """
        embedding = await self._embed(
            self._embedding_input(title, trigger_examples, body_md)
        )

        skill = ProceduralSkill(
            user_id=user_id,
            title=title.strip()[:255],
            body_md=body_md,
            trigger_examples=trigger_examples or [],
            tool_sequence=tool_sequence or [],
            source=SKILL_SOURCE_AUTO_EXTRACTED,
            learned_from_conversation_id=learned_from_conversation_id,
            embedding=embedding,
            circle_tier=int(circle_tier),
        )
        self.db.add(skill)
        await self.db.flush()  # need skill.id for the atom source_id

        # Register in atoms table — same pattern AtomService.upsert_atom uses
        # for conversation memories and KG entities. The created_at/updated_at
        # on the DTO are required by the dataclass but ignored by upsert_atom
        # (it writes its own timestamps to the row), so pass current time.
        # upsert_atom also patches procedural_skills.atom_id + circle_tier
        # itself via its generic source-row UPDATE — no follow-up needed.
        now = datetime.now(UTC).replace(tzinfo=None)
        atom = AtomDTO(
            atom_id="",
            atom_type=ATOM_TYPE_PROCEDURAL_SKILL,
            owner_user_id=user_id,
            policy={"tier": int(circle_tier)},
            payload={"skill_id": skill.id},
            created_at=now,
            updated_at=now,
        )
        atom_svc = AtomService(self.db)
        await atom_svc.upsert_atom(atom)

        # Re-read so we hand the caller the post-atom-registration state
        # (skill.atom_id is now set by upsert_atom's source-row UPDATE).
        await self.db.refresh(skill)

        # Bust the has-skills cache so the next prompt build sees this skill.
        SkillService._has_skills_cache.clear()
        logger.info(
            f"🧠 Skill auto-extracted (user={user_id}, tier={circle_tier}): "
            f"{title!r}"
        )
        return skill

    async def create_user_authored(
        self,
        *,
        user_id: int,
        title: str,
        body_md: str,
        trigger_examples: list[str],
        tool_sequence: list[str] | None = None,
        circle_tier: int = TIER_SELF,
    ) -> ProceduralSkill:
        """Manual create from the UI / API. Same atom registration as
        auto-extraction, only the discriminator differs."""
        skill = await self.create_auto_extracted(
            user_id=user_id,
            title=title,
            body_md=body_md,
            trigger_examples=trigger_examples,
            tool_sequence=tool_sequence or [],
            learned_from_conversation_id=None,
            circle_tier=circle_tier,
        )
        # Patch the discriminator without re-doing the atom dance.
        await self.db.execute(
            update(ProceduralSkill)
            .where(ProceduralSkill.id == skill.id)
            .values(source=SKILL_SOURCE_USER_CREATED)
        )
        await self.db.commit()
        return skill

    async def load_seed(
        self,
        *,
        title: str,
        body_md: str,
        trigger_examples: list[str],
        tool_sequence: list[str] | None = None,
    ) -> ProceduralSkill | None:
        """Insert a system-owned seed skill (no atom registry entry).

        Idempotent: if a seed with the same title already exists, skip.
        Seed skills are public-tier (visible to all users) and owned by
        no user — see class docstring.
        """
        existing = await self.db.execute(
            select(ProceduralSkill).where(
                ProceduralSkill.source == SKILL_SOURCE_SEED,
                ProceduralSkill.title == title.strip()[:255],
            )
        )
        if existing.scalar_one_or_none() is not None:
            return None  # already loaded — boot is repeatable

        embedding = await self._embed(
            self._embedding_input(title, trigger_examples, body_md)
        )
        skill = ProceduralSkill(
            user_id=None,
            title=title.strip()[:255],
            body_md=body_md,
            trigger_examples=trigger_examples or [],
            tool_sequence=tool_sequence or [],
            source=SKILL_SOURCE_SEED,
            embedding=embedding,
            atom_id=None,
            circle_tier=TIER_PUBLIC,
        )
        self.db.add(skill)
        await self.db.commit()
        SkillService._has_skills_cache.clear()
        logger.info(f"🌱 Seed skill loaded: {title!r}")
        return skill

    # =============================================================== reads
    async def find_similar(
        self,
        message: str,
        asker_id: int | None,
        *,
        top_k: int | None = None,
        threshold: float | None = None,
    ) -> list[dict]:
        """Return top-K active skills closest to the message.

        Visibility filter (matches the AUTH_ENABLED=true contract):
          - asker's own skills (user_id = asker_id), OR
          - public seed skills (user_id IS NULL AND circle_tier = 4)

        AUTH_ENABLED=false short-circuits to "all active skills" — same
        single-user fallback the other retrieval modules use.

        v1 does NOT yet honor tier-reach via circle memberships for
        cross-user skills. That's a follow-up; today, skills owned by
        another user are not visible to anyone but their owner regardless
        of household tier. Acceptable because the only path that creates
        skills with tier > 0 is manual user authoring.
        """
        if top_k is None:
            top_k = settings.skill_inject_top_k
        if threshold is None:
            threshold = settings.skill_inject_similarity_threshold

        if not await self.has_any_skills():
            return []

        embedding = await self._embed(message)
        if embedding is None:
            return []

        embedding_str = f"[{','.join(map(str, embedding))}]"

        # Visibility filter — see docstring. The :asker IS NULL branch
        # makes AUTH_ENABLED=false (no asker) collapse to "all active".
        sql = text("""
            SELECT
                id, title, body_md, trigger_examples, tool_sequence,
                source, success_count, failure_count,
                1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM procedural_skills
            WHERE is_active = TRUE
              AND embedding IS NOT NULL
              AND (
                :asker IS NULL
                OR user_id = :asker
                OR (user_id IS NULL AND circle_tier = 4)
              )
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """)
        rows = (await self.db.execute(sql, {
            "embedding": embedding_str,
            "asker": asker_id,
            "limit": top_k * 2,  # over-fetch then threshold-filter
        })).fetchall()

        out: list[dict] = []
        for r in rows:
            sim = float(r.similarity) if r.similarity is not None else 0.0
            if sim < threshold:
                continue
            out.append({
                "id": r.id,
                "title": r.title,
                "body_md": r.body_md,
                "trigger_examples": r.trigger_examples or [],
                "tool_sequence": r.tool_sequence or [],
                "source": r.source,
                "success_count": r.success_count,
                "failure_count": r.failure_count,
                "similarity": round(sim, 3),
            })
            if len(out) >= top_k:
                break
        return out

    # =========================================================== outcomes
    async def record_outcome(self, skill_id: int, success: bool) -> None:
        """Bump success_count or failure_count after a turn that used this skill.

        Auto-demotes (is_active=False) when failure_count >= the configured
        threshold AND the rolling success rate drops below the floor.
        Pinned skills are never auto-demoted — they must be explicitly
        deactivated by the owner. Curator (Phase 4) may later promote
        archived skills back if usage warrants.
        """
        col = ProceduralSkill.success_count if success else ProceduralSkill.failure_count
        await self.db.execute(
            update(ProceduralSkill)
            .where(ProceduralSkill.id == skill_id)
            .values({col: col + 1, "last_used_at": datetime.now(UTC).replace(tzinfo=None)})
        )

        skill = (await self.db.execute(
            select(ProceduralSkill).where(ProceduralSkill.id == skill_id)
        )).scalar_one_or_none()
        if skill is None:
            await self.db.commit()
            return

        total = skill.success_count + skill.failure_count
        if (
            not skill.pinned
            and skill.failure_count >= settings.skill_auto_demote_threshold
            and total > 0
            and (skill.success_count / total) < settings.skill_auto_demote_success_rate
        ):
            skill.is_active = False
            logger.warning(
                f"🧠 Skill {skill.id} auto-demoted (success_rate "
                f"{skill.success_count}/{total}): {skill.title!r}"
            )
        await self.db.commit()

    # ============================================================ format
    def format_for_prompt(self, skills: list[dict], lang: str = "de") -> str:
        """Render a list of skills as a procedural-memory block for the
        agent prompt. Empty list → empty string (clean placeholder)."""
        if not skills:
            return ""

        if lang == "en":
            header = (
                "LEARNED PROCEDURES — apply if the current request matches one of "
                "these (you've handled similar requests this way before):"
            )
            tool_label = "Tools"
        else:
            header = (
                "GELERNTE PROZEDUREN — wenn die aktuelle Anfrage zu einer dieser "
                "passt, wende sie an (du hast aehnliche Anfragen so geloest):"
            )
            tool_label = "Tools"

        out = [header]
        for s in skills:
            tools = s.get("tool_sequence") or []
            triggers = s.get("trigger_examples") or []
            body = (s.get("body_md") or "").strip()
            out.append(f"\n### {s['title']}")
            if triggers:
                out.append("Trigger: " + ", ".join(f'"{t}"' for t in triggers[:3]))
            if tools:
                out.append(f"{tool_label}: {', '.join(tools)}")
            if body:
                out.append(body)
        return "\n".join(out)
