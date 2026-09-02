"""Minimal single-server filesystem MCP client for the document-worker.

The worker deliberately does NOT run the full MCP lifecycle (10 servers +
Whisper + Speechbrain) — see ``workers/document_processor_worker.py``. But the
post-ingest **rename** hook (#881) needs the filesystem MCP to rename the
already-moved archive copy in the share's ``processed/`` dir to the freshly
synthesized ``documents.generated_title``. This spins up **only** the ``files``
streamable-http server connection, lazily and once, so the worker keeps its
memory budget while still reaching the mover.

Requires ``FILES_MCP_ENABLED=true`` + ``FILES_MCP_URL`` in the worker's
environment (the same vars the backend passes via mcp_servers.yaml). Returns
None when the filesystem MCP is disabled/unconfigured or can't connect — callers
then skip the rename (best-effort; the archive keeps its original filename)
rather than crash the ingest.

Mirrors ``services/paperless_worker_client.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from utils.config import settings

_manager: Any = None
_lock = asyncio.Lock()


async def get_files_mcp_manager() -> Any:
    """Lazily create + connect a single-server (files-only) MCP manager.

    Cached on success; a failed connect is NOT cached, so a later call retries
    (e.g. the filesystem MCP came back up)."""
    global _manager
    if _manager is not None:
        return _manager
    async with _lock:
        if _manager is not None:
            return _manager
        from services.mcp_client import MCPManager

        mgr = MCPManager()
        mgr.load_config(settings.mcp_config_path, only={"files"})
        # Reach into the loaded set: with only={"files"} it holds 0 or 1
        # servers. 0 → filesystem MCP disabled/unconfigured (enabled gate off).
        state = mgr._servers.get("files")
        if state is None:
            logger.warning(
                "files-worker-client: filesystem MCP not configured/enabled "
                "(FILES_MCP_ENABLED/FILES_MCP_URL) — worker processed-rename disabled"
            )
            return None
        await mgr.connect_all()
        if not state.connected:
            logger.warning(
                "files-worker-client: filesystem MCP failed to connect in the "
                "worker — skipping processed-rename"
            )
            return None
        _manager = mgr
        logger.info("files-worker-client: connected (single-server filesystem MCP)")
    return _manager
