"""ECAPA input cap — the meeting-OOM root fix (pure, no GPU)."""

import numpy as np

from voice_server.services.speaker_service import cap_clip


def test_short_clip_passes_through_unchanged():
    a = np.arange(1000, dtype=np.float32)
    out = cap_clip(a, 3000)
    assert out is a  # no copy/slice when already within the cap


def test_long_clip_is_centered_to_cap():
    a = np.arange(100_000, dtype=np.float32)
    out = cap_clip(a, 30_000)
    assert out.size == 30_000
    # centered window: starts at (100000-30000)//2 = 35000
    assert out[0] == 35_000 and out[-1] == 64_999


def test_zero_or_negative_cap_disables_capping():
    a = np.arange(50_000, dtype=np.float32)
    assert cap_clip(a, 0).size == 50_000
    assert cap_clip(a, -1).size == 50_000
