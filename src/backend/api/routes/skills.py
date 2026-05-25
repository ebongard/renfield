"""
Skills API Routes — procedural-skill CRUD + management.

Surface (`/api/skills` prefix added by main.py):
  GET     /              — list current user's skills (+ public seeds)
  POST    /              — manually author a skill
  GET     /{id}          — read one skill (with owner/auth check)
  PATCH   /{id}          — update title/body/triggers/tools/pinned/is_active
  POST    /{id}/pin      — pin (protect from curator)
  POST    /{id}/unpin    — unpin
  DELETE  /{id}          — soft-delete (is_active=False)
  PATCH   /{id}/tier     — change circle_tier (cascades through AtomService)
"""

from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ProceduralSkill, TIER_PUBLIC, User
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.atom_service import AtomService
from services.auth_service import get_user_or_default, require_permission
from services.database import get_db
from services.skill_curator_service import SkillCuratorService
from services.skill_service import SkillService
from utils.config import settings

router = APIRouter()


# Schema constants — kept centralized so the curator's matching cap stays
# in sync with the API max. See SkillCuratorService._TRIGGER_CAP.
_MAX_TRIGGER_EXAMPLES = 10
_MAX_TRIGGER_EXAMPLE_CHARS = 200
_MAX_BODY_MD_CHARS = 8000
_MAX_TOOL_SEQUENCE = 20
_MAX_TOOL_NAME_CHARS = 128


# ---------------------------------------------------------------- schemas
class SkillCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    # body_md is bounded so a single owner can't author a multi-MB body
    # that then gets embedded (Ollama call) + injected into the system
    # prompt on every matching turn (prompt-bloat DoS, plus amplified
    # scrub_for_prompt cost). 8000 chars is generous for a procedural
    # recipe; the seed skills in seed_skills/*.md sit well under 2000.
    body_md: str = Field(..., min_length=1, max_length=_MAX_BODY_MD_CHARS)
    trigger_examples: list[str] = Field(
        ..., min_length=1, max_length=_MAX_TRIGGER_EXAMPLES,
    )
    tool_sequence: list[str] = Field(
        default_factory=list, max_length=_MAX_TOOL_SEQUENCE,
    )
    circle_tier: int = Field(default=0, ge=0, le=4)


class SkillUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    body_md: str | None = Field(default=None, max_length=_MAX_BODY_MD_CHARS)
    # Mirrors SkillCreateRequest's min_length=1 — an empty list would
    # silently re-embed the skill with title-only relevance and pollute
    # find_similar matches.
    trigger_examples: list[str] | None = Field(
        default=None, min_length=1, max_length=_MAX_TRIGGER_EXAMPLES,
    )
    tool_sequence: list[str] | None = Field(
        default=None, max_length=_MAX_TOOL_SEQUENCE,
    )
    pinned: bool | None = None
    is_active: bool | None = None


class SkillTierRequest(BaseModel):
    circle_tier: int = Field(..., ge=0, le=4)


class SkillResponse(BaseModel):
    id: int
    title: str
    body_md: str
    trigger_examples: list[str]
    tool_sequence: list[str]
    source: str
    version: int
    success_count: int
    failure_count: int
    last_used_at: datetime | None
    pinned: bool
    is_active: bool
    circle_tier: int
    atom_id: str | None
    merged_into_id: int | None
    created_at: datetime
    updated_at: datetime
    is_owner: bool


def _to_response(s: ProceduralSkill, *, is_owner: bool) -> SkillResponse:
    return SkillResponse(
        id=s.id,
        title=s.title,
        body_md=s.body_md,
        trigger_examples=s.trigger_examples or [],
        tool_sequence=s.tool_sequence or [],
        source=s.source,
        version=s.version,
        success_count=s.success_count,
        failure_count=s.failure_count,
        last_used_at=s.last_used_at,
        pinned=s.pinned,
        is_active=s.is_active,
        circle_tier=s.circle_tier,
        atom_id=s.atom_id,
        merged_into_id=s.merged_into_id,
        created_at=s.created_at,
        updated_at=s.updated_at,
        is_owner=is_owner,
    )


async def _load_owned(
    db: AsyncSession, skill_id: int, user: User
) -> ProceduralSkill:
    """Load a skill the caller is allowed to mutate.

    Allowed: skill.user_id == user.id. Seed skills (user_id IS NULL) are
    read-only via this surface — they live in the repo's seed_skills/ folder
    and are managed via git, not the API.
    """
    skill = (await db.execute(
        select(ProceduralSkill).where(ProceduralSkill.id == skill_id)
    )).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.user_id is None or skill.user_id != user.id:
        # Uniform 404 — don't disclose existence of seeds / other-user skills
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


