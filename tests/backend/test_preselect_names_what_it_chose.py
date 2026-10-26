"""Die Vorauswahl muss protokollieren, WAS sie gewählt hat — nicht wie viel.

🛑 Der Anlass, am 2026-10-26 im Protokoll gemessen. Auf „scanne nach xidra"
griff der Agent dreimal zur Paperless-Suche. Im Protokoll stand nur:

    13:20:52  Tool pre-selection: 3/33 tools selected for: 'scanne nach xidra'

Aus dieser Zeile ist nicht zu entscheiden, ob `scan_document` gar nicht
angeboten oder nur nicht gewählt wurde — also ob die Vorauswahl oder der Agent
danebenlag. Zwei völlig verschiedene Reparaturen. Die Diagnose kostete darum
einen eigenen Durchlauf mit nachgerüsteter Protokollzeile.

Eine Zählung ohne Namen ist für die Diagnose fast wertlos. Dieser Test hält
fest, dass die Namen dastehen.

🛑 Negativkontrolle gemessen: gegen die alte f-string-Zeile schlägt
`test_the_log_line_names_the_chosen_tools` fehl („`mcp.scanner.scan_document`
fehlt in der Zeile"). `test_the_log_line_still_carries_count_and_message`
bleibt dabei GRÜN, und das ist Absicht — Zählung und Anlass trug die alte
Zeile auch; er sichert die loguru-Umstellung, nicht die Namen.
"""
import json
from types import SimpleNamespace

import pytest
from loguru import logger

from services.agent_service import AgentService
from services.agent_tools import AgentToolRegistry, ToolDefinition

pytestmark = [pytest.mark.unit, pytest.mark.backend]

#: Mehr als die sechs, unter denen die Vorauswahl gar nicht erst anläuft.
ALLE = [
    "mcp.scanner.scan_document",
    "mcp.paperless.search_documents",
    "internal.list_rooms",
    "mcp.radio.search_stations",
    "internal.play_radio",
    "mcp.ha.call_service",
    "mcp.email.send",
    "mcp.calendar.list_events",
]
GEWAEHLT = ["mcp.scanner.scan_document", "internal.list_rooms"]


def _dienst() -> AgentService:
    """Eine Instanz mit echter Registratur — nur die LLM-Station wird gestellt."""
    svc = AgentService.__new__(AgentService)
    r = AgentToolRegistry.__new__(AgentToolRegistry)
    r._tools = {
        n: ToolDefinition(name=n, description=f"does {n}") for n in ALLE
    }
    svc.tool_registry = r
    return svc


@pytest.fixture
def protokoll():
    """Fängt die echten loguru-Zeilen ab — nach der Ersetzung der Argumente."""
    zeilen: list[str] = []
    sink = logger.add(lambda m: zeilen.append(m.record["message"]), level="INFO")
    yield zeilen
    logger.remove(sink)


@pytest.fixture
def gestellter_pfad(monkeypatch):
    """Die Stationen um den LLM-Aufruf herum, damit er bis zur Zeile kommt."""
    import services.agent_service as mod

    monkeypatch.setattr(
        mod.prompt_manager, "get",
        lambda *a, **k: "waehle die passenden Werkzeuge",
    )

    async def chat(**kwargs):
        return SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(GEWAEHLT))
        )

    return SimpleNamespace(chat=chat)


@pytest.mark.asyncio
async def test_the_log_line_names_the_chosen_tools(protokoll, gestellter_pfad):
    gewaehlt = await _dienst()._preselect_tools(
        "scanne nach xidra", gestellter_pfad, "irgendein-modell",
    )
    assert set(gewaehlt) == set(GEWAEHLT), (
        "die Vorauswahl selbst hat nicht das Erwartete geliefert — dann "
        f"prueft dieser Test die falsche Sache: {sorted(gewaehlt or [])}")

    zeile = next((z for z in protokoll if "Tool pre-selection" in z), None)
    assert zeile is not None, f"keine Vorauswahl-Zeile im Protokoll: {protokoll}"

    for name in GEWAEHLT:
        assert name in zeile, (
            f"`{name}` fehlt in der Zeile — dann ist aus dem Protokoll nicht "
            f"zu entscheiden, ob das Werkzeug angeboten wurde:\n{zeile}")


@pytest.mark.asyncio
async def test_the_log_line_still_carries_count_and_message(protokoll, gestellter_pfad):
    """Die Namen kommen HINZU; Zählung und Anlass bleiben stehen.

    🛑 Die Zeile wurde von einem f-string auf loguru-Argumente umgestellt.
    Steht dort ein `%s` oder bleibt ein Argument unersetzt, kommt genau hier
    ein rohes `{}` an (siehe `test_loguru_placeholders.py` fuer die Klasse).
    """
    await _dienst()._preselect_tools(
        "scanne nach xidra", gestellter_pfad, "irgendein-modell",
    )
    zeile = next(z for z in protokoll if "Tool pre-selection" in z)
    assert f"{len(GEWAEHLT)}/{len(ALLE)}" in zeile, f"die Zaehlung fehlt:\n{zeile}"
    assert "scanne nach xidra" in zeile, f"der Anlass fehlt:\n{zeile}"
    assert "{}" not in zeile, (
        f"ein Argument wurde nicht ersetzt — die Zeile ist blind:\n{zeile}")
