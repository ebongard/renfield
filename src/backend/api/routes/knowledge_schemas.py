"""
Pydantic schemas for Knowledge API

Extracted from knowledge.py for better maintainability.
"""

from pydantic import BaseModel, Field

# --- Knowledge Base Models ---

class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    is_public: bool = False


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_public: bool | None = None
    is_active: bool | None = None


class KnowledgeBaseResponse(BaseModel):
    id: int
    name: str
    description: str | None
    is_active: bool
    is_public: bool = False
    owner_id: int | None = None
    owner_username: str | None = None
    document_count: int = 0
    created_at: str
    updated_at: str
    permission: str | None = None  # User's permission level on this KB


# --- Permission Models ---

class KBPermissionCreate(BaseModel):
    user_id: int
    permission: str = Field(..., pattern="^(read|write|admin)$")


class KBPermissionResponse(BaseModel):
    id: int
    user_id: int
    username: str
    permission: str
    granted_by: int | None
    granted_by_username: str | None
    created_at: str


# --- Document Models ---

class DocumentResponse(BaseModel):
    id: int
    filename: str
    title: str | None
    file_type: str | None
    file_size: int | None
    status: str
    error_message: str | None
    chunk_count: int
    page_count: int | None
    knowledge_base_id: int | None
    created_at: str
    processed_at: str | None


# --- Search Models ---

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    knowledge_base_id: int | None = None
    similarity_threshold: float | None = Field(default=None, ge=0, le=1)


class SearchResultChunk(BaseModel):
    id: int
    content: str
    chunk_index: int
    page_number: int | None
    section_title: str | None
    chunk_type: str


class SearchResultDocument(BaseModel):
    id: int
    filename: str
    title: str | None


class SearchResult(BaseModel):
    chunk: SearchResultChunk
    document: SearchResultDocument
    similarity: float


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    count: int


# --- Stats Models ---

class MoveDocumentsRequest(BaseModel):
    document_ids: list[int] = Field(..., min_length=1)
    target_knowledge_base_id: int


class StatsResponse(BaseModel):
    document_count: int
    completed_documents: int
    chunk_count: int
    knowledge_base_count: int
    embedding_model: str
    embedding_dimension: int
