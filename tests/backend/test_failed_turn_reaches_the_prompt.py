"""Ende zu Ende: ein asynchron gescheiterter Auftrag erreicht den PROMPT.

🛑 Warum es diese Datei gibt. Am 2026-10-26 bat der Eigentümer dreimal „scanne
nach Haushalt". Der erste Scan scheiterte am Papierstau; auf die beiden
weiteren antwortete der Agent „der Scan läuft bereits im Hintergrund, Du wirst
benachrichtigt" und rief `scan_document` NIE. Eine erfundene Beruhigung über
einen Scan, der nicht lief.

Zwei Lücken, und beide waren nur zusammen tödlich:

1. `handle_job_event` schrieb die Fertigmeldung ohne `action_success`, also
   feuerte der Stale-Error-Riegel nicht.
2. Die Historie einer lebenden Sitzung wird einmal je Socket geladen; ein
   asynchroner Erzeuger fasst sie nicht an, die Meldung erreichte den
   Agentenkontext also gar nicht.

Beide Reparaturen hatten Tests — aber gegen MOCKS. Die erste prüfte, dass das
Feld ÜBERGEBEN wird, nicht dass daraus je ein Marker im Prompt entsteht. Genau
deshalb rutschte Lücke 2 durch und wurde erst im Review gefunden. Dieser Test
schließt die Kette: echter Schreibvorgang → echtes Laden → echter Prompt.
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services.agent_service import AgentContext, AgentService
from services.agent_tools import AgentToolRegistry
from services.conversation_service import ConversationService

pytestmark = [pytest.mark.backend, pytest.mark.database]


MARKE = "[VORHERIGE_FEHLGESCHLAGENE_AKTION]"


def _verlaufszeile(prompt: str, suchtext: str) -> str:
    """Die Verlaufszeile, die `suchtext` enthält.

    🛑 Nie den ganzen Prompt auf die Marke absuchen: der Hinweistext der
    Vorlage NENNT sie („Zeilen, die mit … beginnen, beschreiben historische
    Tool-Fehler"), ein Teilstring-Test trifft also immer und beweist nichts.
    Genau dieser Fehler steckte in der ersten Fassung dieses Tests.
    """
    treffer = [z for z in prompt.splitlines() if suchtext in z and "beschreiben" not in z]
    assert treffer, f"keine Verlaufszeile mit {suchtext!r} im Prompt"
    return treffer[0]


def _agent() -> AgentService:
    registry = MagicMock(spec=AgentToolRegistry)
    registry._tools = {}
    registry.get_tool_names.return_value = []
    registry.build_tools_prompt.return_value = "TOOLS: none"
    role = MagicMock()
    role.max_steps = 5
    role.prompt_key = "agent_prompt"
    role.model = None
    role.ollama_url = None
    role.name = "documents"
    return AgentService(registry, role=role)


async def _verlauf_nach_dem_stau(db: AsyncSession, sid: str) -> list[dict]:
    """Der gemessene Verlauf: Bitte, Zusage, dann die asynchrone Stau-Meldung."""
    service = ConversationService(db)
    await service.save_message(sid, "user", "scanne nach Haushalt")
    await service.save_message(
        sid, "assistant",
        "Der Scan für den Haushalt wurde gestartet. Du wirst benachrichtigt.",
        metadata={"action_success": True})
    # Das schreibt `handle_job_event` — asynchron, nach dem Zug.
    await service.save_message(
        sid, "assistant",
        "Der Scan ist fehlgeschlagen: Papierstau im Einzug.",
        metadata={"scanner_job": {"job_id": "b1abb766", "status": "failed"},
                  "action_success": False})
    return await service.load_context(sid, max_messages=10)


async def test_the_failed_scan_is_marked_in_the_built_prompt(db_session: AsyncSession):
    """Der Marker steht im PROMPT — nicht nur in der Datenbank, nicht nur in
    der geladenen Historie."""
    verlauf = await _verlauf_nach_dem_stau(db_session, "prompt-kette-1")

    prompt = await _agent()._build_agent_prompt(
        "scanne nach Haushalt",
        AgentContext(original_message="scanne nach Haushalt"),
        conversation_history=verlauf,
        lang="de",
    )

    stau = _verlaufszeile(prompt, "Papierstau")
    assert MARKE in stau, (
        f"der Stale-Error-Marker fehlt an der Stau-Zeile — der Agent kann den "
        f"gescheiterten Scan nicht von der Gegenwart unterscheiden: {stau}")
    # Und er sitzt NUR dort, nicht an der erfolgreichen Zusage davor.
    zusage = _verlaufszeile(prompt, "wurde gestartet")
    assert MARKE not in zusage, (
        f"die erfolgreiche Zusage wurde fälschlich markiert: {zusage}")


async def test_the_template_keeps_its_hint_about_the_marker(db_session: AsyncSession):
    """Bewacht die ZWEITE Hälfte: den Hinweis in der Vorlage, nicht den Marker.

    🛑 Der Marker allein genügt NICHT. Ohne den Hinweis, dass solche Zeilen
    HISTORISCH sind, liest das Modell den alten Fehlschlag als gegenwärtigen
    Zustand und antwortet daraus — statt das Werkzeug erneut zu rufen. Beide
    Hälften müssen zusammen im Prompt stehen; die Rule sagt ausdrücklich, dass
    man sie nie einseitig ändern darf."""
    verlauf = await _verlauf_nach_dem_stau(db_session, "prompt-kette-2")

    prompt = await _agent()._build_agent_prompt(
        "scanne nach Haushalt",
        AgentContext(original_message="scanne nach Haushalt"),
        conversation_history=verlauf,
        lang="de",
    )

    # Die Zeile, die den Marker ERKLÄRT — nicht die, die ihn trägt.
    hinweis = [z for z in prompt.splitlines() if MARKE in z and "beschreiben" in z]
    assert hinweis, (
        "der Prompt trägt den Marker, aber keine Zeile erklärt, dass er "
        "HISTORISCH ist — dann antwortet das Modell aus dem alten Fehlschlag")
    unten = hinweis[0].lower()
    assert any(w in unten for w in ("neu aus", "erneut", "nochmal", "wiederhol")), (
        f"der Hinweis fordert keine Neuausführung: {hinweis[0]}")


async def test_without_the_metadata_there_is_no_marker(db_session: AsyncSession):
    """Die Negativkontrolle IM Test: fehlt `action_success`, fehlt der Marker.
    Das war der Zustand bis 2026-10-26, und er ist der Grund, warum der Agent
    den Scan für laufend hielt."""
    service = ConversationService(db_session)
    sid = "prompt-kette-3"
    await service.save_message(sid, "assistant", "Der Scan wurde gestartet.",
                               metadata={"action_success": True})
    await service.save_message(
        sid, "assistant", "Der Scan ist fehlgeschlagen: Papierstau im Einzug.",
        metadata={"scanner_job": {"job_id": "x", "status": "failed"}})  # OHNE Marker

    verlauf = await service.load_context(sid, max_messages=10)
    prompt = await _agent()._build_agent_prompt(
        "scanne nach Haushalt",
        AgentContext(original_message="scanne nach Haushalt"),
        conversation_history=verlauf, lang="de",
    )
    stau = _verlaufszeile(prompt, "Papierstau")
    assert MARKE not in stau, (
        f"ohne `action_success: False` darf die Verlaufszeile keinen Marker "
        f"tragen — sonst beweist der Test oben nichts: {stau}")
