#!/usr/bin/env python3
"""Strip voiceprint fields from ``meetings.segments`` rows written before the pipeline did.

Until the biometric-persistence fix, ``meeting_pipeline.process_meeting`` stored
the voice-server's per-cluster ECAPA ``embedding`` (192 floats, biometric data
under Art. 9 GDPR) on every segment of ``Meeting.segments``. New rows are clean;
this removes the field from existing rows using the SAME
``meeting_pipeline.strip_biometric_fields`` helper the pipeline uses. Nothing else
in the row changes (text, timing, labels, speaker_key, fingerprint ids stay).

Prints COUNTS ONLY (meetings / segments affected) — never content, names or ids'
text. Dry run unless ``--commit``. Idempotent: a second run finds nothing.

Usage:
    python bin/purge_meeting_segment_embeddings.py --dry-run
    python bin/purge_meeting_segment_embeddings.py --commit [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "backend"))

from sqlalchemy import select  # noqa: E402

from models.database import Meeting  # noqa: E402
from services.database import AsyncSessionLocal  # noqa: E402
from services.meeting_pipeline import _set_segments, strip_biometric_fields  # noqa: E402


def count_biometric_segments(segments: list | None) -> int:
    """How many segments of one row carry a voiceprint field."""
    if not segments:
        return 0
    clean = strip_biometric_fields(segments)
    return sum(
        1 for raw, stripped in zip(
            (s for s in segments if isinstance(s, dict)), clean, strict=True
        )
        if len(raw) != len(stripped)
    )


async def run(commit: bool, limit: int | None) -> tuple[int, int]:
    meetings_affected = 0
    segments_affected = 0
    async with AsyncSessionLocal() as db:
        q = select(Meeting).where(Meeting.segments.is_not(None)).order_by(Meeting.id)
        if limit:
            q = q.limit(limit)
        for meeting in (await db.execute(q)).scalars().all():
            n = count_biometric_segments(meeting.segments)
            if not n:
                continue
            meetings_affected += 1
            segments_affected += n
            if commit:
                _set_segments(meeting, meeting.segments)
        if commit and meetings_affected:
            await db.commit()
    print(
        f"meetings_with_voiceprints={meetings_affected} "
        f"segments_with_voiceprints={segments_affected} "
        f"{'(stripped)' if commit else '(dry-run, no writes)'}"
    )
    return meetings_affected, segments_affected


def main() -> None:
    ap = argparse.ArgumentParser(description="Strip voiceprint fields from meetings.segments")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="count only, no writes")
    g.add_argument("--commit", action="store_true", help="strip and persist")
    ap.add_argument("--limit", type=int, default=None, help="cap meetings scanned")
    args = ap.parse_args()
    asyncio.run(run(commit=args.commit, limit=args.limit))


if __name__ == "__main__":
    main()
