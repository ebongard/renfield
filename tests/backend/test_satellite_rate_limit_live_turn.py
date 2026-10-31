"""
Rate limiting must not cost a voice turn in flight (#1284).

Until this fix the satellite handler checked the limiter BEFORE any
distinction of frame type and discarded a rejected frame with nothing in its
place -- an ``audio_end`` included. Two consequences:

* A burst of backed-up audio chunks (a Pi Zero's event loop stalls, after
  which the chunks all go out at once) broke the per-second limit, although
  the sustained rate is physically capped at 12.5 chunks/s.
* A discarded ``audio_end`` left the session stranded in ``listening`` until
  the sweep discarded it, recording included, after ``DEVICE_SESSION_TIMEOUT``.

``_rate_verdict`` gives a rejected frame of a LIVE, OWN turn a second check.
Everything else is rejected as cheaply as before.
"""

import ast
import inspect
import json
from types import SimpleNamespace

import pytest

from ha_glue.api.websocket import satellite_handler
from ha_glue.api.websocket.satellite_handler import (
    _SECOND_LOOK_SUFFIX,
    _live_session_frame,
    _rate_verdict,
)
from ha_glue.services.opus_transport import build_audio_frame
from services.websocket_rate_limiter import WSRateLimiter

SAT = "sat-kueche"
OTHER = "sat-bad"
SESSION = "sess-1"


def _manager(sessions: dict | None = None):
    """Only what the classification touches: ``sessions`` with ``satellite_id``."""
    if sessions is None:
        sessions = {SESSION: SimpleNamespace(satellite_id=SAT)}
    return SimpleNamespace(sessions=sessions)


def _text(msg_type: str, session_id=SESSION, **extra) -> dict:
    return {"type": "websocket.receive", "text": json.dumps({"type": msg_type, "session_id": session_id, **extra})}


def _binary(session_id: str = SESSION) -> dict:
    return {"type": "websocket.receive", "bytes": build_audio_frame(session_id, 1, [b"\x01\x02\x03"])}


def _exhaust_second(limiter: WSRateLimiter, key: str) -> None:
    for _ in range(limiter.per_second):
        assert limiter.check(key)[0]
    assert not limiter.check(key)[0], "precondition: the per-second limit MUST be exceeded"


# ---------------------------------------------------------------------------
# Limiter: burst_ok
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_burst_ok_skips_only_the_per_second_limit():
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    _exhaust_second(limiter, SAT)

    assert limiter.check(SAT, burst_ok=True) == (True, "")


@pytest.mark.unit
def test_burst_ok_still_stops_at_the_minute_budget():
    """The burst is capped -- a flood still does not get through."""
    limiter = WSRateLimiter(per_second=3, per_minute=5, enabled=True)
    for _ in range(5):
        assert limiter.check(SAT, burst_ok=True)[0]

    allowed, reason = limiter.check(SAT, burst_ok=True)
    assert not allowed
    assert "per minute" in reason


@pytest.mark.unit
def test_burst_frames_count_against_the_budget_of_all_frames():
    """A burst that is let through is no free pass for the rest."""
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    _exhaust_second(limiter, SAT)
    assert limiter.check(SAT, burst_ok=True)[0]

    assert not limiter.check(SAT)[0]


# ---------------------------------------------------------------------------
# Klassifikation
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("raw, expected", [
    (_text("audio", chunk="QUJD", sequence=1), "audio"),
    (_text("audio_end", reason="silence"), "audio_end"),
    (_binary(), "audio"),
])
def test_frames_of_its_own_running_turn_are_recognised(raw, expected):
    assert _live_session_frame(raw, SAT, _manager()) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", [
    _text("heartbeat"),
    _text("ble_presence"),
    _text("wakeword_detected"),
    _text("audio", session_id="unbekannt"),
    _text("audio_end", session_id=None),
    _text("audio", session_id=["nicht", "hashbar"]),
    _binary("unbekannt"),
    {"type": "websocket.receive", "bytes": b"\x00"},
    {"type": "websocket.receive", "text": "{kaputt"},
    {"type": "websocket.receive", "text": "[1, 2]"},
    {"type": "websocket.receive", "text": None},
])
def test_everything_else_is_not_a_turn_frame(raw):
    assert _live_session_frame(raw, SAT, _manager()) is None


@pytest.mark.unit
@pytest.mark.parametrize("raw", [_text("audio"), _text("audio_end"), _binary()])
def test_a_foreign_session_does_not_count(raw):
    """A satellite must not buy itself out with somebody else's session."""
    assert _live_session_frame(raw, OTHER, _manager()) is None


# ---------------------------------------------------------------------------
# Urteil
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("raw", [_text("audio", chunk="QUJD"), _binary()])
def test_the_audio_burst_of_a_running_turn_gets_through(raw):
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    _exhaust_second(limiter, SAT)

    assert _rate_verdict(raw, SAT, SAT, limiter, _manager()) == (True, "")


