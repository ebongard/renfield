"""Tests for media transport shortcut detection.

Tests the regex-based _detect_media_transport() function that allows simple
media commands (stop, pause, resume, next, previous) to bypass the agent loop.

NOTE: Imports from api.websocket trigger services.database which needs asyncpg.
We mock missing modules in sys.modules before importing.
"""
import sys
from unittest.mock import MagicMock

_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import pytest

from api.websocket.chat_handler import _detect_media_transport


# ---------------------------------------------------------------------------
# Stop variants
# ---------------------------------------------------------------------------

class TestDetectStop:
    @pytest.mark.parametrize("msg", [
        "Stop",
        "stop",
        "Stopp",
        "stopp",
        "Halt",
        "Stopp die Musik",
        "Musik aus",
        "Beende die Musik",
    ])
    def test_stop_variants(self, msg):
        result = _detect_media_transport(msg)
        assert result is not None
        action, room = result
        assert action == "stop"
        assert room is None


# ---------------------------------------------------------------------------
# Pause variants
# ---------------------------------------------------------------------------

class TestDetectPause:
    @pytest.mark.parametrize("msg", [
        "Pause",
        "pause",
        "Pausiere",
        "pausiere",
    ])
    def test_pause_variants(self, msg):
        result = _detect_media_transport(msg)
        assert result is not None
        action, room = result
        assert action == "pause"
        assert room is None


# ---------------------------------------------------------------------------
# Resume variants
# ---------------------------------------------------------------------------

class TestDetectResume:
    @pytest.mark.parametrize("msg", [
        "Weiter abspielen",
        "weiter abspielen",
        "Weiter spielen",
        "Fortsetzen",
        "fortsetzen",
        "Resume",
        "resume",
        "Play",
        "play",
    ])
    def test_resume_variants(self, msg):
        result = _detect_media_transport(msg)
        assert result is not None
        action, room = result
        assert action == "resume"
        assert room is None


# ---------------------------------------------------------------------------
# Next track variants
# ---------------------------------------------------------------------------

class TestDetectNext:
    @pytest.mark.parametrize("msg", [
        "Nächster Track",
        "nächster track",
        "Nächstes Lied",
        "Nächster Song",
        "Nächster Titel",
        "Next Track",
        "Skip",
        "skip",
        "Überspringen",
        "überspringe",
    ])
    def test_next_variants(self, msg):
        result = _detect_media_transport(msg)
        assert result is not None
        action, room = result
        assert action == "next"
        assert room is None


# ---------------------------------------------------------------------------
# Previous track variants
# ---------------------------------------------------------------------------

class TestDetectPrevious:
    @pytest.mark.parametrize("msg", [
        "Vorheriger Track",
        "vorheriger track",
        "Vorheriges Lied",
        "Vorheriger Song",
        "Vorheriger Titel",
        "Previous Track",
        "Zurück",
        "zurück",
    ])
    def test_previous_variants(self, msg):
        result = _detect_media_transport(msg)
        assert result is not None
        action, room = result
        assert action == "previous"
        assert room is None


# ---------------------------------------------------------------------------
# Room extraction
# ---------------------------------------------------------------------------

class TestRoomExtraction:
    def test_stop_with_room_im(self):
        result = _detect_media_transport("Stop im Arbeitszimmer")
        assert result == ("stop", "Arbeitszimmer")

    def test_stop_with_room_in_der(self):
        result = _detect_media_transport("Stopp in der Küche")
        assert result == ("stop", "Küche")

    def test_pause_with_room_in_dem(self):
        result = _detect_media_transport("Pause in dem Bad")
        assert result == ("pause", "Bad")

    def test_next_with_room(self):
        result = _detect_media_transport("Nächster Track im Wohnzimmer")
        assert result == ("next", "Wohnzimmer")

    def test_musik_aus_with_room(self):
        result = _detect_media_transport("Musik aus im Schlafzimmer")
        assert result == ("stop", "Schlafzimmer")


# ---------------------------------------------------------------------------
# Negative cases — must NOT match
# ---------------------------------------------------------------------------

class TestNoMatch:
    @pytest.mark.parametrize("msg", [
        "Spiele Afterburner von ZZ Top",
        "Spiele Musik im Arbeitszimmer",
        "Was läuft gerade?",
        "Wie wird das Wetter morgen?",
        "Schalte das Licht ein",
        "Suche nach Rock Musik",
        "Spiele den nächsten Song von Queen",
        "Welcher Track ist das?",
        "Stoppe den Timer",
        "",
    ])
    def test_no_match(self, msg):
        result = _detect_media_transport(msg)
        assert result is None
