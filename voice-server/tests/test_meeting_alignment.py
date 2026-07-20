"""Unit tests for the meeting word→speaker alignment (pure logic, no GPU)."""

import numpy as np

from voice_server.services.meeting_service import (
    MeetingDiarizationService,
    MeetingSegment,
    SpeakerRegistry,
    Turn,
    Word,
    _chunk_bounds,
    _merge_adjacent_same_speaker,
    align_words_to_segments,
)


def test_free_cuda_cache_is_safe_without_gpu():
    """The OOM-mitigation cache free is best-effort: it must never raise when
    there's no CUDA (build/test box), so it can't break a transcription."""
    MeetingDiarizationService._free_cuda_cache()  # no exception = pass


# --- chunked transcription: pure stitching/boundary logic (no GPU) ---

def test_chunk_bounds_covers_recording_with_short_last_window():
    assert _chunk_bounds(1000, 400) == [(0, 400), (400, 800), (800, 1000)]
    assert _chunk_bounds(300, 400) == [(0, 300)]      # shorter than a chunk → one pass
    assert _chunk_bounds(1000, 0) == [(0, 1000)]      # chunking disabled → one pass


def _emb(*vals):
    return np.asarray(vals, dtype=np.float32)


def test_speaker_registry_stitches_same_voice_across_chunks():
    """A voice that reappears in a later chunk (near-identical embedding) maps to
    the SAME global speaker; a distinct voice gets its own."""
    reg = SpeakerRegistry(threshold=0.7)
    a1 = reg.assign(_emb(1.0, 0.0, 0.0))   # voice A, chunk 1
    b1 = reg.assign(_emb(0.0, 1.0, 0.0))   # voice B, chunk 1
    a2 = reg.assign(_emb(0.98, 0.02, 0.0)) # voice A again, chunk 2 (cos≈1)
    c = reg.assign(_emb(0.0, 0.0, 1.0))    # voice C
    assert a1 == a2         # stitched
    assert b1 != a1 and c != a1 and c != b1
    assert {a1, b1, c} == {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02"}


def test_speaker_registry_below_threshold_makes_new_speaker():
    reg = SpeakerRegistry(threshold=0.9)
    x = reg.assign(_emb(1.0, 0.0))
    y = reg.assign(_emb(0.6, 0.8))   # cos 0.6 < 0.9 → distinct
    assert x != y


def test_speaker_registry_none_embedding_never_merges():
    """A silent/too-short local speaker (no embedding) always gets its own label
    — never mis-merged into someone else."""
    reg = SpeakerRegistry(threshold=0.5)
    a = reg.assign(_emb(1.0, 0.0))
    n1 = reg.assign(None)
    n2 = reg.assign(None)
    assert len({a, n1, n2}) == 3
    assert reg.centroid(n1) is None


def test_merge_adjacent_same_speaker_joins_across_boundary():
    segs = [
        MeetingSegment("SPEAKER_00", 0.0, 5.0, "hallo"),
        MeetingSegment("SPEAKER_00", 5.0, 8.0, "welt"),   # same speaker, next chunk
        MeetingSegment("SPEAKER_01", 8.0, 9.0, "ja"),
    ]
    merged = _merge_adjacent_same_speaker(segs)
    assert [s.speaker for s in merged] == ["SPEAKER_00", "SPEAKER_01"]
    assert merged[0].text == "hallo welt" and merged[0].end_s == 8.0


def _w(text, a, b):
    return Word(text=text, start_s=a, end_s=b)


def test_overlap_assignment_and_merge():
    """Words are assigned to the most-overlapping turn; consecutive same-speaker
    words merge into one segment."""
    turns = [Turn("SPEAKER_00", 0.0, 2.0), Turn("SPEAKER_01", 2.0, 4.0)]
    words = [_w("hallo", 0.0, 0.5), _w("welt", 0.6, 1.5),
             _w("guten", 2.1, 2.6), _w("tag", 2.7, 3.2)]
    segs = align_words_to_segments(words, turns)
    assert [s.speaker for s in segs] == ["SPEAKER_00", "SPEAKER_01"]
    assert segs[0].text == "hallo welt"
    assert segs[1].text == "guten tag"
    assert segs[0].start_s == 0.0 and segs[0].end_s == 1.5


def test_word_in_gap_goes_to_nearest_turn():
    """A word overlapping NO turn (diarization gap) attaches to the nearest one."""
    turns = [Turn("SPEAKER_00", 0.0, 1.0), Turn("SPEAKER_01", 3.0, 4.0)]
    words = [_w("gap", 1.4, 1.6)]  # closer to SPEAKER_00 (ends 1.0) than SPEAKER_01 (starts 3.0)
    segs = align_words_to_segments(words, turns)
    assert len(segs) == 1 and segs[0].speaker == "SPEAKER_00"


def test_no_diarization_single_speaker():
    turns: list[Turn] = []
    words = [_w("a", 0.0, 0.5), _w("b", 0.5, 1.0)]
    segs = align_words_to_segments(words, turns)
    assert len(segs) == 1
    assert segs[0].speaker == "SPEAKER_00"
    assert segs[0].text == "a b"


def test_empty_words_yields_no_segments():
    assert align_words_to_segments([], [Turn("SPEAKER_00", 0, 1)]) == []
    assert align_words_to_segments([_w("  ", 0, 1)], [Turn("SPEAKER_00", 0, 1)]) == []


def test_speaker_alternation_splits_segments():
    """A→B→A alternation produces three segments (no cross-speaker merge)."""
    turns = [Turn("SPEAKER_00", 0.0, 1.0), Turn("SPEAKER_01", 1.0, 2.0),
             Turn("SPEAKER_00", 2.0, 3.0)]
    words = [_w("one", 0.1, 0.5), _w("two", 1.1, 1.5), _w("three", 2.1, 2.5)]
    segs = align_words_to_segments(words, turns)
    assert [s.speaker for s in segs] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert [s.text for s in segs] == ["one", "two", "three"]


def test_word_partial_overlap_picks_max_overlap_turn():
    """A word straddling two turns lands with the one it overlaps MORE."""
    turns = [Turn("SPEAKER_00", 0.0, 1.0), Turn("SPEAKER_01", 1.0, 3.0)]
    # word 0.8–1.6: 0.2 s in SPEAKER_00, 0.6 s in SPEAKER_01 → SPEAKER_01
    segs = align_words_to_segments([_w("straddle", 0.8, 1.6)], turns)
    assert segs[0].speaker == "SPEAKER_01"
