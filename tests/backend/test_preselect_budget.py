"""Die Längengrenze der Vorauswahl — je Werkzeug, mit Obergrenze auf der Summe.

🛑 Der Anlass: die Grenze war GLOBAL (480), und die Folge war an den Werkzeugen
ohne Hinweis-Kanal nie gemessen. Gemessen am 2026-10-29 gegen den ECHTEN Prompt
(Signatur `aa62fdb7`, über die Signatur belegt, nicht nachgebaut), zwei
Fallsammlungen, je 3 Wiederholungen:

    alles bei 480                Peer 92 %   Scanner 91 %
    diese vier voll (+589)       Peer 92 %   Scanner 95 %   <- beides
    alle neun voll (+2640)       Peer 85 %   Scanner 95 %

Neun eigene Beschreibungen wurden bei 480 MITTEN IM SATZ abgeschnitten, 2640
Zeichen zusammen — darunter Riegel („it never deletes anything"), Abgrenzungen
zu Nachbarwerkzeugen, deutsche Auslöserbeispiele und eine
Berechtigungsangabe (`system_health`: „Nur-lesend. Admin-Berechtigung.").

🛑 Die volle Aufhebung ist dennoch falsch: sie brach die Negativkontrolle
„suche nach Rechnungen" (3/3 → 0/3). Eine gewöhnliche LOKALE Suche wurde zur
föderierten Anfrage — der Freitext hätte die Instanz verlassen. Bei gleicher
Punktsumme (181 gegen 180) entscheidet die FOLGE: ein Grenzübertritt ohne
Anlass ist schlimmer als ein verfehltes Werkzeug.

🛑 Eine höhere GLOBALE Zahl ist auch falsch: 706 wären nötig, das fügte 1719
Zeichen hinzu statt 589 und schnitte fünf der neun weiter mitten im Satz ab.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.agent_service import (
    _PRESELECT_DESC_BUDGET,
    _PRESELECT_DESC_CHARS,
    preselect_tool_line,
)

pytestmark = pytest.mark.unit

#: Die gemessene Kante. +589 holt vier tragende Enden zurück und hält BEIDE
#: Sammlungen; +829 (eine Stufe mehr) brachte 0 Punkte dazu; +2640 kostete 7.
GEMESSENE_KANTE = 589


def test_the_budget_sum_stays_at_the_MEASURED_edge():
    """🛑 Die Zusicherung gehört auf die SUMME, nicht auf einzelne Werkzeuge:
    geschadet hat die Gesamtlänge (+2640), nicht ein einzelner Text. Und nicht
    auf die Promptlänge: die wächst auch, wenn ein neues Werkzeug mit der
    Vorgabe dazukommt, und das ist harmlos.

    Wer ein Budget anhebt, drückt gegen diese Zahl — die Anhebung wird damit
    eine sichtbare Entscheidung gegen einen Messwert, kein stiller Zuwachs.
    Soll sie steigen, gehört eine neue Messung dazu (`bin/measure-preselect.py`
    gegen BEIDE Fallsammlungen), nicht nur eine größere Zahl hier.
    """
    summe = sum(b - _PRESELECT_DESC_CHARS for b in _PRESELECT_DESC_BUDGET.values())
    assert summe <= GEMESSENE_KANTE, (
        f"Budgets summieren sich auf +{summe} Zeichen, gemessen und belegt sind "
        f"+{GEMESSENE_KANTE}. Bei +2640 fiel die Peer-Sammlung auf 85 % und eine "
        "lokale Suche wurde zur föderierten Anfrage. Erst messen, dann anheben.")


def test_no_budget_is_BELOW_the_default():
    """Ein Budget unter 480 wäre eine Verschärfung durch die Hintertür — die
    Abbildung ist für MEHR Raum da, nicht für weniger."""
    for name, b in _PRESELECT_DESC_BUDGET.items():
        assert b >= _PRESELECT_DESC_CHARS, f"{name}: {b} < {_PRESELECT_DESC_CHARS}"


def test_the_line_builder_USES_the_budget():
    """Behaviorally, nicht strukturell: die Zeile muss den vollen Text tragen."""
    name = "internal.find_duplicate_documents"
    budget = _PRESELECT_DESC_BUDGET[name]
    w = MagicMock()
    w.description, w.hint = "x" * budget, ""
    assert len(preselect_tool_line(name, w)) == len(f"- {name}: ") + budget


def test_a_tool_WITHOUT_a_budget_still_gets_the_default():
    """🛑 Negativkontrolle. Ohne sie prüfte der Test oben nur, dass irgendein
    Text durchkommt — nicht, dass die Vorgabe für alle anderen GILT. Wäre sie
    versehentlich aufgehoben, stünden wieder 2640 Zeichen im Prompt."""
    w = MagicMock()
    w.description, w.hint = "y" * 2000, ""
    zeile = preselect_tool_line("internal.gibt_es_nicht", w)
    assert len(zeile) == len("- internal.gibt_es_nicht: ") + _PRESELECT_DESC_CHARS


def test_the_hint_is_STILL_exempt():
    """Der Steuertext des Betreibers war nie gekappt und bleibt es nicht —
    sonst hätte diese Änderung nebenbei `scan_document` beschnitten, dessen
    Hinweis 1760 Zeichen trägt."""
    w = MagicMock()
    w.description, w.hint = "z" * 1000, "H" * 900
    zeile = preselect_tool_line("mcp.scanner.scan_document", w)
    assert "H" * 900 in zeile
    assert zeile.count("z") == _PRESELECT_DESC_CHARS


class TestTheBudgetedToolsAreActuallyComplete:
    """🛑 Ein Budget, das kleiner ist als sein Text, schneidet weiter ab — nur
    diesmal mit dem Anschein, es sei geregelt. Darum gegen die ECHTEN
    Beschreibungen prüfen, nicht gegen die Zahlen allein.
    """

    @pytest.fixture(scope="class")
    def registry(self):
        from services.agent_tools import AgentToolRegistry

        return AgentToolRegistry(_init_only=True)

    def test_every_budgeted_tool_exists(self, registry):
        """Ein Budget für ein umbenanntes oder entferntes Werkzeug ist toter
        Ballast und verdeckt, dass der echte Text wieder gekappt wird."""
        fehlen = [n for n in _PRESELECT_DESC_BUDGET if n not in registry._tools]
        assert not fehlen, f"Budget ohne Werkzeug: {fehlen}"

    def test_no_budgeted_tool_is_truncated(self, registry):
        """Der Zweck der Abbildung: diese vier sollen VOLLSTAENDIG dastehen."""
        zu_kurz = []
        for name, budget in _PRESELECT_DESC_BUDGET.items():
            w = registry._tools.get(name)
            if w is None:
                continue
            d = str(getattr(w, "description", "") or "")
            h = str(getattr(w, "hint", "") or "")
            if h and d.endswith(h):
                d = d[: len(d) - len(h)].rstrip()
            if len(d) > budget:
                zu_kurz.append((name, len(d), budget))
        assert not zu_kurz, (
            "Budget kleiner als der Text — wird weiter abgeschnitten, nur "
            f"nicht mehr sichtbar: {zu_kurz}. Text kürzen oder (nach einer "
            "Messung) Budget und `GEMESSENE_KANTE` gemeinsam anheben.")
