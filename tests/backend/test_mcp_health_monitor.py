"""Tests for the MCP health self-detection monitor (Phase 1)."""
import time
from unittest.mock import MagicMock

import pytest

import services.mcp_health_monitor as m

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    m._reports.clear()
    m._alerted.clear()
    monkeypatch.setattr(m.settings, "mcp_health_monitor_enabled", True)
    monkeypatch.setattr(m.settings, "mcp_health_realert_seconds", 21600.0)
    yield
    m._reports.clear()
    m._alerted.clear()


def _capture_alerts(monkeypatch):
    alerts: list[str] = []

    async def _fake_notify(title, message, dedup_key, data):
        alerts.append(dedup_key)

    monkeypatch.setattr(m, "_notify", _fake_notify)
    return alerts


async def test_ingest_report_records_and_alerts(monkeypatch):
    alerts = _capture_alerts(monkeypatch)
    await m.ingest_report(
        {"source": "renfield-mcp-filesystem", "event": "failure",
         "reason": "retry_exhausted", "root": "xidra-share"}
    )
    assert len(m._reports) == 1
    assert len(alerts) == 1


async def test_ingest_report_dedups_within_ttl(monkeypatch):
    alerts = _capture_alerts(monkeypatch)
    p = {"source": "s", "event": "failure", "reason": "x", "root": "r"}
    await m.ingest_report(p)
    await m.ingest_report(p)
    assert len(alerts) == 1  # same issue within TTL → alerted once


async def test_ingest_report_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(m.settings, "mcp_health_monitor_enabled", False)
    alerts = _capture_alerts(monkeypatch)
    await m.ingest_report({"source": "s", "event": "failure", "reason": "x"})
    assert alerts == []           # no alert when disabled
    assert len(m._reports) == 1   # but still recorded (for surfacing)


async def test_monitor_tick_alerts_degraded_then_clears_on_recovery(monkeypatch):
    alerts = _capture_alerts(monkeypatch)
    app = MagicMock()
    mgr = MagicMock()
    app.state.mcp_manager = mgr
    mgr.get_status = MagicMock(
        return_value={"servers": [{"name": "paperless", "health": "down", "last_error": "boom"}]}
    )
    await m.monitor_tick(app)
    assert any("paperless" in a for a in alerts)
    assert any(k.startswith("planea:paperless") for k in m._alerted)

    # recovery: server healthy again → the ledger key is cleared so a re-failure re-alerts
    mgr.get_status = MagicMock(
        return_value={"servers": [{"name": "paperless", "health": "healthy"}]}
    )
    await m.monitor_tick(app)
    assert not any(k.startswith("planea:paperless") for k in m._alerted)


async def test_monitor_tick_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(m.settings, "mcp_health_monitor_enabled", False)
    alerts = _capture_alerts(monkeypatch)
    app = MagicMock()
    app.state.mcp_manager = MagicMock()
    app.state.mcp_manager.get_status = MagicMock(
        return_value={"servers": [{"name": "x", "health": "down"}]}
    )
    await m.monitor_tick(app)
    assert alerts == []


def test_plane_b_reports_freshness_filter():
    m._reports["stale"] = {"source": "stale", "event": "failure", "at": time.time() - 2000}
    m._reports["fresh"] = {"source": "fresh", "event": "failure", "at": time.time()}
    fresh = m.plane_b_reports()
    assert [r["source"] for r in fresh] == ["fresh"]
    assert len(m.plane_b_reports(fresh_only=False)) == 2


# --- Phase 2: probe-driven self-heal in monitor_tick -------------------------

def _capture_notes(monkeypatch):
    """Capture full _notify calls (dedup_key + data) for self-heal assertions."""
    notes: list[dict] = []

    async def _fake_notify(title, message, dedup_key, data):
        notes.append({"dedup_key": dedup_key, "message": message, "data": data})

    monkeypatch.setattr(m, "_notify", _fake_notify)
    return notes


def _app_with(mgr):
    app = MagicMock()
    app.state.mcp_manager = mgr
    return app


async def test_self_heal_recovers_down_server_no_alert(monkeypatch):
    notes = _capture_notes(monkeypatch)
    mgr = MagicMock()
    from unittest.mock import AsyncMock
    mgr.get_status = MagicMock(side_effect=[
        {"servers": [{"name": "paperless", "health": "down", "last_error": "x"}]},
        {"servers": [{"name": "paperless", "health": "healthy"}]},  # post-heal
    ])
    mgr.probe_server = AsyncMock(return_value={"ok": True})
    await m.monitor_tick(_app_with(mgr))
    mgr.probe_server.assert_awaited_once_with("paperless")
    assert notes == []  # self-healed → no alert
    assert not any(k.startswith("planea:paperless") for k in m._alerted)


async def test_self_heal_fails_then_alerts_with_marker(monkeypatch):
    notes = _capture_notes(monkeypatch)
    mgr = MagicMock()
    from unittest.mock import AsyncMock
    mgr.get_status = MagicMock(side_effect=[
        {"servers": [{"name": "paperless", "health": "down", "last_error": "x"}]},
        {"servers": [{"name": "paperless", "health": "down", "last_error": "x"}]},  # still down
    ])
    mgr.probe_server = AsyncMock(return_value={"ok": False})
    await m.monitor_tick(_app_with(mgr))
    assert len(notes) == 1
    assert notes[0]["data"]["self_heal_attempted"] is True
    assert "Selbstheilung versucht" in notes[0]["message"]