@pytest.mark.unit
def test_audio_end_gets_through_even_with_an_exhausted_minute_budget():
    """A discarded audio_end strands the session -- it is never negotiable."""
    limiter = WSRateLimiter(per_second=100, per_minute=3, enabled=True)
    for _ in range(3):
        assert limiter.check(SAT)[0]
    assert not limiter.check(SAT, burst_ok=True)[0], "Vorbedingung: Minutenbudget erschoepft"

    assert _rate_verdict(_text("audio_end"), SAT, SAT, limiter, _manager()) == (True, "")


@pytest.mark.unit
def test_audio_beyond_the_minute_budget_is_rejected():
    limiter = WSRateLimiter(per_second=100, per_minute=3, enabled=True)
    for _ in range(3):
        assert limiter.check(SAT)[0]

    allowed, reason = _rate_verdict(_text("audio"), SAT, SAT, limiter, _manager())
    assert not allowed
    assert "per minute" in reason


@pytest.mark.unit
def test_another_frame_in_the_burst_stays_rejected():
    """Heartbeat/BLE in the same burst: rejected, with the original reason."""
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    _exhaust_second(limiter, SAT)

    allowed, reason = _rate_verdict(_text("heartbeat"), SAT, SAT, limiter, _manager())
    assert not allowed
    assert "per second" in reason


@pytest.mark.unit
def test_an_unregistered_connection_is_never_classified():
    """No satellite_id, no second look -- the frame is not even parsed."""
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    _exhaust_second(limiter, "10.0.0.9")

    class Explodes:
        @property
        def sessions(self):
            raise AssertionError("ein unregistrierter Absender darf keine Klassifikation ausloesen")

    allowed, _ = _rate_verdict(_text("audio_end"), "10.0.0.9", None, limiter, Explodes())
    assert not allowed


@pytest.mark.unit
def test_an_allowed_frame_is_not_classified():
    """The normal path parses nothing twice."""
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)

    class Explodes:
        @property
        def sessions(self):
            raise AssertionError("ein erlaubter Frame braucht keinen zweiten Blick")

    assert _rate_verdict(_text("audio"), SAT, SAT, limiter, Explodes()) == (True, "")


# ---------------------------------------------------------------------------
# Review findings: parse budget, honest violation report, wiring
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_second_look_has_its_own_budget():
    """Rejected frames must not be parsed without limit."""
    limiter = WSRateLimiter(per_second=1, per_minute=3, enabled=True)
    assert limiter.check(SAT)[0]

    lookups = []

    class Counting:
        @property
        def sessions(self):
            lookups.append(1)
            return {}

    # An audio frame with an unknown session gets as far as the session
    # lookup -- so the lookup counts the frames actually parsed.
    for _ in range(10):
        assert not _rate_verdict(_text("audio", session_id="unbekannt"), SAT, SAT, limiter, Counting())[0]

    assert len(lookups) == 3, "the second look is capped by the per-minute budget"


@pytest.mark.unit
def test_the_second_look_budget_does_not_charge_the_frame_budget():
    limiter = WSRateLimiter(per_second=1, per_minute=100, enabled=True)
    assert limiter.check(SAT)[0]
    _rate_verdict(_text("heartbeat"), SAT, SAT, limiter, _manager())

    assert limiter.get_stats(SAT)["messages_last_minute"] == 1
    assert limiter.get_stats(f"{SAT}{_SECOND_LOOK_SUFFIX}")["messages_last_minute"] == 1


@pytest.mark.unit
def test_a_frame_let_through_does_not_count_as_a_violation():
    """Otherwise the log reports "exceeded" for accepted frames and uses up
    the three-warning allowance that real violations need."""
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    for _ in range(3):
        assert limiter.check(SAT, record_violation=False)[0]

    for _ in range(5):
        assert _rate_verdict(_text("audio"), SAT, SAT, limiter, _manager())[0]
    assert _rate_verdict(_text("audio_end"), SAT, SAT, limiter, _manager())[0]

    assert limiter.get_stats(SAT)["violations"] == 0


@pytest.mark.unit
def test_a_final_rejection_counts_exactly_once():
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    for _ in range(3):
        assert limiter.check(SAT, record_violation=False)[0]

    assert not _rate_verdict(_text("heartbeat"), SAT, SAT, limiter, _manager())[0]

    assert limiter.get_stats(SAT)["violations"] == 1


@pytest.mark.unit
def test_a_session_in_processing_still_counts_as_running():
    """"Live" means: the session exists. A late audio_end (or a straggler
    chunk) of a session in PROCESSING is treated no differently -- the session
    is only deleted at the end of the turn."""
    sessions = {SESSION: SimpleNamespace(satellite_id=SAT, state="processing")}
    assert _live_session_frame(_text("audio_end"), SAT, _manager(sessions)) == "audio_end"


@pytest.mark.unit
def test_the_receive_loop_uses_the_verdict_and_never_the_limiter_directly():
    """Wiring: there is no test that drives the real WebSocket loop. Anyone
    who calls ``rate_limiter.check`` directly there again undoes the fix."""
    tree = ast.parse(inspect.getsource(satellite_handler))
    loop = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "satellite_websocket"
    )
    called = set()
    for node in ast.walk(loop):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                called.add(f"{f.value.id}.{f.attr}")

    assert "_rate_verdict" in called
    assert "rate_limiter.check" not in called
