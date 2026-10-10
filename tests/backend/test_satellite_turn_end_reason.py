"""Der Grund, aus dem ein Sprachdurchlauf endete, ist ein SIGNAL — kein Protokolltext.

🛑 DER FEHLER, GEGEN DEN DIESE DATEI GESCHRIEBEN IST (gemessen 2026-10-10)
=========================================================================
Der Satellit überträgt mit `audio_end`, warum er aufgehört hat:

    "silence"  — zu Ende gesprochen (VAD-Stille nach der Mindesthorchzeit)
    "button"   — Taste
    "timeout"  — `vad.max_recording_seconds` hat gegriffen

Das Backend nahm den Grund an (`satellite_handler.py:898`), **protokollierte ihn
und las ihn nie wieder**. Folge: eine bei der Höchstdauer gekappte Äußerung ging
wie ein fertiger Satz in die Transkription. Der Sprechende bekam eine Antwort auf
einen halben Satz und erfuhr nichts davon.

Am Haushalt gemessen (voice-server, 7 Tage, `Processing audio with duration`):

    exakt 01:00.080     7×   ← die Höchstdauer greift, Äußerung abgeschnitten
    35–52 s             5×
    exakt 00:03.120   133×   ← der Boden (2,0 s Mindesthorchen + 1,2 s Stille)

Die 7 Kappungen sind der stumme Fehlermodus. Dass lange Befehle überhaupt
getragen werden, belegen dieselben Zahlen — der offene Posten war falsch gefasst.

Diese Datei prüft den PRODUKTIVEN Helfer, keinen Nachbau: driftet der Text oder
die Bedingung, fällt der Lauf.
"""

from __future__ import annotations

import pytest

from ha_glue.api.websocket.satellite_handler import (
    TURN_END_TIMEOUT,
    _spawn_satellite_extraction,
    truncation_notice,
)
from ha_glue.services.satellite_manager import SatelliteSession, SatelliteState

pytestmark = [pytest.mark.unit, pytest.mark.backend]


# ---------------------------------------------------------------------------
# Der Hinweis selbst
# ---------------------------------------------------------------------------

def test_kappung_erzeugt_einen_hinweis():
    """Die Vorbedingung für alles andere: bei `timeout` kommt Text heraus.

    Wird zuerst geprüft, damit die Negativkontrollen unten nicht grün sind,
    weil der Helfer GRUNDSÄTZLICH leer liefert.
    """
    hinweis = truncation_notice(TURN_END_TIMEOUT, "de")
    assert hinweis, "bei `timeout` muss ein Hinweis entstehen — sonst ist die Kappung stumm"
    assert hinweis.endswith(" "), (
        "der Hinweis wird der Antwort VORAN gestellt und braucht darum ein "
        "trennendes Leerzeichen, sonst klebt er am ersten Wort"
    )


@pytest.mark.parametrize("grund", ["silence", "button", "unknown", None, ""])
def test_vollstaendige_aeusserung_erzeugt_KEINEN_hinweis(grund):
    """🛑 Negativkontrolle. `silence` ist der NORMALFALL — jeder zu Ende
    gesprochene Satz läuft hier durch.

    Feuerte der Hinweis auch dort, sagte das Haus bei JEDEM Durchlauf, die
    Aufnahme sei abgebrochen. Das wäre schlimmer als das Schweigen, gegen das
    diese Änderung gebaut ist.
    """
    assert truncation_notice(grund, "de") == ""


def test_sprache_wird_beachtet():
    """Der Text geht an die TTS des Satelliten, also in dessen Sprache.

    Ein deutschsprachiges Haushaltsmitglied darf hier keinen englischen Satz
    hören — derselbe Fehler stand am 2026-09-29 vier Zeilen neben einem
    Umbau (`device_account`-Grund blieb englisch).
    """
    de = truncation_notice(TURN_END_TIMEOUT, "de")
    en = truncation_notice(TURN_END_TIMEOUT, "en")
    assert de != en
    assert "Höchstdauer" in de
    assert "maximum length" in en


