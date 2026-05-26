"""
Skills API Routes — procedural-skill CRUD + admin Skills Inbox (v2.10).

Surface (`/api/skills` prefix added by main.py):
  GET     /                — list visible skills (filter via ?status=, ?source=,
                              ?admin_view=true for admin-wide inbox view)
  GET     /draft-count     — sidebar NavBadge count (admin sees global; owner
                              sees own; auth-disabled sees global)
  POST    /                — manually author a skill (lands as ``approved``)
  GET     /{id}            — read one (owner / public-seed / admin)
  PATCH   /{id}            — update title / body / triggers / tools / pinned
                              / status (owner only)
  POST    /{id}/approve    — flip status draft|rejected → approved (admin)
  POST    /{id}/reject     — flip status draft → rejected (admin)
  POST    /{id}/pin        — pin (protect from curator stale-archive)
  POST    /{id}/unpin      — unpin
  DELETE  /{id}            — soft-delete (status='archived')
  PATCH   /{id}/tier       — change circle_tier (cascades via AtomService)

  POST    /curator/run     — admin: trigger curator run (writes audit row)
  GET     /curator/runs    — admin: list recent SkillCuratorRun audit rows
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    CURATOR_RUN_TYPE_MANUAL,
    ProceduralSkill,
    SKILL_STATUS_APPROVED,
    SKILL_STATUS_ARCHIVED,
    SKILL_STATUS_DRAFT,
    SKILL_STATUS_REJECTED,
    SKILL_STATUSES,
    SkillCuratorRun,
    TIER_PUBLIC,
    User,
)
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


def _user_is_admin(user: User | None) -> bool:
    """Has the caller got the ADMIN permission?

    Mirrors the contract of ``services.auth_service.require_permission``:
    when ``auth_enabled`` is False the whole permission system is bypassed
    and the (single-user-mode) default user is treated as admin. Without
    this branch the Skills Inbox + admin-view list filter + global
    draft-count return 403 in every home deployment that hasn't opted
    into auth.

    Auth-enabled path: ``User.has_permission`` delegates to the eagerly-
    loaded role. ``get_user_or_default`` / ``get_current_user`` both
    ``selectinload(User.role)`` so the lazy traversal here is safe.
    """
    if not settings.auth_enabled:
        return True
    if user is None:
        return False
    try:
        return bool(user.has_permission("admin"))
    except Exception:
        return False


# ---------------------------------------------------------------- schemas
class SkillCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
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
    trigger_examples: list[str] | None = Field(
        default=None, min_length=1, max_length=_MAX_TRIGGER_EXAMPLES,
    )
    tool_sequence: list[str] | None = Field(
        default=None, max_length=_MAX_TOOL_SEQUENCE,
    )
    pinned: bool | None = None
    # v2.10: status replaces the old is_active boolean. Owner-supplied
    # value is validated against SKILL_STATUSES at apply-time.
    status: str | None = None


class SkillTierRequest(BaseModel):
    circle_tier: int = Field(..., ge=0, le=4)


class SkillResponse(BaseModel):
    id: int
    title: str
    body_md: str
    trigger_examples: list[str]
    tool_sequence: list[str]
    source: str
    status: str
    version: int
    success_count: int
    failure_count: int
    last_used_at: datetime | None
    pinned: bool
    circle_tier: int
    atom_id: str | None
    merged_into_id: int | None
    user_id: int | None
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
        status=s.status,
        version=s.version,
        success_count=s.success_count,
        failure_count=s.failure_count,
        last_used_at=s.last_used_at,
        pinned=s.pinned,
        circle_tier=s.circle_tier,
        atom_id=s.atom_id,
        merged_into_id=s.merged_into_id,
        user_id=s.user_id,
        created_at=s.created_at,
        updated_at=s.updated_at,
        is_owner=is_owner,
    )


async def _load_owned(
    db: AsyncSession, skill_id: int, user: User
) -> ProceduralSkill:
    """Load a skill the caller is allowed to mutate.

    Allowed: skill.user_id == user.id. Seed skills (user_id IS NULL) are
    read-only via this surface — they live in the repo's seed_skills/
    folder and are managed via git, not the API.
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


async def _load_for_admin(
    db: AsyncSession, skill_id: int
) -> ProceduralSkill:
    """Admin load — bypasses ownership; used by approve/reject."""
    skill = (await db.execute(
        select(ProceduralSkill).where(ProceduralSkill.id == skill_id)
    )).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


