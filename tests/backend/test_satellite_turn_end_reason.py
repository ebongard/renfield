"""The reason a voice turn ended is a SIGNAL -- not log text.

🛑 THE DEFECT THIS FILE IS WRITTEN AGAINST (measured 2026-10-10)
================================================================
The satellite transmits with `audio_end` why it stopped:

    "silence"  -- spoken to the end (VAD silence after the minimum listen time)
    "button"   -- the button
    "timeout"  -- `vad.max_recording_seconds` took effect

The backend accepted the reason (`satellite_handler.py:898`), **logged it and
never read it again**. Consequence: an utterance cut off at the maximum
duration went into transcription like a finished sentence. The speaker got an
answer to half a sentence and was told nothing about it.

Measured in the household (voice-server, 7 days, `Processing audio with
duration`):

    exactly 01:00.080     7x   <- the maximum takes effect, utterance cut off
    35-52 s               5x
    exactly 00:03.120   133x   <- the floor (2.0 s min listen + 1.2 s silence)

The 7 truncations are the silent failure mode. That long commands are carried
at all is shown by the same figures -- the open item was framed wrongly.

This file checks the PRODUCTION helper, not a reconstruction: if the text or
the condition drifts, the run fails.
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
# The notice itself
# ---------------------------------------------------------------------------

def test_kappung_erzeugt_einen_hinweis():
    """The precondition for everything else: on `timeout` text comes out.

    Checked first, so that the negative controls below are not green merely
    because the helper returns empty AS A RULE.
    """
    hinweis = truncation_notice(TURN_END_TIMEOUT, "de")
    assert hinweis, "bei `timeout` muss ein Hinweis entstehen — sonst ist die Kappung stumm"
    assert hinweis.endswith(" "), (
        "der Hinweis wird der Antwort VORAN gestellt und braucht darum ein "
        "trennendes Leerzeichen, sonst klebt er am ersten Wort"
    )


@pytest.mark.parametrize("grund", ["silence", "button", "unknown", None, ""])
def test_vollstaendige_aeusserung_erzeugt_KEINEN_hinweis(grund):
    """🛑 Negative control. `silence` is the NORMAL CASE -- every sentence
    spoken to the end passes through here.

    If the notice fired there too, the house would say on EVERY turn that the
    recording had been cut off. That would be worse than the silence this
    change is built against.
    """
    assert truncation_notice(grund, "de") == ""


def test_sprache_wird_beachtet():
    """The text goes to the satellite's TTS, so it is in that language.

    A German-speaking household member must not hear an English sentence here
    -- the same defect stood four lines from a rework on 2026-09-29 (the
    `device_account` reason stayed English).
    """
    de = truncation_notice(TURN_END_TIMEOUT, "de")
    en = truncation_notice(TURN_END_TIMEOUT, "en")
    assert de != en
    assert "Höchstdauer" in de
    assert "maximum length" in en


def test_unbekannte_sprache_faellt_auf_englisch_zurueck():
    """A language with no text must not return empty -- empty would mean the
    truncation goes mute again."""
    hinweis = truncation_notice(TURN_END_TIMEOUT, "it")
    assert hinweis
    assert "maximum length" in hinweis


def test_gesprochener_text_benutzt_echte_umlaute():
    """🛑 The text is SPOKEN. The TTS would read "Hoechstdauer" aloud as
    "Ho-echst-dauer".

    That was my own mistake in the first draft: an umlaut substitution is
    harmless in a comment and a pronunciation error in a TTS sentence.
    """
    de = truncation_notice(TURN_END_TIMEOUT, "de")

    # A plain substring search does NOT do here: "Höchstdauer" contains "ue"
    # (d-a-u-e-r), and "musste" contains "ss". My first draft of this test
    # would have failed on that -- on the test, not on the text. So: real
    # umlauts must occur, and the substitute spellings OF EXACTLY THE WORDS
    # that stand here must not.
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
# The reason survives into the history
# ---------------------------------------------------------------------------

def test_sitzung_haelt_den_grund_fest():
    """The reason must outlive the session, otherwise it is not in the
    satellite history -- and "was truncated" can no longer be established
    afterwards."""
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
    """`set_turn_end_reason` is the way from the handler into the session.

    An unknown session id must NOT raise: `audio_end` can arrive after a
    timeout or a dropped connection, when the session has already been cleaned
    up. An exception there took the whole turn down with it.
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

    manager.set_turn_end_reason("gibt-es-nicht", TURN_END_TIMEOUT)  # must not raise


# ---------------------------------------------------------------------------
# The handler really uses the helper
# ---------------------------------------------------------------------------

def test_handler_stellt_den_hinweis_VORAN():
    """The order is part of the intent: the notice frames the answer.

    Placed afterwards it would come too late -- the speaker would already have
    heard the answer to their half sentence and taken it for complete.
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
# Half a sentence does not become knowledge (review finding 2026-10-10)
# ---------------------------------------------------------------------------

def test_gekappter_durchlauf_extrahiert_NICHT(monkeypatch):
    """🛑 An utterance cut off mid-word must produce no PERSISTENT memory and
    no KG edge.

    Noticed in review: `response_text` goes to `spawn_memory_extraction` AND
    `spawn_post_message_hooks`. On `timeout` the question is half a sentence --
    and since the truncation notice there would also be device text in it. A
    wrong KG edge is practically impossible to take back; at 7 cases in 7 days
    going without costs almost nothing.
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
        user_text="Überweise an die Stadtkasse dreihundert",   # cut off
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
    """🛑 Negative control, and the more important half: the limit must not
    switch off normal knowledge formation.

    Without this test "nothing extracted" would be green even if the
    extraction no longer runs at all -- and the house would remember nothing
    any more.
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
    """`max_duration_seconds = 30.0` had no reader anywhere in the backend and
    misled about exactly the mechanism at issue here: it read like a
    30-second session limit against a 60-second recording.

    Mirrors `test_device_manager_liveness.py:26`, which asserts the same for
    the sibling class `DeviceSession` -- there it had already been removed.
    The real limit is `settings.device_session_timeout`.
    """
    assert "max_duration_seconds" not in SatelliteSession.__dataclass_fields__
