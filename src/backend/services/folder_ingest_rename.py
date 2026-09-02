"""Post-ingest rename of the archived folder-ingest copy (#881).

Folder-ingest moves the source file into the share's ``processed/`` dir under its
ORIGINAL filename (e.g. ``2026_03_29_14_33_13.pdf``) at push time — but the
human-readable ``documents.generated_title`` (issuer + type + date) does not
exist yet: it is synthesized later, in the worker, AFTER Schicht-A facts commit
(``schicht_a_extractor.generate_document_title``).

So this module is the callback that fires once the title is known: it asks the
filesystem MCP to rename the already-moved file in ``processed/`` to the
generated title, making the share human-browsable.

Design (issue #881, Option 1 — backend-driven rename):

* **Dark by default** — gated on ``folder_ingest_rename_processed_enabled`` so
  the hook is inert until BOTH repos are deployed and the flag is flipped.
* **Best-effort** — a rename failure (MCP down, file gone, tool absent) NEVER
  breaks ingest. Every path here is wrapped so this function cannot raise.
* **Sanitized** — the new base name is scrubbed to a safe SMB filename in the
  backend before sending (and again in the MCP as defense-in-depth).
* **Idempotent / collision-safe** — those properties live in the MCP tool (a
  missing source file is a success no-op; a taken target name gets a `` (2)``
  suffix). This side only has to send the sanitized title.
"""

from __future__ import annotations

import re

from loguru import logger

from models.database import FOLDER_INGEST_SOURCE
from utils.config import settings

# Characters illegal in SMB/Windows filenames, plus the path separators. Replaced
# with a single space (later collapsed). Control chars are stripped separately.
_ILLEGAL_SMB_CHARS = r'/\\:*?"<>|'
_ILLEGAL_RE = re.compile(f"[{re.escape(_ILLEGAL_SMB_CHARS)}]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

# Cap the base name so ``<base><ext>`` stays comfortably within filesystem limits.
_MAX_BASE_LEN = 150


def sanitize_smb_filename(base: str, *, max_len: int = _MAX_BASE_LEN) -> str:
    """Scrub ``base`` into a safe SMB/Windows filename component (NO extension).

    Strips/replaces the illegal chars ``/ \\ : * ? " < > |`` and control chars,
    collapses whitespace, trims leading/trailing whitespace and dots (Windows
    forbids trailing dots/spaces), and caps the length. Returns ``""`` if nothing
    usable remains — the caller then skips the rename.
    """
    if not base:
        return ""
    s = _CONTROL_RE.sub("", str(base))
    s = _ILLEGAL_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    # Windows disallows trailing dots/spaces on a filename component.
    s = s.strip(" .")
    if len(s) > max_len:
        s = s[:max_len].strip(" .")
    return s


async def rename_processed_to_title(
    *,
    source: str | None,
    filename: str | None,
    generated_title: str | None,
    mcp_manager=None,
) -> bool:
    """Best-effort: rename the archived ``processed/<filename>`` copy to the
    sanitized ``generated_title`` via ``mcp.files.rename_processed``.

    Returns True if the rename call was issued, False if skipped (flag off, not a
    folder-ingest doc, no title, nothing to send, or the MCP is unavailable).
    NEVER raises — a failure is logged at WARNING and swallowed so ingest cannot
    break on the archive rename.
    """
    try:
        if not settings.folder_ingest_rename_processed_enabled:
            return False
        if source != FOLDER_INGEST_SOURCE:
            return False
        if not filename or not str(filename).strip():
            return False
        if not generated_title or not str(generated_title).strip():
            return False

        new_base = sanitize_smb_filename(generated_title)
        if not new_base:
            return False

        mgr = mcp_manager
        if mgr is None:
            from services.files_worker_client import get_files_mcp_manager

            mgr = await get_files_mcp_manager()
        if mgr is None:
            return False

        await mgr.execute_tool(
            "mcp.files.rename_processed",
            {"original_name": filename, "new_base": new_base},
        )
        logger.info(
            f"folder-ingest: requested processed rename {filename!r} → {new_base!r}"
        )
        return True
    except Exception as exc:  # noqa: BLE001 — archive rename is non-essential
        logger.warning(
            f"folder-ingest: processed rename failed for {filename!r} "
            f"(best-effort, ingest unaffected): {exc}"
        )
        return False
