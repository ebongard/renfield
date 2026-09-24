"""
Knowledge Graph API Routes — CRUD for entities and relations.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.routes.knowledge_graph_schemas import (
    ApproveMergeRequest,
    CircleTierInfo,
    CircleTiersListResponse,
    CleanupInvalidResponse,
    ClusterResolveRequest,
    ClusterResolveResponse,
    DuplicateCluster,
    DuplicateClustersResponse,
    EntityBrief,
    EntityCircleTierUpdate,
    EntityListResponse,
    EntityResponse,
    EntityUpdate,
    KGStatsResponse,
    MergeDuplicatesResponse,
    MergeEntitiesRequest,
    MergeProposalEntityBrief,
    MergeProposalResponse,
    MergeProposalsListResponse,
    ReconcilerRunResponse,
    RelationCreate,
    RelationListResponse,
    RelationResponse,
    RelationUpdate,
)
from models.database import (
    KG_MERGE_PROPOSAL_PENDING,
    TIER_PUBLIC,
    KgMergeProposal,
    KGRelation,
    User,
)
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.kg_reconciler_service import KgReconcilerService
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
        circle_tier=int(entity.circle_tier or 0),
    )


# Localized tier labels — kept inline because the ladder is fixed at 5 rungs
# and never user-extensible (per Lane B / circles v1 design).
_TIER_LABELS: dict[int, dict[str, dict[str, str]]] = {
    0: {"de": {"label": "Privat", "description": "Nur für mich sichtbar."},
        "en": {"label": "Self", "description": "Visible only to me."}},
    1: {"de": {"label": "Vertraut", "description": "Für vertraute Personen sichtbar."},
        "en": {"label": "Trusted", "description": "Visible to trusted people."}},
    2: {"de": {"label": "Haushalt", "description": "Für den ganzen Haushalt sichtbar."},
        "en": {"label": "Household", "description": "Visible to everyone in the household."}},
    3: {"de": {"label": "Erweitert", "description": "Für den erweiterten Kreis sichtbar."},
        "en": {"label": "Extended", "description": "Visible to the extended circle."}},
    4: {"de": {"label": "Öffentlich", "description": "Öffentlich sichtbar."},
        "en": {"label": "Public", "description": "Publicly visible."}},
}
_TIER_NAMES = {0: "self", 1: "trusted", 2: "household", 3: "extended", 4: "public"}


@router.get("/circle-tiers", response_model=CircleTiersListResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def list_circle_tiers(
    request: Request,
    lang: str = Query("de"),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """List circle tiers (0..4) with localized labels and descriptions."""
    lang_key = lang if lang in ("de", "en") else "de"
    tiers = [
        CircleTierInfo(
            tier=t,
            name=_TIER_NAMES[t],
            label=_TIER_LABELS[t][lang_key]["label"],
            description=_TIER_LABELS[t][lang_key]["description"],
        )
        for t in range(TIER_PUBLIC + 1)
    ]
    return CircleTiersListResponse(tiers=tiers)


@router.get("/entities", response_model=EntityListResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def list_entities(
    request: Request,
    user_id: int | None = Query(None),
    type: str | None = Query(None),
    search: str | None = Query(None),
    circle_tier: int | None = Query(None, ge=0, le=4),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """List knowledge graph entities with optional filters."""
    try:
        svc = KnowledgeGraphService(db)
        entities, total = await svc.list_entities(
            user_id=user_id,
            entity_type=type,
            search=search,
            circle_tier=circle_tier,
            page=page,
            size=size,
            asker_id=user.id if user else None,
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
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Get a single entity by ID."""
    svc = KnowledgeGraphService(db)
    # Circle access (review H4): scope the read to the asker. An inaccessible
    # entity returns 404 (indistinguishable from non-existent — no oracle).
    entity = await svc.get_entity(
        entity_id,
        asker_id=user.id if user else None,
        enforce_circle=True,
    )
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
    user: User = Depends(require_permission(Permission.KG_MANAGE)),
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


@router.patch("/entities/{entity_id}/circle-tier", response_model=EntityResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def update_entity_circle_tier(
    request: Request,
    entity_id: int,
    body: EntityCircleTierUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_MANAGE)),
):
    """Update an entity's circle_tier (0..4). Cascades to incident relations."""
    try:
        svc = KnowledgeGraphService(db)
        entity = await svc.update_entity_circle_tier(entity_id, body.circle_tier)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")
        return _entity_to_response(entity)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update KG entity circle_tier error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/entities/{entity_id}")
