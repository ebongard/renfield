"""Eine ausdrückliche Korrektur des Nutzers überstimmt die Werkzeug-Vorauswahl.

🛑 Der Anlass, am 2026-10-26 vom Eigentümer gemeldet: „wenn ich über falsch
erkannt den Scanner auswähle, sollte das eine Auswirkung haben."

Hatte es nicht. „Korrigieren & neu beantworten" schickt einen KONKRETEN
Werkzeugnamen (`mcp.<server>.<tool>`, siehe `agent_router.role_for_intent`).
Der `chat_handler` leitete davon nur die ROLLE ab und warf den Namen weg — und
die Rolle war in jedem gemeldeten Fehlgriff schon die richtige (`documents`).
Die Korrektur blieb also wirkungslos, weil die Schicht DARUNTER das Werkzeug
wieder entfernte: die Vorauswahl bot `scan_document` nicht an, und der Agent
konnte es nicht rufen, egal was der Nutzer korrigiert hatte.

Wer sagt „das war ein Scan", hat die Frage entschieden. Ein Vorauswahl-Modell
darf sie nicht neu aufwerfen.
"""
import json
from types import SimpleNamespace

import pytest

from services.agent_service import AgentService, _tools_pinned_by_correction
from services.agent_tools import AgentToolRegistry, ToolDefinition

pytestmark = [pytest.mark.unit, pytest.mark.backend]

#: Mehr als die sechs, unter denen die Vorauswahl gar nicht erst anläuft.
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
#: Was das Vorauswahl-Modell liefert — ohne den Scanner, genau der Fehlgriff.
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
    """Die Vorlage: ohne Korrektur bleibt es beim Urteil des Modells.

    🛑 Dieser Test ist die Hälfte, die das Folgende aussagekräftig macht —
    ohne ihn könnte der Riegel unten das Werkzeug immer hinzufügen und
    niemandem fiele es auf.
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
    """🛑 Der ganze Server, nicht der Vertreter aus dem Menü.

    Das Auswahlmenü gruppiert die MCP-Werkzeuge nach Server, zeigt den
    Servernamen („Scanner") und schickt als Wert das ERSTE Werkzeug dieses
    Servers (`IntentCorrectionButton.tsx:57-67`). Hier also absichtlich
    `scanner_status` als Korrektur — ein Vertreter, den der Nutzer so nie
    gemeint hat — und `scan_document` muss trotzdem zurückkommen.

    Die erste Fassung dieses Riegels nagelte genau den gesendeten Namen fest.
    Damit hätte „Scanner" je nach Reihenfolge der API-Liste `list_scanners`
    erzwungen und `scan_document` weiter draußen gelassen: eine Korrektur in
    ein anderes Werkzeug umgedeutet. Vom Eigentümer im Gespräch bemerkt
    („es gibt eine rolle scanner, nicht documents").
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
    # Und sie wirft die Wahl des Modells nicht weg:
    assert set(OHNE_SCANNER) <= set(gewaehlt), (
        "die Korrektur darf die uebrigen Werkzeuge nicht verdraengen")


def test_an_internal_tool_pins_only_itself():
    """`internal.<x>` hat keinen Server dahinter — genau eines, nicht alle."""
    registry = dict.fromkeys(ALLE, object())
    assert _tools_pinned_by_correction("internal.ingest_status", registry) == \
        ["internal.ingest_status"]


def test_a_core_option_pins_nothing():
    """`knowledge.ask` und `general.conversation` benennen kein Werkzeug —
    dort entscheidet weiter die Rolle, und es darf nichts erfunden werden."""
    registry = dict.fromkeys(ALLE, object())
    assert _tools_pinned_by_correction("knowledge.ask", registry) == []
    assert _tools_pinned_by_correction("general.conversation", registry) == []
    assert _tools_pinned_by_correction(None, registry) == []


@pytest.mark.asyncio
async def test_an_unknown_correction_is_ignored(gestellter_pfad):
    """Ein Name, den die Registratur nicht kennt, darf nichts erfinden.

    Die Korrektur kommt vom Klienten; ein unbekannter oder veralteter Name
    darf keinen Eintrag erzeugen, den es nicht gibt.
    """
    gewaehlt = await _dienst()._preselect_tools(
        "scanne nach x-idra", gestellter_pfad, "irgendein-modell",
        corrected_intent="mcp.nichts.gibtesnicht",
    )
    assert set(gewaehlt) == set(OHNE_SCANNER)
    assert all(n in ALLE for n in gewaehlt), "ein unbekanntes Werkzeug kam herein"


@pytest.mark.asyncio
async def test_pinning_is_idempotent(monkeypatch):
    """Was das Modell schon gewählt hat, darf nicht doppelt zählen.

    🛑 Dieser Test behauptete erst, es bleibe GENAU beim benannten Werkzeug
    plus der Modellwahl. Das war die alte Absicht (ein Werkzeug festnageln) und
    ist mit der Server-Regel falsch: wer „Scanner" korrigiert, bekommt den
    ganzen Server. Nach der Projektregel auf die neue Absicht geändert, nicht
    auf das gebogen, was der Code tut — geprüft wird jetzt die
    Wiederholungsfestigkeit.
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
    # Ein dict kann nicht doppeln — die Behauptung haelt fest, dass die
    # Festnagelung nichts ueberschreibt und nichts verliert.
    assert len(gewaehlt) == len(set(gewaehlt))
