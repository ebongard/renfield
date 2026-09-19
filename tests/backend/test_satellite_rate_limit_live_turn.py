"""
Ratenbegrenzung darf keinen laufenden Sprachzug kosten (#1284).

Bis zu diesem Fix pruefte der Satelliten-Handler den Limiter VOR jeder
Unterscheidung des Frame-Typs und verwarf einen abgelehnten Frame ersatzlos —
auch ein ``audio_end``. Zwei Folgen:

* Ein Schwall aufgestauter Audio-Chunks (die Ereignisschleife eines Pi Zero
  stockt, danach gehen die Chunks auf einmal hinaus) riss das Sekundenlimit,
  obwohl die Dauerrate physisch auf 12,5 Chunks/s begrenzt ist.
* Ein verworfenes ``audio_end`` liess die Sitzung in ``listening`` stranden, bis
  der Kehraus sie nach ``DEVICE_SESSION_TIMEOUT`` samt Aufnahme verwarf.

``_rate_verdict`` gibt einem abgelehnten Frame eines LAUFENDEN, EIGENEN Zuges
eine zweite Pruefung. Alles andere wird so billig abgewiesen wie zuvor.
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
    """Nur, was die Klassifikation anfasst: ``sessions`` mit ``satellite_id``."""
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
    assert not limiter.check(key)[0], "Vorbedingung: das Sekundenlimit MUSS gerissen sein"


# ---------------------------------------------------------------------------
# Limiter: burst_ok
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_burst_ok_ueberspringt_nur_das_sekundenlimit():
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    _exhaust_second(limiter, SAT)

    assert limiter.check(SAT, burst_ok=True) == (True, "")


@pytest.mark.unit
def test_burst_ok_bleibt_am_minutenbudget_haengen():
    """Der Schwall ist gedeckelt — eine Flut kommt auch so nicht durch."""
    limiter = WSRateLimiter(per_second=3, per_minute=5, enabled=True)
    for _ in range(5):
        assert limiter.check(SAT, burst_ok=True)[0]

    allowed, reason = limiter.check(SAT, burst_ok=True)
    assert not allowed
    assert "per minute" in reason


@pytest.mark.unit
def test_burst_frames_zaehlen_gegen_das_budget_aller_frames():
    """Ein durchgelassener Schwall ist kein Freifahrtschein fuer den Rest."""
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
def test_frames_eines_eigenen_laufenden_zuges_werden_erkannt(raw, expected):
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
def test_alles_andere_ist_kein_zug_frame(raw):
    assert _live_session_frame(raw, SAT, _manager()) is None


@pytest.mark.unit
@pytest.mark.parametrize("raw", [_text("audio"), _text("audio_end"), _binary()])
def test_fremde_sitzung_zaehlt_nicht(raw):
    """Ein Satellit darf sich nicht mit der Sitzung eines anderen freikaufen."""
    assert _live_session_frame(raw, OTHER, _manager()) is None


# ---------------------------------------------------------------------------
# Urteil
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("raw", [_text("audio", chunk="QUJD"), _binary()])
def test_audio_schwall_eines_laufenden_zuges_kommt_durch(raw):
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    _exhaust_second(limiter, SAT)

    assert _rate_verdict(raw, SAT, SAT, limiter, _manager()) == (True, "")


@pytest.mark.unit
def test_audio_end_kommt_selbst_bei_erschoepftem_minutenbudget_durch():
    """Ein verworfenes audio_end strandet die Sitzung — es ist nie verhandelbar."""
    limiter = WSRateLimiter(per_second=100, per_minute=3, enabled=True)
    for _ in range(3):
        assert limiter.check(SAT)[0]
    assert not limiter.check(SAT, burst_ok=True)[0], "Vorbedingung: Minutenbudget erschoepft"

    assert _rate_verdict(_text("audio_end"), SAT, SAT, limiter, _manager()) == (True, "")


@pytest.mark.unit
def test_audio_jenseits_des_minutenbudgets_wird_abgewiesen():
    limiter = WSRateLimiter(per_second=100, per_minute=3, enabled=True)
    for _ in range(3):
        assert limiter.check(SAT)[0]

    allowed, reason = _rate_verdict(_text("audio"), SAT, SAT, limiter, _manager())
    assert not allowed
    assert "per minute" in reason


@pytest.mark.unit
def test_anderer_frame_im_schwall_bleibt_abgewiesen():
    """Heartbeat/BLE im selben Schwall: abgewiesen, mit dem urspruenglichen Grund."""
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    _exhaust_second(limiter, SAT)

    allowed, reason = _rate_verdict(_text("heartbeat"), SAT, SAT, limiter, _manager())
    assert not allowed
    assert "per second" in reason


@pytest.mark.unit
def test_unregistrierte_verbindung_wird_nie_klassifiziert():
    """Ohne satellite_id kein zweiter Blick — der Frame wird nicht einmal geparst."""
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    _exhaust_second(limiter, "10.0.0.9")

    class Explodes:
        @property
        def sessions(self):
            raise AssertionError("ein unregistrierter Absender darf keine Klassifikation ausloesen")

    allowed, _ = _rate_verdict(_text("audio_end"), "10.0.0.9", None, limiter, Explodes())
    assert not allowed


@pytest.mark.unit
def test_erlaubter_frame_wird_nicht_klassifiziert():
    """Der Normalpfad parst nichts doppelt."""
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)

    class Explodes:
        @property
        def sessions(self):
            raise AssertionError("ein erlaubter Frame braucht keinen zweiten Blick")

    assert _rate_verdict(_text("audio"), SAT, SAT, limiter, Explodes()) == (True, "")


# ---------------------------------------------------------------------------
# Review-Befunde: Parse-Budget, ehrliche Verstossmeldung, Verdrahtung
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_zweiter_blick_hat_ein_eigenes_budget():
    """Abgelehnte Frames duerfen nicht unbegrenzt geparst werden."""
    limiter = WSRateLimiter(per_second=1, per_minute=3, enabled=True)
    assert limiter.check(SAT)[0]

    lookups = []

    class Counting:
        @property
        def sessions(self):
            lookups.append(1)
            return {}

    # Ein audio-Frame mit unbekannter Sitzung kommt bis zum Sitzungs-Nachschlagen —
    # das Nachschlagen zaehlt damit die tatsaechlich geparsten Frames.
    for _ in range(10):
        assert not _rate_verdict(_text("audio", session_id="unbekannt"), SAT, SAT, limiter, Counting())[0]

    assert len(lookups) == 3, "der zweite Blick ist auf das Minutenbudget gedeckelt"


@pytest.mark.unit
def test_budget_des_zweiten_blicks_belastet_das_frame_budget_nicht():
    limiter = WSRateLimiter(per_second=1, per_minute=100, enabled=True)
    assert limiter.check(SAT)[0]
    _rate_verdict(_text("heartbeat"), SAT, SAT, limiter, _manager())

    assert limiter.get_stats(SAT)["messages_last_minute"] == 1
    assert limiter.get_stats(f"{SAT}{_SECOND_LOOK_SUFFIX}")["messages_last_minute"] == 1


@pytest.mark.unit
def test_durchgelassener_frame_zaehlt_nicht_als_verstoss():
    """Sonst meldet das Log "exceeded" fuer angenommene Frames und verbraucht
    das Drei-Warnungen-Kontingent, das echte Verstoesse brauchen."""
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    for _ in range(3):
        assert limiter.check(SAT, record_violation=False)[0]

    for _ in range(5):
        assert _rate_verdict(_text("audio"), SAT, SAT, limiter, _manager())[0]
    assert _rate_verdict(_text("audio_end"), SAT, SAT, limiter, _manager())[0]

    assert limiter.get_stats(SAT)["violations"] == 0


@pytest.mark.unit
def test_endgueltige_ablehnung_zaehlt_genau_einmal():
    limiter = WSRateLimiter(per_second=3, per_minute=100, enabled=True)
    for _ in range(3):
        assert limiter.check(SAT, record_violation=False)[0]

    assert not _rate_verdict(_text("heartbeat"), SAT, SAT, limiter, _manager())[0]

    assert limiter.get_stats(SAT)["violations"] == 1


@pytest.mark.unit
def test_sitzung_in_verarbeitung_gilt_weiter_als_laufend():
    """"Laufend" heisst: die Sitzung existiert. Ein spaetes audio_end (oder ein
    Nachzuegler-Chunk) einer Sitzung in PROCESSING wird nicht anders behandelt —
    die Sitzung wird erst am Zugende geloescht."""
    sessions = {SESSION: SimpleNamespace(satellite_id=SAT, state="processing")}
    assert _live_session_frame(_text("audio_end"), SAT, _manager(sessions)) == "audio_end"


@pytest.mark.unit
def test_empfangsschleife_nutzt_das_urteil_und_nie_den_limiter_direkt():
    """Verdrahtung: es gibt keinen Test, der die echte WebSocket-Schleife faehrt.
    Wer dort wieder ``rate_limiter.check`` direkt aufruft, hebt den Fix auf."""
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
