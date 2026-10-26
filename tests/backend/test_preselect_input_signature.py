"""Die Vorauswahl muss protokollieren, WAS sie angeboten bekam — nicht nur wie viel.

🛑 Der Anlass, am 2026-10-26 gemessen. Auf dieselbe Nachricht
(„scanne nach xidra") lieferte die Vorauswahl in neun Zügen zwei verschiedene
Ergebnisse: siebenmal die Scanner-Werkzeuge, zweimal die Dokumentensuche. Das
Protokoll sagte beide Male `3/33` — und damit war die entscheidende Frage NICHT
zu beantworten: hatte sich die Eingabe geändert, oder schwankte das Modell?

Drei Fehldiagnosen gingen auf diese Lücke:

1. „Die Temperatur würfelt." Nachgemessen: live `temperature: 0`, und zehn
   identische Anfragen gaben zehn identische Antworten. Widerlegt.
2. „Die Position in der Liste entscheidet." Mit vollständigem Hinweistext
   nachgemessen: 14 von 15 Positionen wählen das richtige Werkzeug. Die
   gegenteilige erste Messung benutzte einen HINWEIS, den ich selbst mitten im
   Satz abgeschnitten hatte — ein Artefakt meiner Testdaten, keine Eigenschaft
   des Systems.
3. Darauf gebaut ein neuer Prompt („Position bedeutet nichts, gehe die Liste
   durch"). Gegen das echte Modell gemessen: 7 von 15 statt 14 von 15 — die
   Reparatur wäre eine VERSCHLECHTERUNG gewesen. Nicht ausgerollt.

Was bei allen drei Versuchen fehlte, war dasselbe: die Eingabe. Dieser
Fingerabdruck liefert sie in acht Zeichen.
"""
import pytest

from services.agent_service import preselect_input_signature

pytestmark = [pytest.mark.unit, pytest.mark.backend]

LISTE = [
    "mcp.scanner.scan_document",
    "mcp.paperless.search_documents",
    "internal.knowledge_search",
]


def test_the_same_list_gives_the_same_signature():
    """Sonst wäre ein Vergleich zwischen zwei Zügen wertlos."""
    assert preselect_input_signature(LISTE) == preselect_input_signature(list(LISTE))


def test_a_different_ORDER_gives_a_different_signature():
    """🛑 Das ist der Kern. `get_tool_names()` gibt `list(self._tools.keys())`,
    also die EINFÜGEREIHENFOLGE eines dict — und die ändert sich, wenn Werkzeuge
    neu registriert werden (`refresh_interval: 300` auf den MCP-Servern).

    Eine Signatur, die nur die MENGE abbildet, hätte genau den Fall nicht
    gesehen, für den sie gebaut wurde.
    """
    gedreht = list(reversed(LISTE))
    assert sorted(gedreht) == sorted(LISTE), "der Test soll nur die Reihenfolge ändern"
    assert preselect_input_signature(gedreht) != preselect_input_signature(LISTE), (
        "die Signatur ignoriert die Reihenfolge — dann bleibt eine "
        "Neuregistrierung unsichtbar, und genau die stand am 2026-10-26 im "
        "Verdacht")


def test_an_added_or_removed_tool_changes_the_signature():
    """Ein Server, der weggefallen oder hinzugekommen ist, muss auffallen."""
    assert preselect_input_signature(LISTE[:-1]) != preselect_input_signature(LISTE)
    assert preselect_input_signature([*LISTE, "mcp.email.send"]) != \
        preselect_input_signature(LISTE)


def test_the_signature_is_short_and_stable_in_shape():
    """Sie steht in einer Protokollzeile neben der Nachricht — sie muss kurz
    sein und darf die Zeile nicht sprengen."""
    sig = preselect_input_signature(LISTE)
    assert len(sig) == 8, f"unerwartete Länge: {sig!r}"
    assert sig.isalnum(), f"nicht als Wort lesbar: {sig!r}"


def test_an_empty_list_does_not_raise():
    """Die Vorauswahl läuft bei <= 6 Werkzeugen gar nicht an, aber die Funktion
    darf trotzdem nicht werfen — ein Protokollpfad, der stürzen kann, nimmt im
    Zweifel die Diagnose mit."""
    assert preselect_input_signature([])


def test_two_lists_that_differ_only_in_a_namespace_are_distinguished():
    """Kein Abschneiden auf den Kurznamen: `mcp.a.x` und `mcp.b.x` sind
    verschiedene Werkzeuge."""
    a = ["mcp.scanner.scan_document"]
    b = ["mcp.paperless.scan_document"]
    assert preselect_input_signature(a) != preselect_input_signature(b)
