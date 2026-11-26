"""
Unit tests for the continuous BLE presence scanner (BLEScanner continuous mode).

Pure logic — exercises the detection-callback EWMA smoothing, known-MAC
filtering, threshold gating and freshness pruning without any BLE hardware.
"""
import time
from types import SimpleNamespace

import pytest
from renfield_satellite.ble.scanner import BLEScanner


def _det(scanner, mac, rssi):
    """Feed one advertisement into the detection callback."""
    scanner._on_detection(SimpleNamespace(address=mac), SimpleNamespace(rssi=rssi))


@pytest.mark.satellite
@pytest.mark.unit
class TestBLEScannerContinuous:
    def test_only_known_macs_tracked(self):
        s = BLEScanner(rssi_threshold=-80)
        s.update_known({"AA:BB:CC:DD:EE:FF"})
        _det(s, "AA:BB:CC:DD:EE:FF", -50)
        _det(s, "11:22:33:44:55:66", -40)  # not in whitelist
        macs = {r["mac"] for r in s.get_readings()}
        assert macs == {"AA:BB:CC:DD:EE:FF"}

    def test_case_insensitive_match(self):
        s = BLEScanner(rssi_threshold=-80)
        s.update_known({"aa:bb:cc:dd:ee:ff"})
        _det(s, "AA:BB:CC:DD:EE:FF", -55)
        assert len(s.get_readings()) == 1

    def test_ewma_smoothing(self):
        s = BLEScanner(rssi_threshold=-100, smoothing_alpha=0.5)
        s.update_known({"AA:BB:CC:DD:EE:FF"})
        _det(s, "AA:BB:CC:DD:EE:FF", -60)
        _det(s, "AA:BB:CC:DD:EE:FF", -70)  # 0.5*-70 + 0.5*-60 = -65
        assert s.get_readings()[0]["rssi"] == -65

    def test_threshold_gates_weak_signal(self):
        s = BLEScanner(rssi_threshold=-70)
        s.update_known({"AA:BB:CC:DD:EE:FF"})
        _det(s, "AA:BB:CC:DD:EE:FF", -85)  # below threshold
        assert s.get_readings() == []

    def test_freshness_prune(self):
        s = BLEScanner(rssi_threshold=-100, freshness_seconds=5.0)
        s.update_known({"AA:BB:CC:DD:EE:FF"})
        _det(s, "AA:BB:CC:DD:EE:FF", -50)
        assert len(s.get_readings()) == 1
        # Age the last-seen timestamp beyond the freshness window
        s._readings["AA:BB:CC:DD:EE:FF"][1] = time.monotonic() - 10.0
        assert s.get_readings() == []

    def test_update_known_drops_removed_device(self):
        s = BLEScanner(rssi_threshold=-100)
        s.update_known({"AA:BB:CC:DD:EE:FF"})
        _det(s, "AA:BB:CC:DD:EE:FF", -50)
        assert len(s.get_readings()) == 1
        s.update_known(set())  # device no longer tracked
        _det(s, "AA:BB:CC:DD:EE:FF", -50)  # a new advert must not bring it back
        assert s.get_readings() == []

    def test_none_rssi_ignored(self):
        s = BLEScanner(rssi_threshold=-100)
        s.update_known({"AA:BB:CC:DD:EE:FF"})
        _det(s, "AA:BB:CC:DD:EE:FF", None)
        assert s.get_readings() == []


@pytest.mark.satellite
@pytest.mark.unit
class TestWhatTheBackendNeedsToReadASilence:
    """#1551 stage 2 C: the age of a reading, new-only reporting, and the
    scanner status with the cutoff and the radio's sign of life."""

    def test_a_reading_carries_the_age_of_its_advertisement(self):
        s = BLEScanner(rssi_threshold=-100)
        s.update_known({"AA:BB:CC:DD:EE:FF"})
        _det(s, "AA:BB:CC:DD:EE:FF", -50)
        s._readings["AA:BB:CC:DD:EE:FF"][1] = time.monotonic() - 7.0
        (reading,) = s.get_readings()
        assert 6.9 <= reading["age_s"] <= 7.5

    def test_an_old_value_is_not_reported_again(self):
        """Repeating the smoothed value of an old advert made it look fresh at
        the backend; only a new advertisement produces a new reading."""
        s = BLEScanner(rssi_threshold=-100)
        s.update_known({"AA:BB:CC:DD:EE:FF"})
        _det(s, "AA:BB:CC:DD:EE:FF", -50)
        assert len(s.get_readings()) == 1
        assert s.get_readings() == []
        _det(s, "AA:BB:CC:DD:EE:FF", -60)
        (reading,) = s.get_readings()
        assert reading["rssi"] == -54  # smoothing is unchanged: 0.4*-60 + 0.6*-50

    def test_status_reports_cutoff_and_the_radio_sign_of_life(self):
        s = BLEScanner(rssi_threshold=-80)
        assert s.status("continuous") == {
            "mode": "continuous", "running": False, "cutoff_dbm": -80, "last_advert_age_s": None,
        }
        s._scanner = object()  # running
        # Any device counts as a sign of life — known or not.
        _det(s, "11:22:33:44:55:66", -95)
        status = s.status("continuous")
        assert status["running"] is True
        assert 0.0 <= status["last_advert_age_s"] < 1.0
        assert s.get_readings() == []  # the stranger is still never reported

    def test_an_advert_without_rssi_is_still_a_sign_of_life(self):
        s = BLEScanner()
        _det(s, "11:22:33:44:55:66", None)
        assert s.status("continuous")["last_advert_age_s"] is not None

    @pytest.mark.asyncio
    async def test_periodic_status_follows_the_burst(self, monkeypatch):
        import renfield_satellite.ble.scanner as mod

        class _Discover:
            result: object = {}

            @classmethod
            async def discover(cls, timeout, return_adv):
                if isinstance(cls.result, Exception):
                    raise cls.result
                return cls.result

        monkeypatch.setattr(mod, "_BleakScanner", _Discover)
        monkeypatch.setattr(mod, "BLEAK_AVAILABLE", True)
        s = BLEScanner(rssi_threshold=-80)
        known = {"AA:BB:CC:DD:EE:FF"}
        stranger = (SimpleNamespace(address="11:22:33:44:55:66"), SimpleNamespace(rssi=-90))
        mine = (SimpleNamespace(address="AA:BB:CC:DD:EE:FF"), SimpleNamespace(rssi=-60))
        _Discover.result = {1: stranger, 2: mine}
        assert await s.scan(known) == [{"mac": "AA:BB:CC:DD:EE:FF", "rssi": -60, "age_s": 0.0}]
        status = s.status("periodic")
        assert status["running"] is True and status["last_advert_age_s"] is not None

        _Discover.result = RuntimeError("adapter gone")
        assert await s.scan(known) == []
        assert s.status("periodic")["running"] is False
