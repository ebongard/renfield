"""
Pydantic schemas for Knowledge API

Extracted from knowledge.py for better maintainability.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_serializer

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
    # Whether the user may delete DOCUMENTS in this KB: owner, kb.all, or a share
    # carrying the owner's delete switch. No level implies it. The KB itself is
    # can_delete_kb.
    can_delete: bool = False
    # Whether the user may delete the KB itself: owner or kb.all only.
    can_delete_kb: bool = False


# --- Permission Models ---

class KBPermissionCreate(BaseModel):
    user_id: int
    permission: str = Field(..., pattern="^(read|write|admin)$")
    # The owner's separate delete switch. Omitted (None) = keep the stored value
    # (FALSE for a new share). Only the KB owner or kb.all may send it.
    can_delete: bool | None = None


class KBPermissionResponse(BaseModel):
    """One entry of a KB's share list -- the contract of the frontend's
    ``KbShare`` (``src/frontend/src/api/resources/knowledge.ts``,
    ``isKbLevelShare``).

    ``kind="kb_share"``: a ``kb_shares`` row -- ``permission`` is its level and
    ``documents`` is ABSENT from the JSON. ``kind="document_grants"``: a person
    WITHOUT a KB share who still holds grants on documents of this KB (a legacy
    share the backfill did not convert, #1446) -- ``permission`` is null (no KB
    level), ``can_delete`` is always false, ``documents`` counts the documents
    the grants open. Both kinds are revoked the same way
    (DELETE .../permissions/{id}). ``kind`` is required: every row says which
    it is.
    """

    id: int
    user_id: int
    username: str
    kind: Literal["kb_share", "document_grants"]
    permission: Literal["read", "write", "admin"] | None
    documents: int | None = None
    granted_by: int | None
    granted_by_username: str | None
    created_at: str
    can_delete: bool = False

    @model_serializer(mode="wrap")
    def _omit_documents_on_a_share(self, handler):
        data = handler(self)
        if self.documents is None:
            data.pop("documents", None)
        return data


# --- Document Models ---


class DocumentProgressPages(BaseModel):
    """Current/total page counters for paginated inputs (PDF, DOCX).
    Absent on non-paginated content (TXT, PNG)."""

    current: int
    total: int


class DocumentResponse(BaseModel):
    id: int
    filename: str
    title: str | None
    # UI display name: LLM-synthesized facts title → metadata title → filename.
    # Always populated; defaults to None for legacy callers that don't set it.
    display_name: str | None = None
    file_type: str | None
    file_size: int | None
    status: str
    error_message: str | None
    chunk_count: int
    page_count: int | None
    knowledge_base_id: int | None
    created_at: str
    processed_at: str | None
    # The document's own date (invoice/letter date), for the /wissen/dokumente
    # "Dokumentdatum" sort — distinct from created_at (import). NULL when none
    # derivable. Migration pc20260831.
    document_date: str | None = None
    # Integration upload status for the per-row status icons: whether the doc has
    # been filed in Paperless / uploaded to Simba (xidra). Default False.
    in_paperless: bool = False
    #: A ready-made link to the document in Paperless, or None.
    #:
    #: 🛑 The field MUST stand here, not only in the serializer: Pydantic drops
    #: unknown keys SILENTLY when building the model
    #: (`DocumentResponse(**kwargs)`), so the field would never reach the
    #: frontend and nothing would report it. Found as C1 in the review of this
    #: plan, before it was built.
    paperless_url: str | None = None
    in_simba: bool = False
    # Circle visibility (tier-control UX). circle_tier is the document's
    # denormalized tier (0 self … 4 public); atom_id is its atoms-row UUID
    # (nullable for legacy / global-RAG docs that were never atom-registered).
    # Both default so flag-off / legacy callers still serialize cleanly.
    circle_tier: int = 0
    atom_id: str | None = None
    # Whether the caller may delete this document — the rule of
    # check_document_access(..., "delete"), batch-computed by the document
    # list. None where an endpoint does not compute it.
    can_delete: bool | None = None
    # Live progress fields (#388). All three default to None so legacy
    # callers that don't augment the response (flag off path) still
    # serialize cleanly.
    stage: str | None = None
    pages: DocumentProgressPages | None = None
    queue_position: int | None = None


class SetDocumentTierRequest(BaseModel):
    """Set a single document's circle tier (0 self … 4 public)."""
    tier: int = Field(..., ge=0, le=4)


class BulkSetDocumentTierRequest(BaseModel):
    """Set the circle tier for several documents at once (mirrors
    MoveDocumentsRequest's bulk shape)."""
    document_ids: list[int] = Field(..., min_length=1)
    tier: int = Field(..., ge=0, le=4)


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
