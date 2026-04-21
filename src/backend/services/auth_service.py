"""
Authentication Service for Renfield

Provides JWT-based authentication, password hashing, and permission checks.
"""
import secrets
from datetime import UTC, datetime, timedelta
from typing import Union
from uuid import uuid4

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from loguru import logger
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import Role, User
from models.permissions import DEFAULT_ROLES, Permission
from services.database import get_db
from utils.config import settings

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme for token extraction
# tokenUrl is the endpoint where users can get a token
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# JWT configuration
ALGORITHM = "HS256"


# =============================================================================
# Password Utilities
# =============================================================================

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Generate password hash."""
    return pwd_context.hash(password)


def validate_password(password: str) -> tuple[bool, str]:
    """
    Validate password against policy.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if len(password) < settings.password_min_length:
        return False, f"Password must be at least {settings.password_min_length} characters"
    return True, ""


# =============================================================================
# JWT Token Utilities
# =============================================================================

def create_access_token(
    data: dict,
    expires_delta: timedelta | None = None
) -> str:
    """
    Create a JWT access token.

    Args:
        data: Payload data (should include "sub" for user identification)
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT token string
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(UTC).replace(tzinfo=None) + expires_delta
    else:
        expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=settings.access_token_expire_minutes)

    to_encode.update({
        "exp": expire,
        "type": "access",
        "jti": str(uuid4()),
    })

    encoded_jwt = jwt.encode(to_encode, settings.secret_key.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(user_id: int) -> str:
    """
    Create a JWT refresh token.

    Refresh tokens have longer expiration and can only be used to get new access tokens.
    """
    expire = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=settings.refresh_token_expire_days)

    to_encode = {
        "sub": str(user_id),
        "exp": expire,
        "type": "refresh",
        "jti": str(uuid4()),
    }

    encoded_jwt = jwt.encode(to_encode, settings.secret_key.get_secret_value(), algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> dict | None:
    """
    Decode and validate a JWT token.

    Returns:
        Decoded payload if valid, None otherwise
    """
    try:
        payload = jwt.decode(token, settings.secret_key.get_secret_value(), algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        logger.debug(f"JWT decode error: {e}")
        return None


# =============================================================================
# User Authentication
# =============================================================================

async def authenticate_user(
    db: AsyncSession,
    username: str,
    password: str
) -> User | None:
    """
    Authenticate a user by username and password.

    Args:
        db: Database session
        username: Username to authenticate
        password: Plain text password

    Returns:
        User object if authentication successful, None otherwise
    """
    result = await db.execute(
        select(User)
        .options(selectinload(User.role))
        .where(User.username == username)
    )
    user = result.scalar_one_or_none()

    if not user:
        return None

    if not verify_password(password, user.password_hash):
        return None

    if not user.is_active:
        return None

    return user


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    """Get a user by ID with role loaded."""
    result = await db.execute(
        select(User)
        .options(selectinload(User.role))
        .where(User.id == user_id)
    )
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """Get a user by username with role loaded."""
    result = await db.execute(
        select(User)
        .options(selectinload(User.role))
        .where(User.username == username)
    )
    return result.scalar_one_or_none()


# =============================================================================
# FastAPI Dependencies
# =============================================================================

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User | None:
    """
    FastAPI dependency to get the current authenticated user.

    If auth is disabled, returns None (endpoints should handle this).
    If auth is enabled but token is invalid/missing, raises 401.
    """
    # If auth is disabled, return None (anonymous access)
    if not settings.auth_enabled:
        return None

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check token type
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if token has been revoked (logout)
    jti = payload.get("jti")
    if jti:
        from services.token_blacklist import token_blacklist
        if await token_blacklist.is_blacklisted(jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
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
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )

    return user


async def get_optional_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User | None:
    """
    FastAPI dependency to optionally get the current user.

    Returns None if not authenticated (doesn't raise exception).
    Useful for endpoints that work differently for authenticated vs anonymous users.
    """
    if not token:
        return None

    try:
        return await get_current_user(token, db)
    except HTTPException:
        return None


def require_auth(user: User = Depends(get_current_user)) -> User:
    """
    FastAPI dependency that requires authentication.

    Raises 401 if not authenticated (even when auth is disabled globally).
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_user_or_default(
    current_user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency for routes that need a concrete User but
    support auth-disabled single-user deploys.

    - Auth enabled + authenticated → returns the User.
    - Auth enabled + missing/invalid token → 401 (raised by
      `get_current_user` before we run).
    - Auth disabled (AUTH_ENABLED=false) → resolves to the admin
      user (or the first user by id if `admin` is gone). Matches
      the Circles-v1 "single-user mode sees everything" pattern
      documented in CLAUDE.md.

    Use this for routes that need to scope data by user_id and
    must also work on solo / home deploys where auth is off.
    """
    if current_user is not None:
        return current_user

    # Auth-disabled: resolve to the admin user.
    admin = (await db.execute(
        select(User).where(User.username == "admin").limit(1)
    )).scalar_one_or_none()
    if admin is not None:
        return admin
    # Admin was deleted/renamed — fall back to the first user by id.
    first = (await db.execute(
        select(User).order_by(User.id).limit(1)
    )).scalar_one_or_none()
    if first is not None:
        return first
    # No users at all — this is a bootstrap-edge deploy; fail loud.
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Auth is disabled and no users exist — bootstrap a user via "
            "`/api/auth/register` or seed the DB before using this endpoint."
        ),
    )


# =============================================================================
# Permission Checking Dependencies
# =============================================================================

def require_permission(permission: Union[Permission, str]):
    """
    Create a FastAPI dependency that requires a specific permission.

    Usage:
        @router.get("/admin/stats")
        async def admin_stats(user: User = Depends(require_permission(Permission.ADMIN))):
            ...
    """
    async def permission_checker(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
    ) -> User:
        # If auth is disabled, allow access
        if not settings.auth_enabled:
            return user

        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Convert string to Permission enum if needed
        perm_value = permission.value if isinstance(permission, Permission) else permission

        if not user.has_permission(perm_value):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {perm_value}"
            )

        return user

    return permission_checker


def require_any_permission(permissions: list[Union[Permission, str]]):
    """
    Create a FastAPI dependency that requires any of the specified permissions.
    """
    async def permission_checker(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
    ) -> User:
        if not settings.auth_enabled:
            return user

        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        for permission in permissions:
            perm_value = permission.value if isinstance(permission, Permission) else permission
            if user.has_permission(perm_value):
                return user

        perm_values = [p.value if isinstance(p, Permission) else p for p in permissions]
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"One of these permissions required: {perm_values}"
        )

    return permission_checker


# =============================================================================
# Role Management
# =============================================================================

async def get_role_by_name(db: AsyncSession, name: str) -> Role | None:
    """Get a role by name."""
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one_or_none()


async def get_role_by_id(db: AsyncSession, role_id: int) -> Role | None:
    """Get a role by ID."""
    result = await db.execute(select(Role).where(Role.id == role_id))
    return result.scalar_one_or_none()


async def ensure_default_roles(db: AsyncSession) -> list[Role]:
    """
    Ensure default system roles exist.

    Creates Admin, Familie, and Gast roles if they don't exist.
    Returns list of created/existing roles.
    """
    roles = []

    for role_config in DEFAULT_ROLES:
        existing = await get_role_by_name(db, role_config["name"])

        if existing:
            # Merge new permissions into existing system roles (additive only)
            if existing.is_system:
                expected = set(role_config["permissions"])
                current = set(existing.permissions or [])
                missing = expected - current
                if missing:
                    existing.permissions = list(current | expected)
                    logger.info(f"Updated system role {existing.name}: added {missing}")
            roles.append(existing)
        else:
            role = Role(
                name=role_config["name"],
                description=role_config["description"],
                permissions=role_config["permissions"],
                is_system=role_config["is_system"]
            )
            db.add(role)
            roles.append(role)
            logger.info(f"Created default role: {role_config['name']}")

    await db.commit()
    return roles


async def ensure_admin_user(db: AsyncSession) -> User | None:
    """
    Ensure a default admin user exists.

    Creates an admin user with default credentials if no users exist.
    """
    # Check if any users exist
    result = await db.execute(select(User).limit(1))
    existing_user = result.scalar_one_or_none()

    if existing_user:
        return None  # Users already exist, don't create default admin

    # Get or create admin role
    admin_role = await get_role_by_name(db, "Admin")
    if not admin_role:
        roles = await ensure_default_roles(db)
        admin_role = next((r for r in roles if r.name == "Admin"), None)

    if not admin_role:
        logger.error("Could not find or create Admin role")
        return None

    # Generate random password if default is still "changeme"
    configured_password = settings.default_admin_password.get_secret_value()
    if configured_password == "changeme":
        password = secrets.token_urlsafe(16)
        must_change = True
        # Print to stdout only (not captured by file-based loggers)
        print(f"ADMIN_PASSWORD={password}")
        logger.warning(
            "Random admin password generated. "
            "Retrieve via: docker logs renfield-backend 2>&1 | grep ADMIN_PASSWORD"
        )
    else:
        password = configured_password
        must_change = False

    # Create default admin user
    admin_user = User(
        username=settings.default_admin_username,
        password_hash=get_password_hash(password),
        role_id=admin_role.id,
        is_active=True,
        must_change_password=must_change,
    )

    db.add(admin_user)
    await db.commit()
    await db.refresh(admin_user)

    logger.warning(
        f"Created default admin user '{settings.default_admin_username}'. "
        f"PLEASE CHANGE THE PASSWORD IMMEDIATELY!"
    )

    return admin_user


# =============================================================================
# User Registration
# =============================================================================

async def create_user(
    db: AsyncSession,
    username: str,
    password: str,
    role_id: int,
    email: str | None = None
) -> User:
    """
    Create a new user.

    Args:
        db: Database session
        username: Unique username
        password: Plain text password (will be hashed)
        role_id: ID of the role to assign
        email: Optional email address

    Returns:
        Created User object

    Raises:
        HTTPException if username/email already exists or role not found
    """
    # Check username availability
    existing = await get_user_by_username(db, username)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists"
        )

    # Check email availability if provided
    if email:
        result = await db.execute(select(User).where(User.email == email))
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

    # Verify role exists
    role = await get_role_by_id(db, role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role not found"
        )

    # Validate password
    is_valid, error = validate_password(password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error
        )

    # Create user
    user = User(
        username=username,
        email=email,
        password_hash=get_password_hash(password),
        role_id=role_id,
        is_active=True
    )

    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Load role relationship
    await db.refresh(user, ["role"])

    logger.info(f"Created user: {username} with role: {role.name}")
    return user
