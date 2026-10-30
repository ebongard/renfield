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
class _Werkzeug:
    original_name: str
    hint: str = ""


@dataclass
class _Zustand:
    """As much of `MCPServerState` as the function touches."""
    config: object
    all_discovered_tools: list = field(default_factory=list)
    tools: list = field(default_factory=list)


def _zustand(hinweise: dict, entdeckt: list[_Werkzeug],
             angeboten: list[_Werkzeug] | None = None) -> _Zustand:
    """`angeboten=None` means: the filter throws nothing away (the normal case)."""
    return _Zustand(
        config=SimpleNamespace(name="scanner", tool_hints=hinweise),
        all_discovered_tools=entdeckt,
        tools=list(entdeckt) if angeboten is None else angeboten,
    )


@pytest.fixture
def protokoll():
    zeilen: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: zeilen.append((m.record["level"].name, m.record["message"])),
        level="INFO")
    yield zeilen
    logger.remove(sink)


def test_a_key_that_matches_no_tool_is_a_WARNING(protokoll):
    """A key that matches no discovered tool used to be a silent no-op:
    `config.tool_hints.get(tool.name)` returns `None`, leaving no trace."""
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426, "tippfehler_tool": "y" * 100},
        [_Werkzeug("scan_document", "x" * 1426), _Werkzeug("scanner_status")],
    ))
    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, f"kein WARNING: {protokoll}"
    assert "tippfehler_tool" in warnungen[0], (
        "die Warnung nennt den Schluessel nicht, der ins Leere greift — dann "
        f"ist sie nicht handlungsfaehig:\n{warnungen[0]}")


def test_hints_configured_but_NONE_attached_is_a_WARNING(protokoll):
    """Exactly the situation of 2026-10-26: configured yes, arrived nothing."""
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426},
        [_Werkzeug("scan_document"), _Werkzeug("scanner_status")],
    ))
    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, f"kein WARNING: {protokoll}"
    assert "0 attached" in warnungen[0], warnungen[0]


def test_a_hinted_tool_filtered_OUT_of_the_offered_set_is_a_WARNING(protokoll):
    """🛑 Attached is not offered.

    `prompt_tools` or a DB override can throw a tool out of `state.tools` AFTER
    its hint was attached. The first version counted only
    `all_discovered_tools` and would have reported that as HEALTHY. Found in
    the review of #1415.
    """
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426},
        [_Werkzeug("scan_document", "x" * 1426), _Werkzeug("scanner_status")],
        angeboten=[_Werkzeug("scanner_status")],   # der behinweiste fiel raus
    ))
    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, (
        f"ein behinweistes Werkzeug wurde herausgefiltert und das galt als "
        f"gesund: {protokoll}")
    assert "filtered out" in warnungen[0], warnungen[0]


def test_the_healthy_case_is_INFO_with_counts(protokoll):
    """When everything arrives a count is enough -- with the total length,
    otherwise a truncated hint would be indistinguishable from a complete
    one."""
    log_hint_arrival(_zustand(
        {"scan_document": "x" * 1426, "route_scan": "y" * 791},
        [_Werkzeug("scan_document", "x" * 1426), _Werkzeug("route_scan", "y" * 791)],
    ))
    infos = [m for stufe, m in protokoll if stufe == "INFO"]
    assert infos, f"keine INFO-Zeile: {protokoll}"
    assert "2 hint(s) configured, 2 attached, 2 offered" in infos[0], infos[0]
    assert str(1426 + 791) in infos[0], infos[0]
    assert not [m for stufe, m in protokoll if stufe == "WARNING"]


def test_a_server_WITHOUT_hints_still_says_so(protokoll):
    """🛑 Reversed from the first version, and that is the core.

    It returned silently here, and a test held that silence in place. That made
    "this server has no hints" indistinguishable from "this process's
    configuration has none" and from "this image does not know the function" --
    three situations, one silence. Exactly the guesswork that set off this
    branch.
    """
    log_hint_arrival(_zustand({}, [_Werkzeug("irgendwas")]))
    assert protokoll, (
        "ein Server ohne Hinweise schweigt — dann ist Abwesenheit wieder eine "
        "Diagnose, und drei verschiedene Lagen sehen gleich aus")
    stufe, nachricht = protokoll[0]
    assert stufe == "INFO", f"das ist kein Problem, also kein WARNING: {nachricht}"
    assert "0 hint(s) configured" in nachricht, nachricht


def test_the_line_appears_only_ON_CHANGE(protokoll):
    """🛑 Discovery runs about every 60 s per server (`mcp_refresh_interval`;
    `refresh_tools` does NOT read the per-server value). One line per pass
    would be about 60 an hour, and a permanently mismatched key would be a
    constant barrage that buries the real cases."""
    z = _zustand({"scan_document": "x" * 10},
                 [_Werkzeug("scan_document", "x" * 10)])
    log_hint_arrival(z)
    log_hint_arrival(z)
    log_hint_arrival(z)
    assert len(protokoll) == 1, (
        f"drei Durchlaeufe, {len(protokoll)} Zeilen — ein unveraenderter "
        "Zustand darf nur einmal protokollieren")

    # If something changes, it has to appear again.
    z.all_discovered_tools = [_Werkzeug("scan_document")]
    z.tools = list(z.all_discovered_tools)
    log_hint_arrival(z)
    assert len(protokoll) == 2, (
        "der Hinweis ist weggefallen und das Protokoll schweigt — dann nuetzt "
        "die Drosselung nichts")


def test_the_hint_TEXT_never_reaches_the_log(protokoll):
    """🛑 Only names and lengths -- and on BOTH branches.

    The first version checked only the INFO branch; but the WARNING branch is
    the one that inserts variable content. Found in the review of #1415.
    """
    geheim = "DIESER-TEXT-DARF-NICHT-INS-PROTOKOLL"
    log_hint_arrival(_zustand({"a": geheim}, [_Werkzeug("a", geheim)]))
    log_hint_arrival(_zustand(
        {"a": geheim, "fehlt": geheim}, [_Werkzeug("a", geheim)]))
    assert [s for s, _ in protokoll].count("WARNING") == 1, protokoll
    for _, nachricht in protokoll:
        assert geheim not in nachricht, (
            f"der Hinweistext steht im Protokoll:\n{nachricht}")


def test_the_discovery_path_actually_calls_it(protokoll):
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
    rohe = [SimpleNamespace(name="scan_document", description="Scan a stack.",
                            inputSchema={"properties": {}, "required": []})]

    # 🛑 A REAL manager, not `__new__`: `_apply_tool_filter` needs state that
    # `__init__` creates (`_tool_overrides`). Checked: `__init__` does no I/O
    # -- only assignments.
    mgr = MCPManager()
    mgr._install_discovered_tools(state, rohe)

    warnungen = [m for stufe, m in protokoll if stufe == "WARNING"]
    assert warnungen, (
        "der Entdeckungspfad protokolliert nichts ueber die Hinweise — dann "
        f"bleibt ihr Ausbleiben wieder unsichtbar: {protokoll}")
    assert "gibtesnicht" in warnungen[0], warnungen[0]
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
        quelle = inspect.getsource(getattr(MCPManager, name))
        assert "_install_discovered_tools" in quelle, (
            f"`{name}` geht nicht mehr ueber `_install_discovered_tools` — "
            "dann entfaellt die Hinweis-Zaehlung auf diesem Weg")
