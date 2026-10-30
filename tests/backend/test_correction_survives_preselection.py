"""An explicit correction by the user overrides the tool preselection.

🛑 The occasion, reported by the owner on 2026-10-26: "when I pick the scanner
through 'wrongly recognised', that should have an effect."

It did not. "Correct & re-answer" sends a CONCRETE tool name
(`mcp.<server>.<tool>`, see `agent_router.role_for_intent`). The
`chat_handler` derived only the ROLE from it and threw the name away -- and in
every misfire that was reported the role was already the right one
(`documents`). So the correction had no effect, because the layer BELOW
removed the tool again: the preselection did not offer `scan_document`, and
the agent could not call it, no matter what the user had corrected.

Whoever says "that was a scan" has decided the question. A preselection model
must not reopen it.
"""
import json
from types import SimpleNamespace

import pytest

from services.agent_service import AgentService, _tools_pinned_by_correction
from services.agent_tools import AgentToolRegistry, ToolDefinition

pytestmark = [pytest.mark.unit, pytest.mark.backend]

#: More than the six below which the preselection does not even start.
ALLE = [
    "mcp.scanner.scan_document",
    "mcp.paperless.search_documents",
    "internal.knowledge_search",
    "mcp.paperless.get_document",
    "mcp.scanner.list_pending_scans",
    "mcp.scanner.route_scan",
    "mcp.email.send",
    "internal.ingest_status",
]
#: What the preselection model delivers -- without the scanner, exactly the misfire.
OHNE_SCANNER = ["mcp.paperless.search_documents", "internal.knowledge_search"]


def _dienst() -> AgentService:
    svc = AgentService.__new__(AgentService)
    r = AgentToolRegistry.__new__(AgentToolRegistry)
    r._tools = {n: ToolDefinition(name=n, description=f"does {n}") for n in ALLE}
    svc.tool_registry = r
    return svc


@pytest.fixture
def gestellter_pfad(monkeypatch):
    """Die Stationen um den LLM-Aufruf herum — er liefert OHNE_SCANNER."""
    import services.agent_service as mod

    monkeypatch.setattr(mod.prompt_manager, "get",
                        lambda *a, **k: "waehle die passenden Werkzeuge")

    async def chat(**kwargs):
        return SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(OHNE_SCANNER)))

    return SimpleNamespace(chat=chat)


@pytest.mark.asyncio
async def test_without_a_correction_the_model_decides(gestellter_pfad):
    """The baseline: without a correction the model's judgement stands.

    🛑 This test is the half that makes the next one meaningful -- without it
    the guard below could add the tool always and nobody would notice.
    """
    gewaehlt = await _dienst()._preselect_tools(
        "scanne nach x-idra", gestellter_pfad, "irgendein-modell",
    )
    assert set(gewaehlt) == set(OHNE_SCANNER)
    assert "mcp.scanner.scan_document" not in gewaehlt, (
        "der Aufbau dieses Tests stimmt nicht — das Modell soll den Scanner "
        "gerade NICHT liefern")


@pytest.mark.asyncio
async def test_a_correction_puts_the_WHOLE_SERVER_back(gestellter_pfad):
    """🛑 The whole server, not the stand-in from the menu.

    The selection menu groups the MCP tools by server, shows the server name
    ("Scanner") and sends the FIRST tool of that server as the value
    (`IntentCorrectionButton.tsx:57-67`). So the correction here is
    deliberately `scanner_status` -- a stand-in the user never meant -- and
    `scan_document` has to come back regardless.

    The first version of this guard pinned exactly the name that was sent.
    With that, "Scanner" would have forced `list_scanners` depending on the
    order of the API list and left `scan_document` out: a correction
    reinterpreted into a different tool. Noticed by the owner in conversation
    ("es gibt eine rolle scanner, nicht documents").
    """
    gewaehlt = await _dienst()._preselect_tools(
        "scanne nach x-idra", gestellter_pfad, "irgendein-modell",
        corrected_intent="mcp.scanner.scanner_status",
    )
    assert "mcp.scanner.scan_document" in gewaehlt, (
        "die Korrektur bleibt wirkungslos — der Nutzer waehlte den SERVER "
        "Scanner, und das Werkzeug, das er braucht, fehlt weiter")
    for name in ALLE:
        if name.startswith("mcp.scanner."):
            assert name in gewaehlt, f"{name} fehlt — nicht der ganze Server"
    # And it does not throw the model's choice away:
    assert set(OHNE_SCANNER) <= set(gewaehlt), (
        "die Korrektur darf die uebrigen Werkzeuge nicht verdraengen")


def test_an_internal_tool_pins_only_itself():
    """`internal.<x>` has no server behind it -- exactly one, not all."""
    registry = dict.fromkeys(ALLE, object())
    assert _tools_pinned_by_correction("internal.ingest_status", registry) == \
        ["internal.ingest_status"]


