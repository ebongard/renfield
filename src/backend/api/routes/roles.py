"""
Role Management API Routes

Provides endpoints for managing user roles:
- List all roles
- Create new role
- Update role (name, description, permissions)
- Delete role (non-system roles only)
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Role, User
from models.permissions import Permission, get_all_permissions, get_mcp_permissions
from services.auth_service import require_permission
from services.database import get_db

router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class RoleResponse(BaseModel):
    """Response model for role information."""
    id: int
    name: str
    description: str | None
    permissions: list[str]
    is_system: bool
    user_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CreateRoleRequest(BaseModel):
    """Request model for creating a role."""
    name: str = Field(..., min_length=2, max_length=50)
    description: str | None = Field(None, max_length=255)
    permissions: list[str] = Field(default_factory=list)


class UpdateRoleRequest(BaseModel):
    """Request model for updating a role."""
    name: str | None = Field(None, min_length=2, max_length=50)
    description: str | None = Field(None, max_length=255)
    permissions: list[str] | None = None


# =============================================================================
# Endpoints
# =============================================================================

@router.get("", response_model=list[RoleResponse])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ROLES_VIEW))
):
    """
    List all roles with user counts.

    Requires: roles.view permission
    """
    # Get roles with user counts
    result = await db.execute(
        select(Role, func.count(User.id).label("user_count"))
        .outerjoin(User, User.role_id == Role.id)
        .group_by(Role.id)
        .order_by(Role.name)
    )
    rows = result.all()

    return [
        RoleResponse(
            id=role.id,
            name=role.name,
            description=role.description,
            permissions=role.permissions or [],

            is_system=role.is_system,
            user_count=user_count,
            created_at=role.created_at,
            updated_at=role.updated_at
        )
        for role, user_count in rows
    ]


@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ROLES_VIEW))
):
    """
    Get a specific role by ID.

    Requires: roles.view permission
    """
    result = await db.execute(
        select(Role, func.count(User.id).label("user_count"))
        .outerjoin(User, User.role_id == Role.id)
        .where(Role.id == role_id)
        .group_by(Role.id)
    )
    row = result.first()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )

    role, user_count = row
    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        permissions=role.permissions or [],

        is_system=role.is_system,
        user_count=user_count,
        created_at=role.created_at,
        updated_at=role.updated_at
    )


@router.post("", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    request: CreateRoleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ROLES_MANAGE))
):
    """
    Create a new role.

    Requires: roles.manage permission
    """
    # Check if name already exists
    result = await db.execute(select(Role).where(Role.name == request.name))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role name already exists"
        )

    # Validate permissions (only check if they're valid permission strings)
    valid_perms = {p.value for p in Permission}
    for perm in request.permissions:
        if perm.startswith("mcp."):
            continue  # Dynamic MCP permissions are always valid
        if perm not in valid_perms:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid permission: {perm}"
            )

    # Create role
    role = Role(
        name=request.name,
        description=request.description,
        permissions=request.permissions,
        is_system=False  # User-created roles are never system roles
    )

    db.add(role)
    await db.commit()
    await db.refresh(role)

    logger.info(f"Created role: {role.name} by user {user.username if user else 'anonymous'}")

    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        permissions=role.permissions or [],

        is_system=role.is_system,
        user_count=0,
        created_at=role.created_at,
        updated_at=role.updated_at
    )


@router.patch("/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: int,
    request: UpdateRoleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ROLES_MANAGE))
):
    """
    Update a role.

    System roles can have their permissions updated, but not their name.

    Requires: roles.manage permission
    """
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()

    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )

    # System roles: can update description and permissions, but not name
    if role.is_system and request.name and request.name != role.name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot rename system roles"
        )

    # Check if new name already exists
    if request.name and request.name != role.name:
        existing = await db.execute(select(Role).where(Role.name == request.name))
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Role name already exists"
            )
        role.name = request.name

    if request.description is not None:
        role.description = request.description

    if request.permissions is not None:
        # Validate permissions
        valid_perms = {p.value for p in Permission}
        for perm in request.permissions:
            if perm.startswith("mcp."):
                continue  # Dynamic MCP permissions are always valid
            if perm not in valid_perms:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid permission: {perm}"
                )
        role.permissions = request.permissions

    await db.commit()
    await db.refresh(role)

    # Get user count
    count_result = await db.execute(
        select(func.count(User.id)).where(User.role_id == role.id)
    )
    user_count = count_result.scalar() or 0

    logger.info(f"Updated role: {role.name} by user {user.username if user else 'anonymous'}")

    return RoleResponse(
        id=role.id,
        name=role.name,
        description=role.description,
        permissions=role.permissions or [],

        is_system=role.is_system,
        user_count=user_count,
        created_at=role.created_at,
        updated_at=role.updated_at
    )


@router.delete("/{role_id}")
async def delete_role(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.ROLES_MANAGE))
):
    """
    Delete a role.

    System roles cannot be deleted.
    Roles with assigned users cannot be deleted.

    Requires: roles.manage permission
    """
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()

    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )

    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete system roles"
        )

    # Check if role has users
    count_result = await db.execute(
        select(func.count(User.id)).where(User.role_id == role.id)
    )
    user_count = count_result.scalar() or 0

    if user_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete role with {user_count} assigned users"
        )

    role_name = role.name
    await db.delete(role)
    await db.commit()

    logger.info(f"Deleted role: {role_name} by user {user.username if user else 'anonymous'}")

    return {"message": f"Role '{role_name}' deleted successfully"}


@router.get("/permissions/all")
async def list_permissions(request: Request):
    """
    List all available permissions including dynamic MCP permissions.

    This endpoint is public (no auth required) as it's useful for
    understanding the permission system.
    """
    permissions = get_all_permissions()

    # Add dynamic MCP permissions from connected servers
    try:
        mcp_manager = getattr(request.app.state, "mcp_manager", None)
        if mcp_manager:
            permissions.extend(get_mcp_permissions(mcp_manager))
    except Exception:
        pass  # MCP manager not available, skip dynamic permissions

    return permissions
