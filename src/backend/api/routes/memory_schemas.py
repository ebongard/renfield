"""
Pydantic schemas for Memory API endpoints.
"""

from pydantic import BaseModel, Field, field_validator

from models.database import MEMORY_CATEGORIES


class MemoryCreateRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    category: str
    importance: float = Field(default=0.5, ge=0.1, le=1.0)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v):
        if v not in MEMORY_CATEGORIES:
            raise ValueError(f"category must be one of {MEMORY_CATEGORIES}")
        return v


class MemoryUpdateRequest(BaseModel):
    content: str | None = Field(None, min_length=1, max_length=2000)
    category: str | None = None
    importance: float | None = Field(None, ge=0.1, le=1.0)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v):
        if v is not None and v not in MEMORY_CATEGORIES:
            raise ValueError(f"category must be one of {MEMORY_CATEGORIES}")
        return v


class MemoryResponse(BaseModel):
    id: int
    content: str
    category: str
    importance: float
    access_count: int
    created_at: str
    last_accessed_at: str | None


class MemoryListResponse(BaseModel):
    memories: list[MemoryResponse]
    total: int
    limit: int
    offset: int