def test_a_core_option_pins_nothing():
    """`knowledge.ask` and `general.conversation` name no tool -- there the
    role still decides, and nothing may be invented."""
    registry = dict.fromkeys(ALLE, object())
    assert _tools_pinned_by_correction("knowledge.ask", registry) == []
    assert _tools_pinned_by_correction("general.conversation", registry) == []
    assert _tools_pinned_by_correction(None, registry) == []


@pytest.mark.asyncio
async def test_an_unknown_correction_is_ignored(gestellter_pfad):
    """A name the registry does not know must not invent anything.

    The correction comes from the client; an unknown or stale name must not
    produce an entry that does not exist.
    """
    gewaehlt = await _dienst()._preselect_tools(
        "scanne nach x-idra", gestellter_pfad, "irgendein-modell",
        corrected_intent="mcp.nichts.gibtesnicht",
    )
    assert set(gewaehlt) == set(OHNE_SCANNER)
    assert all(n in ALLE for n in gewaehlt), "ein unbekanntes Werkzeug kam herein"
    # 🛑 And the proof that this test EXERCISES the pinning at all: an
    # unknown server is otherwise indistinguishable from pinning that does not
    # run. A mutation that switched the pinning off entirely left this test
    # green. Hence the same call with a KNOWN server as the counterpart.
    registry = dict.fromkeys(ALLE, object())
    assert _tools_pinned_by_correction("mcp.nichts.gibtesnicht", registry) == []
    assert _tools_pinned_by_correction("mcp.scanner.egal", registry), (
        "die Praefix-Suche laeuft nicht — dann prueft der Fall oben nichts")


@pytest.mark.asyncio
async def test_pinning_is_idempotent(monkeypatch):
    """What the model already chose must not count twice.

    🛑 This test first claimed it stays EXACTLY with the named tool plus the
    model's choice. That was the old intent (pin one tool) and is wrong under
    the server rule: whoever corrects to "Scanner" gets the whole server.
    Changed to the new intent per the project rule, not bent to whatever the
    code does -- what is checked now is idempotence.
    """
    import services.agent_service as mod
    monkeypatch.setattr(mod.prompt_manager, "get", lambda *a, **k: "x")

    async def chat(**kwargs):
        return SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(["mcp.scanner.scan_document", *OHNE_SCANNER])))

    gewaehlt = await _dienst()._preselect_tools(
        "scanne nach x-idra", SimpleNamespace(chat=chat), "m",
        corrected_intent="mcp.scanner.scan_document",
    )
    scanner = {n for n in ALLE if n.startswith("mcp.scanner.")}
    assert set(gewaehlt) == scanner | set(OHNE_SCANNER)
    # 🛑 This used to read `len(gewaehlt) == len(set(gewaehlt))`. Over the
    # keys of a dict that can NEVER fail -- an empty assertion, and the
    # comment next to it even admitted as much instead of deleting it. What
    # really has to be checked: the pinning OVERWRITES nothing the model had
    # already chosen. Found in the review of #1414.
    svc = _dienst()
    for name in scanner | set(OHNE_SCANNER):
        assert gewaehlt[name] is svc.tool_registry._tools[name] or \
            gewaehlt[name].name == name, (
            f"{name} zeigt nicht mehr auf seine Definition")


@pytest.mark.asyncio
async def test_the_pin_runs_BEFORE_the_zero_tools_bailout(monkeypatch):
    """🛑 The order was asserted nowhere.

    If the model delivers only INVALID names, `filtered` is empty and the
    preselection returns `None` -- the agent then gets ALL the tools, so back
    to the "too many" state this layer exists to avoid. If the pinning stands
    only AFTER that fallback, the correction is lost exactly there.

    A mutation that moved the pinning below the fallback left all ten tests
    green. Found by mutation test in the review of #1414.
    """
    import services.agent_service as mod
    monkeypatch.setattr(mod.prompt_manager, "get", lambda *a, **k: "x")

    async def chat(**kwargs):
        # Only names the registry does NOT know.
        return SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(["mcp.gibt.esnicht", "internal.auch_nicht"])))

    gewaehlt = await _dienst()._preselect_tools(
        "scanne nach x-idra", SimpleNamespace(chat=chat), "m",
        corrected_intent="mcp.scanner.scanner_status",
    )
    assert gewaehlt is not None, (
        "die Vorauswahl fiel auf ALLE Werkzeuge zurueck, obwohl die Korrektur "
        "eine gueltige Menge haette liefern koennen — die Festnagelung laeuft "
        "zu spaet")
    assert set(gewaehlt) == {n for n in ALLE if n.startswith("mcp.scanner.")}
