"""
Pydantic schemas for Chat Upload API.
"""
from pydantic import BaseModel


class ChatUploadResponse(BaseModel):
    id: int
    filename: str
    file_type: str | None
    file_size: int | None
    status: str
    text_preview: str | None
    error_message: str | None
    created_at: str


class IndexRequest(BaseModel):
    knowledge_base_id: int


class IndexResponse(BaseModel):
    success: bool
    document_id: int | None = None
    knowledge_base_id: int | None = None
    chunk_count: int | None = None
    message: str


class PaperlessResponse(BaseModel):
    success: bool
    paperless_task_id: str | None = None
    message: str
