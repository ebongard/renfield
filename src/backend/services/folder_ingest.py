"""Folder-ingest bridge — the shared service behind watch-folder auto-ingest.

The dedicated ``renfield-mcp-filesystem`` server pushes a settled new file to
``POST /api/folder-ingest/document`` (Bearer); the route reads the bytes
(stream-hash, early size abort) and hands them here. This module is also the
target of the interactive ``internal.ingest_file`` agent tool, which pulls the
bytes via the filesystem MCP. Either way the backend never mounts the share —
bytes arrive as a parameter.

The load-bearing seam is the **4-state result** (D9). The MCP keys its move
decision off it, and ``ingested`` means *enqueued*, not OCR'd:

    ingested   new row created + enqueued (202-equivalent)  -> move to processed/
    duplicate  row exists, completed, Paperless leg settled  -> move to processed/
    retry      worker down OR row pending/processing          -> leave in inbox, re-push
    failed     terminal reject (bad ext, oversize, ...)        -> move to failed/

Idempotency: the inbox IS the queue. A lost HTTP response → the MCP re-pushes →
the completion+Paperless-aware dedup (D2) returns ``duplicate``/``retry`` (never
a 500). The backend persists its own byte copy (step 0) so a worker retry
survives the MCP having moved the source file.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import aiofiles
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    DOC_STATUS_COMPLETED,
    DOC_STATUS_FAILED,
    DOC_STATUS_PENDING,
    PAPERLESS_STATE_DONE,
    PAPERLESS_STATE_FAILED,
    SETTING_FOLDER_INGEST_TOKEN,
    Document,
    SystemSetting,
)

# paperless_state values that mean "the Paperless leg is settled" — either it
# was filed/duplicate (done) or terminally rejected for a non-duplicate reason
# (failed). Both stop the D2 matrix re-running the leg; only None/unset retries.
_PAPERLESS_SETTLED = (PAPERLESS_STATE_DONE, PAPERLESS_STATE_FAILED)
from services.rag_service import DuplicateDocumentError, RAGService
from services.redis_client import get_redis
from services.task_queue import DocumentTaskQueue
from utils.config import settings

# Cross-repo push-contract version (DX-7). Sent in the request + response
# header; the MCP treats an unknown response status as `retry` and logs a skew
# WARN. Mirror of auth/provider_contract.py::PROVIDER_RESULT_CONTRACT_VERSION.
# Bump on ANY change to the request/response shape or the IngestStatus names.
FOLDER_INGEST_CONTRACT_VERSION = "1"


class IngestStatus(str, Enum):
    """The 4-state response contract (D9). The MCP moves the source file by
    this value, so the names are part of the cross-repo seam — do not rename
    without bumping ``FOLDER_INGEST_CONTRACT_VERSION``."""

    INGESTED = "ingested"
    DUPLICATE = "duplicate"
    RETRY = "retry"
    FAILED = "failed"


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    document_id: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class IngestMeta:
    """Parsed metadata that rides alongside the pushed bytes."""

    filename: str
    root: str | None = None
    relpath: str | None = None
    sha256: str | None = None
    mime: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "IngestMeta":
        filename = str(raw.get("filename") or raw.get("relpath") or "").strip()
        if not filename:
            raise ValueError("metadata is missing a filename")
        return cls(
            filename=os.path.basename(filename.replace("\x00", "")),
            root=raw.get("root"),
            relpath=raw.get("relpath"),
            sha256=raw.get("sha256"),
            mime=raw.get("mime"),
        )


class _Decision(str, Enum):
    """Outcome of the dedup classification against the existing Document row.
    Kept separate from :class:`IngestStatus` because one decision
    (``PAPERLESS_ONLY``) does work before resolving to a response state."""

    CREATE = "create"  # no row → run the full pipeline
    DUPLICATE = "duplicate"  # completed AND Paperless-done → nothing to do
    REINGEST = "reingest"  # status=failed → re-run the full pipeline
    PAPERLESS_ONLY = "paperless_only"  # completed but Paperless not yet done
    RETRY = "retry"  # pending/processing → don't double-enqueue


async def classify_existing(
    db: AsyncSession, file_hash: str, kb_id: int | None
) -> tuple[_Decision, Document | None]:
    """Completion+Paperless-aware dedup (D2). Pure read against the
    ``(file_hash, knowledge_base_id)`` row; the orchestrator turns the
    decision into a 4-state response. Factored out so the risky SQL branch is
    unit-tested directly against real Postgres."""
    doc = (
        await db.execute(
            select(Document).where(
                Document.file_hash == file_hash,
                Document.knowledge_base_id == kb_id,
            )
        )
    ).scalar_one_or_none()

    if doc is None:
        return _Decision.CREATE, None
    if doc.status == DOC_STATUS_COMPLETED:
        if doc.paperless_state in _PAPERLESS_SETTLED:
            return _Decision.DUPLICATE, doc
        return _Decision.PAPERLESS_ONLY, doc
    if doc.status == DOC_STATUS_FAILED:
        return _Decision.REINGEST, doc
    # pending / processing — the worker still owns this row.
    return _Decision.RETRY, doc


# A Paperless leg is an injected coroutine so the orchestrator stays testable
# and T5 can swap in the real PaperlessMetadataExtractor (D5) + duplicate-marker
# detection (D10) without touching the dedup/4-state control flow. It MUST set
# ``doc.paperless_state`` and commit, and returns True when the leg is settled
# (filed / duplicate / skipped) — i.e. paperless-done for the D2 matrix.
PaperlessLeg = Callable[
    [AsyncSession, Document, bytes, IngestMeta], Awaitable[bool]
]


async def _noop_paperless_leg(
    db: AsyncSession, doc: Document, file_bytes: bytes, meta: IngestMeta
) -> bool:
    """Default when Paperless filing is disabled or no leg is supplied: mark
    the leg settled so a re-push of this document dedups cleanly (D2).
    Idempotent — a no-op if the leg already settled."""
    if doc.paperless_state == PAPERLESS_STATE_DONE:
        return True
    doc.paperless_state = PAPERLESS_STATE_DONE
    await db.commit()
    return True


def _ext_ok(filename: str) -> bool:
    ext = Path(filename).suffix.lstrip(".").lower()
    return bool(ext) and ext in settings.allowed_extensions_list


async def _persist_bytes(file_bytes: bytes, filename: str) -> str:
    """Step 0 (D9): write the recovery copy to ``upload_dir`` BEFORE any
    create/enqueue, so a worker retry survives the MCP moving the source."""
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = os.path.basename((filename or "unknown").replace("\x00", ""))
    file_path = upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
    async with aiofiles.open(file_path, "wb") as f:
        await f.write(file_bytes)
    return str(file_path)


async def ingest_document(
    file_bytes: bytes,
    meta: IngestMeta,
    *,
    db: AsyncSession,
    kb_id: int | None,
    owner_user_id: int | None = None,
    paperless_leg: PaperlessLeg | None = None,
    force_ocr: bool = False,
) -> IngestResult:
    """Run the folder-ingest pipeline for one pushed file and return the
    4-state result (D9). See the module docstring for the contract.

    Steps: (0) persist a recovery byte copy, (1) reject bad ext/oversize,
    (2) completion+Paperless-aware dedup (D2), (3) race-safe create (D3) +
    enqueue, (4) Paperless leg, (5) respond. The owner/tier override (D4) is
    derived from the target KB here; the explicit ``folder_ingest_target_user``
    / ``folder_ingest_default_tier`` wiring lands in T6.

    ``paperless_leg`` is the Paperless-filing seam: pass the real leg (T5) to
    file into Paperless, or ``None`` to skip filing entirely — the latter marks
    ``paperless_state='done'`` (nothing to file ⇒ settled), so the caller maps
    ``folder_ingest_to_paperless=False`` to ``None``. It is NEVER defaulted to a
    silent no-op while Paperless is meant to be on: the caller owns that choice.
    """
    # 1. Reject bad extension / empty / oversize up front (before touching
    # disk/DB).
    if not _ext_ok(meta.filename):
        logger.warning(f"folder-ingest: rejected extension for {meta.filename!r}")
        return IngestResult(IngestStatus.FAILED, detail="extension_not_allowed")
    if not file_bytes:
        # A 0-byte push (scanner artifact / truncated-to-empty transfer) would
        # otherwise create an empty Document that pollutes retrieval. Terminal.
        logger.warning(f"folder-ingest: empty file rejected for {meta.filename!r}")
        return IngestResult(IngestStatus.FAILED, detail="empty_file")
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        logger.warning(
            f"folder-ingest: {meta.filename!r} exceeds "
            f"{settings.max_file_size_mb} MB ceiling"
        )
        return IngestResult(IngestStatus.FAILED, detail="file_too_large")

    file_hash = hashlib.sha256(file_bytes).hexdigest()
    if meta.sha256 and meta.sha256.lower() != file_hash:
        # The MCP's pre-push hash disagrees with the bytes we received —
        # a truncated/corrupted transfer. Retryable; the MCP re-pushes.
        logger.warning(
            f"folder-ingest: sha256 mismatch for {meta.filename!r} "
            f"(meta={meta.sha256[:16]}... actual={file_hash[:16]}...)"
        )
        return IngestResult(IngestStatus.RETRY, detail="sha256_mismatch")

    # No leg supplied ⇒ Paperless filing is off for this call; the no-op leg
    # records the leg as settled so a re-push dedups cleanly (D2).
    leg = paperless_leg or _noop_paperless_leg

    # 2. Dedup against the existing row (D2).
    decision, existing = await classify_existing(db, file_hash, kb_id)

    if decision is _Decision.DUPLICATE:
        return IngestResult(
            IngestStatus.DUPLICATE, document_id=existing.id, detail="already_ingested"
        )

    if decision is _Decision.RETRY:
        # The worker still owns this row (pending/processing). Don't
        # double-enqueue; the MCP leaves the file in the inbox and re-pushes.
        return IngestResult(
            IngestStatus.RETRY, document_id=existing.id, detail="in_progress"
        )

    if decision is _Decision.PAPERLESS_ONLY:
        # KB ingest is complete but the Paperless leg never settled. Run only
        # the still-missing leg. If it settles (filed/duplicate/terminal-reject)
        # report duplicate so the MCP moves the file to processed/; if it
        # couldn't settle (upload error / consume still pending) report retry so
        # the MCP re-pushes and the leg is attempted again.
        try:
            settled = await leg(db, existing, file_bytes, meta)
        except Exception as exc:  # noqa: BLE001 - leg failure is non-fatal
            logger.warning(f"folder-ingest: Paperless-only leg failed: {exc}")
            return IngestResult(
                IngestStatus.RETRY, document_id=existing.id, detail="paperless_retry"
            )
        if settled:
            return IngestResult(
                IngestStatus.DUPLICATE, document_id=existing.id, detail="paperless_filed"
            )
        return IngestResult(
            IngestStatus.RETRY, document_id=existing.id, detail="paperless_pending"
        )

    # decision is CREATE or REINGEST: run the full pipeline.
    try:
        file_path = await _persist_bytes(file_bytes, meta.filename)
    except OSError as exc:
        # Disk full / permission denied writing the recovery copy. Transient —
        # the MCP leaves the file in the inbox and re-pushes rather than losing
        # it to failed/.
        logger.error(f"folder-ingest: persist failed for {meta.filename!r}: {exc}")
        return IngestResult(IngestStatus.RETRY, detail="persist_error")

    if decision is _Decision.REINGEST:
        # A prior terminal failure (D9 worker-set status=failed). Re-point the
        # row at the fresh recovery copy, reset the KB-pipeline state, and
        # re-enqueue rather than spawning a second row for the same (hash, kb).
        # paperless_state is preserved: if the Paperless leg already settled
        # ('done') a re-file would duplicate, so the idempotent leg skips it;
        # if it never settled it re-runs below.
        stale_path = existing.file_path
        existing.file_path = file_path
        existing.status = DOC_STATUS_PENDING
        existing.error_message = None
        await db.commit()
        if stale_path and stale_path != file_path:
            _cleanup(stale_path)  # the prior failed copy is now orphaned
        doc = existing
    else:
        rag = RAGService(db)
        try:
            doc = await rag.create_document_record_safe(
                file_path=file_path,
                knowledge_base_id=kb_id,
                filename=meta.filename,
                file_hash=file_hash,
            )
        except DuplicateDocumentError as dup:
            # Lost-race: a concurrent push committed the same (hash, kb)
            # between our classify SELECT and the INSERT. Drop the orphan copy
            # and report on the winner — retry if it's still in flight, else
            # duplicate. Never a 500 (idempotency property).
            _cleanup(file_path)
            winner = dup.winner
            # Mirror the classify_existing matrix on the winning row: only a
            # completed AND Paperless-settled winner is a terminal duplicate.
            # A completed-but-Paperless-missing (or still-in-flight) winner is
            # RETRY, so the re-push routes through PAPERLESS_ONLY / pending next
            # pass instead of being moved to processed/ with Paperless unfiled.
            if (
                winner is not None
                and winner.status == DOC_STATUS_COMPLETED
                and winner.paperless_state in _PAPERLESS_SETTLED
            ):
                return IngestResult(
                    IngestStatus.DUPLICATE,
                    document_id=winner.id,
                    detail="concurrent_winner",
                )
            return IngestResult(
                IngestStatus.RETRY,
                document_id=winner.id if winner else None,
                detail="concurrent_in_progress",
            )
        except Exception as exc:  # noqa: BLE001
            _cleanup(file_path)
            logger.error(f"folder-ingest: create failed for {meta.filename!r}: {exc}")
            return IngestResult(IngestStatus.FAILED, detail="create_error")

    queue = DocumentTaskQueue(redis_client=get_redis())
    await queue.enqueue(
        {
            "document_id": doc.id,
            "force_ocr": force_ocr,
            "user_id": owner_user_id,
        }
    )

    # 4. Paperless leg (best-effort — never fails the KB ingest; the row is
    # already enqueued, so we respond INGESTED regardless of the leg). The leg
    # records its own outcome on paperless_state. KNOWN GAP: on this CREATE path
    # the response is INGESTED → the MCP moves the file to processed/, so a leg
    # that didn't settle (transient Paperless outage) is NOT auto-retried — the
    # document is in the KB but missing from Paperless until a manual re-push or
    # a future paperless-reconciler (P2). A leg exception is likewise swallowed.
    try:
        await leg(db, doc, file_bytes, meta)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"folder-ingest: Paperless leg failed for doc {doc.id} "
            f"(KB ingest unaffected): {exc}"
        )

    return IngestResult(
        IngestStatus.INGESTED, document_id=doc.id, detail="enqueued"
    )


def _cleanup(file_path: str) -> None:
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError as exc:
        logger.warning(f"folder-ingest: failed to clean up {file_path}: {exc}")


# ---------------------------------------------------------------------------
# Bearer token management (mirrors NotificationService.{get,generate,verify}_
# webhook_token). The token authenticates the MCP→backend push; it lives in
# SystemSetting so it is revocable without a redeploy. generate_ backs the
# admin mint route (T14); verify_ guards the push route (T3).
# ---------------------------------------------------------------------------

async def get_folder_ingest_token(db: AsyncSession) -> str | None:
    """Return the stored folder-ingest Bearer token, or None if unset."""
    setting = (
        await db.execute(
            select(SystemSetting).where(
                SystemSetting.key == SETTING_FOLDER_INGEST_TOKEN
            )
        )
    ).scalar_one_or_none()
    return setting.value if setting else None


async def generate_folder_ingest_token(db: AsyncSession) -> str:
    """Mint a new folder-ingest token (rotating any existing one) and persist
    it to SystemSetting. Returns the plaintext token (shown once to the admin)."""
    token = secrets.token_urlsafe(48)
    existing = (
        await db.execute(
            select(SystemSetting).where(
                SystemSetting.key == SETTING_FOLDER_INGEST_TOKEN
            )
        )
    ).scalar_one_or_none()
    if existing:
        existing.value = token
    else:
        db.add(SystemSetting(key=SETTING_FOLDER_INGEST_TOKEN, value=token))
    await db.commit()
    logger.info("🔑 folder-ingest token (re)generated")
    return token


async def verify_folder_ingest_token(db: AsyncSession, token: str) -> bool:
    """Constant-time compare a presented Bearer token against the stored one.
    False when no token is configured (feature unprovisioned)."""
    stored = await get_folder_ingest_token(db)
    if not stored:
        return False
    return secrets.compare_digest(stored, token)
