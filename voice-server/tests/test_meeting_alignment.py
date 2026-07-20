"""Unit tests for the meeting word→speaker alignment (pure logic, no GPU)."""

from voice_server.services.meeting_service import (
    MeetingDiarizationService,
    Turn,
    Word,
    align_words_to_segments,
)


def test_free_cuda_cache_is_safe_without_gpu():
    """The OOM-mitigation cache free is best-effort: it must never raise when
    there's no CUDA (build/test box), so it can't break a transcription."""
    MeetingDiarizationService._free_cuda_cache()  # no exception = pass


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
