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
    DOC_STATUS_SPLIT_ARCHIVED,
    PAPERLESS_STATE_DONE,
    PAPERLESS_STATE_FAILED,
    PAPERLESS_STATE_PENDING,
    SETTING_FOLDER_INGEST_TOKEN,
    Document,
    KnowledgeBase,
)

# paperless_state values that mean "the Paperless leg is settled" — either it
# was filed/duplicate (done) or terminally rejected for a non-duplicate reason
# (failed). Both stop the D2 matrix re-running the leg; only None/unset retries.
_PAPERLESS_SETTLED = (PAPERLESS_STATE_DONE, PAPERLESS_STATE_FAILED)
from services.ingest_common import (
    generate_ingest_token,
    get_ingest_token,
    resolve_user_id,
    verify_ingest_token,
)
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
    Kept separate from :class:`IngestStatus` because a decision may run work
    (create/enqueue) before resolving to a response state."""

    CREATE = "create"  # no row → run the full pipeline
    DUPLICATE = "duplicate"  # completed → dedup (Paperless handled async, Design Z)
    REINGEST = "reingest"  # status=failed → re-run the full pipeline
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
        # Paperless filing is decoupled from the move decision (Design Z): the
        # async ``paperless_reconciler`` owns it, keyed on paperless_state=
        # 'pending'. A completed KB row is therefore always a clean dedup —
        # the file moves to processed/ regardless of the Paperless leg state,
        # and the reconciler files it (or already has) out of band. This is why
        # the push never blocks on the external Paperless round-trip anymore.
        return _Decision.DUPLICATE, doc
    if doc.status == DOC_STATUS_FAILED:
        return _Decision.REINGEST, doc
    if doc.status == DOC_STATUS_SPLIT_ARCHIVED:
        # A re-pushed combined multi-document PDF whose split already executed:
        # the children are (being) ingested individually and the original is
        # deliberately archived without chunks — a clean dedup, never a
        # re-ingest. Without this branch the row would fall into RETRY and the
        # MCP would re-push the file forever.
        return _Decision.DUPLICATE, doc
    # pending / processing / split_pending / split_review — still in flight.
    return _Decision.RETRY, doc


# A Paperless leg is an injected coroutine used by the document-worker's
# ``post_document_ingest`` filing hook (and the retry/refile task) to file a
# document into Paperless. It takes the worker's already-computed ``doc_text``
# (best-quality OCR) — reused for metadata extraction AND written back as the
# Paperless document's searchable content. It MUST set ``doc.paperless_state`` and
# commit, and returns True when settled (filed / duplicate / terminal-reject).
# NOT invoked on the ingest request path — an inline external round-trip there
# caused the pool-exhaustion outage; and NOT in the backend — Docling belongs in
# the worker.
PaperlessLeg = Callable[
    [AsyncSession, Document, bytes, IngestMeta, "str | None"], Awaitable[bool]
]


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
    default_tier: int | None = None,
    file_to_paperless: bool = False,
    force_ocr: bool = False,
    source: str | None = None,
) -> IngestResult:
    """Run the folder-ingest pipeline for one pushed file and return the
    4-state result (D9). See the module docstring for the contract.

    Steps: (0) persist a recovery byte copy, (1) reject bad ext/oversize,
    (2) completion-aware dedup (D2), (3) race-safe create (D3) + enqueue,
    (4) mark for async Paperless filing, (5) respond. D4 owner/tier:
    ``owner_user_id`` (the configured ``folder_ingest_target_user``, None → KB
    owner / first user) and ``default_tier`` (the configured
    ``folder_ingest_default_tier``, None → KB default tier) are applied as
    overrides at create — auto-filed documents are owned by the configured user
    at the configured tier regardless of the KB.

    ``file_to_paperless`` is the Paperless-filing seam (Design Z): when True, a
    newly-created / re-ingested document is stamped ``paperless_state='pending'``
    and the out-of-band ``paperless_reconciler`` files it into Paperless later
    (own session, bounded concurrency). The push itself NEVER performs the
    external Paperless round-trip — that inline await was what pinned a pooled DB
    connection across a multi-second external wait and exhausted the pool under a
    burst. When False (filing disabled), the document is left ``paperless_state``
    NULL and the reconciler ignores it.
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

    # 2. Dedup against the existing row (D2). A completed row is a clean dedup
    # (Paperless is filed out of band by the reconciler — Design Z), so there is
    # no longer a PAPERLESS_ONLY branch running an inline leg on the request.
    decision, existing = await classify_existing(db, file_hash, kb_id)

    if decision is _Decision.DUPLICATE:
        # Self-heal a completed-but-unstamped row: a filing-wanted doc that
        # reached COMPLETED with paperless_state NULL is invisible to the
        # reconciler → silently unfiled. This covers rows completed BEFORE this
        # design shipped and rows completed while the to_paperless flag was off
        # and later turned on. (It does NOT cover a crash between stamp and
        # enqueue: step 4 stamps 'pending' BEFORE enqueue, so "enqueued ⟹
        # pending"; a crash before that leaves status='pending', which
        # classify_existing routes to RETRY, not here.) Re-arm on the re-push
        # (the file stayed in the inbox and is re-pushed). Only for filing-wanted
        # docs; NULL on an interactive upload is the intended "never file" state.
        if file_to_paperless and existing.paperless_state is None:
            existing.paperless_state = PAPERLESS_STATE_PENDING
            existing.paperless_task_id = None  # fresh attempt — no stale task to poll
            await db.commit()
        return IngestResult(
            IngestStatus.DUPLICATE, document_id=existing.id, detail="already_ingested"
        )

    if decision is _Decision.RETRY:
        # The worker still owns this row (pending/processing). Don't
        # double-enqueue; the MCP leaves the file in the inbox and re-pushes.
        return IngestResult(
            IngestStatus.RETRY, document_id=existing.id, detail="in_progress"
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
                owner_user_id_override=owner_user_id,  # D4: configured owner
                circle_tier_override=default_tier,  # D4: configured tier
                source=source,  # §2 D14: e.g. "meeting_transcript"
            )
        except DuplicateDocumentError as dup:
            # Lost-race: a concurrent push committed the same (hash, kb)
            # between our classify SELECT and the INSERT. Drop the orphan copy
            # and report on the winner — retry if it's still in flight, else
            # duplicate. Never a 500 (idempotency property).
            _cleanup(file_path)
            winner = dup.winner
            # Mirror the classify_existing matrix on the winning row (Design Z):
            # a completed winner is a terminal duplicate regardless of Paperless
            # state — the file moves to processed/ and the async reconciler files
            # Paperless out of band. A still-in-flight winner is RETRY.
            if winner is not None and winner.status == DOC_STATUS_COMPLETED:
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

    # 4. Mark for async Paperless filing (Design Z), BEFORE enqueue. We do NOT run
    # the leg on the request path — that inline external round-trip is exactly what
    # pinned a pooled DB connection across a multi-second Paperless wait and
    # exhausted the pool under a burst. Instead stamp paperless_state='pending'
    # (unless already filed on a REINGEST) and let the out-of-band
    # paperless_reconciler file it. 'pending' also doubles as the provenance
    # marker: only folder/email-ingest docs get it, so interactive KB uploads
    # (which stay NULL) are never filed.
    #
    # Ordering matters: stamp BEFORE enqueue so that "enqueued ⟹ pending set".
    # If we stamped after enqueue, a crash between the two (a stamp commit timing
    # out under the very DB-pressure burst this design targets) would let the
    # worker drive the doc to COMPLETED with paperless_state NULL — invisible to
    # the reconciler and silently never filed. Stamping first makes a crash-before-
    # enqueue merely orphan the row at status='pending' (the pre-existing
    # enqueue-failure class, visibly stuck, self-heals on the MCP re-push via the
    # DUPLICATE re-stamp above), never a silently-completed-but-unfiled doc.
    if file_to_paperless and doc.paperless_state != PAPERLESS_STATE_DONE:
        doc.paperless_state = PAPERLESS_STATE_PENDING
        # Clear any stale task_id from a prior attempt — this is a fresh (re)ingest,
        # so the poll-first guard must not re-poll an old task for possibly-changed
        # content; it should upload once and store the new task_id.
        doc.paperless_task_id = None
        await db.commit()

    queue = DocumentTaskQueue(redis_client=get_redis())
    await queue.enqueue(
        {
            "document_id": doc.id,
            "force_ocr": force_ocr,
            "user_id": owner_user_id,
        }
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
    return await get_ingest_token(db, SETTING_FOLDER_INGEST_TOKEN)


async def generate_folder_ingest_token(db: AsyncSession) -> str:
    """Mint a new folder-ingest token (rotating any existing one) and persist
    it to SystemSetting. Returns the plaintext token (shown once to the admin)."""
    return await generate_ingest_token(db, SETTING_FOLDER_INGEST_TOKEN)


async def verify_folder_ingest_token(db: AsyncSession, token: str) -> bool:
    """Constant-time compare a presented Bearer token against the stored one.
    False when no token is configured (feature unprovisioned)."""
    return await verify_ingest_token(db, SETTING_FOLDER_INGEST_TOKEN, token)


# ---------------------------------------------------------------------------
# Target KB + owner resolution (shared by the push route and the
# internal.ingest_file agent tool). Both file into the single configured
# (folder_ingest_kb_name, folder_ingest_target_user) destination.
# ---------------------------------------------------------------------------

async def resolve_target_kb(db: AsyncSession) -> KnowledgeBase:
    """Get-or-create the configured folder-ingest target KB. Mirrors the
    chat-upload default-KB pattern so a fresh install just works."""
    kb = (
        await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.name == settings.folder_ingest_kb_name
            )
        )
    ).scalar_one_or_none()
    if kb:
        return kb
    kb = KnowledgeBase(
        name=settings.folder_ingest_kb_name,
        description="Auto-ingested documents from watched folders",
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def target_kb_exists(db: AsyncSession) -> bool:
    """Whether the configured folder-ingest target KB already exists. SELECT
    only — unlike :func:`resolve_target_kb` it does NOT create the KB, so the
    health handshake can report provisioning state without a side effect."""
    kb_id = (
        await db.execute(
            select(KnowledgeBase.id).where(
                KnowledgeBase.name == settings.folder_ingest_kb_name
            )
        )
    ).scalar_one_or_none()
    return kb_id is not None


async def resolve_owner_user_id(db: AsyncSession) -> int | None:
    """Resolve ``folder_ingest_target_user`` (username or numeric id) to a user
    id. Empty config → None (the bridge/worker handle an ownerless enqueue the
    same way the upload route does for unauthenticated single-user mode)."""
    return await resolve_user_id(db, settings.folder_ingest_target_user)
