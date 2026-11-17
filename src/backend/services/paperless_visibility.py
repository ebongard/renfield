"""Who may see where a document lives in Paperless.

A Paperless link, and the Paperless id it names, is the Paperless
permission's to give -- not the circles'. The circles decide whether a
member may see a knowledge-base document; whether that member may also learn
its Paperless id (and open it there) is decided by `mcp.paperless.read`, the
permission both Paperless read tools require (`config/mcp_servers.yaml`).

🛑 ONE rule for every surface that serves the id or a link built from it: the
chat sources (`api/websocket/document_sources.py`, write and read side), the
document API (`api/routes/knowledge.py`), the duplicate proposals
(`api/routes/document_dedupe.py`) and the filing-status tool
(`services/kb_maintenance_tool.py`). A new surface goes through here.

Auth off is one trust domain: everything stays visible. Auth on: only with the
permission -- `has_mcp_permission` honours `mcp.*` and the server-wide
`mcp.paperless` -- and no identity, or permissions that cannot be read, deny.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from utils.log_safety import error_text_without_content

PAPERLESS_READ_PERMISSION = "mcp.paperless.read"


def holds_paperless_read(permissions: list[str]) -> bool:
    """The check the Paperless read tools make, on a permission list."""
    from models.permissions import has_mcp_permission

    return has_mcp_permission(list(permissions), PAPERLESS_READ_PERMISSION)


def paperless_visible(user_permissions: list[str] | None) -> bool:
    """For a caller identified by its permission list (a chat turn, a tool
    call). `None` under auth is an unidentified caller and denies."""
    from utils.config import settings

    if not settings.auth_enabled:
        return True
    return user_permissions is not None and holds_paperless_read(user_permissions)


def paperless_visible_to(user: Any) -> bool:
    """For a caller identified by its user row (a route). No user under auth
    denies; so does any failure to read the user's permissions."""
    from utils.config import settings

    if not settings.auth_enabled:
        return True
    if user is None:
        return False
    try:
        return holds_paperless_read(user.get_permissions() or [])
    except Exception as e:
        logger.warning(
            "Paperless id withheld: the caller's permissions are unreadable: "
            f"{error_text_without_content(e)}"
        )
        return False