def test_unbekannte_sprache_faellt_auf_englisch_zurueck():
    """Eine Sprache, für die es keinen Text gibt, darf nicht leer liefern —
    leer hieße: die Kappung wird wieder stumm."""
    hinweis = truncation_notice(TURN_END_TIMEOUT, "it")
    assert hinweis
    assert "maximum length" in hinweis


def test_gesprochener_text_benutzt_echte_umlaute():
    """🛑 Der Text wird GESPROCHEN. „Hoechstdauer" läse die TTS als
    „Ho-echst-dauer" vor.

    Das war mein eigener Fehler im ersten Entwurf: Ersatzschreibung ist in
    einem Kommentar harmlos und in einem TTS-Satz ein Aussprachefehler.
    """
    de = truncation_notice(TURN_END_TIMEOUT, "de")

    # Eine reine Buchstabenfolgen-Suche taugt hier NICHT: „Höchstdauer" enthält
    # „ue" (d-a-u-e-r), „musste" enthält „ss". Mein erster Entwurf dieses Tests
    # wäre daran gescheitert — am Test, nicht am Text. Darum: echte Umlaute
    # müssen vorkommen, und die Ersatzschreibungen GENAU DER WÖRTER, die hier
    # stehen, dürfen es nicht.
    assert any(zeichen in de for zeichen in "äöüÄÖÜß"), (
        f"kein echter Umlaut im gesprochenen Text: {de!r}"
    )
    de_klein = de.lower()
    for ersatz in ("hoechst", "gehoert", "aeusserung", "fuer", "ueber"):
        assert ersatz not in de_klein, (
            f"Ersatzschreibung {ersatz!r} im gesprochenen Text: {de!r} — "
            "die TTS spräche das falsch aus"
        )


# ---------------------------------------------------------------------------
# Der Grund überlebt bis in die Historie
# ---------------------------------------------------------------------------

def test_sitzung_haelt_den_grund_fest():
    """Der Grund muss die Sitzung überleben, sonst steht er nicht in der
    Satelliten-Historie — und „wurde gekappt" ist hinterher nicht mehr
    nachweisbar."""
    sitzung = SatelliteSession(
        session_id="sat-test-1",
        satellite_id="sat-test",
        room="Prüfraum",
        state=SatelliteState.LISTENING,
    )
    assert sitzung.turn_end_reason is None, "ohne Meldung kein Grund"

    sitzung.turn_end_reason = TURN_END_TIMEOUT
    assert sitzung.turn_end_reason == TURN_END_TIMEOUT


def test_manager_setzt_den_grund_und_vertraegt_eine_unbekannte_sitzung():
    """`set_turn_end_reason` ist der Weg aus dem Handler in die Sitzung.

    Eine unbekannte Sitzungs-Id darf NICHT werfen: `audio_end` kann nach einem
    Zeitablauf oder einem Verbindungsabbruch eintreffen, wenn die Sitzung schon
    abgeräumt ist. Eine Ausnahme dort riss den ganzen Durchlauf mit.
    """
    from ha_glue.services.satellite_manager import SatelliteManager

    manager = SatelliteManager()
    sitzung = SatelliteSession(
        session_id="sat-test-2",
        satellite_id="sat-test",
        room="Prüfraum",
        state=SatelliteState.LISTENING,
    )
    manager.sessions["sat-test-2"] = sitzung

    manager.set_turn_end_reason("sat-test-2", TURN_END_TIMEOUT)
    assert sitzung.turn_end_reason == TURN_END_TIMEOUT

    manager.set_turn_end_reason("gibt-es-nicht", TURN_END_TIMEOUT)  # darf nicht werfen


# ---------------------------------------------------------------------------
# Der Handler benutzt den Helfer wirklich
# ---------------------------------------------------------------------------

