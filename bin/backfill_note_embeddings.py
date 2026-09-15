#!/usr/bin/env python3
"""Backfill dense embeddings for notes created before the dense-embedding slice.

Notes written under 4B.1/4B.2 have no ``embedding`` (FTS-only). This computes it
from title+body via the same embed client the write path uses, so semantic note
search covers historical notes. Idempotent: only touches rows WHERE embedding IS
NULL; re-runnable. Postgres-only (the embedding column is real pgvector there).

ALWAYS --dry-run first:

    python bin/backfill_note_embeddings.py --dry-run              # count, no writes
    python bin/backfill_note_embeddings.py --commit               # do it
    python bin/backfill_note_embeddings.py --commit --limit 500   # cap rows
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

# --- backend import path (identical block in every bin/backfill_*.py) ---------------
# These scripts are copied into the backend pod on their own, so this cannot live in a
# shared module: it is what makes the shared modules importable in the first place.


def _find_backend_dir(
    script: Path, env: Mapping[str, str] = os.environ, image_root: Path = Path("/app")
) -> Path:
    """The Renfield backend root: ``$RENFIELD_BACKEND_DIR`` (exclusive when set), else
    the repo layout ``bin/../src/backend``, else the image layout ``/app``."""
    override = env.get("RENFIELD_BACKEND_DIR")
    candidates = (
        [Path(override)] if override
        else [script.resolve().parent.parent / "src" / "backend", image_root]
    )
    for candidate in candidates:
        if (candidate / "services" / "__init__.py").is_file() and (candidate / "utils" / "config.py").is_file():
            return candidate
    print(
        f"{script.name}: Renfield backend not found (tried: {', '.join(map(str, candidates))}). "
        "Set RENFIELD_BACKEND_DIR to the directory holding services/ and utils/ "
        "(repo: src/backend, image: /app).",
        file=sys.stderr,
    )
    raise SystemExit(2)


_BACKEND = _find_backend_dir(Path(__file__))
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# --- end backend import path ---------------------------------------------------------

from services.database import AsyncSessionLocal  # noqa: E402
from sqlalchemy import select, func  # noqa: E402
from models.database import Note  # noqa: E402
from utils.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_note_embeddings")


async def _embed(text_in: str) -> list[float] | None:
    from utils.llm_client import get_embed_client
    client = get_embed_client()
    resp = await client.embeddings(model=settings.ollama_embed_model, prompt=text_in)
    return resp.embedding


async def main(dry_run: bool, limit: int | None) -> int:
    async with AsyncSessionLocal() as db:
        if db.bind is None or db.bind.dialect.name != "postgresql":
            log.error("Not Postgres — the embedding column is Text on sqlite; nothing to do.")
            return 1
        total = (await db.execute(
            select(func.count()).select_from(Note).where(Note.embedding.is_(None))
        )).scalar() or 0
        log.info("%d note(s) without an embedding.", total)
        if dry_run:
            log.info("[dry-run] would embed %d note(s).", min(total, limit or total))
            return 0

        stmt = select(Note).where(Note.embedding.is_(None)).order_by(Note.id)
        if limit:
            stmt = stmt.limit(limit)
        notes = (await db.execute(stmt)).scalars().all()
        done = failed = 0
        for note in notes:
            text_in = f"{note.title}\n{note.body or ''}".strip()
            if not text_in:
                continue
            try:
                emb = await _embed(text_in)
                note.embedding = emb
                await db.flush()
                done += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                log.warning("note %s: embed failed (skipped): %s", note.id, e)
        await db.commit()
        log.info("Done: %d embedded, %d failed.", done, failed)
        return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="count only, no writes")
    g.add_argument("--commit", action="store_true", help="compute + store embeddings")
    ap.add_argument("--limit", type=int, default=None, help="cap rows processed")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(dry_run=args.dry_run, limit=args.limit)))
