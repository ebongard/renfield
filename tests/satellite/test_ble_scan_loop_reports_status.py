"""The BLE scan loop reports EVERY cycle with the scanner status, also with no
device (#1551 stage 2 C): without it the backend cannot tell "not here" from
"not heard", and falls back to the heartbeat as the sign of life."""
from types import SimpleNamespace

import pytest
from renfield_satellite.ble.scanner import BLEScanner
from renfield_satellite.satellite import Satellite

pytestmark = [pytest.mark.satellite, pytest.mark.unit]


class _Client:
    is_connected = True

    def __init__(self, owner, cycles):
        self.owner, self.cycles, self.sent = owner, cycles, []

    async def send_ble_presence(self, devices, scanner=None):
        self.sent.append((devices, scanner))
        if len(self.sent) >= self.cycles:
            self.owner._running = False


def _satellite(continuous: bool, cycles: int = 2):
    scanner = BLEScanner(rssi_threshold=-80)
    sat = SimpleNamespace(
        config=SimpleNamespace(ble=SimpleNamespace(continuous=continuous, scan_interval=0)),
        _running=True,
        _ble_known_macs={"AA:BB:CC:DD:EE:FF"},
        _ble_irks={},
        ble_scanner=scanner,
    )
    sat.ws_client = _Client(sat, cycles)
    return sat, scanner


@pytest.mark.asyncio
async def test_continuous_reports_every_cycle_with_its_status(monkeypatch):
    sat, scanner = _satellite(continuous=True)

    async def _start(known):
        scanner._scanner = object()
        return True

    async def _stop():
        scanner._scanner = None

    monkeypatch.setattr(scanner, "start_continuous", _start)
    monkeypatch.setattr(scanner, "stop_continuous", _stop)
    await Satellite._ble_scan_loop(sat)

    assert len(sat.ws_client.sent) == 2  # no device heard: still a report each cycle
    for devices, status in sat.ws_client.sent:
        assert devices == []
        assert status == {"mode": "continuous", "running": True, "cutoff_dbm": -80,
                          "last_advert_age_s": None}


@pytest.mark.asyncio
async def test_a_continuous_scanner_that_cannot_start_reports_as_periodic(monkeypatch):
    sat, scanner = _satellite(continuous=True, cycles=1)

    async def _start(known):
        return False

    async def _scan(known):
        scanner._periodic_ok = True
        return []

    async def _stop():
        pass

    monkeypatch.setattr(scanner, "start_continuous", _start)
    monkeypatch.setattr(scanner, "scan", _scan)
    monkeypatch.setattr(scanner, "stop_continuous", _stop)
    await Satellite._ble_scan_loop(sat)

    (_devices, status), = sat.ws_client.sent
    assert status["mode"] == "periodic" and status["running"] is True
