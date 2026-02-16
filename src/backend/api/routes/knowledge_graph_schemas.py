"""Pydantic schemas for Knowledge Graph API."""

from pydantic import BaseModel, Field


class ScopeInfo(BaseModel):
    """Scope information."""
    name: str           # e.g., "personal", "family", "public"
    label: str          # Localized label (e.g., "Familie", "Family")
    description: str    # Localized description


class ScopesListResponse(BaseModel):
    """List of available scopes."""
    scopes: list[ScopeInfo]


class EntityScopeUpdate(BaseModel):
    """Update entity scope."""
    scope: str  # e.g., "personal", "family", "public", or any custom scope from YAML


class EntityResponse(BaseModel):
    id: int
    name: str
    entity_type: str
    description: str | None = None
    mention_count: int = 1
    first_seen_at: str = ""
    last_seen_at: str = ""
    scope: str = "personal"  # New field


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
