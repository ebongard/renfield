"""Pydantic schemas for Knowledge Graph API."""

from pydantic import BaseModel, Field


class CircleTierInfo(BaseModel):
    """One rung of the circle ladder."""
    tier: int           # 0..4 (self/trusted/household/extended/public)
    name: str           # canonical English label, e.g. "household"
    label: str          # Localized label (e.g., "Familie", "Household")
    description: str    # Localized description


class CircleTiersListResponse(BaseModel):
    """List of circle tiers (0..4) with localized labels."""
    tiers: list[CircleTierInfo]


class EntityCircleTierUpdate(BaseModel):
    """Update entity circle_tier (0..4)."""
    circle_tier: int = Field(ge=0, le=4)


class EntityResponse(BaseModel):
    id: int
    name: str
    entity_type: str
    description: str | None = None
    mention_count: int = 1
    first_seen_at: str = ""
    last_seen_at: str = ""
    circle_tier: int = 0  # 0=self, 1=trusted, 2=household, 3=extended, 4=public


class EntityUpdate(BaseModel):
    name: str | None = None
    entity_type: str | None = None
    description: str | None = None


class EntityBrief(BaseModel):
    id: int
    name: str
    entity_type: str


class RelationResponse(BaseModel):
    id: int
    subject: EntityBrief | None = None
    predicate: str
    object: EntityBrief | None = None
    confidence: float = 0.8
    created_at: str | None = None


class RelationCreate(BaseModel):
    """Create a new relation between two entities."""
    subject_id: int
    predicate: str
    object_id: int
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class RelationUpdate(BaseModel):
    """Update an existing relation."""
    predicate: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    subject_id: int | None = None
    object_id: int | None = None


class MergeEntitiesRequest(BaseModel):
    source_id: int
    target_id: int


class EntityListResponse(BaseModel):
    entities: list[EntityResponse]
    total: int
    page: int = 1
    size: int = 50


class RelationListResponse(BaseModel):
    relations: list[RelationResponse]
    total: int
    page: int = 1
    size: int = 50


class KGStatsResponse(BaseModel):
    entity_count: int = 0
    relation_count: int = 0
    entity_types: dict[str, int] = Field(default_factory=dict)


# =============================================================================
# Cleanup Schemas
# =============================================================================

class InvalidEntitySample(BaseModel):
    id: int
    name: str
    entity_type: str


class CleanupInvalidResponse(BaseModel):
    dry_run: bool
    total_scanned: int
    invalid_count: int
    orphaned_relations: int
    samples: list[InvalidEntitySample] = Field(default_factory=list)


class DuplicateEntityInfo(BaseModel):
    id: int
    name: str
    mention_count: int
    entity_type: str
    similarity: float | None = None


class DuplicateCluster(BaseModel):
    canonical: DuplicateEntityInfo
    duplicates: list[DuplicateEntityInfo]
    cluster_size: int
    entity_type: str


class DuplicateClustersResponse(BaseModel):
    clusters: list[DuplicateCluster]
    total_clusters: int


class MergeDuplicatesResponse(BaseModel):
    dry_run: bool
    clusters_found: int
    entities_merged: int
    clusters: list[dict] = Field(default_factory=list)
