"""
Plugin Management API Routes

Provides endpoints for managing plugins:
- List all available plugins
- Get plugin details and intents
- Enable/disable plugins (admin only)
"""
import os

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel

from models.database import User
from models.permissions import Permission
from services.auth_service import get_current_user, require_permission
from utils.config import settings

router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class PluginIntent(BaseModel):
    """Model for plugin intent information."""
    name: str
    description: str
    parameters: list[dict]


class PluginInfo(BaseModel):
    """Model for plugin information."""
    name: str
    version: str
    description: str
    author: str | None = None
    enabled: bool
    enabled_var: str
    has_config: bool
    config_vars: list[str]
    intents: list[PluginIntent]
    rate_limit: int | None = None


class PluginListResponse(BaseModel):
    """Response model for plugin list."""
    plugins: list[PluginInfo]
    total: int
    plugins_enabled: bool


class PluginToggleRequest(BaseModel):
    """Request model for enabling/disabling a plugin."""
    enabled: bool


class PluginToggleResponse(BaseModel):
    """Response model for plugin toggle."""
    name: str
    enabled: bool
    message: str
    requires_restart: bool = True


# =============================================================================
# Helper Functions
# =============================================================================

def get_plugin_registry():
    """Get the plugin registry from app state."""
    from main import app
    return getattr(app.state, 'plugin_registry', None)


def get_plugin_loader():
    """Get a fresh plugin loader to scan all plugins (including disabled)."""
    from integrations.core.plugin_loader import PluginLoader
    return PluginLoader(settings.plugins_dir)


def is_plugin_enabled(enabled_var: str) -> bool:
    """Check if a plugin is enabled via environment variable."""
    value = os.environ.get(enabled_var, "").lower()
    return value in ("true", "1", "yes", "on")


def get_config_vars(plugin_def) -> list[str]:
    """Get list of config variable names for a plugin."""
    vars = []
    if plugin_def.config:
        if plugin_def.config.url:
            vars.append(plugin_def.config.url)
        if plugin_def.config.api_key:
            vars.append(plugin_def.config.api_key)
        if plugin_def.config.additional:
            vars.extend(plugin_def.config.additional.keys())
    return vars


# =============================================================================
# Endpoints
# =============================================================================

@router.get("", response_model=PluginListResponse)
async def list_plugins(
    current_user: User | None = Depends(get_current_user)
):
    """
    List all available plugins.

    Returns both enabled and disabled plugins with their status.
    Requires: plugins.use permission (or no auth if disabled)
    """
    # Check permission if user is authenticated
    if current_user and not current_user.has_permission(Permission.PLUGINS_USE.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No permission to view plugins"
        )

    plugins = []

    try:
        # Load all plugins (including disabled ones)
        loader = get_plugin_loader()
        all_plugins = loader._scan_plugin_files()  # Get all YAML files

        for yaml_path in all_plugins:
            try:
                plugin_def = loader._load_plugin_file(yaml_path)
                if plugin_def:
                    enabled = is_plugin_enabled(plugin_def.metadata.enabled_var)

                    # Get intents
                    intents = []
                    for intent_def in plugin_def.intents:
                        params = []
                        if intent_def.parameters:
                            for p in intent_def.parameters:
                                params.append({
                                    "name": p.name,
                                    "type": p.type,
                                    "required": p.required,
                                    "description": p.description or "",
                                    "default": p.default,
                                    "enum": p.enum
                                })

                        intents.append(PluginIntent(
                            name=intent_def.name,
                            description=intent_def.description,
                            parameters=params
                        ))

                    plugins.append(PluginInfo(
                        name=plugin_def.metadata.name,
                        version=plugin_def.metadata.version,
                        description=plugin_def.metadata.description,
                        author=plugin_def.metadata.author,
                        enabled=enabled,
                        enabled_var=plugin_def.metadata.enabled_var,
                        has_config=plugin_def.config is not None,
                        config_vars=get_config_vars(plugin_def),
                        intents=intents,
                        rate_limit=plugin_def.rate_limit
                    ))
            except Exception as e:
                logger.warning(f"Failed to load plugin {yaml_path}: {e}")
                continue

    except Exception as e:
        logger.error(f"Failed to scan plugins: {e}")

    return PluginListResponse(
        plugins=plugins,
        total=len(plugins),
        plugins_enabled=settings.plugins_enabled
    )


