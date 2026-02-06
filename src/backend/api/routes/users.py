"""
User Management API Routes

Provides endpoints for managing users:
- List all users (admin)
- Get user details (admin)
- Create user (admin)
- Update user (admin)
- Delete user (admin)
- Reset password (admin)
- Link/unlink speaker (admin)
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import Speaker, User
from models.permissions import Permission
from services.auth_service import (
    get_password_hash,
    get_role_by_id,
    require_permission,
    validate_password,
)
from services.database import get_db

router = APIRouter()


def _escape_like(value: str) -> str:
    """Escape LIKE special characters to prevent wildcard injection."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# =============================================================================
# Request/Response Models
# =============================================================================

class UserResponse(BaseModel):
    """Response model for user information."""
    id: int
    username: str
    email: str | None
    role_id: int
    role_name: str
    permissions: list[str]
    is_active: bool
    speaker_id: int | None
    speaker_name: str | None
    created_at: datetime
    updated_at: datetime
    last_login: datetime | None

    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    """Response model for user list."""
    users: list[UserResponse]
    total: int
    page: int
    page_size: int


class CreateUserRequest(BaseModel):
    """Request model for creating a user."""
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=8)
    email: EmailStr | None = None
    role_id: int
    is_active: bool = True


class UpdateUserRequest(BaseModel):
    """Request model for updating a user."""
    username: str | None = Field(None, min_length=3, max_length=100)
    email: EmailStr | None = None
    role_id: int | None = None
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    """Request model for resetting a user's password."""
    new_password: str = Field(..., min_length=8)


class LinkSpeakerRequest(BaseModel):
    """Request model for linking a speaker to a user."""
    speaker_id: int


# =============================================================================
# Endpoints
# =============================================================================

@router.get("", response_model=UserListResponse)
async def list_users(
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    role_id: int | None = None,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.USERS_VIEW))
):
    """
    List all users with optional filtering.

    Requires: users.view permission
    """
    # Base query
    query = select(User).options(selectinload(User.role), selectinload(User.speaker))

    # Apply filters
    if search:
        safe_search = _escape_like(search)
        query = query.where(
            User.username.ilike(f"%{safe_search}%") |
            User.email.ilike(f"%{safe_search}%")
        )
    if role_id:
        query = query.where(User.role_id == role_id)
    if is_active is not None:
        query = query.where(User.is_active == is_active)

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination
    offset = (page - 1) * page_size
    query = query.order_by(User.username).offset(offset).limit(page_size)

    result = await db.execute(query)
    users = result.scalars().all()

    return UserListResponse(
        users=[
            UserResponse(
                id=user.id,
                username=user.username,
                email=user.email,
                role_id=user.role_id,
                role_name=user.role.name if user.role else "Unknown",
                permissions=user.get_permissions(),
                is_active=user.is_active,
                speaker_id=user.speaker_id,
                speaker_name=user.speaker.name if user.speaker else None,
                created_at=user.created_at,
                updated_at=user.updated_at,
                last_login=user.last_login
            )
            for user in users
        ],
        total=total,
        page=page,
        page_size=page_size
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.USERS_VIEW))
):
    """
    Get a specific user by ID.

    Requires: users.view permission
    """
    result = await db.execute(
        select(User)
        .options(selectinload(User.role), selectinload(User.speaker))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role_id=user.role_id,
        role_name=user.role.name if user.role else "Unknown",
        permissions=user.get_permissions(),
        is_active=user.is_active,
        speaker_id=user.speaker_id,
        speaker_name=user.speaker.name if user.speaker else None,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login=user.last_login
    )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: CreateUserRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.USERS_MANAGE))
):
    """
    Create a new user.

    Requires: users.manage permission
    """
    # Check username uniqueness
    existing = await db.execute(select(User).where(User.username == request.username))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists"
        )

    # Check email uniqueness if provided
    if request.email:
        existing = await db.execute(select(User).where(User.email == request.email))
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

    # Verify role exists
    role = await get_role_by_id(db, request.role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role not found"
        )

    # Validate password
    is_valid, error = validate_password(request.password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error
        )

    # Create user
    user = User(
        username=request.username,
        email=request.email,
        password_hash=get_password_hash(request.password),
        role_id=request.role_id,
        is_active=request.is_active
    )

    db.add(user)
    await db.commit()
    await db.refresh(user, ["role"])

    logger.info(f"Created user: {user.username} by {current_user.username if current_user else 'system'}")

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role_id=user.role_id,
        role_name=role.name,
        permissions=user.get_permissions(),
        is_active=user.is_active,
        speaker_id=user.speaker_id,
        speaker_name=None,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login=user.last_login
    )


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    request: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.USERS_MANAGE))
):
    """
    Update a user.

    Requires: users.manage permission
    """
    result = await db.execute(
        select(User)
        .options(selectinload(User.role), selectinload(User.speaker))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Check username uniqueness if changing
    if request.username and request.username != user.username:
        existing = await db.execute(select(User).where(User.username == request.username))
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already exists"
            )
        user.username = request.username

    # Check email uniqueness if changing
    if request.email is not None and request.email != user.email:
        if request.email:  # Not removing email
            existing = await db.execute(select(User).where(User.email == request.email))
            if existing.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already registered"
                )
        user.email = request.email

    # Update role if specified
    if request.role_id is not None and request.role_id != user.role_id:
        role = await get_role_by_id(db, request.role_id)
        if not role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Role not found"
            )
        user.role_id = request.role_id

    # Update active status
    if request.is_active is not None:
        # Prevent deactivating yourself
        if current_user and user.id == current_user.id and not request.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deactivate your own account"
            )
        user.is_active = request.is_active

    await db.commit()
    await db.refresh(user, ["role", "speaker"])

    logger.info(f"Updated user: {user.username} by {current_user.username if current_user else 'system'}")

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role_id=user.role_id,
        role_name=user.role.name if user.role else "Unknown",
        permissions=user.get_permissions(),
        is_active=user.is_active,
        speaker_id=user.speaker_id,
        speaker_name=user.speaker.name if user.speaker else None,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login=user.last_login
    )


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.USERS_MANAGE))
):
    """
    Delete a user.

    Cannot delete yourself.

    Requires: users.manage permission
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Prevent self-deletion
    if current_user and user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account"
        )

    username = user.username
    await db.delete(user)
    await db.commit()

    logger.info(f"Deleted user: {username} by {current_user.username if current_user else 'system'}")

    return {"message": f"User '{username}' deleted successfully"}


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    request: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.USERS_MANAGE))
):
    """
    Reset a user's password.

    Requires: users.manage permission
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Validate password
    is_valid, error = validate_password(request.new_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error
        )

    user.password_hash = get_password_hash(request.new_password)
    await db.commit()

    logger.info(f"Password reset for user: {user.username} by {current_user.username if current_user else 'system'}")

    return {"message": f"Password reset for user '{user.username}'"}


