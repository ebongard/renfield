"""
User Preferences API Routes

Manages user preferences like language settings.
"""
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import User
from services.auth_service import get_current_user, get_optional_user
from services.database import get_db
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


# --------------------------------------------------------------------------- call number (#1683)
# The user's OWN number Renfield may call ("ruf mich an"). Set only through a code
# call: PUT starts it (the number is NOT stored yet), POST /verify stores it.
# Dark: 404 unless TELEPHONY_OUTBOUND_ENABLED. Auth off resolves every caller to
# the same anonymous context -- there is no "own" number then, so it is refused.


class CallNumberStatus(BaseModel):
    number: str | None = None
    verified_at: str | None = None
    pending: str | None = None  # number awaiting its code


class CallNumberRequest(BaseModel):
    number: str = Field(..., min_length=3, max_length=40)


class CallNumberCode(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


def _call_number_user(user: User | None) -> User:
    from models.permissions import Permission
    from services import telephony_outbound

    if not telephony_outbound.enabled():
        raise HTTPException(status_code=404, detail="Not found")
    if not settings.auth_enabled or user is None:
        raise HTTPException(status_code=403, detail="Nur mit Anmeldung verfügbar")
    if not user.has_permission(Permission.TELEPHONY_CALL.value):
        raise HTTPException(status_code=403, detail="Keine Berechtigung für Anrufe")
    return user


@router.get("/call-number", response_model=CallNumberStatus)
async def get_call_number(user: User = Depends(get_current_user)):
    from services import telephony_outbound

    user = _call_number_user(user)
    return CallNumberStatus(
        number=user.call_number,
        verified_at=user.call_number_verified_at.isoformat() if user.call_number_verified_at else None,
        pending=await telephony_outbound.pending_verification(user.id),
    )


@router.put("/call-number", response_model=CallNumberStatus)
async def start_call_number(req: CallNumberRequest, user: User = Depends(get_current_user)):
    """Ring the number and speak a code; nothing is stored until it is confirmed."""
    from services import telephony_outbound

    user = _call_number_user(user)
    try:
        await telephony_outbound.start_verification(user.id, req.number)
    except telephony_outbound.CallError as e:
        raise HTTPException(status_code=400, detail=e.reason) from None
    return CallNumberStatus(
        number=user.call_number,
        verified_at=user.call_number_verified_at.isoformat() if user.call_number_verified_at else None,
        pending=await telephony_outbound.pending_verification(user.id),
    )


@router.post("/call-number/verify", response_model=CallNumberStatus)
async def verify_call_number(
    req: CallNumberCode, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    from datetime import UTC, datetime

    from services import telephony_outbound

    user = _call_number_user(user)
    number = await telephony_outbound.confirm_verification(user.id, req.code)
    if number is None:
        raise HTTPException(status_code=400, detail="Der Code stimmt nicht oder ist abgelaufen.")
    user.call_number = number
    user.call_number_verified_at = datetime.now(UTC).replace(tzinfo=None)
    db.add(user)
    await db.commit()
    logger.info(f"📞 User {user.id} confirmed a call number")
    return CallNumberStatus(number=user.call_number, verified_at=user.call_number_verified_at.isoformat())


@router.delete("/call-number", response_model=CallNumberStatus)
async def delete_call_number(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from services import telephony_outbound

    user = _call_number_user(user)
    await telephony_outbound.cancel_verification(user.id)
    user.call_number = None
    user.call_number_verified_at = None
    db.add(user)
    await db.commit()
    return CallNumberStatus()
