"""
Authentication API Routes

Provides endpoints for user authentication:
- Login (username/password → JWT tokens)
- Register (create new user account)
- Refresh (get new access token using refresh token)
- Me (get current user info)
"""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from loguru import logger

from services.database import get_db
from services.auth_service import (
    authenticate_user,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_user_by_id,
    create_user,
    get_role_by_name,
    require_auth,
    get_current_user,
    validate_password,
)
from models.database import User, Role
from models.permissions import get_all_permissions
from utils.config import settings

router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class TokenResponse(BaseModel):
    """Response model for successful authentication."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseModel):
    """Request model for token refresh."""
    refresh_token: str


class RegisterRequest(BaseModel):
    """Request model for user registration."""
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=8)
    email: Optional[EmailStr] = None


class UserResponse(BaseModel):
    """Response model for user information."""
    id: int
    username: str
    email: Optional[str]
    role: str
    role_id: int
    permissions: list[str]
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime]
    speaker_id: Optional[int]

    class Config:
        from_attributes = True


class ChangePasswordRequest(BaseModel):
    """Request model for password change."""
    current_password: str
    new_password: str = Field(..., min_length=8)


class AuthStatusResponse(BaseModel):
    """Response model for authentication status."""
    auth_enabled: bool
    allow_registration: bool
    authenticated: bool
    user: Optional[UserResponse] = None


# =============================================================================
# Endpoints
# =============================================================================

@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    """
    Authenticate user and return JWT tokens.

    Uses OAuth2 password flow (username + password in form data).
    Returns access token (short-lived) and refresh token (long-lived).
    """
    user = await authenticate_user(db, form_data.username, form_data.password)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Update last login time
    user.last_login = datetime.utcnow()
    await db.commit()

    # Create tokens
    access_token = create_access_token(
        data={"sub": str(user.id), "username": user.username}
    )
    refresh_token = create_refresh_token(user.id)

    logger.info(f"User logged in: {user.username}")

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Get a new access token using a refresh token.

    Refresh tokens are long-lived and can only be used to get new access tokens.
    """
    payload = decode_token(request.refresh_token)

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await get_user_by_id(db, int(user_id))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create new tokens
    access_token = create_access_token(
        data={"sub": str(user.id), "username": user.username}
    )
    new_refresh_token = create_refresh_token(user.id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Register a new user account.

    New users are assigned the "Gast" (Guest) role by default.
    Registration can be disabled via settings.
    """
    if not settings.allow_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled"
        )

    # Validate password
    is_valid, error = validate_password(request.password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error
        )

    # Get default role (Gast)
    default_role = await get_role_by_name(db, "Gast")
    if not default_role:
        # Fallback: get any non-admin role
        result = await db.execute(
            select(Role).where(Role.name != "Admin").limit(1)
        )
        default_role = result.scalar_one_or_none()

        if not default_role:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="No default role available"
            )

    # Create user
    user = await create_user(
        db=db,
        username=request.username,
        password=request.password,
        role_id=default_role.id,
        email=request.email
    )

    logger.info(f"New user registered: {user.username}")

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role.name,
        role_id=user.role_id,
        permissions=user.get_permissions(),
        is_active=user.is_active,
        created_at=user.created_at,
        last_login=user.last_login,
        speaker_id=user.speaker_id
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    user: User = Depends(require_auth)
):
    """
    Get information about the currently authenticated user.

    Returns user details including role and permissions.
    """
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role.name,
        role_id=user.role_id,
        permissions=user.get_permissions(),
        is_active=user.is_active,
        created_at=user.created_at,
        last_login=user.last_login,
        speaker_id=user.speaker_id
    )


@router.post("/change-password")
async def change_password(
    request: ChangePasswordRequest,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db)
):
    """
    Change the current user's password.

    Requires the current password for verification.
    """
    from services.auth_service import verify_password, get_password_hash

    # Verify current password
    if not verify_password(request.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect"
        )

    # Validate new password
    is_valid, error = validate_password(request.new_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error
        )

    # Update password
    user.password_hash = get_password_hash(request.new_password)
    await db.commit()

    logger.info(f"Password changed for user: {user.username}")

    return {"message": "Password changed successfully"}


@router.get("/status", response_model=AuthStatusResponse)
async def get_auth_status(
    user: Optional[User] = Depends(get_current_user)
):
    """
    Get authentication status and settings.

    Returns whether auth is enabled, if user is authenticated, etc.
    Useful for frontend to determine what to show.
    """
    user_response = None
    if user:
        user_response = UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role.name,
            role_id=user.role_id,
            permissions=user.get_permissions(),
            is_active=user.is_active,
            created_at=user.created_at,
            last_login=user.last_login,
            speaker_id=user.speaker_id
        )

    return AuthStatusResponse(
        auth_enabled=settings.auth_enabled,
        allow_registration=settings.allow_registration,
        authenticated=user is not None,
        user=user_response
    )


@router.get("/permissions")
async def list_all_permissions():
    """
    List all available permissions in the system.

    Useful for admin UIs when creating/editing roles.
    """
    return get_all_permissions()


# =============================================================================
# Voice Authentication Endpoints
# =============================================================================

class VoiceAuthResponse(BaseModel):
    """Response model for voice authentication."""
    success: bool
    speaker_id: Optional[int] = None
    speaker_name: Optional[str] = None
    confidence: float = 0.0
    user_id: Optional[int] = None
    username: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    message: str


@router.post("/voice", response_model=VoiceAuthResponse)
async def voice_authenticate(
    audio_file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Authenticate using voice (speaker recognition).

    Requires:
    - voice_auth_enabled in settings
    - Speaker profile linked to a User account

    Process:
    1. Receive audio file
    2. Run speaker recognition
    3. If speaker identified with confidence >= threshold:
       - Check if speaker is linked to a User
       - If linked, return JWT tokens
    4. Otherwise, return identification result without tokens
    """
    from services.speaker_service import SpeakerService

    if not settings.voice_auth_enabled:
        return VoiceAuthResponse(
            success=False,
            message="Voice authentication is disabled"
        )

    # Read audio file
    audio_bytes = await audio_file.read()

    if len(audio_bytes) == 0:
        return VoiceAuthResponse(
            success=False,
            message="Empty audio file"
        )

    try:
        # Get speaker service
        speaker_service = SpeakerService()

        # Identify speaker
        result = speaker_service.identify_speaker(audio_bytes)

        if not result or not result.get("speaker_id"):
            return VoiceAuthResponse(
                success=False,
                confidence=result.get("confidence", 0.0) if result else 0.0,
                message="Speaker not recognized"
            )

        speaker_id = result["speaker_id"]
        confidence = result.get("confidence", 0.0)
        speaker_name = result.get("name", "Unknown")

        # Check confidence threshold
        if confidence < settings.voice_auth_min_confidence:
            return VoiceAuthResponse(
                success=False,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                confidence=confidence,
                message=f"Confidence too low ({confidence:.2f} < {settings.voice_auth_min_confidence})"
            )

        # Check if speaker is linked to a user
        from models.database import Speaker, User
        from sqlalchemy.orm import selectinload

        speaker_result = await db.execute(
            select(Speaker).where(Speaker.id == speaker_id)
        )
        speaker = speaker_result.scalar_one_or_none()

        if not speaker:
            return VoiceAuthResponse(
                success=False,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                confidence=confidence,
                message="Speaker profile not found in database"
            )

        # Check if speaker is linked to a user
        user_result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(User.speaker_id == speaker_id)
        )
        user = user_result.scalar_one_or_none()

        if not user:
            return VoiceAuthResponse(
                success=False,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                confidence=confidence,
                message="Speaker is not linked to a user account"
            )

        if not user.is_active:
            return VoiceAuthResponse(
                success=False,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                confidence=confidence,
                user_id=user.id,
                username=user.username,
                message="User account is disabled"
            )

        # Success! Generate tokens
        user.last_login = datetime.utcnow()
        await db.commit()

        access_token = create_access_token(
            data={"sub": str(user.id), "username": user.username}
        )
        refresh_token = create_refresh_token(user.id)

        logger.info(f"Voice authentication successful: {user.username} (speaker: {speaker_name}, confidence: {confidence:.2f})")

        return VoiceAuthResponse(
            success=True,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            confidence=confidence,
            user_id=user.id,
            username=user.username,
            access_token=access_token,
            refresh_token=refresh_token,
            message="Voice authentication successful"
        )

    except Exception as e:
        logger.error(f"Voice authentication error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return VoiceAuthResponse(
            success=False,
            message=f"Voice authentication error: {str(e)}"
        )