# ----------------------------------------------------------------- list
@router.get("", response_model=list[SkillResponse])
@limiter.limit(settings.api_rate_limit_chat)
async def list_skills(
    request: Request,
    include_seeds: bool = True,
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    admin_view: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """List skills.

    Default scope: skills owned by current_user, plus public seeds when
    ``include_seeds=true``, restricted to ``status='approved'``.

    Filters:
      - ``status=draft|approved|rejected|archived`` — narrow by lifecycle
      - ``source=auto_extracted|user_created|seed`` — narrow by origin
      - ``admin_view=true`` — bypass the per-user visibility filter and
        return every skill in the table (admins only). The Skills Inbox
        uses this in combination with ``status=draft`` to see drafts
        across the whole household.
    """
    user = current_user

    if status is not None and status not in SKILL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status!r}")

    if admin_view and not _user_is_admin(user):
        raise HTTPException(status_code=403, detail="Admin permission required")

    stmt = select(ProceduralSkill)

    # Status filter. Default = approved-only; the admin Skills Inbox sets
    # ?status=draft to surface pending rows; the owner's BrainSkillsPage
    # passes nothing and gets the user-facing default.
    stmt = stmt.where(ProceduralSkill.status == (status or SKILL_STATUS_APPROVED))

    if source is not None:
        stmt = stmt.where(ProceduralSkill.source == source)

    if admin_view:
        # No per-user visibility filter — admin sees everything.
        pass
    elif include_seeds:
        stmt = stmt.where(
            or_(
                ProceduralSkill.user_id == user.id,
                (ProceduralSkill.user_id.is_(None))
                & (ProceduralSkill.circle_tier == TIER_PUBLIC),
            )
        )
    else:
        stmt = stmt.where(ProceduralSkill.user_id == user.id)

    stmt = stmt.order_by(ProceduralSkill.updated_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_response(s, is_owner=(s.user_id == user.id)) for s in rows]


# ------------------------------------------------------------ draft-count
class DraftCountResponse(BaseModel):
    count: int


