"""
Tests for Authentication and Authorization System

Tests cover:
- Permission enum and hierarchy
- Password hashing and verification
- JWT token creation and validation
- Role and User operations
- Permission checking dependencies
- API endpoints (when auth is enabled/disabled)
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from models.permissions import (
    Permission,
    has_permission,
    has_any_permission,
    has_all_permissions,
    get_all_permissions,
    DEFAULT_ROLES,
    PERMISSION_HIERARCHY,
)
from models.database import Role, User


# ============================================================================
# Permission Model Tests
# ============================================================================

class TestPermissionEnum:
    """Tests for Permission enum"""

    def test_permission_values(self):
        """Test that permission values follow the expected format"""
        for perm in Permission:
            # ADMIN is a special standalone permission without resource.action format
            if perm == Permission.ADMIN:
                assert perm.value == "admin"
            else:
                assert "." in perm.value, f"Permission {perm.name} should have format 'resource.action'"

    def test_permission_groups(self):
        """Test that all expected permission groups exist"""
        # Extract prefixes - ADMIN is standalone so use its value directly
        prefixes = set()
        for perm in Permission:
            if "." in perm.value:
                prefixes.add(perm.value.split(".")[0])
            else:
                prefixes.add(perm.value)  # Standalone permissions like "admin"
        expected = {"kb", "ha", "cam", "chat", "rooms", "speakers", "tasks", "rag", "admin", "users", "roles", "settings", "plugins"}
        assert expected.issubset(prefixes), f"Missing permission groups: {expected - prefixes}"


class TestPermissionHierarchy:
    """Tests for permission hierarchy logic"""

    def test_kb_hierarchy(self):
        """Test KB permission hierarchy: kb.all > kb.shared > kb.own > kb.none"""
        # kb.all includes all lower permissions
        assert has_permission([Permission.KB_ALL.value], Permission.KB_SHARED)
        assert has_permission([Permission.KB_ALL.value], Permission.KB_OWN)
        assert has_permission([Permission.KB_ALL.value], Permission.KB_NONE)

        # kb.shared includes kb.own and kb.none
        assert has_permission([Permission.KB_SHARED.value], Permission.KB_OWN)
        assert has_permission([Permission.KB_SHARED.value], Permission.KB_NONE)
        assert not has_permission([Permission.KB_SHARED.value], Permission.KB_ALL)

        # kb.own includes only kb.none
        assert has_permission([Permission.KB_OWN.value], Permission.KB_NONE)
        assert not has_permission([Permission.KB_OWN.value], Permission.KB_SHARED)

    def test_ha_hierarchy(self):
        """Test HA permission hierarchy: ha.full > ha.control > ha.read > ha.none"""
        assert has_permission([Permission.HA_FULL.value], Permission.HA_CONTROL)
        assert has_permission([Permission.HA_FULL.value], Permission.HA_READ)
        assert has_permission([Permission.HA_CONTROL.value], Permission.HA_READ)
        assert not has_permission([Permission.HA_READ.value], Permission.HA_CONTROL)

    def test_direct_permission(self):
        """Test that direct permission matching works"""
        assert has_permission([Permission.ADMIN.value], Permission.ADMIN)
        assert has_permission([Permission.CAM_VIEW.value], Permission.CAM_VIEW)

    def test_no_permission(self):
        """Test that missing permissions return False"""
        assert not has_permission([], Permission.ADMIN)
        assert not has_permission([Permission.HA_READ.value], Permission.CAM_VIEW)

    def test_has_any_permission(self):
        """Test has_any_permission helper"""
        perms = [Permission.HA_READ.value, Permission.CAM_VIEW.value]

        assert has_any_permission(perms, [Permission.HA_READ, Permission.ADMIN])
        assert has_any_permission(perms, [Permission.CAM_VIEW])
        assert not has_any_permission(perms, [Permission.ADMIN, Permission.KB_ALL])

    def test_has_all_permissions(self):
        """Test has_all_permissions helper"""
        perms = [Permission.HA_FULL.value, Permission.KB_ALL.value]

        assert has_all_permissions(perms, [Permission.HA_READ, Permission.KB_OWN])  # via hierarchy
        assert has_all_permissions(perms, [Permission.HA_FULL, Permission.KB_ALL])
        assert not has_all_permissions(perms, [Permission.HA_FULL, Permission.ADMIN])


class TestGetAllPermissions:
    """Tests for get_all_permissions helper"""

    def test_returns_all_permissions(self):
        """Test that all permissions are returned with descriptions"""
        perms = get_all_permissions()

        assert len(perms) == len(Permission)
        for p in perms:
            assert "value" in p
            assert "name" in p
            assert "description" in p


# ============================================================================
# Default Roles Tests
# ============================================================================

class TestDefaultRoles:
    """Tests for default role configurations"""

    def test_default_roles_exist(self):
        """Test that default roles are defined"""
        role_names = [r["name"] for r in DEFAULT_ROLES]
        assert "Admin" in role_names
        assert "Familie" in role_names
        assert "Gast" in role_names

    def test_admin_has_all_permissions(self):
        """Test that Admin role has all critical permissions"""
        admin_role = next(r for r in DEFAULT_ROLES if r["name"] == "Admin")

        assert Permission.ADMIN.value in admin_role["permissions"]
        assert Permission.KB_ALL.value in admin_role["permissions"]
        assert Permission.HA_FULL.value in admin_role["permissions"]
        assert Permission.USERS_MANAGE.value in admin_role["permissions"]

    def test_gast_has_limited_permissions(self):
        """Test that Gast role has limited permissions"""
        gast_role = next(r for r in DEFAULT_ROLES if r["name"] == "Gast")

        assert Permission.KB_NONE.value in gast_role["permissions"]
        assert Permission.HA_READ.value in gast_role["permissions"]
        assert Permission.ADMIN.value not in gast_role["permissions"]
        assert Permission.HA_CONTROL.value not in gast_role["permissions"]

    def test_system_roles_are_protected(self):
        """Test that default roles are marked as system roles"""
        for role in DEFAULT_ROLES:
            assert role["is_system"] is True


# ============================================================================
# Auth Service Tests
# ============================================================================

class TestPasswordUtils:
    """Tests for password utilities"""

    def test_password_hash_and_verify(self):
        """Test password hashing and verification"""
        from services.auth_service import get_password_hash, verify_password

        password = "test_password_123"
        hashed = get_password_hash(password)

        # Hash should be different from plain password
        assert hashed != password

        # Verification should work
        assert verify_password(password, hashed)

        # Wrong password should fail
        assert not verify_password("wrong_password", hashed)

    def test_password_validation(self):
        """Test password validation against policy"""
        from services.auth_service import validate_password

        # Too short
        is_valid, error = validate_password("short")
        assert not is_valid
        assert "at least" in error.lower()

        # Valid password
        is_valid, error = validate_password("valid_password_123")
        assert is_valid
        assert error == ""


class TestJWTTokens:
    """Tests for JWT token utilities"""

    def test_create_access_token(self):
        """Test access token creation"""
        from services.auth_service import create_access_token, decode_token

        token = create_access_token(data={"sub": "123", "username": "testuser"})

        # Token should be a string
        assert isinstance(token, str)

        # Token should be decodable
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "123"
        assert payload["username"] == "testuser"
        assert payload["type"] == "access"

    def test_create_refresh_token(self):
        """Test refresh token creation"""
        from services.auth_service import create_refresh_token, decode_token

        token = create_refresh_token(user_id=123)

        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "123"
        assert payload["type"] == "refresh"

    def test_expired_token(self):
        """Test that expired tokens are rejected"""
        from services.auth_service import create_access_token, decode_token

        # Create token that expires immediately
        token = create_access_token(
            data={"sub": "123"},
            expires_delta=timedelta(seconds=-1)  # Already expired
        )

        payload = decode_token(token)
        assert payload is None

    def test_invalid_token(self):
        """Test that invalid tokens are rejected"""
        from services.auth_service import decode_token

        assert decode_token("invalid_token") is None
        assert decode_token("") is None


# ============================================================================
# Role Model Tests
# ============================================================================

class TestRoleModel:
    """Tests for Role model"""

    @pytest.mark.asyncio
    async def test_role_creation(self, db_session):
        """Test creating a role"""
        role = Role(
            name="TestRole",
            description="A test role",
            permissions=[Permission.HA_READ.value, Permission.KB_OWN.value],
            is_system=False
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        assert role.id is not None
        assert role.name == "TestRole"
        assert len(role.permissions) == 2

    @pytest.mark.asyncio
    async def test_role_has_permission(self, db_session):
        """Test Role.has_permission method"""
        role = Role(
            name="TestRole2",
            permissions=[Permission.HA_FULL.value],
            is_system=False
        )
        db_session.add(role)
        await db_session.commit()

        # Direct permission
        assert role.has_permission(Permission.HA_FULL.value)

        # Via hierarchy
        assert role.has_permission(Permission.HA_CONTROL.value)
        assert role.has_permission(Permission.HA_READ.value)

        # Not granted
        assert not role.has_permission(Permission.ADMIN.value)


# ============================================================================
# User Model Tests
# ============================================================================

class TestUserModel:
    """Tests for User model"""

    @pytest.mark.asyncio
    async def test_user_creation(self, db_session):
        """Test creating a user"""
        # Create role first
        role = Role(
            name="UserTestRole",
            permissions=[Permission.HA_READ.value],
            is_system=False
        )
        db_session.add(role)
        await db_session.commit()

        # Create user
        from services.auth_service import get_password_hash
        user = User(
            username="testuser",
            password_hash=get_password_hash("password123"),
            role_id=role.id,
            is_active=True
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        assert user.id is not None
        assert user.username == "testuser"
        assert user.is_active is True

    @pytest.mark.asyncio
    async def test_user_has_permission(self, db_session):
        """Test User.has_permission via role"""
        role = Role(
            name="UserPermTestRole",
            permissions=[Permission.KB_ALL.value, Permission.HA_CONTROL.value],
            is_system=False
        )
        db_session.add(role)
        await db_session.commit()

        from services.auth_service import get_password_hash
        user = User(
            username="permuser",
            password_hash=get_password_hash("password123"),
            role_id=role.id
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user, ["role"])

        # Check permissions
        assert user.has_permission(Permission.KB_ALL.value)
        assert user.has_permission(Permission.KB_SHARED.value)  # via hierarchy
        assert user.has_permission(Permission.HA_CONTROL.value)
        assert user.has_permission(Permission.HA_READ.value)  # via hierarchy
        assert not user.has_permission(Permission.ADMIN.value)


# ============================================================================
# Auth Service Integration Tests
# ============================================================================

class TestAuthenticateUser:
    """Tests for authenticate_user function"""

    @pytest.mark.asyncio
    async def test_authenticate_valid_user(self, db_session):
        """Test authenticating a valid user"""
        from services.auth_service import authenticate_user, get_password_hash

        # Create role and user
        role = Role(name="AuthTestRole", permissions=[], is_system=False)
        db_session.add(role)
        await db_session.commit()

        user = User(
            username="authuser",
            password_hash=get_password_hash("correct_password"),
            role_id=role.id,
            is_active=True
        )
        db_session.add(user)
        await db_session.commit()

        # Authenticate
        result = await authenticate_user(db_session, "authuser", "correct_password")
        assert result is not None
        assert result.username == "authuser"

    @pytest.mark.asyncio
    async def test_authenticate_wrong_password(self, db_session):
        """Test authentication with wrong password"""
        from services.auth_service import authenticate_user, get_password_hash

        role = Role(name="AuthTestRole2", permissions=[], is_system=False)
        db_session.add(role)
        await db_session.commit()

        user = User(
            username="authuser2",
            password_hash=get_password_hash("correct_password"),
            role_id=role.id,
            is_active=True
        )
        db_session.add(user)
        await db_session.commit()

        result = await authenticate_user(db_session, "authuser2", "wrong_password")
        assert result is None

    @pytest.mark.asyncio
    async def test_authenticate_nonexistent_user(self, db_session):
        """Test authentication with nonexistent user"""
        from services.auth_service import authenticate_user

        result = await authenticate_user(db_session, "nonexistent", "password")
        assert result is None

    @pytest.mark.asyncio
    async def test_authenticate_inactive_user(self, db_session):
        """Test authentication with inactive user"""
        from services.auth_service import authenticate_user, get_password_hash

        role = Role(name="AuthTestRole3", permissions=[], is_system=False)
        db_session.add(role)
        await db_session.commit()

        user = User(
            username="inactiveuser",
            password_hash=get_password_hash("password"),
            role_id=role.id,
            is_active=False  # Inactive!
        )
        db_session.add(user)
        await db_session.commit()

        result = await authenticate_user(db_session, "inactiveuser", "password")
        assert result is None


# ============================================================================
# Role Management Tests
# ============================================================================

class TestRoleManagement:
    """Tests for role management functions"""

    @pytest.mark.asyncio
    async def test_ensure_default_roles(self, db_session):
        """Test that default roles are created"""
        from services.auth_service import ensure_default_roles

        roles = await ensure_default_roles(db_session)

        assert len(roles) >= 3
        role_names = [r.name for r in roles]
        assert "Admin" in role_names
        assert "Familie" in role_names
        assert "Gast" in role_names

    @pytest.mark.asyncio
    async def test_ensure_default_roles_idempotent(self, db_session):
        """Test that calling ensure_default_roles twice doesn't create duplicates"""
        from services.auth_service import ensure_default_roles

        roles1 = await ensure_default_roles(db_session)
        roles2 = await ensure_default_roles(db_session)

        assert len(roles1) == len(roles2)


