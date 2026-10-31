"""The rule for when a live history is reloaded.

🛑 The occasion, measured on 2026-10-26: the owner asked "scanne nach Haushalt"
three times and twice got "der Scan läuft bereits im Hintergrund, Du wirst
benachrichtigt" -- without the tool being called. The first scan had failed on a
paper jam, and the notice was in the database. Except: a live session's history
is loaded ONCE per socket and after that grows only through `add_to_history`. An
asynchronous producer -- the scanner completion notice, the conversation
handover, the Paperless confirmation -- does not touch that list. So the agent
read a context in which the scan was still running.

The rule is a PURE function, so that exactly this is testable instead of being
buried in the WebSocket handler.
"""
import pytest

from api.websocket.shared import history_needs_reload

pytestmark = [pytest.mark.unit, pytest.mark.backend]


def _needs_reload(**kw):
    basis = dict(history_loaded=True, known_session_id="s1", wanted_session_id="s1",
                 watermark=100, newest_in_db=100)
    return history_needs_reload(**{**basis, **kw})


def test_a_foreign_write_triggers_a_reload():
    """The case that blocked the scan."""
    assert _needs_reload(watermark=100, newest_in_db=101) is True


def test_an_unchanged_database_does_not_reload():
    """The normal case: nothing foreign written, no reload -- otherwise the
    check would have turned into a permanent reload cycle."""
    assert _needs_reload(watermark=100, newest_in_db=100) is False


def test_the_first_message_always_loads():
    assert _needs_reload(history_loaded=False) is True


def test_a_session_switch_always_loads():
    assert _needs_reload(known_session_id="s1", wanted_session_id="s2") is True


def test_no_answer_from_the_database_does_not_reload():
    """🛑 `None` means "no answer", not "stale".

    Turning that into a reload would mean: on every database hiccup the check
    fires on EVERY turn. A missing measurement must not force a cycle -- the
    same rule as for the scanner's `ioreg`.
    """
    assert _needs_reload(newest_in_db=None) is False


def test_a_missing_watermark_does_not_reload():
    """The mark is only absent when reading the number failed. A permanent
    reload cycle would be worse than the state it prevents."""
    assert _needs_reload(watermark=None, newest_in_db=500) is False


def test_an_older_database_id_does_not_reload():
    """Lower than the mark can only arise through deletion -- that is not a
    foreign write and does not justify a reload."""
    assert _needs_reload(watermark=100, newest_in_db=99) is False
