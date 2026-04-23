"""
paperless_example_retriever — fetch the most-relevant past confirm-diffs
for prompt augmentation.

Lifecycle:

    1. PR 2 commit tool writes a row into ``paperless_extraction_examples``
       every time the user approves an extraction whose final field set
       differs from the LLM's post-fuzzy output (i.e. a real correction
       signal).
    2. PR 3 (this file) embeds every persisted row's ``doc_text`` and
       stores the vector in ``doc_text_embedding``.
    3. On each subsequent extraction, the extractor calls
       ``fetch_relevant_examples(doc_text)`` to pull the top-N rows by
       cosine similarity, which it then renders into the prompt as
       additional in-context examples (ahead of the input doc, after
       the seed examples baked into the YAML).

Design constraints (from ``docs/design/paperless-llm-metadata.md``):

- Cap the learned set so total in-context examples ≤ 5
  (3 baked + 2 learned). The default ``limit=2`` enforces this.
- Skip ``superseded=true`` rows. PR 4 sets the flag when a UI-sweep
  correction turns out to be taxonomy drift.
- Silent fallback. Embedding outage or DB issue must not block the
  extraction path — return ``[]`` and let the caller continue with the
  seed examples.
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from sqlalchemy import select

# Embedding lookups happen at retrieval time, so we want to cap latency.
# Production embedding p50 lands around 80–250 ms on a warm Ollama;
# we accept up to ~3× headroom before falling back to the seed-only
# prompt. Beyond that, blocking the user-visible upload flow on the
# learning loop is the wrong tradeoff.
_EMBED_TIMEOUT_S = 5.0


async def fetch_relevant_examples(
    doc_text: str,
    *,
    user_id: int | None,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Return up to *limit* past corrections most similar to *doc_text*.

    Each entry is a dict with keys ``llm_output`` and ``user_approved``
    (the JSON payloads persisted by the commit tool). The caller decides
    how to render them; this function only handles retrieval.

    *user_id* scopes the query to the asker's own corrections. Passing
    ``None`` (AUTH_ENABLED=false dev mode) disables owner-scoping and
    returns matches across all NULL-user rows — the only rows that
    exist in that mode anyway. Never cross the user boundary.

    Returns ``[]`` if:
      - input is empty/blank,
      - no rows have a non-null embedding yet,
      - the embedding call or DB query fails (logged at WARNING, not
        re-raised — extraction must continue).
    """
    if not doc_text or not doc_text.strip():
        return []
    if limit <= 0:
        return []

    try:
        embedding = await _embed_doc_text(doc_text)
    except Exception as exc:
        logger.warning("Paperless example retrieval skipped — embed failed: %s", exc)
        return []

    if embedding is None:
        return []

    # Lazy DB import — same anti-engine-at-import-time pattern as PR 2b.
    try:
        from models.database import PaperlessExtractionExample
        from services.database import AsyncSessionLocal
    except ImportError as exc:
        logger.warning("Paperless example retrieval skipped — DB not available: %s", exc)
        return []

    try:
        async with AsyncSessionLocal() as db:
            # cosine_distance comes from pgvector.sqlalchemy. Lower is
            # more similar. We don't filter by a distance threshold —
            # the prompt can absorb a couple of mediocre matches with
            # marginal cost, and a hard threshold tuned without data
            # would be guesswork at this stage.
            #
            # Source filter: PR 3 ships before PR 4 so today the column
            # only ever holds 'confirm_diff'. Pinning the filter here
            # keeps it that way — once PR 4 adds ``paperless_ui_sweep``
            # rows, someone has to come back and decide whether to
            # include them (with what weighting). Blocking the
            # forward-compat gap today is better than silently picking
            # up lower-quality UI-sweep rows the day PR 4 lands.
            stmt = (
                select(PaperlessExtractionExample)
                .where(PaperlessExtractionExample.superseded.is_(False))
                .where(PaperlessExtractionExample.doc_text_embedding.is_not(None))
                .where(PaperlessExtractionExample.source == "confirm_diff")
                .where(PaperlessExtractionExample.user_id == user_id)
                .order_by(
                    PaperlessExtractionExample.doc_text_embedding.cosine_distance(embedding)
                )
                .limit(limit)
            )
            result = await db.execute(stmt)
            rows = result.scalars().all()
    except Exception as exc:
        logger.warning("Paperless example retrieval skipped — DB query failed: %s", exc)
        return []

    examples: list[dict[str, Any]] = []
    for row in rows:
        examples.append({
            "doc_text": row.doc_text or "",
            "llm_output": row.llm_output or {},
            "user_approved": row.user_approved or {},
            "source": row.source,
        })
    return examples


async def embed_doc_text(doc_text: str) -> list[float] | None:
    """Public wrapper used by the commit tool to embed at write time.

    Returns ``None`` on failure — callers persist the row anyway, just
    with a NULL embedding (still valuable for the raw correction signal,
    just invisible to similarity retrieval until backfilled).
    """
    if not doc_text or not doc_text.strip():
        return None
    try:
        return await _embed_doc_text(doc_text)
    except Exception as exc:
        logger.warning("Paperless example embed failed (row will store NULL): %s", exc)
        return None


async def _embed_doc_text(doc_text: str) -> list[float] | None:
    """Single-shot embedding call. Raises on real failure so the
    retriever can distinguish 'blank input' (None) from 'transient
    Ollama issue' (exception)."""
    # Lazy import — keep this module importable in test environments
    # without a live Ollama config.
    from utils.config import settings
    from utils.llm_client import get_embed_client

    client = get_embed_client()
    response = await asyncio.wait_for(
        client.embeddings(
            model=settings.ollama_embed_model,
            prompt=doc_text,
        ),
        timeout=_EMBED_TIMEOUT_S,
    )
    # ollama>=0.4.0 wraps response in a Pydantic model with .embedding,
    # but older clients / mocks may return a dict. Tolerate both.
    if hasattr(response, "embedding"):
        return list(response.embedding)
    if isinstance(response, dict):
        emb = response.get("embedding")
        return list(emb) if emb is not None else None
    return None
