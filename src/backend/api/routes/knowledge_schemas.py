"""
Pydantic schemas for Knowledge API

Extracted from knowledge.py for better maintainability.
"""
from typing import List, Optional
from pydantic import BaseModel, Field


# --- Knowledge Base Models ---

class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    is_public: bool = False


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    is_public: Optional[bool] = None
    is_active: Optional[bool] = None


class KnowledgeBaseResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    is_active: bool
    is_public: bool = False
    owner_id: Optional[int] = None
    owner_username: Optional[str] = None
    document_count: int = 0
    created_at: str
    updated_at: str
    permission: Optional[str] = None  # User's permission level on this KB


# --- Permission Models ---

class KBPermissionCreate(BaseModel):
    user_id: int
    permission: str = Field(..., pattern="^(read|write|admin)$")


class KBPermissionResponse(BaseModel):
    id: int
    user_id: int
    username: str
    permission: str
    granted_by: Optional[int]
    granted_by_username: Optional[str]
    created_at: str


# --- Document Models ---

class DocumentResponse(BaseModel):
    id: int
    filename: str
    title: Optional[str]
    file_type: Optional[str]
    file_size: Optional[int]
    status: str
    error_message: Optional[str]
    chunk_count: int
    page_count: Optional[int]
    knowledge_base_id: Optional[int]
    created_at: str
    processed_at: Optional[str]


# --- Search Models ---

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    knowledge_base_id: Optional[int] = None
    similarity_threshold: Optional[float] = Field(default=None, ge=0, le=1)


class SearchResultChunk(BaseModel):
    id: int
    content: str
    chunk_index: int
    page_number: Optional[int]
    section_title: Optional[str]
    chunk_type: str


class SearchResultDocument(BaseModel):
    id: int
    filename: str
    title: Optional[str]


class SearchResult(BaseModel):
    chunk: SearchResultChunk
    document: SearchResultDocument
    similarity: float


class SearchResponse(BaseModel):
    query: str
    results: List[SearchResult]
    count: int


# --- Stats Models ---

class StatsResponse(BaseModel):
    document_count: int
    completed_documents: int
    chunk_count: int
    knowledge_base_count: int
    embedding_model: str
    embedding_dimension: int
