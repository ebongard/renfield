"""Die Vorauswahl muss protokollieren, WAS sie angeboten bekam — nicht nur wie viel.

🛑 Der Anlass, am 2026-10-26 gemessen. Auf dieselbe Nachricht
(„scanne nach xidra") lieferte die Vorauswahl in neun Zügen zwei verschiedene
Ergebnisse: siebenmal die Scanner-Werkzeuge, zweimal die Dokumentensuche. Das
Protokoll sagte beide Male `3/33`. Die entscheidende Frage war damit NICHT zu
beantworten: hatte sich die Eingabe geändert, oder schwankte das Modell?

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

#: So, wie `_preselect_tools` sie zusammensetzt: eine Zeile je Werkzeug, mit
#: Name, beschnittener Beschreibung und ungekürztem Hinweis.
LISTE = "\n".join([
    "- mcp.scanner.scan_document: Startet einen NEUEN Scan. „scanne nach <X>“ "
    "bedeutet beim PAPIERSCANNER IMMER, dass <X> das ZIEL ist.",
    "- mcp.paperless.search_documents: Search archived documents by text.",
    "- internal.knowledge_search: Search the knowledge base.",
])


def _prompt(lang: str, tool_list: str, message: str = "scanne nach xidra") -> str:
    """Der Prompt, wie die PRODUKTION ihn baut — über den ECHTEN prompt_manager.

    🛑 Kein Nachbau der Vorlage. Täte dieser Test das, prüfte der Sprachtest
    meine eigene Zeichenkette statt der ausgelieferten `de`- und `en`-Vorlage,
    und die Abdeckung der Nachricht wäre behauptet statt gemessen.
    """
    from services.prompt_manager import prompt_manager

    fertig = prompt_manager.get(
        "agent", "tool_preselect_prompt", lang=lang,
        message=message, tool_list=tool_list,
    )
    assert fertig, f"keine `tool_preselect_prompt`-Vorlage fuer lang={lang!r}"
    return fertig


def test_the_same_input_gives_the_same_signature():
    """Sonst wäre ein Vergleich zwischen zwei Zügen wertlos."""
    assert preselect_input_signature(_prompt("de", LISTE)) == \
        preselect_input_signature(_prompt("de", str(LISTE)))


def test_a_changed_DESCRIPTION_changes_the_signature():
    """🛑 Der Fehler der ERSTEN Fassung: sie hashte nur die Namen.

    In den Prompt geht `tool_list` ein, also Name PLUS beschnittene
    Beschreibung PLUS ungekürzter Hinweis. Ein geänderter Hinweis — etwa durch
    einen ConfigMap-Patch, wie am 2026-10-26 geschehen — stellte den Prompt um
    und ließ die Signatur unberührt.
    """
    geaendert = LISTE.replace("das ZIEL ist", "ein Suchbegriff ist")
    assert geaendert != LISTE, "der Test soll den Hinweistext wirklich ändern"
    # Dieselben Namen, anderer Text:
    assert [z.split(":")[0] for z in geaendert.splitlines()] == \
           [z.split(":")[0] for z in LISTE.splitlines()]
    assert preselect_input_signature(_prompt("de", geaendert)) != \
        preselect_input_signature(_prompt("de", LISTE)), (
        "die Signatur ignoriert den Beschreibungstext — dann bleibt ein "
        "geaenderter Hinweis unsichtbar, obwohl er den Prompt umstellt")


def test_a_MESSAGE_that_differs_only_after_60_characters_changes_the_signature():
    """🛑 Der Fehler der ZWEITEN Fassung: sie hashte `lang` + `tool_list`.

    Die NACHRICHT fehlte — und die Protokollzeile zeigt nur `message[:60]`.
    Zwei Züge, die in den ersten 60 Zeichen übereinstimmen und danach
    auseinandergehen, hätten damit identische Nachricht UND identische Signatur
    über zwei VERSCHIEDENE Prompts gezeigt. Genau die Aussage, die der
    Fingerabdruck tragen soll, wäre falsch gewesen. Diktierte Sprachzüge
    überschreiten 60 Zeichen regelmäßig.

    Im Review zu #1413 gefunden, bevor es gemergt wurde.
    """
    vorsilbe = "scanne bitte die Seiten aus dem Einzug ein und lege sie dann ab"
    assert len(vorsilbe) > 60, "die Vorsilbe muss die 60 Zeichen ueberschreiten"
    a = _prompt("de", LISTE, message=f"{vorsilbe} in der Instanz xidra")
    b = _prompt("de", LISTE, message=f"{vorsilbe} in der Instanz haushalt")
    assert preselect_input_signature(a) != preselect_input_signature(b), (
        "die Signatur ignoriert die Nachricht — dann behauptet sie Gleichheit "
        "ueber zwei verschiedene Prompts, und die Zeile zeigt wegen "
        "`message[:60]` denselben Anfang")


def test_a_different_LANGUAGE_changes_the_signature():
    """`tool_preselect_prompt` existiert in de UND en — zwei verschiedene
    Vorlagen. Eine Signatur, die das verschweigt, behauptet Gleichheit über
    zwei verschiedene Prompts hinweg."""
    assert preselect_input_signature(_prompt("en", LISTE)) != \
        preselect_input_signature(_prompt("de", LISTE)), (
        "die Signatur ignoriert die Sprache")


def test_a_different_ORDER_changes_the_signature():
    """🛑 `get_tool_names()` gibt `list(self._tools.keys())`, also die
    EINFÜGEREIHENFOLGE eines dict — und die ändert sich, wenn Werkzeuge neu
    registriert werden (`refresh_interval: 300` auf den MCP-Servern).

    Eine Signatur, die nur die MENGE abbildet, hätte genau den Fall nicht
    gesehen, für den sie gebaut wurde.
    """
    zeilen = LISTE.splitlines()
    gedreht = "\n".join(reversed(zeilen))
    assert sorted(gedreht.splitlines()) == sorted(zeilen), \
        "der Test soll nur die Reihenfolge ändern"
    assert preselect_input_signature(_prompt("de", gedreht)) != \
        preselect_input_signature(_prompt("de", LISTE)), (
        "die Signatur ignoriert die Reihenfolge — dann bleibt eine "
        "Neuregistrierung unsichtbar, und genau die stand am 2026-10-26 im "
        "Verdacht")


def test_an_added_or_removed_tool_changes_the_signature():
    """Ein Server, der weggefallen oder hinzugekommen ist, muss auffallen."""
    zeilen = LISTE.splitlines()
    assert preselect_input_signature(_prompt("de", "\n".join(zeilen[:-1]))) != \
        preselect_input_signature(_prompt("de", LISTE))
    assert preselect_input_signature(_prompt("de", LISTE + "\n- mcp.email.send: Send mail.")) != \
        preselect_input_signature(_prompt("de", LISTE))


def test_the_signature_fits_into_a_log_line():
    """Sie steht neben der Nachricht in einer Zeile — kurz und als Wort lesbar.

    🛑 Auf die FORM geprüft, nicht auf eine Zahl: `len == 8` würde nur die
    aktuelle Wahl festnageln, ohne etwas über die Eignung zu sagen. Geprüft
    wird, was die Zeile braucht.
    """
    sig = preselect_input_signature(_prompt("de", LISTE))
    assert sig.isalnum(), f"nicht als Wort lesbar: {sig!r}"
    assert 6 <= len(sig) <= 16, f"fuer eine Protokollzeile unbrauchbar: {sig!r}"


def test_an_empty_list_does_not_raise():
    """Die Vorauswahl läuft bei <= 6 Werkzeugen gar nicht an, aber die Funktion
    darf trotzdem nicht werfen — ein Protokollpfad, der stürzen kann, nimmt im
    Zweifel die Diagnose mit."""
    assert preselect_input_signature(_prompt("de", ""))


def test_two_tools_that_differ_only_in_a_namespace_are_distinguished():
    """Kein Abschneiden auf den Kurznamen: `mcp.a.x` und `mcp.b.x` sind
    verschiedene Werkzeuge."""
    assert preselect_input_signature(_prompt("de", "- mcp.scanner.scan_document: X")) != \
        preselect_input_signature(_prompt("de", "- mcp.paperless.scan_document: X"))
