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
