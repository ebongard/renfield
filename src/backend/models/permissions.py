"""
Permission Enums for Role-Permission Based Access Control (RPBAC)

This module defines all granular permissions used in Renfield.
Permissions are grouped by resource type and follow the format: resource.action
"""
from enum import Enum
from typing import List


class Permission(str, Enum):
    """
    Granular permissions for RPBAC.

    Naming convention: RESOURCE_ACTION
    String value format: resource.action

    Permissions are hierarchical within each resource type:
    - NONE < READ < WRITE/CONTROL < FULL/ALL
    """

    # === Knowledge Bases ===
    KB_NONE = "kb.none"           # No KB access at all
    KB_OWN = "kb.own"             # Own KBs only (CRUD on owned)
    KB_SHARED = "kb.shared"       # Own + shared KBs (read on shared)
    KB_ALL = "kb.all"             # All KBs (Admin) - full CRUD

    # === Home Assistant ===
    HA_NONE = "ha.none"           # No HA access
    HA_READ = "ha.read"           # Read device states only
    HA_CONTROL = "ha.control"     # Read + control devices (turn_on, turn_off, etc.)
    HA_FULL = "ha.full"           # Full access including calling services

    # === Cameras / Frigate ===
    CAM_NONE = "cam.none"         # No camera access
    CAM_VIEW = "cam.view"         # View events list
    CAM_FULL = "cam.full"         # View + snapshots + live streams

    # === Conversations / Chat History ===
    CHAT_OWN = "chat.own"         # Own conversations only
    CHAT_ALL = "chat.all"         # All conversations (Admin)

    # === Rooms & Devices ===
    ROOMS_READ = "rooms.read"     # View room topology
    ROOMS_MANAGE = "rooms.manage" # Add/edit/delete rooms and devices

    # === Speaker Profiles ===
    SPEAKERS_OWN = "speakers.own"   # Manage own speaker profile
    SPEAKERS_ALL = "speakers.all"   # Manage all speakers (Admin)

    # === Tasks ===
    TASKS_VIEW = "tasks.view"       # View task queue
    TASKS_MANAGE = "tasks.manage"   # Create/cancel tasks

    # === RAG / Documents ===
    RAG_USE = "rag.use"            # Use RAG in conversations
    RAG_MANAGE = "rag.manage"      # Upload/process documents

    # === Admin ===
    ADMIN = "admin"               # Access to /admin/* and /debug/* endpoints

    # === User Management ===
    USERS_VIEW = "users.view"     # View user list
    USERS_MANAGE = "users.manage" # Create/edit/delete users

    # === Role Management ===
    ROLES_VIEW = "roles.view"     # View roles
    ROLES_MANAGE = "roles.manage" # Create/edit/delete roles

    # === Settings ===
    SETTINGS_VIEW = "settings.view"     # View system settings
    SETTINGS_MANAGE = "settings.manage" # Modify system settings

    # === Plugins ===
    PLUGINS_NONE = "plugins.none"       # No plugin access
    PLUGINS_USE = "plugins.use"         # Use enabled plugins (can trigger intents)
    PLUGINS_MANAGE = "plugins.manage"   # Enable/disable plugins, configure


# Permission hierarchy definitions
# Used to check if a user has "at least" a certain permission level

