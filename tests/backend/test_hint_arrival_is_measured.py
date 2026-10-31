"""Whether the `tool_hints` ARRIVE at discovery time has to be in the log.

🛑 The occasion, measured on 2026-10-26. Four hints stood for the scanner in
`config/mcp_servers.yaml` (1426/791/186/379 characters). The mounted path
carried them, the loader carried them, and the same code path produced a
1529-character preselection line in a reproduction INSIDE THE RUNNING POD.

In production the same line was **102 characters** long and carried no hint --
only the first line of the scanner server's docstring. Four review rounds
worked on these texts (#1411 the language trap, the Bluetooth demarcation, the
`known:` correction) without a single one of them reaching the model. Nothing
in the log said so; it only became visible through a purpose-built prompt dump.

🛑 **Absence is not a diagnosis.** The first version of this function returned
silently for a server without hints -- and a test pinned that silence down as
intended behaviour. That made three different situations look alike: a server
without hints, a configuration without hints, an image without this function.
Found in the review of #1415 and reversed.
"""
import inspect
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from loguru import logger

from services.mcp_client import log_hint_arrival

pytestmark = [pytest.mark.unit, pytest.mark.backend]


@dataclass
class _Tool:
    original_name: str
    hint: str = ""


@dataclass
class _State:
    """As much of `MCPServerState` as the function touches."""
    config: object
    all_discovered_tools: list = field(default_factory=list)
    tools: list = field(default_factory=list)


def _state(hints: dict, discovered: list[_Tool],
             offered: list[_Tool] | None = None) -> _State:
    """`offered=None` means: the filter throws nothing away (the normal case)."""
    return _State(
        config=SimpleNamespace(name="scanner", tool_hints=hints),
        all_discovered_tools=discovered,
        tools=list(discovered) if offered is None else offered,
    )


@pytest.fixture
def log_lines():
    lines: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: lines.append((m.record["level"].name, m.record["message"])),
        level="INFO")
    yield lines
    logger.remove(sink)


def test_a_key_that_matches_no_tool_is_a_WARNING(log_lines):
    """A key that matches no discovered tool used to be a silent no-op:
    `config.tool_hints.get(tool.name)` returns `None`, leaving no trace."""
    log_hint_arrival(_state(
        {"scan_document": "x" * 1426, "tippfehler_tool": "y" * 100},
        [_Tool("scan_document", "x" * 1426), _Tool("scanner_status")],
    ))
    warnings = [m for severity, m in log_lines if severity == "WARNING"]
    assert warnings, f"no WARNING: {log_lines}"
    assert "tippfehler_tool" in warnings[0], (
        "the warning does not name the key that matches nothing — then "
        f"it is not actionable:\n{warnings[0]}")


def test_hints_configured_but_NONE_attached_is_a_WARNING(log_lines):
    """Exactly the situation of 2026-10-26: configured yes, arrived nothing."""
    log_hint_arrival(_state(
        {"scan_document": "x" * 1426},
        [_Tool("scan_document"), _Tool("scanner_status")],
    ))
    warnings = [m for severity, m in log_lines if severity == "WARNING"]
    assert warnings, f"no WARNING: {log_lines}"
    assert "0 attached" in warnings[0], warnings[0]


def test_a_hinted_tool_filtered_OUT_of_the_offered_set_is_a_WARNING(log_lines):
    """🛑 Attached is not offered.

    `prompt_tools` or a DB override can throw a tool out of `state.tools` AFTER
    its hint was attached. The first version counted only
    `all_discovered_tools` and would have reported that as HEALTHY. Found in
    the review of #1415.
    """
    log_hint_arrival(_state(
        {"scan_document": "x" * 1426},
        [_Tool("scan_document", "x" * 1426), _Tool("scanner_status")],
        offered=[_Tool("scanner_status")],   # the hinted one dropped out
    ))
    warnings = [m for severity, m in log_lines if severity == "WARNING"]
    assert warnings, (
        f"a hinted tool was filtered out and that counted as "
        f"healthy: {log_lines}")
    assert "filtered out" in warnings[0], warnings[0]


def test_the_healthy_case_is_INFO_with_counts(log_lines):
    """When everything arrives a count is enough -- with the total length,
    otherwise a truncated hint would be indistinguishable from a complete
    one."""
    log_hint_arrival(_state(
        {"scan_document": "x" * 1426, "route_scan": "y" * 791},
        [_Tool("scan_document", "x" * 1426), _Tool("route_scan", "y" * 791)],
    ))
    info_lines = [m for severity, m in log_lines if severity == "INFO"]
    assert info_lines, f"no INFO line: {log_lines}"
    assert "2 hint(s) configured, 2 attached, 2 offered" in info_lines[0], info_lines[0]
    assert str(1426 + 791) in info_lines[0], info_lines[0]
    assert not [m for severity, m in log_lines if severity == "WARNING"]


