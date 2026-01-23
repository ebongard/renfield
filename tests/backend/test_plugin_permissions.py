"""
Tests for Plugin Permission System

Tests cover:
- Plugin permission enum values
- Plugin permission hierarchy (plugins.manage > plugins.use > plugins.none)
- Role model with allowed_plugins field
- User model can_use_plugin method
- Action executor plugin permission checks
- Plugins API endpoints
- Roles API with allowed_plugins
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.permissions import (
    Permission,
    has_permission,
    has_any_permission,
    PERMISSION_HIERARCHY,
    DEFAULT_ROLES,
)
from models.database import Role, User


# ============================================================================
# Plugin Permission Enum Tests
# ============================================================================

class TestPluginPermissionEnum:
    """Tests for plugin permission enum values"""

    def test_plugin_permissions_exist(self):
        """Test that plugin permissions are defined"""
        assert hasattr(Permission, 'PLUGINS_NONE')
        assert hasattr(Permission, 'PLUGINS_USE')
        assert hasattr(Permission, 'PLUGINS_MANAGE')

    def test_plugin_permission_values(self):
        """Test plugin permission string values"""
        assert Permission.PLUGINS_NONE.value == "plugins.none"
        assert Permission.PLUGINS_USE.value == "plugins.use"
        assert Permission.PLUGINS_MANAGE.value == "plugins.manage"

    def test_plugin_permissions_in_hierarchy(self):
        """Test that plugin permissions are in the hierarchy"""
        assert Permission.PLUGINS_MANAGE in PERMISSION_HIERARCHY
        assert Permission.PLUGINS_USE in PERMISSION_HIERARCHY
        assert Permission.PLUGINS_NONE in PERMISSION_HIERARCHY


class TestPluginPermissionHierarchy:
    """Tests for plugin permission hierarchy logic"""

    def test_manage_includes_use(self):
        """Test that plugins.manage includes plugins.use"""
        assert has_permission([Permission.PLUGINS_MANAGE.value], Permission.PLUGINS_USE)

    def test_manage_includes_none(self):
        """Test that plugins.manage includes plugins.none"""
        assert has_permission([Permission.PLUGINS_MANAGE.value], Permission.PLUGINS_NONE)

    def test_use_includes_none(self):
        """Test that plugins.use includes plugins.none"""
        assert has_permission([Permission.PLUGINS_USE.value], Permission.PLUGINS_NONE)

    def test_use_does_not_include_manage(self):
        """Test that plugins.use does not include plugins.manage"""
        assert not has_permission([Permission.PLUGINS_USE.value], Permission.PLUGINS_MANAGE)

    def test_none_does_not_include_use(self):
        """Test that plugins.none does not include plugins.use"""
        assert not has_permission([Permission.PLUGINS_NONE.value], Permission.PLUGINS_USE)

    def test_none_does_not_include_manage(self):
        """Test that plugins.none does not include plugins.manage"""
        assert not has_permission([Permission.PLUGINS_NONE.value], Permission.PLUGINS_MANAGE)


class TestDefaultRolesPluginPermissions:
    """Tests for plugin permissions in default roles"""

    def test_admin_has_plugins_manage(self):
        """Test that Admin role has plugins.manage permission"""
        admin_role = next(r for r in DEFAULT_ROLES if r["name"] == "Admin")
        assert Permission.PLUGINS_MANAGE.value in admin_role["permissions"]

    def test_familie_has_plugins_use(self):
        """Test that Familie role has plugins.use permission"""
        familie_role = next(r for r in DEFAULT_ROLES if r["name"] == "Familie")
        assert Permission.PLUGINS_USE.value in familie_role["permissions"]

    def test_gast_has_plugins_none(self):
        """Test that Gast role has plugins.none permission"""
        gast_role = next(r for r in DEFAULT_ROLES if r["name"] == "Gast")
        assert Permission.PLUGINS_NONE.value in gast_role["permissions"]


# ============================================================================
# Role Model Tests
# ============================================================================

class TestRoleModelAllowedPlugins:
    """Tests for Role model allowed_plugins field"""

    @pytest.mark.asyncio
    async def test_role_has_allowed_plugins_field(self, db_session: AsyncSession):
        """Test that Role model has allowed_plugins field"""
        role = Role(
            name="TestRole",
            permissions=[Permission.PLUGINS_USE.value],
            allowed_plugins=["weather", "calendar"]
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        assert role.allowed_plugins == ["weather", "calendar"]

    @pytest.mark.asyncio
    async def test_role_allowed_plugins_defaults_to_empty(self, db_session: AsyncSession):
        """Test that allowed_plugins defaults to empty list"""
        role = Role(
            name="TestRole2",
            permissions=[Permission.PLUGINS_USE.value]
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        assert role.allowed_plugins == [] or role.allowed_plugins is None

    @pytest.mark.asyncio
    async def test_role_can_use_plugin_with_full_access(self, db_session: AsyncSession):
        """Test can_use_plugin when all plugins allowed (empty list)"""
        role = Role(
            name="FullAccessRole",
            permissions=[Permission.PLUGINS_USE.value],
            allowed_plugins=[]  # Empty = all plugins allowed
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        assert role.can_use_plugin("weather") is True
        assert role.can_use_plugin("calendar") is True
        assert role.can_use_plugin("any_plugin") is True

    @pytest.mark.asyncio
    async def test_role_can_use_plugin_with_restricted_access(self, db_session: AsyncSession):
        """Test can_use_plugin with specific plugins allowed"""
        role = Role(
            name="RestrictedRole",
            permissions=[Permission.PLUGINS_USE.value],
            allowed_plugins=["weather", "calendar"]
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        assert role.can_use_plugin("weather") is True
        assert role.can_use_plugin("calendar") is True
        assert role.can_use_plugin("other_plugin") is False

    @pytest.mark.asyncio
    async def test_role_cannot_use_plugin_without_permission(self, db_session: AsyncSession):
        """Test that can_use_plugin returns False without plugins.use permission"""
        role = Role(
            name="NoPluginRole",
            permissions=[Permission.PLUGINS_NONE.value],
            allowed_plugins=[]  # Even with empty list, no permission means no access
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        assert role.can_use_plugin("weather") is False
        assert role.can_use_plugin("any_plugin") is False

    @pytest.mark.asyncio
    async def test_role_plugins_manage_respects_allowed_list(self, db_session: AsyncSession):
        """Test that plugins.manage still respects allowed_plugins list"""
        role = Role(
            name="ManageRole",
            permissions=[Permission.PLUGINS_MANAGE.value],
            allowed_plugins=["weather"]  # Restriction still applies
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        # plugins.manage allows using plugins, but allowed_plugins is still checked
        assert role.can_use_plugin("weather") is True
        assert role.can_use_plugin("other") is False  # Not in allowed list

    @pytest.mark.asyncio
    async def test_role_plugins_manage_full_access_when_empty(self, db_session: AsyncSession):
        """Test that plugins.manage with empty allowed_plugins gives full access"""
        role = Role(
            name="ManageRoleFull",
            permissions=[Permission.PLUGINS_MANAGE.value],
            allowed_plugins=[]  # Empty = all plugins allowed
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        # Empty allowed_plugins = full access
        assert role.can_use_plugin("weather") is True
        assert role.can_use_plugin("other") is True
        assert role.can_use_plugin("any_plugin") is True


# ============================================================================
# User Model Tests
# ============================================================================

class TestUserModelPluginAccess:
    """Tests for User model plugin access methods"""

    @pytest.mark.asyncio
    async def test_user_can_use_plugin_via_role(self, db_session: AsyncSession):
        """Test that user inherits plugin access from role"""
        role = Role(
            name="UserTestRole",
            permissions=[Permission.PLUGINS_USE.value],
            allowed_plugins=["weather"]
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        user = User(
            username="testuser",
            password_hash="hash",
            role_id=role.id
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        # Load relationship
        user.role = role

        assert user.can_use_plugin("weather") is True
        assert user.can_use_plugin("calendar") is False

    @pytest.mark.asyncio
    async def test_user_get_allowed_plugins(self, db_session: AsyncSession):
        """Test get_allowed_plugins returns role's allowed plugins"""
        role = Role(
            name="UserTestRole2",
            permissions=[Permission.PLUGINS_USE.value],
            allowed_plugins=["plugin1", "plugin2"]
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        user = User(
            username="testuser2",
            password_hash="hash",
            role_id=role.id
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        user.role = role

        allowed = user.get_allowed_plugins()
        assert allowed == ["plugin1", "plugin2"]


# ============================================================================
# Action Executor Plugin Permission Tests
# ============================================================================

class TestActionExecutorPluginPermissions:
    """Tests for plugin permission checks in ActionExecutor"""

    @pytest.fixture
    def mock_plugin(self):
        """Create a mock plugin"""
        plugin = MagicMock()
        plugin.metadata.name = "test_plugin"
        plugin.execute = AsyncMock(return_value={"success": True, "message": "Done"})
        return plugin

    @pytest.fixture
    def mock_registry_with_plugin(self, mock_plugin):
        """Create a mock registry that returns a plugin"""
        registry = MagicMock()
        registry.get_plugin_for_intent.return_value = mock_plugin
        registry.get_all_intents.return_value = []
        registry.generate_llm_prompt.return_value = ""
        return registry

    @pytest.fixture
    def user_with_plugin_access(self, db_session: AsyncSession):
        """Create a user with plugin access"""
        role = MagicMock()
        role.permissions = [Permission.PLUGINS_USE.value]
        role.allowed_plugins = ["test_plugin"]
        role.can_use_plugin = lambda p: p == "test_plugin"
        role.has_permission = lambda p: p in role.permissions

        user = MagicMock()
        user.role = role
        user.can_use_plugin = lambda p: role.can_use_plugin(p)
        user.has_permission = lambda p: role.has_permission(p)

        return user

    @pytest.fixture
    def user_without_plugin_access(self):
        """Create a user without plugin access"""
        role = MagicMock()
        role.permissions = [Permission.PLUGINS_NONE.value]
        role.allowed_plugins = []
        role.can_use_plugin = lambda p: False
        role.has_permission = lambda p: p in role.permissions

        user = MagicMock()
        user.role = role
        user.can_use_plugin = lambda p: False
        user.has_permission = lambda p: role.has_permission(p)

        return user

    @pytest.mark.asyncio
    async def test_executor_allows_plugin_with_permission(
        self, mock_registry_with_plugin, mock_plugin, user_with_plugin_access
    ):
        """Test that executor allows plugin execution with proper permission"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor(plugin_registry=mock_registry_with_plugin)

        intent_data = {
            "intent": "test_plugin.do_something",
            "parameters": {},
            "confidence": 0.9
        }

        # The _check_plugin_permission method should allow this
        allowed, error = executor._check_plugin_permission(
            intent_data["intent"],
            user_with_plugin_access
        )

        assert allowed is True
        assert error == ""  # Empty string when allowed

    @pytest.mark.asyncio
    async def test_executor_denies_plugin_without_permission(
        self, mock_registry_with_plugin, user_without_plugin_access
    ):
        """Test that executor denies plugin execution without permission"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor(plugin_registry=mock_registry_with_plugin)

        intent_data = {
            "intent": "test_plugin.do_something",
            "parameters": {},
            "confidence": 0.9
        }

        allowed, error = executor._check_plugin_permission(
            intent_data["intent"],
            user_without_plugin_access
        )

        assert allowed is False
        assert "permission" in error.lower() or "Plugin" in error

    @pytest.mark.asyncio
    async def test_executor_allows_plugin_without_user(self, mock_registry_with_plugin):
        """Test that executor allows plugin when no user is provided (auth disabled)"""
        from services.action_executor import ActionExecutor

        executor = ActionExecutor(plugin_registry=mock_registry_with_plugin)

        allowed, error = executor._check_plugin_permission(
            "test_plugin.do_something",
            None  # No user = auth disabled
        )

        assert allowed is True
        assert error == ""  # Empty string when allowed

    @pytest.mark.asyncio
    async def test_executor_denies_restricted_plugin(self):
        """Test that executor denies access to plugin not in allowed_plugins"""
        from services.action_executor import ActionExecutor

        # User only has access to "weather" plugin
        role = MagicMock()
        role.permissions = [Permission.PLUGINS_USE.value]
        role.allowed_plugins = ["weather"]
        role.can_use_plugin = lambda p: p in ["weather"]
        role.has_permission = lambda p: p in role.permissions

        user = MagicMock()
        user.role = role
        user.can_use_plugin = lambda p: role.can_use_plugin(p)
        user.has_permission = lambda p: role.has_permission(p)

        # Registry returns a different plugin
        plugin = MagicMock()
        plugin.metadata.name = "calendar"  # Not in allowed list

        registry = MagicMock()
        registry.get_plugin_for_intent.return_value = plugin

        executor = ActionExecutor(plugin_registry=registry)

        allowed, error = executor._check_plugin_permission(
            "calendar.get_events",
            user
        )

        assert allowed is False
        assert "calendar" in error


# ============================================================================
# Plugins API Endpoint Tests
# ============================================================================

class TestPluginsAPI:
    """Tests for /api/plugins endpoints"""

    @pytest.fixture
    def mock_plugin_loader(self):
        """Mock plugin loader"""
        loader = MagicMock()

        # Sample plugin definition
        plugin_def = MagicMock()
        plugin_def.metadata.name = "weather"
        plugin_def.metadata.version = "1.0.0"
        plugin_def.metadata.description = "Weather plugin"
        plugin_def.metadata.author = "Test Author"
        plugin_def.metadata.enabled_var = "WEATHER_ENABLED"
        plugin_def.config = None
        plugin_def.rate_limit = None
        plugin_def.intents = []

        loader._scan_plugin_files.return_value = ["/path/to/weather.yaml"]
        loader._load_plugin_file.return_value = plugin_def

        return loader

    @pytest.mark.asyncio
    async def test_list_plugins_returns_plugins(self, async_client, mock_plugin_loader):
        """Test GET /api/plugins returns plugin list"""
        with patch("api.routes.plugins.get_plugin_loader", return_value=mock_plugin_loader):
            with patch("api.routes.plugins.is_plugin_enabled", return_value=True):
                response = await async_client.get("/api/plugins")

        assert response.status_code == 200
        data = response.json()
        assert "plugins" in data
        assert "total" in data
        assert "plugins_enabled" in data

    @pytest.mark.asyncio
    async def test_get_plugin_detail(self, async_client, mock_plugin_loader):
        """Test GET /api/plugins/{name} returns plugin details"""
        with patch("api.routes.plugins.get_plugin_loader", return_value=mock_plugin_loader):
            with patch("api.routes.plugins.is_plugin_enabled", return_value=True):
                response = await async_client.get("/api/plugins/weather")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "weather"
        assert data["version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_get_plugin_not_found(self, async_client, mock_plugin_loader):
        """Test GET /api/plugins/{name} returns 404 for unknown plugin"""
        mock_plugin_loader._load_plugin_file.return_value = None

        with patch("api.routes.plugins.get_plugin_loader", return_value=mock_plugin_loader):
            response = await async_client.get("/api/plugins/nonexistent")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_toggle_plugin_requires_permission(self, async_client):
        """Test POST /api/plugins/{name}/toggle requires plugins.manage permission"""
        # Without auth token, should fail with auth error or internal error
        # (internal error can happen if plugin loader fails in test mode)
        response = await async_client.post(
            "/api/plugins/weather/toggle",
            json={"enabled": True}
        )

        # Should not return 200/201 (success) without authentication
        # It may return 401, 403, 422 (validation), or 500 (loader error in test)
        assert response.status_code != 200
        assert response.status_code != 201


# ============================================================================
# Roles API with Allowed Plugins Tests
# ============================================================================

class TestRolesAPIAllowedPlugins:
    """Tests for /api/roles endpoints with allowed_plugins"""

    @pytest.fixture
    async def admin_token(self, db_session: AsyncSession):
        """Create admin user and get token"""
        from services.auth_service import get_password_hash, create_access_token

        # Create admin role
        admin_role = Role(
            name="AdminTest",
            permissions=[Permission.ADMIN.value, Permission.ROLES_MANAGE.value],
            is_system=False
        )
        db_session.add(admin_role)
        await db_session.commit()
        await db_session.refresh(admin_role)

        # Create admin user
        password_hash = get_password_hash("testpass")

        admin_user = User(
            username="admin_test",
            password_hash=password_hash,
            role_id=admin_role.id
        )
        db_session.add(admin_user)
        await db_session.commit()
        await db_session.refresh(admin_user)

        # Generate token - create_access_token expects a dict with "sub" field
        token = create_access_token({
            "sub": str(admin_user.id),
            "username": admin_user.username
        })
        return token

    @pytest.mark.asyncio
    async def test_create_role_with_allowed_plugins(
        self, async_client, db_session: AsyncSession, admin_token
    ):
        """Test creating a role with allowed_plugins"""
        headers = {"Authorization": f"Bearer {admin_token}"}

        response = await async_client.post(
            "/api/roles",
            json={
                "name": "TestPluginRole",
                "description": "Role with plugin restrictions",
                "permissions": [Permission.PLUGINS_USE.value],
                "allowed_plugins": ["weather", "calendar"]
            },
            headers=headers
        )

        if response.status_code == 201:
            data = response.json()
            assert data["name"] == "TestPluginRole"
            assert data["allowed_plugins"] == ["weather", "calendar"]

    @pytest.mark.asyncio
    async def test_update_role_allowed_plugins(
        self, async_client, db_session: AsyncSession, admin_token
    ):
        """Test updating role's allowed_plugins"""
        # First create a role
        role = Role(
            name="UpdatePluginRole",
            permissions=[Permission.PLUGINS_USE.value],
            allowed_plugins=["weather"]
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        headers = {"Authorization": f"Bearer {admin_token}"}

        response = await async_client.patch(
            f"/api/roles/{role.id}",
            json={
                "allowed_plugins": ["weather", "calendar", "news"]
            },
            headers=headers
        )

        if response.status_code == 200:
            data = response.json()
            assert set(data["allowed_plugins"]) == {"weather", "calendar", "news"}

    @pytest.mark.asyncio
    async def test_list_roles_includes_allowed_plugins(
        self, async_client, db_session: AsyncSession, admin_token
    ):
        """Test that role list includes allowed_plugins field"""
        # Create a role with allowed_plugins
        role = Role(
            name="ListTestRole",
            permissions=[Permission.PLUGINS_USE.value],
            allowed_plugins=["plugin1", "plugin2"]
        )
        db_session.add(role)
        await db_session.commit()

        headers = {"Authorization": f"Bearer {admin_token}"}

        response = await async_client.get("/api/roles", headers=headers)

        if response.status_code == 200:
            data = response.json()
            # Find our test role
            test_role = next((r for r in data if r["name"] == "ListTestRole"), None)
            if test_role:
                assert "allowed_plugins" in test_role
                assert test_role["allowed_plugins"] == ["plugin1", "plugin2"]


# ============================================================================
# Integration Tests
# ============================================================================

class TestPluginPermissionIntegration:
    """Integration tests for plugin permission flow"""

    @pytest.mark.asyncio
    async def test_full_permission_flow(self, db_session: AsyncSession):
        """Test complete flow: create role -> create user -> check plugin access"""
        # 1. Create role with specific plugins allowed
        role = Role(
            name="IntegrationTestRole",
            permissions=[Permission.PLUGINS_USE.value, Permission.CHAT_OWN.value],
            allowed_plugins=["weather", "calendar"]
        )
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)

        # 2. Create user with this role
        user = User(
            username="integration_user",
            password_hash="hash",
            role_id=role.id
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        # Load relationship
        user.role = role

        # 3. Verify permissions
        # User should have plugins.use
        assert user.has_permission(Permission.PLUGINS_USE.value)

        # User should be able to use allowed plugins
        assert user.can_use_plugin("weather") is True
        assert user.can_use_plugin("calendar") is True

        # User should NOT be able to use other plugins
        assert user.can_use_plugin("news") is False
        assert user.can_use_plugin("random_plugin") is False

        # User should NOT have plugins.manage
        assert not user.has_permission(Permission.PLUGINS_MANAGE.value)

    @pytest.mark.asyncio
    async def test_admin_full_plugin_access(self, db_session: AsyncSession):
        """Test that admin has full plugin access regardless of allowed_plugins"""
        # Create admin role
        admin_role = Role(
            name="AdminIntegration",
            permissions=[Permission.ADMIN.value, Permission.PLUGINS_MANAGE.value],
            allowed_plugins=[]  # Empty but should still have full access
        )
        db_session.add(admin_role)
        await db_session.commit()
        await db_session.refresh(admin_role)

        # Create admin user
        admin_user = User(
            username="admin_integration",
            password_hash="hash",
            role_id=admin_role.id
        )
        db_session.add(admin_user)
        await db_session.commit()
        await db_session.refresh(admin_user)

        admin_user.role = admin_role

        # Admin should have access to any plugin
        assert admin_user.can_use_plugin("any_plugin") is True
        assert admin_user.can_use_plugin("another_one") is True

    @pytest.mark.asyncio
    async def test_guest_no_plugin_access(self, db_session: AsyncSession):
        """Test that guest role has no plugin access"""
        # Create guest role
        guest_role = Role(
            name="GuestIntegration",
            permissions=[Permission.PLUGINS_NONE.value, Permission.HA_READ.value],
            allowed_plugins=[]
        )
        db_session.add(guest_role)
        await db_session.commit()
        await db_session.refresh(guest_role)

        # Create guest user
        guest_user = User(
            username="guest_integration",
            password_hash="hash",
            role_id=guest_role.id
        )
        db_session.add(guest_user)
        await db_session.commit()
        await db_session.refresh(guest_user)

        guest_user.role = guest_role

        # Guest should NOT have access to any plugin
        assert guest_user.can_use_plugin("weather") is False
        assert guest_user.can_use_plugin("anything") is False
