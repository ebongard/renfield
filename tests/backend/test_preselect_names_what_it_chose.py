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

    # 🛑 Auf „tools selected for:" pruefen, nicht auf „Tool pre-selection":
    # das Praefix tragen auch die PROMPT- und die Rueckfall-Zeile. Mit
    # angelegtem `agent_preselect_prompt_dump` griffen diese Tests sonst die
    # falsche Zeile und schlugen irrefuehrend fehl — von der Negativkontrolle
    # des Schalters aufgedeckt.
    zeile = next((z for z in protokoll if "tools selected for:" in z), None)
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
    zeile = next(z for z in protokoll if "tools selected for:" in z)
    # 🛑 Der Fingerabdruck der Eingabe MUSS in der Zeile stehen — er ist der
    # ganze Liefergegenstand von #1413. Ohne diese Behauptung bemerkt kein Test
    # den stillen Fall: `logger.info` mit einem Argument zu wenig laesst die
    # Zeile sauber formatieren, nur ohne `in=`. Die umgekehrte Richtung (ein
    # Platzhalter zu viel) wirft und wird vom blanken `except Exception`
    # geschluckt — dann faellt die Vorauswahl auf ALLE Werkzeuge zurueck und
    # meldet das als Modellfehler. Im Review zu #1413 gefunden.
    assert "(in=" in zeile, (
        f"der Eingabe-Fingerabdruck fehlt in der Zeile:\n{zeile}")
    assert f"{len(GEWAEHLT)}/{len(ALLE)}" in zeile, f"die Zaehlung fehlt:\n{zeile}"
    assert "scanne nach xidra" in zeile, f"der Anlass fehlt:\n{zeile}"
    assert "{}" not in zeile, (
        f"ein Argument wurde nicht ersetzt — die Zeile ist blind:\n{zeile}")


@pytest.mark.asyncio
async def test_the_prompt_dump_is_dark_by_default(protokoll, gestellter_pfad):
    """🛑 Dunkel heißt: KEINE Zeile. Der Prompt trägt die Nachricht des
    Nutzers UNGEKÜRZT — die reguläre Zeile zeigt nur `message[:60]`."""
    await _dienst()._preselect_tools(
        "scanne nach xidra", gestellter_pfad, "irgendein-modell",
    )
    assert not [z for z in protokoll if "PROMPT" in z], (
        "der Prompt-Auszug erscheint ohne Schalter — damit stünde die volle "
        "Nachricht jedes Zuges im Protokoll")


@pytest.mark.asyncio
async def test_the_prompt_dump_shows_the_real_prompt_when_switched_on(
    protokoll, gestellter_pfad, monkeypatch,
):
    """Angelegt liefert er den Prompt, an dem eine Diagnose scheitern kann.

    Am 2026-10-26 gebraucht: `scanne nach x-idra` griff reproduzierbar zur
    Suche, die Eingabe-Signatur belegte die Reproduzierbarkeit — aber WAS im
    Prompt stand, war nirgends ablesbar, und jede Nachbildung (6, dann 10 von
    33 Werkzeugen) wählte richtig und bewies damit nichts.
    """
    import services.agent_service as mod
    monkeypatch.setattr(mod.settings, "agent_preselect_prompt_dump", True)

    await _dienst()._preselect_tools(
        "scanne nach xidra", gestellter_pfad, "irgendein-modell",
    )
    zeilen = [z for z in protokoll if "PROMPT" in z]
    assert zeilen, "der Schalter ist an, aber es kommt keine Zeile"
    assert "(in=" in zeilen[0], (
        "ohne die Signatur ist der Auszug keiner bestimmten Zeile zuzuordnen")
