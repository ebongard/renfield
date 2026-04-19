"""
DocumentProgress — live per-document processing progress in Redis (#388).

Why in Redis, not Postgres:
  Docling/EasyOCR emit progress callbacks at sub-second intervals while a
  multi-page PDF is being OCR'd. Writing those into the ``documents`` table
  would generate hundreds of UPDATEs per ingestion and defeat the Postgres
  connection pool. Redis is the right tool for ephemeral, write-heavy,
  TTL-bound state. The ``Document`` row only tracks terminal status
  (``pending``/``processing``/``completed``/``failed``); stage + page counts
  live here.

Key shape:
    renfield:doc:{doc_id}:stage       → "parsing" | "ocr" | "chunking" | "embedding"
    renfield:doc:{doc_id}:progress    → "<current>/<total>" (empty for non-paginated)

Both keys use the same 30-minute TTL so a dead task can't leak state
forever. The frontend reads these via ``GET /api/knowledge/documents/{id}``
alongside the DB status. ``clear()`` is called on both success and failure
so the UI doesn't flash stale progress against a ``completed`` row.
"""
from __future__ import annotations

from typing import Literal

import redis.asyncio as aioredis
from loguru import logger

Stage = Literal["parsing", "ocr", "chunking", "embedding"]

KEY_PREFIX = "renfield:doc"
STAGE_SUFFIX = "stage"
PROGRESS_SUFFIX = "progress"
DEFAULT_TTL_S = 30 * 60  # 30 minutes; matches worker visibility + UI polling timeout


def _stage_key(doc_id: int) -> str:
    return f"{KEY_PREFIX}:{doc_id}:{STAGE_SUFFIX}"


def _progress_key(doc_id: int) -> str:
    return f"{KEY_PREFIX}:{doc_id}:{PROGRESS_SUFFIX}"


class DocumentProgress:
    """Live progress publisher for a single Document being ingested.

    Instances are cheap. Typically constructed per-task inside the worker
    and passed down into ``DocumentProcessor`` as a callback sink.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        doc_id: int,
        ttl_s: int = DEFAULT_TTL_S,
    ):
        self.redis = redis_client
        self.doc_id = doc_id
        self.ttl_s = ttl_s

    async def set_stage(self, stage: Stage) -> None:
        """Advance the stage label. Refreshes the TTL on the stage key."""
        await self.redis.set(_stage_key(self.doc_id), stage, ex=self.ttl_s)

    async def set_pages(self, current: int, total: int) -> None:
        """Record ``current/total`` page progress. Only meaningful for paginated
        formats (PDF, DOCX). Non-paginated flows just call ``set_stage``.
        """
        if total <= 0:
            return
        if current < 0:
            current = 0
        if current > total:
            current = total
        value = f"{current}/{total}"
        await self.redis.set(_progress_key(self.doc_id), value, ex=self.ttl_s)

    async def clear(self) -> None:
        """Remove both keys. Called from the worker's ``finally`` block so a
        completed/failed document doesn't keep advertising stage info."""
        await self.redis.delete(
            _stage_key(self.doc_id),
            _progress_key(self.doc_id),
        )

    async def read(self) -> dict:
        """Read the current stage + pages. Returned dict has shape:

            {"stage": "ocr" | None, "pages": {"current": 47, "total": 120} | None}

        Used by the ``/api/knowledge/documents/{id}`` response to surface
        progress to the frontend. Missing keys map to ``None`` rather than
        raising so the API can safely call ``read()`` for any document.
        """
        stage_raw, progress_raw = await self.redis.mget(
            _stage_key(self.doc_id),
            _progress_key(self.doc_id),
        )
        pages = None
        if progress_raw:
            try:
                current_str, total_str = progress_raw.split("/", 1)
                pages = {"current": int(current_str), "total": int(total_str)}
            except ValueError:
                logger.warning(
                    f"DocumentProgress: malformed value for doc {self.doc_id}: "
                    f"{progress_raw!r}"
                )
        return {"stage": stage_raw, "pages": pages}