@limiter.limit(settings.api_rate_limit_admin)
async def delete_entity(
    request: Request,
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_MANAGE)),
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
    user: User = Depends(require_permission(Permission.KG_MANAGE)),
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
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """List knowledge graph relations."""
    try:
        svc = KnowledgeGraphService(db)
        relations, total = await svc.list_relations(
            user_id=user_id,
            entity_id=entity_id,
            page=page,
            size=size,
            asker_id=user.id if user else None,
            enforce_circle=True,
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


@router.post("/relations", response_model=RelationResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def create_relation(
    request: Request,
    body: RelationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_MANAGE)),
):
    """Create a new relation between two entities."""
    if body.subject_id == body.object_id:
        raise HTTPException(status_code=400, detail="Subject and object must be different entities")
    try:
        svc = KnowledgeGraphService(db)
        # Validate entities exist
        subject = await svc.get_entity(body.subject_id)
        if not subject:
            raise HTTPException(status_code=400, detail=f"Subject entity {body.subject_id} not found")
        obj = await svc.get_entity(body.object_id)
        if not obj:
            raise HTTPException(status_code=400, detail=f"Object entity {body.object_id} not found")

        relation = await svc.save_relation(
            subject_id=body.subject_id,
            predicate=body.predicate,
            object_id=body.object_id,
            user_id=user.id if user else None,
            confidence=body.confidence,
        )
        await db.commit()
        return RelationResponse(
            id=relation.id,
            subject=EntityBrief(id=subject.id, name=subject.name, entity_type=subject.entity_type),
            predicate=relation.predicate,
            object=EntityBrief(id=obj.id, name=obj.name, entity_type=obj.entity_type),
            confidence=relation.confidence,
            created_at=relation.created_at.isoformat() if relation.created_at else None,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create KG relation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/relations/{relation_id}", response_model=RelationResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def update_relation(
    request: Request,
    relation_id: int,
    body: RelationUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_MANAGE)),
):
    """Update a relation's predicate, confidence, or endpoints."""
    try:
        svc = KnowledgeGraphService(db)
        relation = await svc.update_relation(
            relation_id=relation_id,
            predicate=body.predicate,
            confidence=body.confidence,
            subject_id=body.subject_id,
            object_id=body.object_id,
        )
        if not relation:
            raise HTTPException(status_code=404, detail="Relation not found")

        # Fetch entity data for response
        subject = await svc.get_entity(relation.subject_id)
        obj = await svc.get_entity(relation.object_id)
        return RelationResponse(
            id=relation.id,
            subject=EntityBrief(id=subject.id, name=subject.name, entity_type=subject.entity_type) if subject else None,
            predicate=relation.predicate,
            object=EntityBrief(id=obj.id, name=obj.name, entity_type=obj.entity_type) if obj else None,
            confidence=relation.confidence,
            created_at=relation.created_at.isoformat() if relation.created_at else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update KG relation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/relations/{relation_id}")
@limiter.limit(settings.api_rate_limit_admin)
async def delete_relation(
    request: Request,
    relation_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_MANAGE)),
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
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Get knowledge graph statistics."""
    try:
        svc = KnowledgeGraphService(db)
        stats = await svc.get_stats(
            user_id=user_id,
            asker_id=user.id if user else None,
            enforce_circle=True,
        )
        return KGStatsResponse(**stats)
    except Exception as e:
        logger.error(f"KG stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Cleanup Endpoints (admin only)
# =============================================================================

@router.post("/cleanup/invalid", response_model=CleanupInvalidResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def cleanup_invalid_entities(
    request: Request,
    dry_run: bool = Query(True, description="Preview mode — no deletions"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_MANAGE)),
):
    """Scan and soft-delete entities failing validation rules. dry_run=true by default."""
    try:
        from services.kg_cleanup_service import KGCleanupService

        svc = KGCleanupService(db)
        result = await svc.cleanup_invalid_entities(dry_run=dry_run)
        return CleanupInvalidResponse(**result)
    except Exception as e:
        logger.error(f"KG cleanup invalid error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cleanup/duplicates", response_model=DuplicateClustersResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def find_duplicate_clusters(
    request: Request,
    entity_type: str | None = Query(None, description="Filter by entity type"),
    threshold: float | None = Query(None, ge=0.5, le=1.0, description="Similarity threshold"),
    limit: int = Query(50, ge=1, le=200, description="Max clusters to return"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Find clusters of likely-duplicate entities via embedding similarity."""
    try:
        from services.kg_cleanup_service import KGCleanupService

        svc = KGCleanupService(db)
        clusters = await svc.find_duplicate_clusters(
            entity_type=entity_type,
            threshold=threshold,
            limit=limit,
        )
        return DuplicateClustersResponse(
            clusters=[DuplicateCluster(**c) for c in clusters],
            total_clusters=len(clusters),
        )
    except Exception as e:
        logger.error(f"KG find duplicates error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cleanup/merge-duplicates", response_model=MergeDuplicatesResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def merge_duplicate_clusters(
    request: Request,
    entity_type: str | None = Query(None, description="Filter by entity type"),
    threshold: float | None = Query(None, ge=0.5, le=1.0, description="Similarity threshold"),
    dry_run: bool = Query(True, description="Preview mode — no merges"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_MANAGE)),
):
    """Auto-merge duplicate entity clusters. dry_run=true by default."""
    try:
        from services.kg_cleanup_service import KGCleanupService

        svc = KGCleanupService(db)
        result = await svc.merge_duplicate_clusters(
            entity_type=entity_type,
            threshold=threshold,
            dry_run=dry_run,
        )
        return MergeDuplicatesResponse(**result)
    except Exception as e:
        logger.error(f"KG merge duplicates error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================================
# Merge-proposal review queue (Structured Memory Phase 1, T5/D3)
# =========================================================================

def _merge_brief(e, relation_counts: dict[int, int] | None = None) -> MergeProposalEntityBrief:
    counts = relation_counts or {}
    return MergeProposalEntityBrief(
        id=e.id,
        name=e.name,
        entity_type=e.entity_type,
        circle_tier=e.circle_tier or 0,
        mention_count=e.mention_count or 1,
        surface_forms=list(e.surface_forms or []),
        description=e.description,
        relation_count=counts.get(e.id, 0),
        first_seen_at=e.first_seen_at.isoformat() if e.first_seen_at else None,
        last_seen_at=e.last_seen_at.isoformat() if e.last_seen_at else None,
    )


def _proposal_to_response(
    p: KgMergeProposal, relation_counts: dict[int, int] | None = None
) -> MergeProposalResponse:
    return MergeProposalResponse(
        id=p.id,
        similarity=p.similarity,
        reason=p.reason,
        status=p.status,
        created_at=p.created_at.isoformat() if p.created_at else "",
        loser=_merge_brief(p.loser, relation_counts),
        winner=_merge_brief(p.winner, relation_counts),
    )


async def _owned_pending_proposal(db: AsyncSession, proposal_id: int, user: User | None) -> KgMergeProposal:
    p = (await db.execute(
        select(KgMergeProposal).where(KgMergeProposal.id == proposal_id)
    )).scalar_one_or_none()
    # uniform 404 for not-found AND not-owned (don't leak existence cross-user).
    # Single-user mode (AUTH_ENABLED=false) → user is None and owns everything,
    # so the ownership branch is skipped.
    uid = user.id if user else None
    if p is None or (uid is not None and p.user_id is not None and p.user_id != uid):
        raise HTTPException(status_code=404, detail="Merge proposal not found")
    return p


@router.get("/merge-proposals", response_model=MergeProposalsListResponse)
async def list_merge_proposals(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Pending entity-merge proposals owned by the caller (D3 review queue).

    Single-user mode (AUTH_ENABLED=false) → user is None and sees every pending
    proposal (consistent with the circle filter short-circuit in that mode)."""
    uid = user.id if user else None
    q = (
        select(KgMergeProposal)
        .options(
            selectinload(KgMergeProposal.loser),
            selectinload(KgMergeProposal.winner),
        )
        .where(KgMergeProposal.status == KG_MERGE_PROPOSAL_PENDING)
        .order_by(KgMergeProposal.similarity.desc(), KgMergeProposal.created_at.desc())
    )
    if uid is not None:
        q = q.where(KgMergeProposal.user_id == uid)
    rows = (await db.execute(q)).scalars().all()
    # Edge counts for every entity in the queue, in ONE aggregate — the owner
    # needs them to tell same-named entities apart, and a per-entity query would
    # be 2N round-trips over a queue that runs into the thousands.
    ids: set[int] = set()
    for p in rows:
        ids.add(p.loser_entity_id)
        ids.add(p.winner_entity_id)
    relation_counts: dict[int, int] = {}
    if ids:
        id_list = list(ids)
        counted = (await db.execute(
            select(KGRelation.subject_id, func.count())
            .where(KGRelation.subject_id.in_(id_list), KGRelation.is_active.is_(True))
            .group_by(KGRelation.subject_id)
        )).all()
        for ent_id, n in counted:
            relation_counts[ent_id] = relation_counts.get(ent_id, 0) + int(n)
        counted = (await db.execute(
            select(KGRelation.object_id, func.count())
            .where(KGRelation.object_id.in_(id_list), KGRelation.is_active.is_(True))
            .group_by(KGRelation.object_id)
        )).all()
        for ent_id, n in counted:
            relation_counts[ent_id] = relation_counts.get(ent_id, 0) + int(n)
    proposals = [_proposal_to_response(p, relation_counts) for p in rows]
    return MergeProposalsListResponse(proposals=proposals, total=len(proposals))


@router.post("/merge-proposals/{proposal_id}/approve", response_model=EntityResponse)
async def approve_merge_proposal(
    proposal_id: int,
    body: ApproveMergeRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Approve a pending proposal: merge into the survivor (tier=MIN), mark
    approved. Optional body.winner_id overrides which entity survives (D2).

    KG_VIEW (not KG_MANAGE): proposals are the caller's OWN duplicates surfaced
    in their /brain/review queue; ownership is enforced by _owned_pending_proposal.
    KG_MANAGE (admin) would dead-end the queue for the household owners it serves."""
    await _owned_pending_proposal(db, proposal_id, user)
    survivor = await KgReconcilerService(db).approve_proposal(
        proposal_id, resolved_by=user.id if user else None,
        winner_id=body.winner_id if body else None,
    )
    if survivor is None:
        raise HTTPException(status_code=409, detail="Proposal already resolved or merge was a no-op")
    return _entity_to_response(survivor)


@router.post("/merge-proposals/{proposal_id}/reject")
async def reject_merge_proposal(
    proposal_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Reject a pending proposal (no merge; keeps both entities). KG_VIEW +
    ownership (see approve) — the owner resolves their own review queue."""
    await _owned_pending_proposal(db, proposal_id, user)
    ok = await KgReconcilerService(db).reject_proposal(proposal_id, resolved_by=user.id if user else None)
    if not ok:
        raise HTTPException(status_code=409, detail="Proposal already resolved")
    return {"status": "rejected", "proposal_id": proposal_id}


@router.post("/merge-proposals/cluster", response_model=ClusterResolveResponse)
async def resolve_merge_cluster(
    body: ClusterResolveRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Resolve a whole NAME CLUSTER in one decision (merge all / reject all).

    The queue's unit of judgement is the cluster, not the pair: the owner decides
    once that a group of same-named entities is (or is not) the same thing. Only
    pairs the reconciler already proposed take part, and only SAME-TIER ones —
    a cross-tier pair changes an atom's reach and stays individually decidable
    (it is reported back in ``skipped_cross_tier``, never silently swept).

    KG_VIEW like approve/reject: this is the owner resolving their own queue.
    Ownership is enforced inside the service by filtering the proposals on
    ``user_id`` — an id the caller does not own simply contributes no pair.
    """
    uid = user.id if user else None
    res = await KgReconcilerService(db).resolve_cluster(
        user_id=uid,
        entity_ids=body.entity_ids,
        survivor_id=body.survivor_id,
        decision=body.decision,
        resolved_by=uid,
    )
    if res.notes and not (res.merged or res.approved or res.rejected):
        raise HTTPException(status_code=400, detail="; ".join(res.notes))
    return ClusterResolveResponse(
        merged=res.merged,
        approved=res.approved,
        rejected=res.rejected,
        skipped_cross_tier=res.skipped_cross_tier,
        notes=res.notes,
    )


@router.post("/reconciler/run", response_model=ReconcilerRunResponse)
async def run_reconciler(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Trigger a reconciler pass over the caller's own entities (auto-merge
    same-tier high-confidence dupes; queue cross-tier/gray-zone for review).
    KG_VIEW: acts only on the caller's own graph. Single-user mode
    (AUTH_ENABLED=false) → user is None → reconcile every active user's graph,
    aggregating the report (mirrors the boot scheduler)."""
    svc = KgReconcilerService(db)
    uid = user.id if user else None
    if uid is not None:
        report = await svc.run_for_user(uid)
        candidates, auto_merged, proposed, backfilled, notes = (
            report.candidates, report.auto_merged, report.proposed,
            report.embedded_backfilled, report.notes,
        )
    else:
        candidates = auto_merged = proposed = backfilled = 0
        notes: list[str] = []
        for active_uid in await svc.list_active_user_ids():
            r = await svc.run_for_user(active_uid)
            candidates += r.candidates
            auto_merged += r.auto_merged
            proposed += r.proposed
            backfilled += r.embedded_backfilled
            notes.extend(r.notes)
    return ReconcilerRunResponse(
        candidates=candidates,
        auto_merged=auto_merged,
        proposed=proposed,
        embedded_backfilled=backfilled,
        notes=notes,
    )
