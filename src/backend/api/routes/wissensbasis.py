"""
Wissensbasis API routes — native backend for the 3D Wissensgraph.

Serves the three shapes the frontend ``GraphView`` component consumes:

  - GET /api/wissensbasis/graph              → corpus clusters
  - GET /api/wissensbasis/focus?entity_id=   → entity neighborhood
  - GET /api/wissensbasis/search?q=          → entity suggestions

These mirror the Reva ``/api/wissensbasis/*`` contract so GraphView is
unchanged across Home (this) and Enterprise (Reva). Reva's richer endpoints
(``/trace``, ``/me/mix``, observed fields) are intentionally NOT implemented
here — they stay 404 in standalone Renfield, which is exactly what
``useWissensbasisAvailable`` keys off to keep the Reva-only side panels hidden.

All endpoints gate on ``Permission.KG_VIEW`` and are circle-filtered by the
asker via ``KGGraphService`` (see that module for the access model).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import require_permission
from services.database import get_db
from services.kg_graph_service import (
    DEFAULT_MAX_PER_HOP,
    SEARCH_LIMIT_DEFAULT,
    KGGraphService,
)
from utils.config import settings
from utils.log_safety import error_text_without_content

router = APIRouter()


# --- Response schemas (mirror src/frontend/src/api/resources/wissensbasis.ts) ---


class FocusEdge(BaseModel):
    from_entity: str
    to_entity: str
    relation: str


class Hub(BaseModel):
    entity_id: str
    name: str
    entity_type: str
    mention_count: int
    # Circle tier of the entity (0 self … 4 public) — drives the tier-token
    # node colour in the 3D scene. Defaulted for backward compatibility.
    circle_tier: int = 0


class Cluster(BaseModel):
    id: str
    label: str
    sub_label: str
    entity_count: int
    hubs: list[Hub]
    # Relations whose both endpoints are rendered hubs of this cluster —
    # real intra-cluster structure for the scene's filament lines.
    hub_edges: list[FocusEdge] = []
    color_seed: int
    namesake_entity_id: str | None


class GraphResponse(BaseModel):
    clusters: list[Cluster]
    total_entities: int
    total_relations: int
    truncated: bool


class FocusEntity(BaseModel):
    entity_id: str
    display_name: str
    entity_type: str
    importance: float
    circle_tier: int = 0


class FocusNeighborhood(BaseModel):
    focus: FocusEntity
    hop1: list[FocusEntity]
    hop2: list[FocusEntity]
    edges: list[FocusEdge]
    overflow_hop1: int
    overflow_hop2: int


class SearchHit(BaseModel):
    entity_id: str
    display_name: str
    entity_type: str
    mention_count: int


class SearchResults(BaseModel):
    items: list[SearchHit]


@router.get("/graph", response_model=GraphResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def get_graph(
    request: Request,
    lang: str = Query("de"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Corpus view — connected-component clusters of the most-mentioned entities."""
    try:
        svc = KGGraphService(db)
        return await svc.corpus(asker_id=user.id if user else None, lang=lang)
    except Exception as e:
        logger.error(f"Wissensbasis graph error: {error_text_without_content(e)}")
        raise HTTPException(status_code=500, detail=error_text_without_content(e))


@router.get("/focus", response_model=FocusNeighborhood)
@limiter.limit(settings.api_rate_limit_admin)
async def get_focus(
    request: Request,
    entity_id: str = Query(...),
    hops: int = Query(2, ge=1, le=2),
    max_per_hop: int = Query(DEFAULT_MAX_PER_HOP, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Neighborhood view — focus entity + hop1 (+hop2) it can reach.

    404 when the entity does not exist or is not accessible to the asker
    (identical response so an inaccessible entity's existence does not leak).
    """
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Entity not found")

    try:
        svc = KGGraphService(db)
        result = await svc.focus(
            entity_id=eid,
            asker_id=user.id if user else None,
            hops=hops,
            max_per_hop=max_per_hop,
        )
    except Exception as e:
        logger.error(f"Wissensbasis focus error: {error_text_without_content(e)}")
        raise HTTPException(status_code=500, detail=error_text_without_content(e))

    if result is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return result


@router.get("/search", response_model=SearchResults)
@limiter.limit(settings.api_rate_limit_admin)
async def search_entities(
    request: Request,
    q: str = Query(""),
    limit: int = Query(SEARCH_LIMIT_DEFAULT, ge=1, le=25),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.KG_VIEW)),
):
    """Name-substring entity suggestions for the search overlay."""
    try:
        svc = KGGraphService(db)
        items = await svc.search(
            query=q, asker_id=user.id if user else None, limit=limit
        )
        return {"items": items}
    except Exception as e:
        logger.error(f"Wissensbasis search error: {error_text_without_content(e)}")
        raise HTTPException(status_code=500, detail=error_text_without_content(e))
