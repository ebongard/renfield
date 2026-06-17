"""OutputRoutingService: audio-quality tiebreak within equal priority.

Policy: `priority` (the user-set primary ordering) wins first; when devices
share a priority (e.g. all at the default — the user never ordered them), the
higher-audio-quality device class wins: external AV renderer (DLNA / Samsung /
Sonos) > HA media_player > Renfield tablet/satellite. So an unordered room with
a HiFiBerry + a tablet auto-prefers the HiFiBerry.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Pre-mock heavy/optional modules pulled in transitively (mirror the sibling
# routing test's guarded stubbing).
for _mod in ("asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
             "speechbrain.inference", "speechbrain.inference.speaker",
             "openwakeword", "openwakeword.model"):
    if _mod in sys.modules:
        continue
    try:
        __import__(_mod)
    except Exception:  # noqa: BLE001
        sys.modules[_mod] = MagicMock()

import pytest

from ha_glue.services.output_routing_service import (
    OutputRoutingService,
    _audio_quality_rank,
)


def _dev(*, target_type, priority=1, dev_id=0):
    d = MagicMock()
    d.target_type = target_type
    d.priority = priority
    d.id = dev_id
    return d


def _svc():
    with patch("ha_glue.services.output_routing_service.HomeAssistantClient", return_value=AsyncMock()):
        return OutputRoutingService(AsyncMock())


def _mock_query_result(svc, devices):
    """Make svc._get_output_devices's DB query return `devices` (unsorted)."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = devices
    svc.db.execute = AsyncMock(return_value=result)


@pytest.mark.unit
def test_quality_rank_by_class() -> None:
    assert _audio_quality_rank(_dev(target_type="dlna")) == 3
    assert _audio_quality_rank(_dev(target_type="samsung")) == 3
    assert _audio_quality_rank(_dev(target_type="sonos")) == 3
    assert _audio_quality_rank(_dev(target_type="homeassistant")) == 2
    assert _audio_quality_rank(_dev(target_type="renfield")) == 1
    # Unknown / future provider → treated as an external renderer (high).
    assert _audio_quality_rank(_dev(target_type="future_brand")) == 3


@pytest.mark.unit
async def test_tiebreak_prefers_highest_quality_when_unordered() -> None:
    """All default priority → DLNA > HA > renfield, regardless of DB order."""
    svc = _svc()
    tablet = _dev(target_type="renfield", priority=1, dev_id=1)
    speaker = _dev(target_type="homeassistant", priority=1, dev_id=2)
    hifi = _dev(target_type="dlna", priority=1, dev_id=3)
    _mock_query_result(svc, [tablet, speaker, hifi])  # arbitrary insertion order

    ordered = await svc._get_output_devices(room_id=1, output_type="audio")

    assert [d.target_type for d in ordered] == ["dlna", "homeassistant", "renfield"]


@pytest.mark.unit
async def test_explicit_priority_beats_quality() -> None:
    """A tablet the user ordered first (priority 1) beats a HiFiBerry at 2."""
    svc = _svc()
    tablet = _dev(target_type="renfield", priority=1, dev_id=1)
    hifi = _dev(target_type="dlna", priority=2, dev_id=2)
    _mock_query_result(svc, [hifi, tablet])

    ordered = await svc._get_output_devices(room_id=1, output_type="audio")

    assert [d.target_type for d in ordered] == ["renfield", "dlna"]  # priority wins


@pytest.mark.unit
async def test_same_class_same_priority_is_deterministic_by_id() -> None:
    """Two externals tied on priority + quality → stable by id."""
    svc = _svc()
    a = _dev(target_type="dlna", priority=1, dev_id=7)
    b = _dev(target_type="samsung", priority=1, dev_id=3)
    _mock_query_result(svc, [a, b])

    ordered = await svc._get_output_devices(room_id=1, output_type="audio")

    assert [d.id for d in ordered] == [3, 7]
