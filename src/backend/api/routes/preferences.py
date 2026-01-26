"""
User Preferences API Routes

Manages user preferences like language settings.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from services.database import get_db
from services.auth_service import get_optional_user, get_current_user
from models.database import User
from utils.config import settings

router = APIRouter()


class LanguagePreference(BaseModel):
    """Language preference request/response"""
    language: str = Field(..., min_length=2, max_length=10, description="Language code (e.g., 'de', 'en')")


class PreferencesResponse(BaseModel):
    """Full preferences response"""
    language: str
    supported_languages: list[str]


@router.get("/language", response_model=LanguagePreference)
async def get_language_preference(
    user: User = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get the current user's language preference.

    If the user is authenticated, returns their stored preference.
    If not authenticated, returns the default language.
    """
    if user:
        return LanguagePreference(language=user.preferred_language)
    else:
        return LanguagePreference(language=settings.default_language)


@router.put("/language", response_model=LanguagePreference)
async def set_language_preference(
    pref: LanguagePreference,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Set the current user's language preference.

    Requires authentication.
    """
    # Check if user is authenticated (get_current_user returns None when auth is disabled)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required to save language preference"
        )

    # Validate language code
    if pref.language.lower() not in settings.supported_languages_list:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language '{pref.language}'. Supported: {settings.supported_languages_list}"
        )

    # Update user preference
    user.preferred_language = pref.language.lower()
    db.add(user)
    await db.commit()
    await db.refresh(user)

    logger.info(f"🌐 User {user.username} changed language to: {pref.language}")

    return LanguagePreference(language=user.preferred_language)


@router.get("", response_model=PreferencesResponse)
async def get_all_preferences(
    user: User = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all user preferences.

    Returns the user's preferences along with available options.
    """
    language = user.preferred_language if user else settings.default_language

    return PreferencesResponse(
        language=language,
        supported_languages=settings.supported_languages_list
    )