@router.get("/{plugin_name}", response_model=PluginInfo)
async def get_plugin(
    plugin_name: str,
    current_user: User | None = Depends(get_current_user)
):
    """
    Get details for a specific plugin.

    Requires: plugins.use permission
    """
    if current_user and not current_user.has_permission(Permission.PLUGINS_USE.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No permission to view plugins"
        )

    try:
        loader = get_plugin_loader()
        all_plugins = loader._scan_plugin_files()

        for yaml_path in all_plugins:
            try:
                plugin_def = loader._load_plugin_file(yaml_path)
                if plugin_def and plugin_def.metadata.name == plugin_name:
                    enabled = is_plugin_enabled(plugin_def.metadata.enabled_var)

                    intents = []
                    for intent_def in plugin_def.intents:
                        params = []
                        if intent_def.parameters:
                            for p in intent_def.parameters:
                                params.append({
                                    "name": p.name,
                                    "type": p.type,
                                    "required": p.required,
                                    "description": p.description or "",
                                    "default": p.default,
                                    "enum": p.enum
                                })

                        intents.append(PluginIntent(
                            name=intent_def.name,
                            description=intent_def.description,
                            parameters=params
                        ))

                    return PluginInfo(
                        name=plugin_def.metadata.name,
                        version=plugin_def.metadata.version,
                        description=plugin_def.metadata.description,
                        author=plugin_def.metadata.author,
                        enabled=enabled,
                        enabled_var=plugin_def.metadata.enabled_var,
                        has_config=plugin_def.config is not None,
                        config_vars=get_config_vars(plugin_def),
                        intents=intents,
                        rate_limit=plugin_def.rate_limit
                    )
            except Exception as e:
                logger.warning(f"Failed to load plugin {yaml_path}: {e}")
                continue

    except Exception as e:
        logger.error(f"Failed to get plugin {plugin_name}: {e}")

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Plugin not found: {plugin_name}"
    )


@router.post("/{plugin_name}/toggle", response_model=PluginToggleResponse)
async def toggle_plugin(
    plugin_name: str,
    request: PluginToggleRequest,
    current_user: User = Depends(require_permission(Permission.PLUGINS_MANAGE))
):
    """
    Enable or disable a plugin.

    Note: This updates the environment variable. A restart is required
    for the change to take effect.

    Requires: plugins.manage permission
    """
    try:
        loader = get_plugin_loader()
        all_plugins = loader._scan_plugin_files()

        for yaml_path in all_plugins:
            try:
                plugin_def = loader._load_plugin_file(yaml_path)
                if plugin_def and plugin_def.metadata.name == plugin_name:
                    enabled_var = plugin_def.metadata.enabled_var

                    # Update environment variable
                    os.environ[enabled_var] = "true" if request.enabled else "false"

                    action = "enabled" if request.enabled else "disabled"
                    logger.info(f"Plugin {plugin_name} {action} by {current_user.username}")

                    return PluginToggleResponse(
                        name=plugin_name,
                        enabled=request.enabled,
                        message=f"Plugin {plugin_name} {action}. Restart required for changes to take effect.",
                        requires_restart=True
                    )
            except Exception as e:
                logger.warning(f"Failed to process plugin {yaml_path}: {e}")
                continue

    except Exception as e:
        logger.error(f"Failed to toggle plugin {plugin_name}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to toggle plugin: {e!s}"
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Plugin not found: {plugin_name}"
    )


@router.get("/{plugin_name}/intents")
async def get_plugin_intents(
    plugin_name: str,
    current_user: User | None = Depends(get_current_user)
):
    """
    Get all intents for a specific plugin.

    Requires: plugins.use permission
    """
    if current_user and not current_user.has_permission(Permission.PLUGINS_USE.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No permission to view plugins"
        )

    plugin = await get_plugin(plugin_name, current_user)
    return {"plugin": plugin_name, "intents": plugin.intents}


@router.get("/registry/active")
async def get_active_plugins(
    current_user: User | None = Depends(get_current_user)
):
    """
    Get currently active (loaded) plugins from the registry.

    These are plugins that are enabled and successfully loaded.

    Requires: plugins.use permission
    """
    if current_user and not current_user.has_permission(Permission.PLUGINS_USE.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No permission to view plugins"
        )

    registry = get_plugin_registry()
    if not registry:
        return {"active_plugins": [], "total": 0}

    active = registry.list_plugins()
    return {"active_plugins": active, "total": len(active)}