@router.get("/draft-count", response_model=DraftCountResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def get_draft_count(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Sidebar NavBadge: count of skills waiting in the Skills Inbox.

    Admins see the global count (the inbox is admin-scoped). Non-admins
    see their own drafts only — so an owner using the BrainSkillsPage
    still gets a hint when something they should review is queued.
    """
    user = current_user
    # COUNT(*) scalar — the NavBadge polls this every 60 s per active
    # user; materializing every draft row into ORM objects just to len()
    # would scale linearly with the inbox depth.
    stmt = select(func.count(ProceduralSkill.id)).where(
        ProceduralSkill.status == SKILL_STATUS_DRAFT
    )
    if not _user_is_admin(user):
        stmt = stmt.where(ProceduralSkill.user_id == user.id)
    count = (await db.execute(stmt)).scalar() or 0
    return DraftCountResponse(count=int(count))


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
    is_admin = _user_is_admin(user)
    if not (is_owner or is_seed_public or is_admin):
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
    if body.status is not None:
        if body.status not in SKILL_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status: {body.status!r}",
            )
        # Reactivating an archived/merged skill must also clear its
        # merged_into_id pointer — otherwise the row stays marked as
        # "merged into X" while being approved, and the next curator
        # pass may pair it against X again (transitive audit loop, double
        # counter-bumps, version churn).
        was_inactive = skill.status in (SKILL_STATUS_ARCHIVED, SKILL_STATUS_REJECTED)
        skill.status = body.status
        if (
            body.status == SKILL_STATUS_APPROVED
            and was_inactive
            and skill.merged_into_id is not None
        ):
            skill.merged_into_id = None
        changed = True

    if changed:
        skill.version += 1
        if any(v is not None for v in (body.title, body.body_md, body.trigger_examples)):
            svc = SkillService(db)
            new_emb = await svc.compute_embedding_for(
                skill.title, skill.trigger_examples or [], skill.body_md,
            )
            if new_emb is not None:
                skill.embedding = new_emb
        await db.commit()
        SkillService.invalidate_has_skills_cache()

    return _to_response(skill, is_owner=True)


# -------------------------------------------------------- approve/reject
@router.post("/{skill_id}/approve", response_model=SkillResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def approve_skill(
    request: Request,
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.ADMIN)),
):
    """Admin: flip a draft (or previously-rejected) skill to approved.

    Approving a skill moves it from the Inbox into the live retrieval
    corpus. Idempotent — approving an already-approved skill is a no-op
    (returns the row unchanged).
    """
    skill = await _load_for_admin(db, skill_id)
    if skill.status == SKILL_STATUS_APPROVED:
        return _to_response(skill, is_owner=(skill.user_id == admin.id))

    if skill.status not in (SKILL_STATUS_DRAFT, SKILL_STATUS_REJECTED):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve from status {skill.status!r}",
        )
    skill.status = SKILL_STATUS_APPROVED
    skill.version += 1
    # Clear merged_into_id if the reviewer is re-promoting an archived row
    # that had been merged. Not strictly possible from the draft path but
    # cheap defense-in-depth.
    skill.merged_into_id = None
    await db.commit()
    SkillService.invalidate_has_skills_cache()
    logger.info(
        f"🧠 Skill #{skill.id} approved by admin={admin.id} "
        f"({skill.title!r})"
    )
    return _to_response(skill, is_owner=(skill.user_id == admin.id))


@router.post("/{skill_id}/reject", response_model=SkillResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def reject_skill(
    request: Request,
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.ADMIN)),
):
    """Admin: flip a draft skill to rejected (excluded from retrieval,
    kept for audit). Idempotent."""
    skill = await _load_for_admin(db, skill_id)
    if skill.status == SKILL_STATUS_REJECTED:
        return _to_response(skill, is_owner=(skill.user_id == admin.id))

    if skill.status != SKILL_STATUS_DRAFT:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reject from status {skill.status!r}",
        )
    skill.status = SKILL_STATUS_REJECTED
    skill.version += 1
    await db.commit()
    SkillService.invalidate_has_skills_cache()
    logger.info(
        f"🧠 Skill #{skill.id} rejected by admin={admin.id} "
        f"({skill.title!r})"
    )
    return _to_response(skill, is_owner=(skill.user_id == admin.id))


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
    """Soft-delete (status='archived'). The atoms row stays for audit trail."""
    user = current_user
    skill = await _load_owned(db, skill_id, user)
    skill.status = SKILL_STATUS_ARCHIVED
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
        raise HTTPException(
            status_code=409,
            detail="Skill has no atom registration; cannot change tier",
        )

    svc = AtomService(db)
    await svc.update_tier(skill.atom_id, {"tier": int(body.circle_tier)})
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


class CuratorRunResponse(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    run_type: str
    triggered_by_user_id: int | None
    status: str
    skills_examined: int
    duplicate_pairs_found: int
    duplicate_pairs_merged: int
    stale_skills_archived: int
    error_message: str | None


def _curator_run_to_response(r: SkillCuratorRun) -> CuratorRunResponse:
    return CuratorRunResponse(
        id=r.id,
        started_at=r.started_at,
        finished_at=r.finished_at,
        duration_seconds=r.duration_seconds,
        run_type=r.run_type,
        triggered_by_user_id=r.triggered_by_user_id,
        status=r.status,
        skills_examined=r.skills_examined,
        duplicate_pairs_found=r.duplicate_pairs_found,
        duplicate_pairs_merged=r.duplicate_pairs_merged,
        stale_skills_archived=r.stale_skills_archived,
        error_message=r.error_message,
    )


@router.post("/curator/run", response_model=CuratorRunResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def run_curator(
    request: Request,
    body: CuratorRunRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.ADMIN)),
):
    """Manual curator trigger from the AdminCuratorPage "Run Now" button.

    Writes a SkillCuratorRun audit row (run_type='manual', triggered_by
    = admin.id), runs the curator synchronously, and returns the
    completed run row.

    ``body.user_id`` is accepted as a scoping hint but the v2.10 audit
    flow always runs the full pass — a per-user scope would split the
    audit accounting and we'd need a separate run row per user. If you
    need per-user inspection, filter the response from GET
    /curator/runs by ``triggered_by_user_id``.
    """
    svc = SkillCuratorService(db)
    logger.info(
        f"🧹 Curator manual run requested: admin_id={admin.id} "
        f"admin_username={admin.username!r} target_user_id={body.user_id!r}"
    )
    run_row = await svc.run_curator(
        triggered_by_user_id=admin.id,
        run_type=CURATOR_RUN_TYPE_MANUAL,
    )
    return _curator_run_to_response(run_row)


@router.get("/curator/runs", response_model=list[CuratorRunResponse])
@limiter.limit(settings.api_rate_limit_admin)
async def list_curator_runs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_permission(Permission.ADMIN)),
):
    """Admin: list recent curator audit rows for the AdminCuratorPage
    history pane. Newest first."""
    rows = (await db.execute(
        select(SkillCuratorRun)
        .order_by(SkillCuratorRun.started_at.desc())
        .limit(limit)
        .offset(offset)
    )).scalars().all()
    return [_curator_run_to_response(r) for r in rows]
