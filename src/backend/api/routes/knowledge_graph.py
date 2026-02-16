"""
Knowledge Graph API Routes — CRUD for entities and relations.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.knowledge_graph_schemas import (
    EntityBrief,
    EntityListResponse,
    EntityResponse,
    EntityScopeUpdate,
    EntityUpdate,
    KGStatsResponse,
    MergeEntitiesRequest,
    RelationListResponse,
    RelationResponse,
    ScopesListResponse,
)
from models.database import User
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.knowledge_graph_service import KnowledgeGraphService
from utils.config import settings

router = APIRouter()


def _entity_to_response(entity) -> EntityResponse:
    return EntityResponse(
        id=entity.id,
        name=entity.name,
        entity_type=entity.entity_type,
        description=entity.description,
        mention_count=entity.mention_count or 1,
        first_seen_at=entity.first_seen_at.isoformat() if entity.first_seen_at else "",
        last_seen_at=entity.last_seen_at.isoformat() if entity.last_seen_at else "",
        scope=entity.scope or "personal",
    )


@router.get("/scopes", response_model=ScopesListResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def list_scopes(
    request: Request,
    lang: str = Query("de"),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """List available KG scopes with labels and descriptions."""
    from services.kg_scope_loader import get_scope_loader
    scope_loader = get_scope_loader()

    scopes = scope_loader.get_all_scopes(lang)
    return ScopesListResponse(scopes=scopes)


@router.get("/entities", response_model=EntityListResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def list_entities(
    request: Request,
    user_id: int | None = Query(None),
    type: str | None = Query(None),
    search: str | None = Query(None),
    scope: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """List knowledge graph entities with optional filters."""
    try:
        svc = KnowledgeGraphService(db)
        entities, total = await svc.list_entities(
            user_id=user_id,
            entity_type=type,
            search=search,
            scope=scope,
            page=page,
            size=size,
        )
        return EntityListResponse(
            entities=[_entity_to_response(e) for e in entities],
            total=total,
            page=page,
            size=size,
        )
    except Exception as e:
        logger.error(f"List KG entities error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entities/{entity_id}", response_model=EntityResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def get_entity(
    request: Request,
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Get a single entity by ID."""
    svc = KnowledgeGraphService(db)
    entity = await svc.get_entity(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    return _entity_to_response(entity)


@router.put("/entities/{entity_id}", response_model=EntityResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def update_entity(
    request: Request,
    entity_id: int,
    body: EntityUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Update an entity's name, type, or description."""
    try:
        svc = KnowledgeGraphService(db)
        entity = await svc.update_entity(
            entity_id=entity_id,
            name=body.name,
            entity_type=body.entity_type,
            description=body.description,
        )
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")
        return _entity_to_response(entity)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update KG entity error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/entities/{entity_id}/scope")
@limiter.limit(settings.api_rate_limit_admin)
async def update_entity_scope(
    request: Request,
    entity_id: int,
    body: EntityScopeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Update scope of an entity (admin only)."""
    try:
        svc = KnowledgeGraphService(db)
        entity = await svc.update_entity_scope(entity_id, body.scope)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")
        return _entity_to_response(entity)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update KG entity scope error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/entities/{entity_id}")
@limiter.limit(settings.api_rate_limit_admin)
async def delete_entity(
    request: Request,
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Soft-delete an entity and its relations."""
    svc = KnowledgeGraphService(db)
    success = await svc.delete_entity(entity_id)
    if not success:
        raise HTTPException(status_code=404, detail="Entity not found")
    return {"success": True}


@router.post("/entities/merge", response_model=EntityResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def merge_entities(
    request: Request,
    body: MergeEntitiesRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Merge source entity into target entity."""
    if body.source_id == body.target_id:
        raise HTTPException(status_code=400, detail="Cannot merge entity with itself")
    try:
        svc = KnowledgeGraphService(db)
        entity = await svc.merge_entities(body.source_id, body.target_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")
        return _entity_to_response(entity)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Merge KG entities error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/relations", response_model=RelationListResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def list_relations(
    request: Request,
    user_id: int | None = Query(None),
    entity_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """List knowledge graph relations."""
    try:
        svc = KnowledgeGraphService(db)
        relations, total = await svc.list_relations(
            user_id=user_id,
            entity_id=entity_id,
            page=page,
            size=size,
        )
        return RelationListResponse(
            relations=[
                RelationResponse(
                    id=r["id"],
                    subject=EntityBrief(**r["subject"]) if r.get("subject") else None,
                    predicate=r["predicate"],
                    object=EntityBrief(**r["object"]) if r.get("object") else None,
                    confidence=r["confidence"],
                    created_at=r.get("created_at"),
                )
                for r in relations
            ],
            total=total,
            page=page,
            size=size,
        )
    except Exception as e:
        logger.error(f"List KG relations error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/relations/{relation_id}")
@limiter.limit(settings.api_rate_limit_admin)
async def delete_relation(
    request: Request,
    relation_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Soft-delete a relation."""
    svc = KnowledgeGraphService(db)
    success = await svc.delete_relation(relation_id)
    if not success:
        raise HTTPException(status_code=404, detail="Relation not found")
    return {"success": True}


@router.get("/stats", response_model=KGStatsResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def get_stats(
    request: Request,
    user_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ADMIN)),
):
    """Get knowledge graph statistics."""
    try:
        svc = KnowledgeGraphService(db)
        stats = await svc.get_stats(user_id=user_id)
        return KGStatsResponse(**stats)
    except Exception as e:
        logger.error(f"KG stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
