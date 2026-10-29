"""Die Tokenisierung der Lexikalsuche haelt Umlaute aus.

🛑 Der Anlass, am 2026-10-29: beim Umstellen meiner eigenen Kommentare auf
ASCII habe ich die Zeichenklasse in `_significant_tokens` gleich mit
transliteriert — aus `[A-Za-zAeOeUeaeoeuess0-9]` wurde eine Klasse, die genau
die Buchstaben NICHT mehr matcht, fuer die sie da war. Ein blankes
Suchen-und-Ersetzen gegen eine Datei, ohne nachzusehen, was es trifft.

Gefunden habe ich es beim Nachsehen, nicht durch einen Test: KEIN Test deckte
Umlaute in dieser Funktion ab. Eine deutsche Suche nach „Mietvertrag fuer die
Wohnung in Koeln" waere stillschweigend schlechter geworden — der Fehler haette
keine Ausnahme geworfen, nur weniger gefunden.

Diese Datei schliesst die Luecke. Sie prueft die Funktion, nicht die Schreibweise
der Zeichenklasse: wer sie umbaut, darf das, solange Umlaute weiter durchkommen.
"""
from __future__ import annotations

import pytest

from services.lexical_retrieval import _significant_tokens

pytestmark = pytest.mark.unit


class TestUmlautsSurviveTokenisation:
    @pytest.mark.parametrize("wort", ["Koeln", "Köln", "Grundstück", "Straße",
                                      "Übergabe", "Ärztin", "Öffnungszeiten"])
    def test_a_word_with_an_umlaut_is_one_token(self, wort):
        """🛑 Nicht nur „irgendein Token": das Wort muss GANZ durchkommen.
        Faellt der Umlaut aus der Zeichenklasse, zerfaellt `Grundstück` in
        `Grundst` und `ck` — beides ueberlebt die Mindestlaenge, der Test
        bliebe also gruen, wenn er nur auf „nicht leer" prueft.

        🛑 Die Funktion senkt NICHT die Schreibweise — sie tut das nur fuer den
        Stoppwort-Vergleich und gibt das Token unveraendert zurueck. Meine erste
        Fassung behauptete `[wort.lower()]` und fiel fuer ALLE Woerter, auch
        reines ASCII; die Erwartung war falsch, nicht der Code."""
        assert _significant_tokens(wort) == [wort]

    def test_a_real_german_query_keeps_every_word(self):
        treffer = _significant_tokens("Mietvertrag für die Wohnung in Köln")
        assert "Mietvertrag" in treffer
        assert "Köln" in treffer, "der Umlaut darf das Wort nicht zerlegen"
        assert "Wohnung" in treffer

    def test_the_eszett_is_not_a_separator(self):
        assert _significant_tokens("Hauptstraße") == ["Hauptstraße"]

    def test_plain_ASCII_still_works(self):
        """Negativkontrolle: die Pruefung darf nicht nur Umlaute messen."""
        assert _significant_tokens("rental contract") == ["rental", "contract"]

    def test_short_tokens_are_STILL_dropped(self):
        """Negativkontrolle gegen eine zu weite Zeichenklasse: die
        Mindestlaenge bleibt wirksam."""
        assert "in" not in _significant_tokens("Vertrag in Köln")
