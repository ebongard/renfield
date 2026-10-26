"""Die Regel, wann eine lebende Historie neu geladen wird.

🛑 Der Anlass, gemessen am 2026-10-26: der Eigentümer bat dreimal „scanne nach
Haushalt" und bekam zweimal „der Scan läuft bereits im Hintergrund, Du wirst
benachrichtigt" — ohne dass das Werkzeug gerufen wurde. Der erste Scan war am
Papierstau gescheitert, die Meldung stand in der Datenbank. Nur: die Historie
einer lebenden Sitzung wird EINMAL je Socket geladen und wächst danach allein
über `add_to_history`. Ein asynchroner Erzeuger — Scanner-Fertigmeldung,
Gesprächsübergabe, Paperless-Bestätigung — fasst diese Liste nicht an. Der
Agent las also einen Kontext, in dem der Scan noch lief.

Die Regel ist eine REINE Funktion, damit genau das prüfbar ist, statt im
WebSocket-Handler zu stecken.
"""
import pytest

from api.websocket.shared import history_needs_reload

pytestmark = [pytest.mark.unit, pytest.mark.backend]


def _regel(**kw):
    basis = dict(history_loaded=True, known_session_id="s1", wanted_session_id="s1",
                 watermark=100, newest_in_db=100)
    return history_needs_reload(**{**basis, **kw})


def test_a_foreign_write_triggers_a_reload():
    """Der Fall, der den Scan blockierte."""
    assert _regel(watermark=100, newest_in_db=101) is True


def test_an_unchanged_database_does_not_reload():
    """Der Normalfall: nichts Fremdes geschrieben, kein Neuladen — sonst wäre
    aus der Prüfung ein Dauer-Neuladezyklus geworden."""
    assert _regel(watermark=100, newest_in_db=100) is False


def test_the_first_message_always_loads():
    assert _regel(history_loaded=False) is True


def test_a_session_switch_always_loads():
    assert _regel(known_session_id="s1", wanted_session_id="s2") is True


def test_no_answer_from_the_database_does_not_reload():
    """🛑 `None` heisst „keine Auskunft", nicht „veraltet".

    Daraus ein Neuladen zu machen hiesse: bei jeder Störung der Datenbank
    schlägt die Prüfung bei JEDEM Zug zu. Eine fehlende Messung darf keinen
    Zyklus erzwingen — dieselbe Regel wie beim `ioreg` des Scanners.
    """
    assert _regel(newest_in_db=None) is False


def test_a_missing_watermark_does_not_reload():
    """Die Marke fehlt nur, wenn das Lesen der Zahl scheiterte. Ein
    Dauer-Neuladezyklus wäre schlimmer als der Zustand, den sie verhindert."""
    assert _regel(watermark=None, newest_in_db=500) is False


def test_an_older_database_id_does_not_reload():
    """Kleiner als die Marke kann nur durch Löschen entstehen — das ist kein
    fremder Schreibvorgang und rechtfertigt kein Neuladen."""
    assert _regel(watermark=100, newest_in_db=99) is False
