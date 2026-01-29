"""
Feedback API Routes — Intent correction learning
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from typing import Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from services.database import get_db
from services.auth_service import get_current_user
from services.api_rate_limiter import limiter
from utils.config import settings
from models.database import User

router = APIRouter()

VALID_FEEDBACK_TYPES = {"intent", "agent_tool", "complexity"}


class CorrectionRequest(BaseModel):
    message_text: str
    feedback_type: str
    original_value: str
    corrected_value: str
    context: Optional[dict] = None

    @field_validator("feedback_type")
    @classmethod
    def validate_feedback_type(cls, v):
        if v not in VALID_FEEDBACK_TYPES:
            raise ValueError(f"feedback_type must be one of {VALID_FEEDBACK_TYPES}")
        return v


class CorrectionResponse(BaseModel):
    id: int
    message_text: str
    feedback_type: str
    original_value: str
    corrected_value: str
    created_at: str


@router.post("/correction", response_model=CorrectionResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def submit_correction(
    request: Request,
    body: CorrectionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Submit a correction for wrong intent/tool/complexity classification."""
    try:
        from services.intent_feedback_service import IntentFeedbackService
        service = IntentFeedbackService(db)

        correction = await service.save_correction(
            message_text=body.message_text,
            feedback_type=body.feedback_type,
            original_value=body.original_value,
            corrected_value=body.corrected_value,
            user_id=current_user.id if current_user else None,
            context=body.context,
        )

        return CorrectionResponse(
            id=correction.id,
            message_text=correction.message_text,
            feedback_type=correction.feedback_type,
            original_value=correction.original_value,
            corrected_value=correction.corrected_value,
            created_at=correction.created_at.isoformat(),
        )
    except Exception as e:
        logger.error(f"❌ Feedback error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/corrections")
async def list_corrections(
    feedback_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """List all corrections (admin view)."""
    try:
        if feedback_type and feedback_type not in VALID_FEEDBACK_TYPES:
            raise HTTPException(status_code=400, detail=f"Invalid feedback_type: {feedback_type}")

        from services.intent_feedback_service import IntentFeedbackService
        service = IntentFeedbackService(db)

        corrections = await service.list_corrections(
            feedback_type=feedback_type, limit=limit, offset=offset
        )
        total = await service.get_correction_count(feedback_type=feedback_type)

        return {
            "corrections": corrections,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ List corrections error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/corrections/{correction_id}")
async def delete_correction(
    correction_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user),
):
    """Delete a specific correction."""
    try:
        from services.intent_feedback_service import IntentFeedbackService
        service = IntentFeedbackService(db)

        success = await service.delete_correction(correction_id)
        if not success:
            raise HTTPException(status_code=404, detail="Correction not found")

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Delete correction error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
