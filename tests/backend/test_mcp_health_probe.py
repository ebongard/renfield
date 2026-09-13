"""Functional health probes for MCP servers (A1).

The failure this encodes, measured 2026-09-12: `/api/mcp/status` reported 13 of 13
servers `healthy` while Paperless had been answering every authenticated call with
HTTP 500 for three days and n8n was not reachable from the cluster at all.

Phase 2 already had a functional signal, but it counts only TIMEOUTS — an
app-level error says nothing about the server's health, and reversing that would
flag a healthy server whose *target* failed. A probe escapes the bind: WE choose a
call that must succeed, so its failure IS a health signal. These tests pin that
separation, the hysteresis, the deliberate skips, and the tick ordering.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# asyncio is marked per async class — pyproject runs asyncio_mode=auto, and a
# module-wide marker would decorate the synchronous tests here too.
pytestmark = [pytest.mark.unit]


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [None, {}, {"enabled": False, "tool": "x"}, {"args": {}}, {"tool": ""}],
    ids=["none", "empty", "disabled", "no-tool", "blank-tool"],
)
def test_parse_rejects_unusable_stanzas(raw):
    """A malformed or disabled stanza means 'not probed', never an exception — one
    typo must not stop the whole MCP fleet from loading."""
    from services.mcp_client import _parse_health_probe

    assert _parse_health_probe(raw) is None


def test_parse_applies_defaults_and_overrides():
    from services.mcp_client import _parse_health_probe
    from utils.config import settings

    parsed = _parse_health_probe({"tool": "list_tags"})
    assert parsed["tool"] == "list_tags"
    assert parsed["interval"] == settings.mcp_health_probe_interval
    assert parsed["timeout"] == settings.mcp_health_probe_timeout
    assert parsed["expect"] == {"min_items": 0, "path": None}

    parsed = _parse_health_probe(
        {"tool": "search", "interval": 120, "timeout": 5, "expect": {"min_items": 3, "path": "results"}}
    )
    assert (parsed["interval"], parsed["timeout"]) == (120, 5.0)
    assert parsed["expect"] == {"min_items": 3, "path": "results"}


def test_parse_floors_a_silly_interval():
    """A 1-second probe against a real upstream is a load generator, not a check."""
    from services.mcp_client import _parse_health_probe

    assert _parse_health_probe({"tool": "t", "interval": 1})["interval"] >= 30


# --- expectation evaluation ------------------------------------------------

class TestExpectation:
    """The evaluation runs against the JOINED MESSAGE TEXT, which is where the
    payload actually is. `execute_tool` returns `data` as the list of raw MCP
    content parts (`[{"type":..., "text":...}]`) — an earlier version counted THOSE,
    which made every `min_items` above 1 fail permanently against a healthy server
    while the unit tests fed a shape production never produces."""

    def _eval(self, message, expect):
        from services.mcp_client import MCPManager

        return MCPManager._evaluate_probe_expectation(message, expect)

    def test_min_items_zero_accepts_anything_that_came_back(self):
        """An empty list from a fresh archive is legitimate; the error envelope is
        what the default probe is looking for — and it needs no payload at all."""
        assert self._eval("[]", {"min_items": 0, "path": None}) == (True, None)
        assert self._eval(None, {"min_items": 0, "path": None}) == (True, None)
        assert self._eval("irgendein Fließtext", {"min_items": 0, "path": None}) == (True, None)

    def test_counts_a_json_list(self):
        assert self._eval("[1, 2]", {"min_items": 2, "path": None})[0] is True
        ok, reason = self._eval("[1]", {"min_items": 2, "path": None})
        assert ok is False and "nur 1" in reason

    def test_counts_a_named_field(self):
        payload = json.dumps({"results": [1, 2]})
        assert self._eval(payload, {"min_items": 2, "path": "results"})[0] is True
        ok, reason = self._eval(json.dumps({"other": [1]}), {"min_items": 1, "path": "results"})
        assert ok is False and "results" in reason

    def test_realistic_paperless_shape(self):
        """What a real MCP answer looks like — a JSON envelope in the text."""
        payload = json.dumps({"count": 2, "results": [{"id": 1}, {"id": 2}]})
        assert self._eval(payload, {"min_items": 1, "path": "results"}) == (True, None)

    def test_prose_answer_is_inconclusive_not_a_failure(self):
        """A server that answers prose cannot be counted. That is a configuration
        mistake, not evidence of ill health — inventing a failure here would fire a
        critical alert about a healthy server."""
        verdict, reason = self._eval("Alles in Ordnung.", {"min_items": 1, "path": None})
        assert verdict is None and "kein JSON" in reason

    def test_uncountable_payload_is_inconclusive(self):
        verdict, reason = self._eval("42", {"min_items": 1, "path": None})
        assert verdict is None and "nicht zählbar" in reason

    def test_empty_message_is_inconclusive(self):
        verdict, reason = self._eval("", {"min_items": 1, "path": None})
        assert verdict is None and "nicht prüfbar" in reason


# --- state + hysteresis ----------------------------------------------------

def _state(**cfg):
    from services.mcp_client import MCPServerConfig, MCPServerState

    defaults = dict(name="paperless", health_probe={"tool": "t", "interval": 600,
                                                    "timeout": 5, "args": {},
                                                    "expect": {"min_items": 0, "path": None}})
    defaults.update(cfg)
    return MCPServerState(config=MCPServerConfig(**defaults))


class TestHysteresis:
    def test_one_failure_is_not_a_verdict(self, monkeypatch):
        """An upstream hiccup, a rate-limit, a restart window — a single miss must
        not alarm, or the cure is noisier than the silence."""
        from utils.config import settings

        monkeypatch.setattr(settings, "mcp_health_probe_fail_threshold", 2)
        st = _state()
        st.record_probe_outcome(False, "boom")
        assert st.probe_failing() is False
        st.record_probe_outcome(False, "boom")
        assert st.probe_failing() is True

    def test_a_success_clears_the_streak(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "mcp_health_probe_fail_threshold", 2)
        st = _state()
        st.record_probe_outcome(False, "boom")
        st.record_probe_outcome(False, "boom")
        st.record_probe_outcome(True)
        assert st.probe_failing() is False
        assert st.last_probe_detail is None


class TestServerHealthFolding:
    def _manager_with(self, state):
        from services.mcp_client import MCPManager

        mgr = MCPManager.__new__(MCPManager)
        mgr._servers = {state.config.name: state}
        return mgr

    def test_probe_failure_degrades_a_connected_server(self, monkeypatch):
        from services.mcp_client import MCPToolInfo
        from utils.config import settings

        monkeypatch.setattr(settings, "mcp_health_probe_fail_threshold", 1)
        monkeypatch.setattr(settings, "plugin_mcp_bindings", "")
        st = _state()
        st.connected = True
        st.all_discovered_tools = [MCPToolInfo("paperless", "t", "mcp.paperless.t", "")]
        st.record_probe_outcome(False, "HTTP 500")

        mgr = self._manager_with(st)
        assert mgr._server_health("paperless", st) == ("degraded", "probe_failed")

    def test_healthy_probe_leaves_the_server_green(self, monkeypatch):
        from services.mcp_client import MCPToolInfo
        from utils.config import settings

        monkeypatch.setattr(settings, "plugin_mcp_bindings", "")
        st = _state()
        st.connected = True
        st.all_discovered_tools = [MCPToolInfo("paperless", "t", "mcp.paperless.t", "")]
        st.record_probe_outcome(True)

        mgr = self._manager_with(st)
        assert mgr._server_health("paperless", st) == ("healthy", None)

    def test_disconnected_still_reads_down_not_probe_failed(self, monkeypatch):
        """Connectivity is the more basic fact and must keep precedence."""
        from utils.config import settings

        monkeypatch.setattr(settings, "mcp_health_probe_fail_threshold", 1)
        st = _state()
        st.connected = False
        st.record_probe_outcome(False, "HTTP 500")

        mgr = self._manager_with(st)
        assert mgr._server_health("paperless", st) == ("down", None)


# --- due-selection: the deliberate skips -----------------------------------

class TestDueSelection:
    def _manager(self, *states):
        from services.mcp_client import MCPManager

        mgr = MCPManager.__new__(MCPManager)
        mgr._servers = {s.config.name: s for s in states}
        return mgr

    def test_server_without_a_stanza_is_never_probed(self):
        """The blast radius of this feature is the YAML, not the flag."""
        st = _state(name="weather", health_probe=None)
        st.connected = True
        assert self._manager(st).health_probe_due() == []

    def test_disconnected_server_is_not_probed(self):
        st = _state()
        st.connected = False
        assert self._manager(st).health_probe_due() == []

    def test_per_user_auth_server_is_not_probed(self):
        """execute_tool denies a user_id=None call FAIL-CLOSED for these, so a probe
        would report a permanent false failure."""
        st = _state(per_user_auth=True)
        st.connected = True
        assert self._manager(st).health_probe_due() == []

    def test_federation_server_is_not_probed(self):
        from services.mcp_client import MCPTransportType

        st = _state(transport=MCPTransportType.FEDERATION)
        st.connected = True
        assert self._manager(st).health_probe_due() == []

    def test_interval_gates_repeat_probes(self):
        import time

        st = _state()
        st.connected = True
        mgr = self._manager(st)
        assert mgr.health_probe_due() == ["paperless"]  # never probed
        st.record_probe_outcome(True)
        assert mgr.health_probe_due() == []             # too soon
        st.last_probe_at = time.monotonic() - 601
        assert mgr.health_probe_due() == ["paperless"]  # interval elapsed


# --- running the probe -----------------------------------------------------

@pytest.mark.asyncio
class TestRunHealthProbe:
    def _manager(self, state, result):
        from services.mcp_client import MCPManager

        mgr = MCPManager.__new__(MCPManager)
        mgr._servers = {state.config.name: state}
        mgr.execute_tool = AsyncMock(return_value=result)
        return mgr

    async def test_error_envelope_is_a_failure(self):
        """The Paperless case: HTTP 500 arrives as a failed tool result, which the
        Phase-2 timeout window deliberately does NOT count."""
        st = _state()
        mgr = self._manager(st, {"success": False, "message": "HTTP 500"})

        res = await mgr.run_health_probe("paperless")
        assert res["ok"] is False
        assert st.probe_consecutive_failures == 1
        assert "500" in st.last_probe_detail

    async def test_clean_result_is_a_success(self):
        st = _state()
        mgr = self._manager(st, {"success": True, "data": []})

        assert (await mgr.run_health_probe("paperless"))["ok"] is True
        assert st.probe_consecutive_failures == 0

    async def test_probe_passes_its_own_timeout_and_no_user(self):
        """A probe is a system call: no permissions, no user — which also keeps it
        out of the per-user ToolOutcomeStat the kiosk reads."""
        st = _state()
        mgr = self._manager(st, {"success": True, "data": []})
        await mgr.run_health_probe("paperless")

        kwargs = mgr.execute_tool.await_args.kwargs
        assert kwargs["user_permissions"] is None
        assert kwargs["user_id"] is None
        assert kwargs["call_timeout"] == 5

    async def test_an_exploding_probe_is_recorded_not_raised(self):
        st = _state()
        mgr = self._manager(st, {})
        mgr.execute_tool = AsyncMock(side_effect=RuntimeError("transport gone"))

        res = await mgr.run_health_probe("paperless")
        assert res["ok"] is False and st.probe_consecutive_failures == 1

    async def test_unknown_server_is_a_no_op(self):
        st = _state()
        mgr = self._manager(st, {"success": True, "data": []})
        assert (await mgr.run_health_probe("nope"))["skipped"] is True


# --- reconnect must NOT clear a probe verdict ------------------------------

def test_probe_verdict_survives_the_outcome_window_reset():
    """A reconnect proves the transport works and nothing about the service behind
    it — Paperless answered HTTP 500 across many healthy reconnects."""
    st = _state()
    st.record_probe_outcome(False, "HTTP 500")
    st.record_call_outcome(False)
    st.recent_outcomes.clear()          # what _connect_server does on success
    assert st.probe_consecutive_failures == 1
    assert st.last_probe_detail == "HTTP 500"


# --- monitor tick ordering + the bespoke hook ------------------------------

@pytest.mark.asyncio
class TestMonitorProbePass:
    async def test_probes_run_and_health_is_re_read_after(self, monkeypatch):
        """Ordering is the whole point: self-heal first (so the probe judges the
        SERVICE on a fresh transport), then probe, then re-read so the verdict
        reaches the SAME alert pass everything else uses."""
        import services.mcp_health_monitor as m

        monkeypatch.setattr(m.settings, "mcp_health_monitor_enabled", True)
        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", True)

        calls: list[str] = []
        healthy = {"servers": [{"name": "paperless", "health": "healthy"}]}

        def _get_status():
            calls.append("status")
            return healthy

        mgr = SimpleNamespace(
            get_status=_get_status,
            health_probe_due=lambda: ["paperless"],
            run_health_probe=AsyncMock(return_value={"ok": True}),
            record_external_probe=lambda *a: None,
            _servers={},
        )
        await m._monitor_tick_body(mgr)

        mgr.run_health_probe.assert_awaited_once_with("paperless")
        assert calls.count("status") == 2, "health must be re-read after probing"

    async def test_probe_pass_is_skipped_when_disabled(self, monkeypatch):
        import services.mcp_health_monitor as m

        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", False)
        mgr = SimpleNamespace(health_probe_due=lambda: ["paperless"],
                              run_health_probe=AsyncMock(), _servers={})
        assert await m._run_probes(mgr) == []
        mgr.run_health_probe.assert_not_awaited()

    async def test_probe_cap_per_tick(self, monkeypatch):
        import services.mcp_health_monitor as m

        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", True)
        monkeypatch.setattr(m.settings, "mcp_health_probe_max_per_tick", 2)
        mgr = SimpleNamespace(health_probe_due=lambda: ["a", "b", "c", "d"],
                              run_health_probe=AsyncMock(return_value={"ok": True}),
                              _servers={})
        assert len(await m._run_probes(mgr)) == 2

    async def test_search_uses_its_purpose_built_probe(self, monkeypatch):
        """A bare result count is a DOCUMENTED false-green for search (Wikipedia
        answers almost anything), so search keeps its own probe — this wires that
        verdict into the same channel instead of duplicating it badly."""
        import services.mcp_health_monitor as m
        import services.search_health as sh

        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", True)
        monkeypatch.setattr(
            sh, "probe_search_functional",
            AsyncMock(return_value={"verdict": "degraded", "reason": "nur 1 Engine aktiv"}),
        )
        recorded: list[tuple] = []
        state = SimpleNamespace(connected=True, last_probe_at=0.0)
        mgr = SimpleNamespace(
            health_probe_due=lambda: [],
            run_health_probe=AsyncMock(),
            record_external_probe=lambda *a: recorded.append(a),
            _servers={"search": state},
        )
        await m._run_probes(mgr)

        assert recorded == [("search", False, "nur 1 Engine aktiv")]
        mgr.run_health_probe.assert_not_awaited()

    async def test_unknown_search_verdict_records_nothing(self, monkeypatch):
        """Probe disabled / no URL / HTTP failure — absence of evidence must not
        read as evidence of failure."""
        import services.mcp_health_monitor as m
        import services.search_health as sh

        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", True)
        monkeypatch.setattr(
            sh, "probe_search_functional",
            AsyncMock(return_value={"verdict": "unknown", "reason": "deaktiviert"}),
        )
        recorded: list[tuple] = []
        mgr = SimpleNamespace(
            health_probe_due=lambda: [],
            run_health_probe=AsyncMock(),
            record_external_probe=lambda *a: recorded.append(a),
            _servers={"search": SimpleNamespace(connected=True, last_probe_at=0.0)},
        )
        await m._run_probes(mgr)
        assert recorded == []

    async def test_alert_names_what_failed(self, monkeypatch):
        """'Funktionstest fehlgeschlagen' alone would send the reader back to the
        logs — the dead end this whole feature exists to close."""
        import services.mcp_health_monitor as m
        from services import ops_alert

        ops_alert.reset_ledger()
        monkeypatch.setattr(m.settings, "mcp_health_monitor_enabled", True)
        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", False)
        monkeypatch.setattr(m.settings, "mcp_health_self_heal_enabled", False)

        sent: list[str] = []

        async def _fake_notify(title, message, dedup_key, data):
            sent.append(message)

        monkeypatch.setattr(m, "_notify", _fake_notify)
        mgr = SimpleNamespace(
            get_status=lambda: {"servers": [
                {"name": "paperless", "health": "degraded", "impaired_code": "probe_failed"}
            ]},
            _servers={"paperless": SimpleNamespace(last_probe_detail="HTTP 500")},
        )
        await m._monitor_tick_body(mgr)
        ops_alert.reset_ledger()

        assert sent and "HTTP 500" in sent[0]


@pytest.mark.asyncio
class TestProbeCadenceOnFailure:
    """A probe that never records an outcome never advances its due-time, so a
    wedged server would be re-probed every 120s tick instead of every interval —
    each attempt costing the full hang-guard."""

    async def test_hang_guard_records_a_failure(self, monkeypatch):
        import asyncio

        import services.mcp_health_monitor as m

        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", True)
        monkeypatch.setattr(m.settings, "mcp_health_probe_guard_timeout", 0.01)

        async def _hang(_name):
            await asyncio.sleep(5)

        recorded: list[tuple] = []
        mgr = SimpleNamespace(
            health_probe_due=lambda: ["wedged"],
            run_health_probe=_hang,
            record_external_probe=lambda *a: recorded.append(a),
            _servers={},
        )
        probed = await m._run_probes(mgr)

        assert probed == ["wedged"]
        assert recorded and recorded[0][0] == "wedged" and recorded[0][1] is False

    async def test_raising_probe_records_a_failure(self, monkeypatch):
        import services.mcp_health_monitor as m

        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", True)

        async def _boom(_name):
            raise RuntimeError("transport gone")

        recorded: list[tuple] = []
        mgr = SimpleNamespace(
            health_probe_due=lambda: ["broken"],
            run_health_probe=_boom,
            record_external_probe=lambda *a: recorded.append(a),
            _servers={},
        )
        await m._run_probes(mgr)

        assert recorded and recorded[0][1] is False
        assert "transport gone" in recorded[0][2]


class TestReviewFindings:
    """Each of these pins a bug that got as far as a commit. They are the reason
    the probe cannot quietly lie about a server being green."""

    def test_malformed_stanza_costs_only_the_probe_not_the_server(self):
        """`interval: 10m` used to raise, and the exception escaped into the
        per-entry config try/except — which drops the WHOLE server. Paperless would
        have vanished from the fleet: no tools, no health entry, nothing."""
        from services.mcp_client import _parse_health_probe
        from utils.config import settings

        parsed = _parse_health_probe({"tool": "t", "interval": "10m", "timeout": "bald",
                                      "expect": {"min_items": "viele"}})
        assert parsed is not None
        assert parsed["interval"] == settings.mcp_health_probe_interval
        assert parsed["timeout"] == settings.mcp_health_probe_timeout
        assert parsed["expect"]["min_items"] == 0


@pytest.mark.asyncio
class TestReviewFindingsAsync:
    async def test_probe_tool_must_resolve_exactly(self):
        """execute_tool has a deliberate fuzzy fallback that substitutes a similar
        tool — helpful for the agent, poison here: the probe would either answer
        fine while the intended check never ran, or fail schema validation forever."""
        from services.mcp_client import MCPManager, MCPToolInfo

        st = _state()
        st.connected = True
        st.all_discovered_tools = [MCPToolInfo("paperless", "list_tags", "x", "")]
        st.config.health_probe["tool"] = "list_correspondents"  # renamed upstream

        mgr = MCPManager.__new__(MCPManager)
        mgr._servers = {"paperless": st}
        mgr.execute_tool = AsyncMock()

        res = await mgr.run_health_probe("paperless")
        assert res["skipped"] is True
        mgr.execute_tool.assert_not_awaited()
        assert st.probe_consecutive_failures == 0, "a typo is not ill health"

    async def test_inconclusive_expectation_records_nothing(self):
        """Neither a false failure nor a false success — the warning is the signal."""
        from services.mcp_client import MCPManager

        st = _state()
        st.config.health_probe["expect"] = {"min_items": 2, "path": None}
        mgr = MCPManager.__new__(MCPManager)
        mgr._servers = {"paperless": st}
        mgr.execute_tool = AsyncMock(
            return_value={"success": True, "message": "Alles gut.", "data": []}
        )

        res = await mgr.run_health_probe("paperless")
        assert res["skipped"] is True
        assert st.probe_consecutive_failures == 0
        assert st.last_probe_ok is None, "no verdict may be recorded"

    async def test_bespoke_unknown_is_a_real_tristate(self, monkeypatch):
        """`(True, None)` used to mean 'unknown', colliding with a healthy verdict
        that carried no reason string — such a server would stay degraded forever."""
        import services.mcp_health_monitor as m
        import services.search_health as sh

        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", True)
        monkeypatch.setattr(
            sh, "probe_search_functional",
            AsyncMock(return_value={"verdict": "healthy", "reason": None}),
        )
        recorded: list[tuple] = []
        mgr = SimpleNamespace(
            health_probe_due=lambda: [],
            run_health_probe=AsyncMock(),
            record_external_probe=lambda *a: recorded.append(a),
            _servers={"search": SimpleNamespace(connected=True, last_probe_at=0.0)},
        )
        await m._run_probes(mgr)

        assert recorded == [("search", True, None)], "a healthy verdict must be recorded"

    async def test_per_tick_cap_does_not_starve_the_same_servers(self, monkeypatch):
        """A stable due-order plus a cap is a blacklist, not a throttle: the same
        prefix would win every tick and later servers — bespoke ones especially,
        since they are appended last — would never be probed at all."""
        import services.mcp_health_monitor as m

        monkeypatch.setattr(m.settings, "mcp_health_probe_enabled", True)
        monkeypatch.setattr(m.settings, "mcp_health_probe_max_per_tick", 2)

        # c and d have waited longest (last_probe_at 0 = never probed).
        servers = {
            "a": SimpleNamespace(connected=True, last_probe_at=900.0),
            "b": SimpleNamespace(connected=True, last_probe_at=800.0),
            "c": SimpleNamespace(connected=True, last_probe_at=0.0),
            "d": SimpleNamespace(connected=True, last_probe_at=100.0),
        }
        mgr = SimpleNamespace(
            health_probe_due=lambda: ["a", "b", "c", "d"],
            run_health_probe=AsyncMock(return_value={"ok": True}),
            record_external_probe=lambda *a: None,
            _servers=servers,
        )
        assert await m._run_probes(mgr) == ["c", "d"]
