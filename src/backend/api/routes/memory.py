"""
Memory API Routes — CRUD for conversation memories.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.memory_schemas import (
    MemoryCreateRequest,
    MemoryHistoryEntry,
    MemoryHistoryResponse,
    MemoryListResponse,
    MemoryResponse,
    MemoryUpdateRequest,
)
from models.database import User
from services.api_rate_limiter import limiter
from services.auth_service import get_current_user
from services.conversation_memory_service import ConversationMemoryService
from services.database import get_db
from utils.config import settings

router = APIRouter()


def _memory_to_response(memory) -> MemoryResponse:
    """Convert a ConversationMemory ORM object to a response schema."""
    return MemoryResponse(
        id=memory.id,
        content=memory.content,
        category=memory.category,
        importance=memory.importance,
        access_count=memory.access_count,
        created_at=memory.created_at.isoformat() if memory.created_at else "",
        last_accessed_at=memory.last_accessed_at.isoformat() if memory.last_accessed_at else None,
    )


@router.get("", response_model=MemoryListResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def list_memories(
    request: Request,
    category: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """List active memories with optional category filter."""
    try:
        service = ConversationMemoryService(db)
        user_id = current_user.id if current_user else None

        memories = await service.list_for_user(
            user_id=user_id,
            category=category,
            limit=limit,
            offset=offset,
        )
        total = await service.get_count(user_id=user_id, category=category)

        return MemoryListResponse(
            memories=[
                MemoryResponse(
                    id=m["id"],
                    content=m["content"],
                    category=m["category"],
                    importance=m["importance"],
                    access_count=m["access_count"],
                    created_at=m["created_at"] or "",
                    last_accessed_at=m.get("last_accessed_at"),
                )
                for m in memories
            ],
            total=total,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        logger.error(f"List memories error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("", response_model=MemoryResponse, status_code=201)
@limiter.limit(settings.api_rate_limit_chat)
async def create_memory(
    request: Request,
    body: MemoryCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Manually create a memory."""
    try:
        service = ConversationMemoryService(db)
        memory = await service.save(
            content=body.content,
            category=body.category,
            user_id=current_user.id if current_user else None,
            importance=body.importance,
        )

        if not memory:
            raise HTTPException(status_code=400, detail="Could not create memory")

        return _memory_to_response(memory)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create memory error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{memory_id}", response_model=MemoryResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def update_memory(
    request: Request,
    memory_id: int,
    body: MemoryUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Update a memory's content, category, or importance."""
    try:
        service = ConversationMemoryService(db)
        memory = await service.update(
            memory_id=memory_id,
            content=body.content,
            category=body.category,
            importance=body.importance,
        )

        if not memory:
            raise HTTPException(status_code=404, detail="Memory not found")

        return _memory_to_response(memory)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update memory error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{memory_id}/history", response_model=MemoryHistoryResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def get_memory_history(
    request: Request,
    memory_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Get modification history for a memory."""
    try:
        service = ConversationMemoryService(db)
        entries = await service.get_history(memory_id)

        return MemoryHistoryResponse(
            entries=[
                MemoryHistoryEntry(
                    id=e["id"],
                    memory_id=e["memory_id"],
                    action=e["action"],
                    old_content=e.get("old_content"),
                    old_category=e.get("old_category"),
                    old_importance=e.get("old_importance"),
                    new_content=e.get("new_content"),
                    new_category=e.get("new_category"),
                    new_importance=e.get("new_importance"),
                    changed_by=e["changed_by"],
                    created_at=e["created_at"] or "",
                )
                for e in entries
            ],
            total=len(entries),
        )
    except Exception as e:
        logger.error(f"Get memory history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{memory_id}")
@limiter.limit(settings.api_rate_limit_chat)
async def delete_memory(
    request: Request,
    memory_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Soft-delete a memory (set is_active=False)."""
    try:
        service = ConversationMemoryService(db)
        success = await service.delete(memory_id)

        if not success:
            raise HTTPException(status_code=404, detail="Memory not found")

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete memory error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