# ============================================================================
# API Endpoint Tests (with auth disabled)
# ============================================================================

@pytest.mark.asyncio
class TestAuthAPIDisabled:
    """Tests for auth API endpoints when auth is disabled"""

    async def test_get_auth_status(self, async_client):
        """Test GET /api/auth/status returns auth disabled"""
        response = await async_client.get("/api/auth/status")

        assert response.status_code == 200
        data = response.json()
        # Auth is disabled by default in tests
        assert "auth_enabled" in data

    async def test_list_permissions(self, async_client):
        """Test GET /api/auth/permissions returns all permissions"""
        response = await async_client.get("/api/auth/permissions")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == len(Permission)


# ============================================================================
# Permission Dependency Tests
# ============================================================================

class TestPermissionDependencies:
    """Tests for permission checking dependencies"""

    @pytest.mark.asyncio
    async def test_require_permission_with_auth_disabled(self, db_session):
        """Test that require_permission allows access when auth is disabled"""
        from services.auth_service import require_permission
        from utils.config import settings

        # Ensure auth is disabled
        original = settings.auth_enabled
        settings.auth_enabled = False

        try:
            checker = require_permission(Permission.ADMIN)
            # Should not raise when auth is disabled
            result = await checker(user=None, db=db_session)
            assert result is None  # Returns None when auth disabled
        finally:
            settings.auth_enabled = original

    @pytest.mark.asyncio
    async def test_require_permission_with_auth_enabled_no_user(self, db_session):
        """Test that require_permission raises 401 when auth enabled but no user"""
        from services.auth_service import require_permission
        from fastapi import HTTPException
        from utils.config import settings

        original = settings.auth_enabled
        settings.auth_enabled = True

        try:
            checker = require_permission(Permission.ADMIN)
            with pytest.raises(HTTPException) as exc_info:
                await checker(user=None, db=db_session)
            assert exc_info.value.status_code == 401
        finally:
            settings.auth_enabled = original

    @pytest.mark.asyncio
    async def test_require_permission_with_valid_permission(self, db_session):
        """Test that require_permission allows access with valid permission"""
        from services.auth_service import require_permission, get_password_hash
        from utils.config import settings

        # Create user with admin permission
        role = Role(
            name="AdminRoleTest",
            permissions=[Permission.ADMIN.value],
            is_system=False
        )
        db_session.add(role)
        await db_session.commit()

        user = User(
            username="admintest",
            password_hash=get_password_hash("password"),
            role_id=role.id
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user, ["role"])

        original = settings.auth_enabled
        settings.auth_enabled = True

        try:
            checker = require_permission(Permission.ADMIN)
            result = await checker(user=user, db=db_session)
            assert result == user
        finally:
            settings.auth_enabled = original

    @pytest.mark.asyncio
    async def test_require_permission_with_missing_permission(self, db_session):
        """Test that require_permission raises 403 when permission missing"""
        from services.auth_service import require_permission, get_password_hash
        from fastapi import HTTPException
        from utils.config import settings

        # Create user without admin permission
        role = Role(
            name="NonAdminRoleTest",
            permissions=[Permission.HA_READ.value],
            is_system=False
        )
        db_session.add(role)
        await db_session.commit()

        user = User(
            username="nonadmintest",
            password_hash=get_password_hash("password"),
            role_id=role.id
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user, ["role"])

        original = settings.auth_enabled
        settings.auth_enabled = True

        try:
            checker = require_permission(Permission.ADMIN)
            with pytest.raises(HTTPException) as exc_info:
                await checker(user=user, db=db_session)
            assert exc_info.value.status_code == 403
        finally:
            settings.auth_enabled = original
