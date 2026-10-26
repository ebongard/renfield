"""Jeder Einstieg in den Agenten muss `corrected_intent` weiterreichen.

🛑 Der reparierte Fehler WAR ein weggelassener Parameter eine Schicht höher:
„Korrigieren & neu beantworten" schickte den Werkzeugnamen, der `chat_handler`
leitete daraus nur die Rolle ab und warf den Rest weg — und die Rolle war in
jedem gemeldeten Fehlgriff schon die richtige. Die Korrektur blieb wirkungslos,
und zwar still.

Gegen eine WIEDERHOLUNG schützte zunächst nichts. Im Review zu #1414 per
Mutationstest belegt: das Entfernen von `corrected_intent=` an der
Aufrufstelle im `chat_handler`, oder an einer der beiden Weiterleitungen in
`agent_service`, ließ ALLE Tests grün. Zwei weitere Aufrufstellen
(`orchestrator.py`, `routes/chat.py`) hatten den Parameter von Anfang an nicht
und fielen niemandem auf.

Das ist die Klasse, nicht die Instanz: ein Strukturtest über die Aufrufstellen
statt eines Tests je Stelle. Verwandte Vorbilder im Projekt:
`test_ha_glue_boundary.py`, `test_loguru_placeholders.py`.
"""
import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.backend]

WURZEL = Path(__file__).resolve().parents[2] / "src" / "backend"

#: Die Dateien, die den Agenten starten. Wächst die Liste, muss die neue Stelle
#: den Parameter tragen — oder hier mit Begründung ausgenommen werden.
#: 🛑 Keine Ausnahmen eintragen, ohne den Grund daneben zu schreiben.
_AUSNAHMEN: dict[str, str] = {}


def _aufrufstellen() -> list[tuple[str, int, set[str]]]:
    """`(relativer Pfad, Zeile, Satz der Schlüsselwortnamen)` je Einstieg.

    Gesucht werden `agent.run(...)` UND `orchestrator.run_orchestrated(...)`,
    jeweils als Attributaufruf — nicht `run(` als freie Funktion und nicht
    `asyncio.run`/`loop.run`. Der Orchestrator gehört dazu, weil er seine
    Argumente per `**agent_kwargs` an `agent.run` weitergibt: wer ihn füllt,
    entscheidet, ob die Korrektur dort ankommt.
    """
    treffer = []
    for datei in sorted(WURZEL.rglob("*.py")):
        if "/tests/" in str(datei):
            continue
        try:
            baum = ast.parse(datei.read_text(), filename=str(datei))
        except SyntaxError:  # pragma: no cover
            continue
        for knoten in ast.walk(baum):
            if not isinstance(knoten, ast.Call):
                continue
            f = knoten.func
            if not isinstance(f, ast.Attribute):
                continue
            # 🛑 `run_orchestrated` gehoert MIT dazu. Die erste Fassung dieses
            # Tests nahm nur `.run`, und damit war die Stelle unbewacht, die
            # `agent_kwargs` FUELLT (`chat_handler` -> `run_orchestrated` ->
            # `agent.run(**agent_kwargs)`) — genau das Loch, das der Review zu
            # #1414 gefunden hatte. Mein eigener Mutationsversuch loeschte
            # zufaellig diese Zeile, der Test blieb gruen, und erst das
            # Nachzaehlen der Vorkommnisse zeigte warum.
            if f.attr not in ("run", "run_orchestrated"):
                continue
            # Der Empfänger muss „agent" oder „orchestrator" heissen, sonst ist
            # es ein fremdes `run` (asyncio.run, loop.run, …).
            empfaenger = f.value
            name = (empfaenger.id if isinstance(empfaenger, ast.Name)
                    else empfaenger.attr if isinstance(empfaenger, ast.Attribute)
                    else "")
            if not any(w in name.lower() for w in ("agent", "orchestrator")):
                continue
            schluessel = {k.arg for k in knoten.keywords if k.arg}
            hat_sternchen = any(k.arg is None for k in knoten.keywords)
            if hat_sternchen:
                schluessel.add("**")
            treffer.append(
                (str(datei.relative_to(WURZEL)), knoten.lineno, schluessel))
    return treffer


def test_the_call_sites_are_found_at_all():
    """🛑 Erst beweisen, dass die Suche greift.

    Ein Strukturtest, der nichts findet, ist immer grün und sagt nichts. Genau
    diese Negativkontrolle fehlte den Tests, die das Weglassen des Parameters
    nicht bemerkten.
    """
    stellen = _aufrufstellen()
    assert len(stellen) >= 4, (
        f"nur {len(stellen)} Einstiegsstellen gefunden — die Suche "
        f"greift nicht mehr, und dann prueft der Test unten nichts: {stellen}")
    dateien = {d for d, _, _ in stellen}
    assert any("chat_handler" in d for d in dateien), (
        f"die Stelle im chat_handler fehlt: {sorted(dateien)}")
    # 🛑 Und die Orchestrator-Stelle, an der die Korrektur zuerst fehlte.
    assert any("orchestrator" in d for d in dateien), (
        f"die Orchestrator-Stelle fehlt: {sorted(dateien)}")
    # Genau zwei Stellen im chat_handler: `agent.run` und `run_orchestrated`.
    im_handler = [z for d, z, _ in stellen if "chat_handler" in d]
    assert len(im_handler) == 2, (
        f"erwartet: agent.run UND run_orchestrated im chat_handler, "
        f"gefunden: {im_handler}")


def test_every_agent_entry_point_forwards_the_correction():
    """Entweder ausdrücklich `corrected_intent=`, oder `**kwargs`."""
    fehlend = [
        f"{datei}:{zeile}"
        for datei, zeile, schluessel in _aufrufstellen()
        if "corrected_intent" not in schluessel
        and "**" not in schluessel
        and datei not in _AUSNAHMEN
    ]
    assert not fehlend, (
        "eine Aufrufstelle des Agenten reicht `corrected_intent` nicht weiter "
        "— dort bleibt eine Nutzerkorrektur still wirkungslos:\n  "
        + "\n  ".join(fehlend))