@router.post("/{user_id}/link-speaker", response_model=UserResponse)
async def link_speaker(
    user_id: int,
    request: LinkSpeakerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.USERS_MANAGE))
):
    """
    Link a speaker profile to a user for voice authentication.

    Requires: users.manage permission
    """
    # Get user
    result = await db.execute(
        select(User)
        .options(selectinload(User.role))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Get speaker
    result = await db.execute(select(Speaker).where(Speaker.id == request.speaker_id))
    speaker = result.scalar_one_or_none()

    if not speaker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Speaker not found"
        )

    # Check if speaker is already linked to another user
    result = await db.execute(
        select(User).where(User.speaker_id == request.speaker_id)
    )
    existing_link = result.scalar_one_or_none()

    if existing_link and existing_link.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Speaker is already linked to user '{existing_link.username}'"
        )

    user.speaker_id = request.speaker_id
    await db.commit()
    await db.refresh(user, ["speaker"])

    logger.info(f"Linked speaker '{speaker.name}' to user '{user.username}' by {current_user.username if current_user else 'system'}")

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role_id=user.role_id,
        role_name=user.role.name if user.role else "Unknown",
        permissions=user.get_permissions(),
        is_active=user.is_active,
        speaker_id=user.speaker_id,
        speaker_name=speaker.name,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login=user.last_login
    )


@router.delete("/{user_id}/link-speaker", response_model=UserResponse)
async def unlink_speaker(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission(Permission.USERS_MANAGE))
):
    """
    Unlink a speaker profile from a user.

    Requires: users.manage permission
    """
    result = await db.execute(
        select(User)
        .options(selectinload(User.role))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    if not user.speaker_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no linked speaker"
        )

    user.speaker_id = None
    await db.commit()

    logger.info(f"Unlinked speaker from user '{user.username}' by {current_user.username if current_user else 'system'}")

    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role_id=user.role_id,
        role_name=user.role.name if user.role else "Unknown",
        permissions=user.get_permissions(),
        is_active=user.is_active,
        speaker_id=None,
        speaker_name=None,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login=user.last_login
    )
