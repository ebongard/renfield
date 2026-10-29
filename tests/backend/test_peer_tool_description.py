"""Die Beschreibung des Peer-Werkzeugs — gemessen, nicht geraten.

🛑 Was gemessen wurde, wird ausgeliefert, **byteweise**. Darum steht die
gemessene Fassung hier als Literal: ändert jemand den Text im Code, wird dieser
Test rot und verlangt eine neue Messung mit `bin/measure-preselect.py`, statt
dass eine ungemessene Fassung still live geht. Vier Review-Runden am Hinweistext
ohne Messung haben in diesem Projekt am 2026-10-27 acht Messrunden gekostet.

Gemessen am 2026-10-29 gegen den ECHTEN Prompt des Live-Zuges (Signatur
`aa62fdb7`, über die Signatur als Orakel belegt, nicht nachgebaut):

    alte Fassung (nur die Person)   27/39   69 %
    diese Fassung                   36/39   92 %

Die alte nannte nur die PERSON. „frag <jemand>s Renfield" traf damit 3/3 — das
Modell erkennt das Muster, nicht den Namen (auch „frag Mamas renfield" traf,
obwohl Mama nicht der Anzeigename ist). 0/3 traf jede Frage, die die INSTANZ
nennt oder die MEHRZAHL verlangt; diese drei sind jetzt 3/3.

Offen geblieben (0/3, auch mit drei verschiedenen Dokumentformulierungen):
„Welche Dokumente sind in der x-idra instanz heute angelegt worden?" — dafür
müsste der INSTANZNAME in der Beschreibung stehen, und er ist nicht verfügbar.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.agent_service import _PRESELECT_DESC_CHARS, preselect_tool_line
from services.peer_mcp_registry import _build_query_brain_tool

pytestmark = pytest.mark.unit

#: Die Fassung, die 36/39 gemessen hat — byteweise, mit `evdb` als Anzeigename.
GEMESSEN = (
    "Ask evdb's Renfield — another paired INSTANCE — for information its "
    "people chose to share with you through circles. Use for „frag <jemand>s "
    "Renfield“, „was liegt in <Instanz>“, „die andere Instanz“ "
    "and „in allen Renfields suchen“. NOT for scanning paper: in "
    "„scanne nach <Ziel>“ <Ziel> is a filing target, not an instance."
)


def _peer(name: str = "evdb") -> MagicMock:
    p = MagicMock()
    p.id, p.remote_display_name = 3, name
    return p


def test_the_shipped_description_is_the_MEASURED_one_byte_for_byte():
    """🛑 Der Riegel gegen eine ungemessene Änderung. Wird dieser Test rot,
    ist das kein Formfehler: der Text wurde geändert, ohne neu zu messen."""
    assert _build_query_brain_tool(_peer()).description == GEMESSEN


def test_it_names_the_INSTANCE_not_only_the_person():
    """Die Lücke, die 0/3 kostete: drei Fragearten fanden keinen Anker."""
    d = _build_query_brain_tool(_peer()).description
    assert "INSTANCE" in d
    assert "andere Instanz" in d
    assert "allen Renfields" in d


def test_it_keeps_the_pattern_that_ALREADY_worked():
    """🛑 Negativkontrolle gegen eine Verschlechterung: „frag <jemand>s
    Renfield" traf schon mit der alten Fassung 3/3. Ein neuer Text, der das
    verliert, wäre in der Gesamtzahl kaum sichtbar und in der Benutzung sehr."""
    d = _build_query_brain_tool(_peer()).description
    assert "Renfield" in d
    assert "frag <jemand>s Renfield" in d


def test_the_scanner_boundary_is_in_the_text():
    """🛑 „scanne nach x-idra" nennt ein ABLAGEZIEL, keine Instanz, und muss
    beim Scanner bleiben (gemessen 3/3). Der Satz, der das trägt, steht am
    ENDE — siehe den Kappungstest darunter."""
    d = _build_query_brain_tool(_peer()).description
    assert "NOT for scanning paper" in d
    assert "filing target, not an instance" in d


class TestTheCapCannotEatTheBoundary:
    """🛑 Die Beschreibung wird bei `_PRESELECT_DESC_CHARS` gekappt, und der
    Scanner-Abgrenzungssatz steht am Ende. Ein langer Anzeigename schiebt
    genau ihn über die Kante — und dann wird aus „scanne nach x-idra" wieder
    eine Peer-Anfrage. Der Name kommt von der GEGENSEITE
    (`offer.display_name`), ist also nicht unter eigener Kontrolle.
    """

    @pytest.mark.parametrize("name", ["evdb", "x" * 200, "Ü" * 300, ""])
    def test_the_description_stays_under_the_cap(self, name):
        d = _build_query_brain_tool(_peer(name)).description
        assert len(d) <= _PRESELECT_DESC_CHARS, (
            f"{len(d)} Zeichen bei einem {len(name)}-Zeichen-Namen — die Kappung "
            "schneidet den Scanner-Satz ab")

    @pytest.mark.parametrize("name", ["evdb", "x" * 200, "Ü" * 300])
    def test_the_boundary_survives_the_line_build(self, name):
        """Nicht die Beschreibung allein prüfen, sondern die ZEILE, die der
        Prompt wirklich trägt — gebaut mit der Produktionsfunktion, nicht
        nachgestellt."""
        werkzeug = _build_query_brain_tool(_peer(name))
        zeile = preselect_tool_line(werkzeug.namespaced_name, werkzeug)
        assert "filing target, not an instance" in zeile

    def test_a_long_name_is_SHORTENED_not_the_sentence(self):
        """Negativkontrolle zur Kappung: sie greift am NAMEN, nicht am Satz.
        Ohne diese Prüfung wäre nicht zu sehen, ob der Test oben grün ist,
        weil der Name gekürzt wurde oder weil der Satz verschwand."""
        d = _build_query_brain_tool(_peer("x" * 200)).description
        assert "x" * 200 not in d
        assert d.startswith("Ask " + "x" * 40 + "'s Renfield")
