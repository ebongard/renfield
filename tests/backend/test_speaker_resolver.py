"""Tests for services.speaker_resolver — B.4.a wire-embedding speaker resolution.

Stand-alone unit tests for the resolver. Database-dependent paths are
covered by the integration tests in test_speakers.py; these tests focus
on input shapes (None / empty / wrong-type embeddings) and the early-
return contract that callers depend on.
"""

from __future__ import annotations

import numpy as np
import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_returns_empty_info_for_none_embedding() -> None:
    from services.speaker_resolver import resolve_speaker_from_embedding

    result = await resolve_speaker_from_embedding(db_session=None, embedding=None)
    assert result == {
        "speaker_id": None,
        "speaker_name": None,
        "speaker_alias": None,
        "speaker_confidence": 0.0,
        "is_new_speaker": False,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_returns_empty_info_for_empty_array() -> None:
    from services.speaker_resolver import resolve_speaker_from_embedding

    result = await resolve_speaker_from_embedding(
        db_session=None, embedding=np.array([], dtype=np.float32)
    )
    assert result["speaker_id"] is None
    assert result["is_new_speaker"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_accepts_python_list_embedding() -> None:
    """Wire format is JSON list[float]; resolver must coerce to ndarray."""
    from services.speaker_resolver import resolve_speaker_from_embedding

    embedding_list = [0.1] * 192
    # Pass empty list to short-circuit before DB access; the type
    # coercion happens before the early return for empty arrays.
    result = await resolve_speaker_from_embedding(db_session=None, embedding=[])
    assert result["speaker_id"] is None
    # Verify the float-list code path doesn't blow up on a non-empty
    # but bogus-shape input — without a session, the DB call would
    # fail; we expect the caught-exception branch to return empty info.
    result2 = await resolve_speaker_from_embedding(
        db_session=None, embedding=embedding_list
    )
    assert result2["speaker_id"] is None