PERMISSION_HIERARCHY = {
    # KB permissions (kb.all > kb.shared > kb.own > kb.none)
    Permission.KB_ALL: {Permission.KB_SHARED, Permission.KB_OWN, Permission.KB_NONE},
    Permission.KB_SHARED: {Permission.KB_OWN, Permission.KB_NONE},
    Permission.KB_OWN: {Permission.KB_NONE},
    Permission.KB_NONE: set(),

    # HA permissions (ha.full > ha.control > ha.read > ha.none)
    Permission.HA_FULL: {Permission.HA_CONTROL, Permission.HA_READ, Permission.HA_NONE},
    Permission.HA_CONTROL: {Permission.HA_READ, Permission.HA_NONE},
    Permission.HA_READ: {Permission.HA_NONE},
    Permission.HA_NONE: set(),

    # Camera permissions (cam.full > cam.view > cam.none)
    Permission.CAM_FULL: {Permission.CAM_VIEW, Permission.CAM_NONE},
    Permission.CAM_VIEW: {Permission.CAM_NONE},
    Permission.CAM_NONE: set(),

    # Chat permissions (chat.all > chat.own)
    Permission.CHAT_ALL: {Permission.CHAT_OWN},
    Permission.CHAT_OWN: set(),

    # Room permissions (rooms.manage > rooms.read)
    Permission.ROOMS_MANAGE: {Permission.ROOMS_READ},
    Permission.ROOMS_READ: set(),

    # Speaker permissions (speakers.all > speakers.own)
    Permission.SPEAKERS_ALL: {Permission.SPEAKERS_OWN},
    Permission.SPEAKERS_OWN: set(),

    # Task permissions (tasks.manage > tasks.view)
    Permission.TASKS_MANAGE: {Permission.TASKS_VIEW},
    Permission.TASKS_VIEW: set(),

    # RAG permissions (rag.manage > rag.use)
    Permission.RAG_MANAGE: {Permission.RAG_USE},
    Permission.RAG_USE: set(),

    # User permissions (users.manage > users.view)
    Permission.USERS_MANAGE: {Permission.USERS_VIEW},
    Permission.USERS_VIEW: set(),

    # Role permissions (roles.manage > roles.view)
    Permission.ROLES_MANAGE: {Permission.ROLES_VIEW},
    Permission.ROLES_VIEW: set(),

    # Settings permissions (settings.manage > settings.view)
    Permission.SETTINGS_MANAGE: {Permission.SETTINGS_VIEW},
    Permission.SETTINGS_VIEW: set(),

    # Plugin permissions (plugins.manage > plugins.use > plugins.none)
    Permission.PLUGINS_MANAGE: {Permission.PLUGINS_USE, Permission.PLUGINS_NONE},
    Permission.PLUGINS_USE: {Permission.PLUGINS_NONE},
    Permission.PLUGINS_NONE: set(),

    # Admin (no hierarchy, standalone)
    Permission.ADMIN: set(),
}


def has_permission(user_permissions: List[str], required: Permission) -> bool:
    """
    Check if a user has the required permission.

    Takes into account permission hierarchy - e.g., if user has kb.all,
    they implicitly have kb.shared, kb.own, and kb.none.

    Args:
        user_permissions: List of permission strings the user has
        required: The permission that is required

    Returns:
        True if the user has the required permission (directly or via hierarchy)
    """
    # Direct permission check
    if required.value in user_permissions:
        return True

    # Check hierarchy - does user have a higher permission?
    for perm_str in user_permissions:
        try:
            perm = Permission(perm_str)
            # Check if this permission implies the required one
            implied = PERMISSION_HIERARCHY.get(perm, set())
            if required in implied:
                return True
        except ValueError:
            # Unknown permission string, skip
            continue

    return False


def has_any_permission(user_permissions: List[str], required: List[Permission]) -> bool:
    """
    Check if a user has any of the required permissions.

    Args:
        user_permissions: List of permission strings the user has
        required: List of permissions, user needs at least one

    Returns:
        True if the user has at least one of the required permissions
    """
    return any(has_permission(user_permissions, perm) for perm in required)


def has_all_permissions(user_permissions: List[str], required: List[Permission]) -> bool:
    """
    Check if a user has all of the required permissions.

    Args:
        user_permissions: List of permission strings the user has
        required: List of permissions, user needs all of them

    Returns:
        True if the user has all of the required permissions
    """
    return all(has_permission(user_permissions, perm) for perm in required)