async def test_self_heal_disabled_alerts_without_probe(monkeypatch):
    monkeypatch.setattr(m.settings, "mcp_health_self_heal_enabled", False)
    notes = _capture_notes(monkeypatch)
    mgr = MagicMock()
    from unittest.mock import AsyncMock
    mgr.get_status = MagicMock(return_value={"servers": [{"name": "x", "health": "down"}]})
    mgr.probe_server = AsyncMock(return_value={"ok": True})
    await m.monitor_tick(_app_with(mgr))
    mgr.probe_server.assert_not_awaited()
    assert len(notes) == 1
    assert notes[0]["data"]["self_heal_attempted"] is False


async def test_self_heal_hang_guard_bounds_a_wedged_probe(monkeypatch):
    """#1107: a probe whose reconnect wedges must NOT freeze the tick — the
    hang-guard cancels it, the tick completes, the alert still fires and is
    honest about the attempted heal."""
    import asyncio

    monkeypatch.setattr(m.settings, "mcp_health_self_heal_probe_timeout", 0.05)
    monkeypatch.setattr(m.settings, "mcp_connect_timeout", 0.001)
    monkeypatch.setattr(m, "_PROBE_GUARD_OVERHEAD_S", 0.001)
    notes = _capture_notes(monkeypatch)
    mgr = MagicMock()

    async def _wedged_probe(name):
        await asyncio.sleep(3600)

    mgr.probe_server = _wedged_probe
    down = {"servers": [{"name": "twin", "health": "down", "last_error": "no session"}]}
    mgr.get_status = MagicMock(return_value=down)

    await asyncio.wait_for(m.monitor_tick(_app_with(mgr)), timeout=5.0)

    assert len(notes) == 1
    assert notes[0]["data"]["self_heal_attempted"] is True
    assert "Selbstheilung versucht" in notes[0]["message"]


async def test_self_heal_hang_guard_spares_other_servers(monkeypatch):
    """One wedged server must not starve the heal attempts of the others."""
    import asyncio

    monkeypatch.setattr(m.settings, "mcp_health_self_heal_probe_timeout", 0.05)
    monkeypatch.setattr(m.settings, "mcp_connect_timeout", 0.001)
    monkeypatch.setattr(m, "_PROBE_GUARD_OVERHEAD_S", 0.001)
    _capture_notes(monkeypatch)
    mgr = MagicMock()
    probed: list[str] = []

    async def _probe(name):
        probed.append(name)
        if name == "wedged":
            await asyncio.sleep(3600)
        return {"ok": False}

    mgr.probe_server = _probe
    mgr.get_status = MagicMock(return_value={"servers": [
        {"name": "wedged", "health": "down"},
        {"name": "other", "health": "down"},
    ]})

    await asyncio.wait_for(m.monitor_tick(_app_with(mgr)), timeout=5.0)
    assert probed == ["wedged", "other"]


async def test_monitor_tick_records_heartbeat(monkeypatch):
    """#1107: every COMPLETED tick must emit the heartbeat metric so a stuck
    monitor is distinguishable from an all-healthy one."""
    ticks: list[int] = []
    monkeypatch.setattr(
        "utils.metrics.record_mcp_health_tick", lambda n: ticks.append(n)
    )
    _capture_notes(monkeypatch)
    mgr = MagicMock()
    mgr.get_status = MagicMock(return_value={"servers": [{"name": "ok", "health": "healthy"}]})
    await m.monitor_tick(_app_with(mgr))
    assert ticks == [0]


async def test_heartbeat_ticks_even_when_get_status_fails(monkeypatch):
    """The liveness counter must tick for a live-but-erroring loop (else a
    persistently failing get_status flatlines the counter and mimics the very
    hang the metric exists to expose); the gauge verdict stays None."""
    ticks: list = []
    monkeypatch.setattr(
        "utils.metrics.record_mcp_health_tick", lambda n: ticks.append(n)
    )
    _capture_notes(monkeypatch)
    mgr = MagicMock()
    mgr.get_status = MagicMock(side_effect=RuntimeError("broken status shape"))
    await m.monitor_tick(_app_with(mgr))
    assert ticks == [None]


async def test_plugin_failed_skips_probe_still_alerts(monkeypatch):
    # A reconnect provably can't reload a failed startup plugin → we DON'T probe it
    # (no wasted RPC, no false "Selbstheilung versucht"), but it STILL alerts.
    notes = _capture_notes(monkeypatch)
    mgr = MagicMock()
    from unittest.mock import AsyncMock
    degraded = {"servers": [{"name": "twin", "health": "degraded", "impaired_code": "plugin_failed"}]}
    mgr.get_status = MagicMock(return_value=degraded)
    mgr.probe_server = AsyncMock(return_value={"ok": True})
    await m.monitor_tick(_app_with(mgr))
    mgr.probe_server.assert_not_awaited()
    assert len(notes) == 1
    assert "twin" in notes[0]["dedup_key"]
    assert notes[0]["data"]["self_heal_attempted"] is False
