"""Projects API — business-instance Phase 1.

Minimal CRUD over the ``Project`` model: create a project (+ its dedicated 1:1
KnowledgeBase), list/get owner-scoped, delete the project row. Ingest-to-project
then just targets the project's KB via the existing knowledge routes, so a
project becomes usable for chat/doc-based history immediately.

Gated by ``settings.projects_enabled`` — every route 404s when off, so the
household instance never exposes this surface. Owner-scoped when auth is on;
auth-disabled single-user mode sees all (mirrors the Circles-v1 pattern).

Meetings, timeline, and the minutes pipeline are later phases (business-instance
plan §7) and are NOT here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Project, User
from services.auth_service import get_optional_user
from services.database import get_db
from services.project_service import create_project, document_count_for_kb
from utils.config import settings

router = APIRouter()


def _require_enabled() -> None:
    """404 the whole surface when the feature flag is off."""
    if not settings.projects_enabled:
        raise HTTPException(status_code=404, detail="Projects feature is not enabled")


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    # Circles tier for team/project access; default 2 = household/team.
    circle_tier: int = Field(default=2, ge=0, le=4)


class ProjectResponse(BaseModel):
    id: int
    name: str
    description: str | None
    owner_id: int | None
    knowledge_base_id: int | None
    circle_tier: int
    status: str
    created_at: str
    document_count: int


async def _to_response(db: AsyncSession, project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        owner_id=project.owner_id,
        knowledge_base_id=project.knowledge_base_id,
        circle_tier=project.circle_tier,
        status=project.status,
        created_at=project.created_at.isoformat() if project.created_at else "",
        document_count=await document_count_for_kb(db, project.knowledge_base_id),
    )


@router.post("", response_model=ProjectResponse)
async def create_project_route(
    data: ProjectCreate,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Create a project and its dedicated (1:1) KnowledgeBase.

    Auth on → the authenticated user owns both rows. Auth off → single-user
    mode, owner_id is left NULL (mirrors ``POST /api/knowledge/bases``).
    """
    _require_enabled()
    if settings.auth_enabled and not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    project = await create_project(
        db,
        name=data.name,
        description=data.description,
        owner_id=user.id if user else None,
        circle_tier=data.circle_tier,
    )
    return await _to_response(db, project)


@router.get("", response_model=list[ProjectResponse])
async def list_projects_route(
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectResponse]:
    """List projects. Owner-scoped when auth is on; auth-off single-user sees all."""
    _require_enabled()

    stmt = select(Project).order_by(Project.created_at.desc())
    if settings.auth_enabled:
        if not user:
            return []
        stmt = stmt.where(Project.owner_id == user.id)

    result = await db.execute(stmt)
    projects = result.scalars().all()
    return [await _to_response(db, p) for p in projects]


async def _get_owned_project(
    project_id: int, user: User | None, db: AsyncSession
) -> Project:
    """Fetch a project, enforcing owner scoping. 404 when missing OR not the
    caller's (owner-gated 404 — never leak existence). Auth off => any project."""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if settings.auth_enabled:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if project.owner_id != user.id:
            raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project_route(
    project_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Get one project (owner-gated 404)."""
    _require_enabled()
    project = await _get_owned_project(project_id, user, db)
    return await _to_response(db, project)


@router.delete("/{project_id}")
async def delete_project_route(
    project_id: int,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete the project row only (owner-gated 404).

    Deliberately does NOT delete the linked KnowledgeBase or its documents —
    the KB and its ingested history are kept so a project delete is never a
    silent data-loss of the corpus. Detach/cleanup of the KB is a later phase.
    """
    _require_enabled()
    project = await _get_owned_project(project_id, user, db)
    kb_id = project.knowledge_base_id
    await db.delete(project)
    await db.commit()
    return {
        "message": "Project deleted",
        "id": project_id,
        "knowledge_base_id": kb_id,
        "knowledge_base_retained": True,
    }
