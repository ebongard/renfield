"""MCP self-detection Phase 3.

Three things, each pinned against the REAL ``MCPManager`` where it matters —
every earlier monitor test faked ``get_status`` with dicts, which is exactly how the
alert-path defects below stayed invisible:

1. **The 0-tools gap.** A server exposing zero tools reads ``degraded/no_tools``,
   yet the alert could go missing: a hand-off that failed was never retried (the
   ledger is stamped before delivery and the result was discarded), a server that
   was already degraded for another reason never alerted on the new one (the key
   carried no reason), and the self-heal "recovered" a tool-less server because
   ``tools/list`` answers fine with an empty list. Plus a grace so a server whose
   tools register a moment after connect does not alert on every boot.
2. **Upstream rate-limit as its own signal.** Kept apart from the timeout window and
   from the probe verdict; windowed so a burst can never pin a server red; only ever
   read from ERROR results.
3. **Retry-After** honoured per tool, without a transparent retry.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.mcp_health_monitor as monitor
from services import ops_alert
from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
    _classify_rate_limit,
)
from utils.config import settings

pytestmark = [pytest.mark.unit]


# --- helpers ---------------------------------------------------------------

class _Session:
    """Minimal MCP session: a tool list and a scripted call_tool."""

    def __init__(self, tools=(), results=None):
        self._tools = list(tools)
        self.call_tool = AsyncMock(side_effect=list(results or []))

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)


def _result(text: str, is_error: bool = False):
    return SimpleNamespace(isError=is_error, content=[SimpleNamespace(text=text, type="text")])


def _manager(states: dict) -> MCPManager:
    mgr = MCPManager.__new__(MCPManager)  # bypass heavy __init__
    mgr._servers = states
    mgr._tool_index = {}
    mgr._tool_overrides = {}
    mgr._health_bg_tasks = set()
    return mgr


def _server(name="optional", tools=0, session=None, connected=True):
    infos = [MCPToolInfo(name, f"t{i}", f"mcp.{name}.t{i}", "T") for i in range(tools)]
    return MCPServerState(
        config=MCPServerConfig(name=name, transport=MCPTransportType.STREAMABLE_HTTP),
        connected=connected,
        tools=list(infos),
        all_discovered_tools=list(infos),
        session=session,
    )


def _app(mgr):
    return SimpleNamespace(state=SimpleNamespace(mcp_manager=mgr))


@pytest.fixture(autouse=True)
def _monitor_on(monkeypatch):
    ops_alert.reset_ledger()
    monkeypatch.setattr(settings, "mcp_health_monitor_enabled", True)
    monkeypatch.setattr(settings, "mcp_health_self_heal_enabled", True)
    monkeypatch.setattr(settings, "mcp_health_probe_enabled", True)
    monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", False)
    monkeypatch.setattr(settings, "mcp_rate_limit_backoff_enabled", False)
    monkeypatch.setattr("utils.metrics.record_mcp_health_tick", lambda n: None)
    yield
    ops_alert.reset_ledger()


def _capture_delivery(monkeypatch, outcomes=None):
    """Patch the REAL delivery seam (ops_alert.notify_admin), so the monitor's own
    handling of the returned bool is exercised — not a fake _notify."""
    calls: list[dict] = []
    scripted = iter(outcomes or [])

    async def _notify_admin(**kw):
        calls.append(kw)
        return next(scripted, True)

    monkeypatch.setattr(monitor.ops_alert, "notify_admin", _notify_admin)
    return calls


# ===========================================================================
# 1. The 0-tools gap
# ===========================================================================

@pytest.mark.asyncio
class TestZeroToolsAlerting:
    async def test_zero_tool_server_alerts_through_the_real_manager(self, monkeypatch):
        calls = _capture_delivery(monkeypatch)
        state = _server(session=_Session())
        state.note_discovered_tools(now=0.0)  # tool-less for a long time
        mgr = _manager({"optional": state})

        await monitor.monitor_tick(_app(mgr))

        assert len(calls) == 1
        assert calls[0]["data"]["server"] == "optional"
        # A reason a human can act on, not the raw machine code.
        assert "keine Werkzeuge" in calls[0]["message"]
        assert "no_tools" not in calls[0]["message"]

    async def test_zero_tool_server_is_not_self_healed(self, monkeypatch):
        """tools/list answers fine with an empty list, so a probe used to count a
        tool-less server as 'recovered on reconnect' and the alert then claimed a
        heal had been tried. A reconnect cannot create tools."""
        calls = _capture_delivery(monkeypatch)
        state = _server(session=_Session())
        state.note_discovered_tools(now=0.0)
        mgr = _manager({"optional": state})
        mgr.probe_server = AsyncMock(return_value={"ok": True})

        await monitor.monitor_tick(_app(mgr))

        mgr.probe_server.assert_not_awaited()
        assert calls[0]["data"]["self_heal_attempted"] is False

    def _pipeline(self, monkeypatch, *, raises, persisted):
        """Drive the REAL ops_alert.notify_admin with a stubbed pipeline. Returns the
        list of process_webhook calls (= notification rows attempted)."""
        import sys
        import types

        rows: list[dict] = []

        class _Svc:
            def __init__(self, db):
                pass

            async def process_webhook(self, **kw):
                rows.append(kw)
                if raises is not None:
                    raise raises

        class _Session:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False

        ns_mod = types.ModuleType("services.notification_service")
        ns_mod.NotificationService = _Svc
        monkeypatch.setitem(sys.modules, "services.notification_service", ns_mod)
        monkeypatch.setattr("services.database.AsyncSessionLocal", lambda: _Session())
        monkeypatch.setattr(settings, "proactive_enabled", True)

        async def _admin(db):
            return 1

        async def _persisted(**_kw):
            return persisted

        monkeypatch.setattr(ops_alert, "resolve_admin_user_id", _admin)
        monkeypatch.setattr(ops_alert, "_persisted_since", _persisted)
        return rows

    async def test_failure_after_persist_stores_no_second_row(self, monkeypatch):
        """Review finding: delivery raised AFTER the row was committed, the monitor
        cleared the key, and every 120 s tick stored a new row + push."""
        clock = [1000.0]
        monkeypatch.setattr(ops_alert, "_now", lambda: clock[0])
        rows = self._pipeline(monkeypatch, raises=RuntimeError("push failed"), persisted=True)
        state = _server(session=_Session())
        state.note_discovered_tools(now=0.0)
        mgr = _manager({"optional": state})

        for _ in range(5):
            await monitor.monitor_tick(_app(mgr))
            clock[0] += 120.0

        assert len(rows) == 1

    async def test_unpersisted_alert_is_retried_only_after_the_backoff(self, monkeypatch):
        """Nothing reached the admin: not silent for 6 h, but not every tick either."""
        clock = [1000.0]
        monkeypatch.setattr(ops_alert, "_now", lambda: clock[0])
        monkeypatch.setattr(settings, "mcp_health_alert_retry_seconds", 600.0)
        rows = self._pipeline(monkeypatch, raises=RuntimeError("db down"), persisted=False)
        state = _server(session=_Session())
        state.note_discovered_tools(now=0.0)
        mgr = _manager({"optional": state})

        await monitor.monitor_tick(_app(mgr))          # t=0: attempt, not persisted
        clock[0] += 120.0
        await monitor.monitor_tick(_app(mgr))          # t=120: inside the backoff
        clock[0] += 360.0
        await monitor.monitor_tick(_app(mgr))          # t=480: still inside
        assert len(rows) == 1

        clock[0] += 121.0
        await monitor.monitor_tick(_app(mgr))          # t=601: retry
        assert len(rows) == 2

    async def test_delivered_alert_is_not_repeated_within_the_ttl(self, monkeypatch):
        calls = _capture_delivery(monkeypatch)
        state = _server(session=_Session())
        state.note_discovered_tools(now=0.0)
        mgr = _manager({"optional": state})

        for _ in range(3):
            await monitor.monitor_tick(_app(mgr))

        assert len(calls) == 1

    class _FlapManager:
        """A manager whose single server's verdict the test sets per tick."""

        def __init__(self):
            self.server = None

        def get_status(self):
            return {"servers": [self.server] if self.server else []}

    async def test_reason_flapping_within_the_ttl_alerts_once(self, monkeypatch):
        """Review finding: with the reason in the key, rate_limited <-> calls_failing
        re-alerted on every switch and bypassed the 6 h limit."""
        clock = [1000.0]
        monkeypatch.setattr(ops_alert, "_now", lambda: clock[0])
        calls = _capture_delivery(monkeypatch)
        mgr = self._FlapManager()
        a = {"name": "tracking", "health": "degraded", "impaired_code": "rate_limited"}
        b = {"name": "tracking", "health": "degraded", "impaired_code": "calls_failing"}

        for verdict in (a, b, a, b, a):
            mgr.server = verdict
            await monitor.monitor_tick(_app(mgr))
            clock[0] += 60.0

        assert len(calls) == 1
        assert ops_alert.alerted_keys("planea:") == ["planea:tracking:degraded"]

    async def test_after_the_ttl_the_due_alert_names_the_current_reason(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(ops_alert, "_now", lambda: clock[0])
        monkeypatch.setattr(settings, "mcp_health_realert_seconds", 21600.0)
        calls = _capture_delivery(monkeypatch)
        mgr = self._FlapManager()
        mgr.server = {"name": "optional", "health": "degraded", "impaired_code": "probe_failed"}
        await monitor.monitor_tick(_app(mgr))

        mgr.server = {"name": "optional", "health": "degraded", "impaired_code": "no_tools"}
        clock[0] += 300.0
        await monitor.monitor_tick(_app(mgr))
        assert len(calls) == 1

        clock[0] += 21600.0
        await monitor.monitor_tick(_app(mgr))
        assert len(calls) == 2
        assert "keine Werkzeuge" in calls[1]["message"]

    async def test_real_recovery_clears_the_keys_and_rearms(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(ops_alert, "_now", lambda: clock[0])
        calls = _capture_delivery(monkeypatch)
        mgr = self._FlapManager()
        broken = {"name": "paperless", "health": "down", "last_error": "gone"}
        mgr.server = broken
        await monitor.monitor_tick(_app(mgr))

        mgr.server = {"name": "paperless", "health": "healthy"}
        clock[0] += 120.0
        await monitor.monitor_tick(_app(mgr))
        assert ops_alert.alerted_keys("planea:") == []

        mgr.server = broken
        clock[0] += 120.0
        await monitor.monitor_tick(_app(mgr))
        assert len(calls) == 2  # a re-failure after a real recovery alerts at once

    async def test_a_still_broken_server_keeps_its_keys(self, monkeypatch):
        """The sweep must not forget an alert just because the server's health or
        reason moved — only full recovery does."""
        calls = _capture_delivery(monkeypatch)
        mgr = self._FlapManager()
        mgr.server = {"name": "paperless", "health": "down", "last_error": "gone"}
        await monitor.monitor_tick(_app(mgr))
        mgr.server = {"name": "paperless", "health": "degraded", "impaired_code": "probe_failed"}
        await monitor.monitor_tick(_app(mgr))
        mgr.server = {"name": "paperless", "health": "down", "last_error": "gone"}
        await monitor.monitor_tick(_app(mgr))

        # down alerted once, degraded once — the return to down is inside its TTL.
        assert len(calls) == 2

    async def test_boot_grace_holds_the_alert_but_not_the_verdict(self, monkeypatch):
        """No alert storm on startup: a server whose tools have not registered yet
        reads degraded immediately (kiosk honest) but does not alert until the grace
        has passed."""
        monkeypatch.setattr(settings, "mcp_health_no_tools_grace_seconds", 300.0)
        calls = _capture_delivery(monkeypatch)
        state = _server(session=_Session())
        state.note_discovered_tools()  # just now
        mgr = _manager({"optional": state})

        info = mgr.get_status()["servers"][0]
        assert info["health"] == "degraded" and info["impaired_code"] == "no_tools"
        await monitor.monitor_tick(_app(mgr))
        assert calls == []

        state.no_tools_since -= 301.0  # the grace has passed, still no tools
        await monitor.monitor_tick(_app(mgr))
        assert len(calls) == 1

    async def test_tools_appearing_within_the_grace_never_alert(self, monkeypatch):
        calls = _capture_delivery(monkeypatch)
        state = _server(session=_Session())
        state.note_discovered_tools()
        mgr = _manager({"optional": state})
        await monitor.monitor_tick(_app(mgr))

        state.all_discovered_tools = [MCPToolInfo("optional", "t", "mcp.optional.t", "T")]
        state.note_discovered_tools()
        await monitor.monitor_tick(_app(mgr))

        assert calls == []
        assert state.no_tools_since is None

    async def test_filtered_to_zero_active_tools_does_not_alert(self, monkeypatch):
        """The existing false-alarm guard must survive: tools discovered but all
        filtered out of the prompt is a healthy server."""
        calls = _capture_delivery(monkeypatch)
        state = _server(tools=2, session=_Session())
        state.tools = []
        mgr = _manager({"optional": state})
        await monitor.monitor_tick(_app(mgr))
        assert calls == []


class TestNoToolsClock:
    def test_clock_starts_on_an_empty_discovery_and_survives_a_second_one(self):
        state = _server()
        state.note_discovered_tools(now=10.0)
        state.note_discovered_tools(now=50.0)  # a reconnect that still finds nothing
        assert state.no_tools_since == 10.0
        assert state.no_tools_age(now=70.0) == 60.0

    def test_clock_clears_when_tools_appear(self):
        state = _server()
        state.note_discovered_tools(now=10.0)
        state.all_discovered_tools = [MCPToolInfo("x", "t", "mcp.x.t", "T")]
        state.note_discovered_tools(now=20.0)
        assert state.no_tools_since is None and state.no_tools_age() is None

    def test_get_status_exposes_the_age_only_for_no_tools(self):
        empty = _server(name="empty")
        empty.note_discovered_tools()
        full = _server(name="full", tools=1)
        full.note_discovered_tools()
        servers = {s["name"]: s for s in _manager({"empty": empty, "full": full}).get_status()["servers"]}
        assert "no_tools_for_seconds" in servers["empty"]
        assert "no_tools_for_seconds" not in servers["full"]

    @pytest.mark.asyncio
    async def test_refresh_tools_starts_the_clock(self):
        state = _server(tools=1, session=_Session(tools=[]))
        mgr = _manager({"optional": state})
        await mgr.refresh_tools()
        assert state.all_discovered_tools == []
        assert state.no_tools_since is not None


# ===========================================================================
# 2. Upstream rate-limit signal
# ===========================================================================

class TestClassifier:
    @pytest.mark.parametrize(
        "text",
        [
            "Client error '429 Too Many Requests' for url 'https://api.example/track'",
            '{"success": false, "error": "rate limit exceeded"}',
            '{"status": 429, "error": "slow down"}',
            "HTTP 429 from upstream",
            "Rate-Limit erreicht",
        ],
        ids=["httpx", "envelope-prose", "json-status", "http-429", "german"],
    )
    def test_recognises_real_throttle_shapes(self, text):
        assert _classify_rate_limit(text)[0] is True

    @pytest.mark.parametrize(
        "text",
        [
            "Dokument 429 nicht gefunden",
            "invoice 429 is already paid",
            '{"error": "device offline"}',
            "HTTP 500 Internal Server Error",
            "",
        ],
        ids=["doc-number", "invoice-number", "device-off", "http-500", "empty"],
    )
    def test_does_not_mistake_other_errors_for_a_throttle(self, text):
        assert _classify_rate_limit(text) == (False, None)

    def test_reads_retry_after_from_text_json_and_header(self):
        assert _classify_rate_limit("429 Too Many Requests; Retry-After: 30") == (True, 30.0)
        assert _classify_rate_limit('{"status": 429, "retry_after": 12}') == (True, 12.0)
        assert _classify_rate_limit("boom", status_code=429, retry_after_header="7") == (True, 7.0)

    def test_http_date_retry_after_is_ignored_not_guessed(self):
        limited, retry = _classify_rate_limit(
            "x", status_code=429, retry_after_header="Wed, 21 Oct 2026 07:28:00 GMT"
        )
        assert limited is True and retry is None


class TestRateLimitWindow:
    def test_below_min_events_is_not_a_verdict(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_min_events", 3)
        state = _server()
        for t in (0.0, 1.0):
            state.record_rate_limit("t", None, now=t)
        assert state.rate_limit_failing(now=2.0) is False

    def test_enough_events_in_the_window_is_a_verdict(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_min_events", 3)
        state = _server()
        for t in (0.0, 1.0, 2.0):
            state.record_rate_limit("t", None, now=t)
        assert state.rate_limit_failing(now=3.0) is True

    def test_events_age_out_so_a_burst_never_pins_red(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_min_events", 3)
        monkeypatch.setattr(settings, "mcp_health_rate_limit_window_seconds", 900.0)
        state = _server()
        for t in (0.0, 1.0, 2.0):
            state.record_rate_limit("t", None, now=t)
        assert state.rate_limit_failing(now=10.0) is True
        assert state.rate_limit_failing(now=903.0) is False  # nothing after the burst

    def test_retry_after_is_capped(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_rate_limit_max_backoff_seconds", 60.0)
        state = _server()
        state.record_rate_limit("t", 999_999.0, now=0.0)
        assert state.rate_limit_retry_in("t", now=0.0) == 60.0
        assert state.rate_limit_retry_in("t", now=61.0) is None


class TestHealthFolding:
    def _throttled(self, monkeypatch, n=5):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_min_events", n)
        state = _server(tools=1)
        for _ in range(n):
            state.record_rate_limit("t", None)
        return state

    def test_folds_rate_limited_when_the_signal_is_on(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        state = self._throttled(monkeypatch)
        mgr = _manager({"optional": state})
        info = mgr.get_status()["servers"][0]
        assert (info["health"], info["impaired_code"]) == ("degraded", "rate_limited")
        assert info["rate_limit_events"] == 5

    def test_flag_off_is_byte_identical_healthy(self, monkeypatch):
        state = self._throttled(monkeypatch)
        mgr = _manager({"optional": state})
        assert mgr._server_health("optional", state) == ("healthy", None)

    def test_not_mixed_with_the_timeout_window(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        state = self._throttled(monkeypatch)
        assert list(state.recent_outcomes) == []
        assert state.calls_failing() is False
        assert state.probe_consecutive_failures == 0

    def test_a_failed_probe_outranks_a_throttle(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        monkeypatch.setattr(settings, "mcp_health_probe_fail_threshold", 1)
        state = self._throttled(monkeypatch)
        state.record_probe_outcome(False, "HTTP 500")
        assert _manager({"o": state})._server_health("o", state) == ("degraded", "probe_failed")


@pytest.mark.asyncio
class TestExecuteToolRecording:
    def _mgr(self, *results):
        session = _Session(results=results)
        state = _server(tools=1, session=session)
        mgr = _manager({"optional": state})
        mgr._tool_index["mcp.optional.t0"] = state.tools[0]
        return mgr, state, session

    async def test_throttled_error_result_is_recorded(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        mgr, state, _ = self._mgr(_result("Client error '429 Too Many Requests'", is_error=True))
        res = await mgr.execute_tool("mcp.optional.t0", {})
        assert res["success"] is False
        assert state.rate_limit_count() == 1
        assert list(state.recent_outcomes) == []  # not a timeout, not a success

    async def test_successful_result_mentioning_rate_limits_is_not_a_throttle(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        mgr, state, _ = self._mgr(_result("Our API docs: too many requests return 429."))
        res = await mgr.execute_tool("mcp.optional.t0", {})
        assert res["success"] is True
        assert state.rate_limit_count() == 0

    async def test_other_app_errors_are_not_throttles(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        mgr, state, _ = self._mgr(_result('{"error": "device offline"}'))
        await mgr.execute_tool("mcp.optional.t0", {})
        assert state.rate_limit_count() == 0

    async def test_flags_off_records_nothing(self):
        mgr, state, _ = self._mgr(_result("429 Too Many Requests", is_error=True))
        await mgr.execute_tool("mcp.optional.t0", {})
        assert state.rate_limit_count() == 0 and state.rate_limited_until == {}

    async def test_app_exception_with_a_429_response_is_recorded(self, monkeypatch):
        import httpx

        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        request = httpx.Request("POST", "https://upstream.example/")
        response = httpx.Response(429, headers={"Retry-After": "20"}, request=request)
        err = httpx.HTTPStatusError("throttled", request=request, response=response)
        mgr, state, _ = self._mgr(err)
        await mgr.execute_tool("mcp.optional.t0", {})
        assert state.rate_limit_count() == 1
        assert state.rate_limited_until["t0"] > 0


@pytest.mark.asyncio
class TestRetryAfterGate:
    def _mgr(self, *results):
        session = _Session(results=results)
        state = _server(tools=2, session=session)
        mgr = _manager({"optional": state})
        for info in state.tools:
            mgr._tool_index[info.namespaced_name] = info
        return mgr, state, session

    async def test_honours_retry_after_without_calling_the_upstream(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_rate_limit_backoff_enabled", True)
        mgr, state, session = self._mgr(
            _result("429 Too Many Requests; Retry-After: 60", is_error=True)
        )
        await mgr.execute_tool("mcp.optional.t0", {})
        res = await mgr.execute_tool("mcp.optional.t0", {})

        assert session.call_tool.await_count == 1  # the second call never left
        assert res["success"] is False and "Rate-Limit" in res["message"]
        # Our own refusal is not new evidence — else the gate keeps itself red.
        assert state.rate_limit_count() == 1

    async def test_gate_is_per_tool(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_rate_limit_backoff_enabled", True)
        mgr, _state, session = self._mgr(
            _result("429 Too Many Requests; Retry-After: 60", is_error=True),
            _result("ok"),
        )
        await mgr.execute_tool("mcp.optional.t0", {})
        res = await mgr.execute_tool("mcp.optional.t1", {})
        assert res["success"] is True and session.call_tool.await_count == 2

    async def test_gate_off_still_calls_through(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        mgr, _state, session = self._mgr(
            _result("429 Too Many Requests; Retry-After: 60", is_error=True),
            _result("ok"),
        )
        await mgr.execute_tool("mcp.optional.t0", {})
        res = await mgr.execute_tool("mcp.optional.t0", {})
        assert res["success"] is True and session.call_tool.await_count == 2

    async def test_a_clean_result_lifts_the_gate(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_rate_limit_backoff_enabled", True)
        mgr, state, _ = self._mgr(_result("ok"))
        state.rate_limited_until["t0"] = 0.0  # expired horizon
        await mgr.execute_tool("mcp.optional.t0", {})
        assert "t0" not in state.rate_limited_until


# ===========================================================================
# 2b. Probe + monitor integration
# ===========================================================================

@pytest.mark.asyncio
class TestProbeAndMonitorIntegration:
    def _probe_state(self):
        state = _server(name="paperless", tools=1)
        state.config.health_probe = {
            "tool": "t0", "args": {}, "interval": 600, "timeout": 5,
            "expect": {"min_items": 0, "path": None},
        }
        return state

    async def test_throttled_probe_is_inconclusive_when_the_signal_owns_it(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        state = self._probe_state()
        mgr = _manager({"paperless": state})
        mgr.execute_tool = AsyncMock(
            return_value={"success": False, "message": "429 Too Many Requests"}
        )
        res = await mgr.run_health_probe("paperless")
        assert res["skipped"] is True
        assert state.probe_consecutive_failures == 0
        # ...but the cadence advances, so a throttled upstream is not re-probed every tick.
        assert state.last_probe_at > 0

    async def test_our_own_retry_after_refusal_never_fails_a_probe(self, monkeypatch):
        """Backoff gate on, signal off: the probe's 'failure' is our own refusal. Two of
        those must not add up to probe_failed — that would be self-inflicted red."""
        monkeypatch.setattr(settings, "mcp_rate_limit_backoff_enabled", True)
        monkeypatch.setattr(settings, "mcp_health_probe_fail_threshold", 1)
        state = self._probe_state()
        state.config.transport = MCPTransportType.STREAMABLE_HTTP
        state.connected = True
        state.session = _Session(tools=[object()])
        state.record_rate_limit("t0", 60.0)  # an honoured Retry-After is pending
        mgr = _manager({"paperless": state})
        mgr._tool_index["mcp.paperless.t0"] = state.tools[0]

        for _ in range(2):
            res = await mgr.run_health_probe("paperless")
            assert res["skipped"] is True

        state.session.call_tool.assert_not_awaited()  # the gate held
        assert state.probe_consecutive_failures == 0
        assert mgr._server_health("paperless", state) == ("healthy", None)

    async def test_throttled_probe_still_fails_when_the_signal_is_off(self):
        state = self._probe_state()
        mgr = _manager({"paperless": state})
        mgr.execute_tool = AsyncMock(
            return_value={"success": False, "message": "429 Too Many Requests"}
        )
        res = await mgr.run_health_probe("paperless")
        assert res["ok"] is False and state.probe_consecutive_failures == 1

    async def test_rate_limited_server_alerts_once_and_is_not_self_healed(self, monkeypatch):
        monkeypatch.setattr(settings, "mcp_health_rate_limit_signal_enabled", True)
        monkeypatch.setattr(settings, "mcp_health_rate_limit_min_events", 2)
        calls = _capture_delivery(monkeypatch)
        state = _server(name="tracking", tools=1, session=_Session(tools=[object()]))
        state.record_rate_limit("t0", None)
        state.record_rate_limit("t0", None)
        mgr = _manager({"tracking": state})
        mgr.probe_server = AsyncMock(return_value={"ok": True})

        await monitor.monitor_tick(_app(mgr))
        await monitor.monitor_tick(_app(mgr))

        mgr.probe_server.assert_not_awaited()
        assert len(calls) == 1
        assert "drosselt" in calls[0]["message"]
