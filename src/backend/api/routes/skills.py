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

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ProceduralSkill, TIER_PUBLIC, User
from services.atom_service import AtomService
from services.auth_service import get_current_user
from services.database import get_db
from services.skill_service import SkillService

router = APIRouter()


# ---------------------------------------------------------------- schemas
class SkillCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    body_md: str = Field(..., min_length=1)
    trigger_examples: list[str] = Field(..., min_length=1, max_length=10)
    tool_sequence: list[str] = Field(default_factory=list, max_length=20)
    circle_tier: int = Field(default=0, ge=0, le=4)


class SkillUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    body_md: str | None = None
    trigger_examples: list[str] | None = None
    tool_sequence: list[str] | None = None
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
        created_at=s.created_at,
        updated_at=s.updated_at,
        is_owner=is_owner,
    )


def _require_user(current_user: User | None) -> User:
    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return current_user


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
async def list_skills(
    include_seeds: bool = True,
    include_inactive: bool = False,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """List skills visible to the current user.

    Visible = owned by current_user OR public seed (user_id IS NULL,
    circle_tier=4). The latter is opt-out via ``include_seeds=false``.
    """
    user = _require_user(current_user)

    stmt = select(ProceduralSkill)
    if not include_inactive:
        stmt = stmt.where(ProceduralSkill.is_active.is_(True))

    if include_seeds:
        from sqlalchemy import or_
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
async def get_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    user = _require_user(current_user)
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
async def create_skill(
    body: SkillCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    user = _require_user(current_user)
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
        logger.error(f"❌ Skill create failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --------------------------------------------------------------- update
@router.patch("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: int,
    body: SkillUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    user = _require_user(current_user)
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
        skill.is_active = body.is_active
        changed = True

    if changed:
        skill.version += 1
        # Re-embed if the content that drives similarity changed
        if any(v is not None for v in (body.title, body.body_md, body.trigger_examples)):
            svc = SkillService(db)
            emb_input = svc._embedding_input(
                skill.title, skill.trigger_examples or [], skill.body_md
            )
            skill.embedding = await svc._embed(emb_input)
        await db.commit()
        SkillService._has_skills_cache.clear()

    return _to_response(skill, is_owner=True)


# ----------------------------------------------------------------- pin
@router.post("/{skill_id}/pin", response_model=SkillResponse)
async def pin_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    user = _require_user(current_user)
    skill = await _load_owned(db, skill_id, user)
    skill.pinned = True
    await db.commit()
    return _to_response(skill, is_owner=True)


@router.post("/{skill_id}/unpin", response_model=SkillResponse)
async def unpin_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    user = _require_user(current_user)
    skill = await _load_owned(db, skill_id, user)
    skill.pinned = False
    await db.commit()
    return _to_response(skill, is_owner=True)


# --------------------------------------------------------------- delete
@router.delete("/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Soft-delete (is_active=False). The atoms row stays for audit trail."""
    user = _require_user(current_user)
    skill = await _load_owned(db, skill_id, user)
    skill.is_active = False
    await db.commit()
    SkillService._has_skills_cache.clear()


# ----------------------------------------------------------------- tier
@router.patch("/{skill_id}/tier", response_model=SkillResponse)
async def change_skill_tier(
    skill_id: int,
    body: SkillTierRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Change circle_tier and cascade via AtomService — same path KG
    entities and memories use."""
    user = _require_user(current_user)
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
    # Re-fetch — update_tier cascaded the source-row column.
    skill = (await db.execute(
        select(ProceduralSkill).where(ProceduralSkill.id == skill_id)
    )).scalar_one()
    return _to_response(skill, is_owner=True)