def test_a_server_WITHOUT_hints_still_says_so(log_lines):
    """🛑 Reversed from the first version, and that is the core.

    It returned silently here, and a test held that silence in place. That made
    "this server has no hints" indistinguishable from "this process's
    configuration has none" and from "this image does not know the function" --
    three situations, one silence. Exactly the guesswork that set off this
    branch.
    """
    log_hint_arrival(_state({}, [_Tool("irgendwas")]))
    assert log_lines, (
        "a server without hints stays silent — then absence is a diagnosis "
        "again, and three different situations look the same")
    severity, message = log_lines[0]
    assert severity == "INFO", f"this is not a problem, so no WARNING: {message}"
    assert "0 hint(s) configured" in message, message


def test_the_line_appears_only_ON_CHANGE(log_lines):
    """🛑 Discovery runs about every 60 s per server (`mcp_refresh_interval`;
    `refresh_tools` does NOT read the per-server value). One line per pass
    would be about 60 an hour, and a permanently mismatched key would be a
    constant barrage that buries the real cases."""
    z = _state({"scan_document": "x" * 10},
                 [_Tool("scan_document", "x" * 10)])
    log_hint_arrival(z)
    log_hint_arrival(z)
    log_hint_arrival(z)
    assert len(log_lines) == 1, (
        f"three passes, {len(log_lines)} lines — an unchanged "
        "state may log only once")

    # If something changes, it has to appear again.
    z.all_discovered_tools = [_Tool("scan_document")]
    z.tools = list(z.all_discovered_tools)
    log_hint_arrival(z)
    assert len(log_lines) == 2, (
        "the hint dropped out and the log stays silent — then "
        "the throttling is useless")


def test_the_hint_TEXT_never_reaches_the_log(log_lines):
    """🛑 Only names and lengths -- and on BOTH branches.

    The first version checked only the INFO branch; but the WARNING branch is
    the one that inserts variable content. Found in the review of #1415.
    """
    secret = "DIESER-TEXT-DARF-NICHT-INS-PROTOKOLL"
    log_hint_arrival(_state({"a": secret}, [_Tool("a", secret)]))
    log_hint_arrival(_state(
        {"a": secret, "fehlt": secret}, [_Tool("a", secret)]))
    assert [s for s, _ in log_lines].count("WARNING") == 1, log_lines
    for _, message in log_lines:
        assert secret not in message, (
            f"the hint text is in the log:\n{message}")


def test_the_discovery_path_actually_calls_it(log_lines):
    """🛑 The WIRING, not just the function.

    That day's defect was the same one five times over: a guard built
    correctly and called nowhere, or a parameter dropped one layer up -- and
    every unit test stayed green.
    """
    from services.mcp_client import MCPManager, MCPServerConfig, MCPServerState

    cfg = MCPServerConfig(
        name="scanner", transport="streamable_http",
        tool_hints={"scan_document": "H" * 50, "gibtesnicht": "X" * 10},
    )
    state = MCPServerState(config=cfg)
    raw_tools = [SimpleNamespace(name="scan_document", description="Scan a stack.",
                            inputSchema={"properties": {}, "required": []})]

    # 🛑 A REAL manager, not `__new__`: `_apply_tool_filter` needs state that
    # `__init__` creates (`_tool_overrides`). Checked: `__init__` does no I/O
    # -- only assignments.
    mgr = MCPManager()
    mgr._install_discovered_tools(state, raw_tools)

    warnings = [m for severity, m in log_lines if severity == "WARNING"]
    assert warnings, (
        "the discovery path logs nothing about the hints — then "
        f"their absence stays invisible again: {log_lines}")
    assert "gibtesnicht" in warnings[0], warnings[0]
    assert state.all_discovered_tools[0].hint == "H" * 50


def test_the_two_discovery_entry_points_reach_the_install():
    """🛑 And the wiring BEFORE that.

    The test above proves only that `_install_discovered_tools` calls the
    count -- not that connecting and refreshing arrive there. A rework that
    pulls discovery back into `_connect_server` would leave it green and make
    the line disappear. Named in the review of #1415.
    """
    from services.mcp_client import MCPManager

    for name in ("_connect_server", "refresh_tools"):
        source = inspect.getsource(getattr(MCPManager, name))
        assert "_install_discovered_tools" in source, (
            f"`{name}` no longer goes through `_install_discovered_tools` — "
            "then the hint count is lost on this path")