def get_all_permissions() -> List[dict]:
    """
    Get all available permissions with descriptions.

    Returns:
        List of dicts with permission value and description
    """
    descriptions = {
        Permission.KB_NONE: "Kein Zugriff auf Wissensdatenbanken",
        Permission.KB_OWN: "Zugriff nur auf eigene Wissensdatenbanken",
        Permission.KB_SHARED: "Zugriff auf eigene und geteilte Wissensdatenbanken",
        Permission.KB_ALL: "Vollzugriff auf alle Wissensdatenbanken",

        Permission.HA_NONE: "Kein Zugriff auf Smart Home",
        Permission.HA_READ: "Nur Gerätestatus lesen",
        Permission.HA_CONTROL: "Geräte steuern (ein/ausschalten)",
        Permission.HA_FULL: "Vollzugriff inkl. Services aufrufen",

        Permission.CAM_NONE: "Kein Zugriff auf Kameras",
        Permission.CAM_VIEW: "Kamera-Events ansehen",
        Permission.CAM_FULL: "Kamera-Events und Snapshots",

        Permission.CHAT_OWN: "Nur eigene Konversationen",
        Permission.CHAT_ALL: "Alle Konversationen",

        Permission.ROOMS_READ: "Räume und Geräte ansehen",
        Permission.ROOMS_MANAGE: "Räume und Geräte verwalten",

        Permission.SPEAKERS_OWN: "Eigenes Sprecherprofil verwalten",
        Permission.SPEAKERS_ALL: "Alle Sprecherprofile verwalten",

        Permission.TASKS_VIEW: "Task-Queue ansehen",
        Permission.TASKS_MANAGE: "Tasks erstellen und abbrechen",

        Permission.RAG_USE: "RAG in Gesprächen nutzen",
        Permission.RAG_MANAGE: "Dokumente hochladen und verarbeiten",

        Permission.ADMIN: "Admin-Funktionen",

        Permission.USERS_VIEW: "Benutzerliste ansehen",
        Permission.USERS_MANAGE: "Benutzer verwalten",

        Permission.ROLES_VIEW: "Rollen ansehen",
        Permission.ROLES_MANAGE: "Rollen verwalten",

        Permission.SETTINGS_VIEW: "System-Einstellungen ansehen",
        Permission.SETTINGS_MANAGE: "System-Einstellungen ändern",

        Permission.PLUGINS_NONE: "Kein Zugriff auf Plugins",
        Permission.PLUGINS_USE: "Plugins verwenden (Intents ausführen)",
        Permission.PLUGINS_MANAGE: "Plugins verwalten (aktivieren/deaktivieren)",
    }

    return [
        {
            "value": perm.value,
            "name": perm.name,
            "description": descriptions.get(perm, perm.value)
        }
        for perm in Permission
    ]


# Default role configurations
DEFAULT_ROLES = [
    {
        "name": "Admin",
        "description": "Vollzugriff auf alle Ressourcen",
        "permissions": [
            Permission.ADMIN.value,
            Permission.KB_ALL.value,
            Permission.HA_FULL.value,
            Permission.CAM_FULL.value,
            Permission.CHAT_ALL.value,
            Permission.ROOMS_MANAGE.value,
            Permission.SPEAKERS_ALL.value,
            Permission.TASKS_MANAGE.value,
            Permission.RAG_MANAGE.value,
            Permission.USERS_MANAGE.value,
            Permission.ROLES_MANAGE.value,
            Permission.SETTINGS_MANAGE.value,
            Permission.PLUGINS_MANAGE.value,
        ],
        "is_system": True
    },
    {
        "name": "Familie",
        "description": "Voller Smart Home Zugriff, eigene und geteilte KBs",
        "permissions": [
            Permission.KB_SHARED.value,
            Permission.HA_FULL.value,
            Permission.CAM_VIEW.value,
            Permission.CHAT_OWN.value,
            Permission.ROOMS_READ.value,
            Permission.SPEAKERS_OWN.value,
            Permission.TASKS_VIEW.value,
            Permission.RAG_USE.value,
            Permission.PLUGINS_USE.value,
        ],
        "is_system": True
    },
    {
        "name": "Gast",
        "description": "Eingeschränkter Lesezugriff",
        "permissions": [
            Permission.KB_NONE.value,
            Permission.HA_READ.value,
            Permission.CAM_NONE.value,
            Permission.CHAT_OWN.value,
            Permission.ROOMS_READ.value,
            Permission.PLUGINS_NONE.value,
        ],
        "is_system": True
    }
]