def test_handler_stellt_den_hinweis_VORAN():
    """Die Reihenfolge ist Teil der Absicht: der Hinweis ordnet die Antwort ein.

    Hinterher gestellt käme er zu spät — der Sprechende hätte die Antwort auf
    seinen halben Satz schon gehört und für vollständig genommen.
    """
    import inspect

    from ha_glue.api.websocket import satellite_handler

    quelle = inspect.getsource(satellite_handler)
    assert "response_text = _cut_notice + response_text" in quelle, (
        "der Hinweis muss VORAN gestellt werden, nicht angehängt"
    )
    assert "truncation_notice(reason, satellite_language)" in quelle, (
        "der Handler muss den produktiven Helfer benutzen, keinen eigenen Text"
    )
    assert "satellite_manager.set_turn_end_reason(session_id, reason)" in quelle, (
        "der Grund muss festgehalten werden, nicht nur protokolliert — "
        "genau das war der Fehler"
    )


# ---------------------------------------------------------------------------
# Ein halber Satz wird kein Wissen (Review-Befund 2026-10-10)
# ---------------------------------------------------------------------------

def test_gekappter_durchlauf_extrahiert_NICHT(monkeypatch):
    """🛑 Aus einer mitten im Wort abgeschnittenen Äußerung darf keine
    DAUERHAFTE Erinnerung und keine KG-Kante entstehen.

    Im Review aufgefallen: `response_text` geht nach `spawn_memory_extraction`
    UND `spawn_post_message_hooks`. Bei `timeout` ist die Frage ein halber Satz
    — und seit dem Kappungshinweis stünde zusätzlich Geräte-Text darin. Eine
    falsche KG-Kante ist praktisch nicht zurückzunehmen; der Verzicht kostet
    bei 7 Fällen in 7 Tagen fast nichts.
    """
    gerufen: list[str] = []
    monkeypatch.setattr(
        "ha_glue.api.websocket.satellite_handler.spawn_memory_extraction",
        lambda **kw: gerufen.append("memory"),
    )
    monkeypatch.setattr(
        "ha_glue.api.websocket.satellite_handler.spawn_post_message_hooks",
        lambda **kw: gerufen.append("post_message"),
    )

    ergebnis = _spawn_satellite_extraction(
        user_text="Überweise an die Stadtkasse dreihundert",   # abgeschnitten
        response_text="Ich musste die Aufnahme … ",
        user_id=1,
        session_id="sess-1",
        lang="de",
        action_success=True,
        turn_truncated=True,
    )
    assert ergebnis is None
    assert gerufen == [], (
        f"ein gekappter Satz darf nichts extrahieren, gerufen wurde: {gerufen}"
    )


def test_vollstaendiger_durchlauf_extrahiert_WEITERHIN(monkeypatch):
    """🛑 Negativkontrolle, und die wichtigere Hälfte: die Grenze darf die
    normale Wissensbildung nicht abschalten.

    Ohne diesen Test wäre „nichts extrahiert" grün, auch wenn die Extraktion
    gar nicht mehr läuft — und das Haus würde sich nichts mehr merken.
    """
    gerufen: list[str] = []

    class _Spawn:
        owns_post_message = False

    monkeypatch.setattr(
        "ha_glue.api.websocket.satellite_handler.spawn_memory_extraction",
        lambda **kw: (gerufen.append("memory"), _Spawn())[1],
    )
    monkeypatch.setattr(
        "ha_glue.api.websocket.satellite_handler.spawn_post_message_hooks",
        lambda **kw: gerufen.append("post_message"),
    )

    ergebnis = _spawn_satellite_extraction(
        user_text="Stelle den Wecker auf sieben Uhr",
        response_text="Ist gestellt.",
        user_id=1,
        session_id="sess-2",
        lang="de",
        action_success=True,
        turn_truncated=False,
    )
    assert ergebnis is not None
    assert gerufen == ["memory", "post_message"]


def test_tote_sitzungsgrenze_ist_weg():
    """`max_duration_seconds = 30.0` hatte im ganzen Backend keinen Leser und
    täuschte über genau den Mechanismus, um den es hier geht: es las sich wie
    eine 30-Sekunden-Sitzungsgrenze gegen eine 60-Sekunden-Aufnahme.

    Spiegelt `test_device_manager_liveness.py:26`, das dasselbe für die
    Schwesterklasse `DeviceSession` behauptet — dort war es schon entfernt.
    Die echte Grenze ist `settings.device_session_timeout`.
    """
    assert "max_duration_seconds" not in SatelliteSession.__dataclass_fields__