# ----------------------------------------------------------------- list
@router.get("", response_model=list[SkillResponse])
@limiter.limit(settings.api_rate_limit_chat)
async def list_skills(
    request: Request,
    include_seeds: bool = True,
    include_inactive: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """List skills visible to the current user.

    Visible = owned by current_user OR public seed (user_id IS NULL,
    circle_tier=4). The latter is opt-out via ``include_seeds=false``.
    """
    user = current_user

    stmt = select(ProceduralSkill)
    if not include_inactive:
        stmt = stmt.where(ProceduralSkill.is_active.is_(True))

    if include_seeds:
        stmt = stmt.where(
            or_(
                ProceduralSkill.user_id == user.id,
                (ProceduralSkill.user_id.is_(None)) & (ProceduralSkill.circle_tier == TIER_PUBLIC),
            )
        )
    else:
        stmt = stmt.where(ProceduralSkill.user_id == user.id)

    stmt = stmt.order_by(ProceduralSkill.updated_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_response(s, is_owner=(s.user_id == user.id)) for s in rows]


# ----------------------------------------------------------------- read
@router.get("/{skill_id}", response_model=SkillResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def get_skill(
    request: Request,
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    user = current_user
    skill = (await db.execute(
        select(ProceduralSkill).where(ProceduralSkill.id == skill_id)
    )).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    is_seed_public = skill.user_id is None and skill.circle_tier == TIER_PUBLIC
    is_owner = skill.user_id == user.id
    if not (is_owner or is_seed_public):
        raise HTTPException(status_code=404, detail="Skill not found")
    return _to_response(skill, is_owner=is_owner)


# --------------------------------------------------------------- create
@router.post("", response_model=SkillResponse, status_code=201)
@limiter.limit(settings.api_rate_limit_chat)
async def create_skill(
    request: Request,
    body: SkillCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    user = current_user
    try:
        svc = SkillService(db)
        skill = await svc.create_user_authored(
            user_id=user.id,
            title=body.title,
            body_md=body.body_md,
            trigger_examples=body.trigger_examples,
            tool_sequence=body.tool_sequence,
            circle_tier=body.circle_tier,
        )
        return _to_response(skill, is_owner=True)
    except Exception as e:
        # Log full exception server-side; return a generic message to
        # the client so internal details (SQL errors, Ollama timeouts,
        # embedding-service stack hints) don't surface in the HTTP
        # response body. Same pattern as atoms.py:184.
        logger.error(f"❌ Skill create failed for user={user.id}: {e}")
        raise HTTPException(status_code=500, detail="Skill create failed")


# --------------------------------------------------------------- update
@router.patch("/{skill_id}", response_model=SkillResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def update_skill(
    request: Request,
    skill_id: int,
    body: SkillUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    user = current_user
    skill = await _load_owned(db, skill_id, user)

    changed = False
    if body.title is not None:
        skill.title = body.title.strip()[:255]
        changed = True
    if body.body_md is not None:
        skill.body_md = body.body_md
        changed = True
    if body.trigger_examples is not None:
        skill.trigger_examples = body.trigger_examples
        changed = True
    if body.tool_sequence is not None:
        skill.tool_sequence = body.tool_sequence
        changed = True
    if body.pinned is not None:
        skill.pinned = body.pinned
        changed = True
    if body.is_active is not None:
        # Reactivating a curator-archived skill must also clear its
        # merged_into_id pointer — otherwise the row stays marked as
        # "merged into X" while being active, and the next curator pass
        # may pair it against X again (transitive audit loop, double
        # counter-bumps, version churn). Pinned-flip semantics are
        # independent of merged_into_id so we don't touch it there.
        was_inactive = skill.is_active is False
        skill.is_active = body.is_active
        if body.is_active is True and was_inactive and skill.merged_into_id is not None:
            skill.merged_into_id = None
        changed = True

    if changed:
        skill.version += 1
        # Re-embed if any field that drives similarity changed. Public
        # method on the service — the route deliberately does NOT reach
        # into _embed/_embedding_input now that reembed_skill exposes the
        # combined operation.
        if any(v is not None for v in (body.title, body.body_md, body.trigger_examples)):
            svc = SkillService(db)
            new_emb = await svc.compute_embedding_for(
                skill.title, skill.trigger_examples or [], skill.body_md,
            )
            # Don't NUKE the existing embedding on a transient Ollama
            # failure (new_emb is None when _embed swallowed an error).
            # The old vector keeps the skill retrievable until the next
            # edit / boot when embedding can be retried.
            if new_emb is not None:
                skill.embedding = new_emb
        await db.commit()
        SkillService.invalidate_has_skills_cache()

    return _to_response(skill, is_owner=True)


# ----------------------------------------------------------------- pin
@router.post("/{skill_id}/pin", response_model=SkillResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def pin_skill(
    request: Request,
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    user = current_user
    skill = await _load_owned(db, skill_id, user)
    skill.pinned = True
    await db.commit()
    return _to_response(skill, is_owner=True)


@router.post("/{skill_id}/unpin", response_model=SkillResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def unpin_skill(
    request: Request,
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    user = current_user
    skill = await _load_owned(db, skill_id, user)
    skill.pinned = False
    await db.commit()
    return _to_response(skill, is_owner=True)


# --------------------------------------------------------------- delete
@router.delete("/{skill_id}", status_code=204)
@limiter.limit(settings.api_rate_limit_chat)
async def delete_skill(
    request: Request,
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Soft-delete (is_active=False). The atoms row stays for audit trail."""
    user = current_user
    skill = await _load_owned(db, skill_id, user)
    skill.is_active = False
    await db.commit()
    SkillService.invalidate_has_skills_cache()


# ----------------------------------------------------------------- tier
@router.patch("/{skill_id}/tier", response_model=SkillResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def change_skill_tier(
    request: Request,
    skill_id: int,
    body: SkillTierRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Change circle_tier and cascade via AtomService — same path KG
    entities and memories use."""
    user = current_user
    skill = await _load_owned(db, skill_id, user)

    if skill.atom_id is None:
        # Auto-extracted/user-created skills always have an atom_id; this
        # branch only hits if a manual DB tweak created a half-state row.
        raise HTTPException(
            status_code=409,
            detail="Skill has no atom registration; cannot change tier",
        )

    svc = AtomService(db)
    await svc.update_tier(skill.atom_id, {"tier": int(body.circle_tier)})
    # Re-fetch — update_tier cascaded the source-row column, so we need
    # a fresh read to see the post-cascade circle_tier. Pin the user_id
    # check so a concurrent owner-change can't surface a row that isn't
    # ours anymore (defense-in-depth; _load_owned already verified ownership
    # at the top of this handler).
    skill = (await db.execute(
        select(ProceduralSkill).where(
            ProceduralSkill.id == skill_id,
            ProceduralSkill.user_id == user.id,
        )
    )).scalar_one()
    return _to_response(skill, is_owner=True)


# -------------------------------------------------------- curator (Phase 4)
class CuratorRunRequest(BaseModel):
    """Optional ``user_id`` lets an admin run for a specific user; the
    default is "every active user", same scope as the scheduler."""
    user_id: int | None = None


class CuratorReportResponse(BaseModel):
    user_id: int | None
    duplicates_found: int
    merges_applied: int
    stale_archived: int
    notes: list[str]


@router.post("/curator/run", response_model=list[CuratorReportResponse])
@limiter.limit(settings.api_rate_limit_admin)
async def run_curator(
    request: Request,
    body: CuratorRunRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.ADMIN)),
):
    """Manual curator trigger — same logic as the scheduler, runs
    synchronously and returns the report. Admin-only because it can
    flip ``is_active`` on rows owned by other users.

    Emits a structured audit log per call so manual admin-driven curator
    runs are distinguishable from the scheduler's autonomous passes in
    log-aggregation.
    """
    svc = SkillCuratorService(db)
    logger.info(
        f"🧹 Curator manual run: admin_id={admin.id} admin_username={admin.username!r} "
        f"target_user_id={body.user_id!r}"
    )
    if body.user_id is not None:
        report = await svc.run_for_user(body.user_id)
        return [CuratorReportResponse(**asdict(report))]

    user_ids = await svc.list_active_user_ids()
    reports = []
    for uid in user_ids:
        try:
            report = await svc.run_for_user(uid)
            reports.append(CuratorReportResponse(**asdict(report)))
        except Exception as e:
            logger.warning(f"Curator failed for user {uid}: {e}")
    return reports
