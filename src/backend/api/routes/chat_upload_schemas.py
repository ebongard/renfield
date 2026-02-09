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
