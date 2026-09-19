"""OTA-Laufzustand: Endstufen, verspätete Meldungen, Zeitgrenze (#1209).

Beim v1.4.7-Rollout blieb ein Satellit dauerhaft auf
``in_progress``/``rolling_back`` stehen, mit ``update_error: null`` — ein
hängendes Update ohne erkennbaren Grund. Drei Lücken steckten dahinter, und
jede bekommt hier ihre Fälle:

* Der Fortschrittszweig behandelte ``failed``/``rolling_back`` als „läuft
  noch", obwohl beide einen Lauf beenden.
* Er überschrieb dabei ``update_error`` mit ``None`` und löschte so die vom
  Satelliten mitgelieferte Ursache.
* ``cleanup_stale`` — die einzige Stelle mit einer Zeitgrenze — hatte
  überhaupt keinen Aufrufer im Produktivcode.

Läuft auf der Baubox .159 (CI ist bewusst funktionslos), siehe
``memory/reference_test_runner_159.md``.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _manager_with_satellite(status=None, stage=None, error=None):
    """Ein Manager mit genau einem registrierten Satelliten im gewünschten Lauf."""
    from ha_glue.services.satellite_manager import (
        SatelliteCapabilities,
        SatelliteInfo,
        SatelliteManager,
    )

    mgr = SatelliteManager()
    mgr.satellites["sat-1"] = SatelliteInfo(
        satellite_id="sat-1",
        room="Fitnessraum",
        websocket=MagicMock(),
        capabilities=SatelliteCapabilities(),
        version="1.4.6",
    )
    if status is not None:
        mgr.set_update_status("sat-1", status, stage=stage, progress=0, error=error)
    return mgr


# ==========================================================================
# 1. Endstufen beenden den Lauf
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_rolling_back_terminates_the_run():
    """``rolling_back`` ist die letzte Meldung eines zurückgerollten Laufs —
    danach darf der Status nicht weiter „läuft" behaupten."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage="rolling_back", progress=0,
                              message="Rolling back...")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert sat.update_stage == "rolling_back"


@pytest.mark.backend
@pytest.mark.unit
def test_failed_stage_keeps_the_reported_cause():
    """Die Begründung steckt in der Meldung des Satelliten. Genau sie fehlte
    im Fehlerbild (``update_error: null``)."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress(
        "sat-1", stage="failed", progress=0,
        message="Unknown packages in requirements: ['opuslib>=3.0.1']",
    )

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert "opuslib" in sat.update_error


@pytest.mark.backend
@pytest.mark.unit
def test_rollback_after_failure_does_not_erase_the_cause():
    """Die beobachtete Reihenfolge: erst ``failed`` mit Grund, dann
    ``rolling_back`` ohne. Der Grund muss die zweite Meldung überleben."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress("sat-1", stage="failed", progress=0,
                              message="Unknown packages in requirements")
    # WORTGETREU wie auf der Leitung: der Satellit sendet IMMER diesen Text
    # (update_manager.py:762), nie einen leeren. Die frühere Fassung dieses
    # Tests übergab "" und war deshalb grün, während der Code die Ursache in
    # Wahrheit bei JEDEM Rückbau überschrieb.
    mgr.apply_update_progress("sat-1", stage="rolling_back", progress=0,
                              message="Rolling back...")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert sat.update_error == "Unknown packages in requirements"


# ==========================================================================
# 2. Ein beendeter Lauf wird nicht zurückgeholt
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_late_progress_does_not_resurrect_a_failed_run():
    """Der Satellit plant Fortschrittsmeldungen abgesetzt ein, während er die
    Endmeldung abwartet — eine nachlaufende Meldung ist der Normalfall."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite(status=US.FAILED, stage="failed",
                                     error="Installer abgebrochen")
    mgr.apply_update_progress("sat-1", stage="installing", progress=80, message="")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert sat.update_stage == "failed"
    assert sat.update_error == "Installer abgebrochen"


@pytest.mark.backend
@pytest.mark.unit
def test_late_progress_does_not_resurrect_a_completed_run():
    """Dieselbe Klemme in die andere Richtung: ein geglückter Lauf bleibt
    geglückt, auch wenn noch ein ``restarting`` nachtröpfelt."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite(status=US.COMPLETED, stage="completed")
    mgr.apply_update_progress("sat-1", stage="restarting", progress=90, message="")

    assert mgr.get_satellite("sat-1").update_status == US.COMPLETED


@pytest.mark.backend
@pytest.mark.unit
def test_a_new_update_may_start_after_a_failed_one():
    """Die Sperre darf NUR Fortschrittsmeldungen betreffen. Ein neuer Lauf
    setzt über ``set_update_status`` an und muss weiter anlaufen können —
    sonst repariert der Fix die Anzeige und zerstört das Ausrollen."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite(status=US.FAILED, stage="failed", error="alt")
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage="downloading", progress=20, message="")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.IN_PROGRESS
    assert sat.update_stage == "downloading"


@pytest.mark.backend
@pytest.mark.unit
def test_normal_progress_still_advances():
    """Der unauffällige Fall bleibt unverändert."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage="downloading", progress=40, message="")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.IN_PROGRESS
    assert sat.update_progress == 40


@pytest.mark.backend
@pytest.mark.unit
def test_progress_for_unknown_satellite_is_ignored():
    mgr = _manager_with_satellite()
    mgr.apply_update_progress("gibt-es-nicht", stage="downloading", progress=1)
    assert "gibt-es-nicht" not in mgr.satellites


# ==========================================================================
# 3. Die Zeitgrenze im Kehraus
# ==========================================================================


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_stale_fails_a_run_without_terminal_state():
    """Das Auffangnetz: ein Lauf, dessen Endmeldung nie eintrifft."""
    import time

    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    sat = mgr.get_satellite("sat-1")
    sat.update_started_at = time.time() - 100_000  # weit jenseits jeder Grenze

    await mgr.cleanup_stale()

    assert sat.update_status == US.FAILED
    assert "Endzustand" in sat.update_error
    assert sat.update_started_at is None  # feuert nicht erneut


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_stale_leaves_a_young_run_alone():
    """Ein laufendes Update darf der Kehraus nicht abschießen."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="downloading", progress=10)

    await mgr.cleanup_stale()

    assert mgr.get_satellite("sat-1").update_status == US.IN_PROGRESS


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_stale_keeps_a_cause_the_satellite_gave():
    """Erfinde keine Begründung, wenn schon eine vorliegt."""
    import time

    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    sat = mgr.get_satellite("sat-1")
    sat.update_error = "Paketquelle nicht erreichbar"
    sat.update_started_at = time.time() - 100_000

    await mgr.cleanup_stale()

    assert sat.update_error == "Paketquelle nicht erreichbar"


@pytest.mark.backend
@pytest.mark.unit
def test_progress_frames_do_not_push_the_deadline_out():
    """Die Startmarke wird EINMAL gesetzt. Sonst entkäme ein Satellit, der
    endlos Fortschritt meldet, der Zeitgrenze dauerhaft."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    first = mgr.get_satellite("sat-1").update_started_at

    mgr.apply_update_progress("sat-1", stage="downloading", progress=10)
    mgr.apply_update_progress("sat-1", stage="installing", progress=70)

    assert mgr.get_satellite("sat-1").update_started_at == first


@pytest.mark.backend
@pytest.mark.unit
def test_terminal_state_clears_the_start_mark():
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.set_update_status("sat-1", US.COMPLETED, stage="completed", progress=100)

    assert mgr.get_satellite("sat-1").update_started_at is None


# ==========================================================================
# 4. Der fehlende Takt — ohne ihn ist alles oben wirkungslos
# ==========================================================================


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_scheduler_actually_calls_the_sweep():
    """``cleanup_stale`` hatte KEINEN Aufrufer im Produktivcode. Der Test hält
    fest, dass der Takt existiert, und dass er den Manager-Singleton nimmt —
    eine zweite Instanz würde ein leeres Verzeichnis kehren."""
    import ha_glue.bootstrap as bootstrap

    fake = MagicMock()
    fake.cleanup_stale = AsyncMock()

    with patch("ha_glue.utils.config.ha_glue_settings.satellite_cleanup_interval", 0), \
         patch("ha_glue.services.satellite_manager.get_satellite_manager",
               return_value=fake):
        bootstrap._schedule_satellite_cleanup()
        try:
            await asyncio.sleep(0.05)
            assert fake.cleanup_stale.await_count >= 1
        finally:
            for task in bootstrap._ha_glue_tasks:
                task.cancel()
            bootstrap._ha_glue_tasks.clear()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_cleanup_scheduler_survives_a_failing_sweep():
    """Ein Fehler im Kehraus darf den Takt nicht beenden — sonst wäre die
    Zeitgrenze nach dem ersten Schluckauf wieder tot."""
    import ha_glue.bootstrap as bootstrap

    fake = MagicMock()
    fake.cleanup_stale = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("ha_glue.utils.config.ha_glue_settings.satellite_cleanup_interval", 0), \
         patch("ha_glue.services.satellite_manager.get_satellite_manager",
               return_value=fake):
        bootstrap._schedule_satellite_cleanup()
        try:
            await asyncio.sleep(0.05)
            assert fake.cleanup_stale.await_count >= 2  # lief weiter
            assert not bootstrap._ha_glue_tasks[0].done()
        finally:
            for task in bootstrap._ha_glue_tasks:
                task.cancel()
            bootstrap._ha_glue_tasks.clear()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_startup_hook_wires_the_cleanup_scheduler():
    """Der Takt muss aus dem Startvorgang heraus wirklich angelegt werden.

    Ohne diesen Fall belegen die Tests oben nur, dass die Regeln stimmen — nicht,
    dass sie je laufen. Genau diese Lücke war der ursprüngliche Befund: eine
    fertige Kehraus-Funktion, die niemand aufrief.
    """
    import ha_glue.bootstrap as bootstrap

    led = MagicMock()
    led.return_value.initialize = AsyncMock()

    with patch.object(bootstrap, "_schedule_satellite_cleanup") as sched, \
         patch.object(bootstrap, "_schedule_ha_keywords_preload"), \
         patch.object(bootstrap, "_init_zeroconf", new=AsyncMock()), \
         patch("ha_glue.utils.config.ha_glue_settings.presence_enabled", False), \
         patch("ha_glue.services.led_dimming_service.get_led_dimming_service", led):
        await bootstrap.ha_glue_on_startup(app=MagicMock())

    sched.assert_called_once()


# ==========================================================================
# 5. Verspätete ENDSTUFEN — die Sperre gilt für beide Zweige
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_late_terminal_stage_does_not_resurrect_a_completed_run():
    """Eine doppelte ``rolling_back``-Meldung nach einem geglückten Lauf darf
    ``COMPLETED`` nicht auf ``FAILED`` kippen."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=90)
    mgr.set_update_status("sat-1", US.COMPLETED, stage="completed", progress=100)

    mgr.apply_update_progress("sat-1", stage="rolling_back", progress=0,
                              message="Rolling back...")

    assert mgr.get_satellite("sat-1").update_status == US.COMPLETED


@pytest.mark.backend
@pytest.mark.unit
def test_a_stray_terminal_frame_still_ends_the_current_run():
    """BEKANNTE GRENZE — hier festgehalten statt verschwiegen.

    Die Sperre schützt einen bereits BEENDETEN Lauf. Sie kann einen Nachzügler
    aus einem alten Lauf nicht von einer echten Meldung des gerade laufenden
    unterscheiden, weil das Protokoll keine Lauf-Kennung führt:
    ``send_update_progress`` überträgt ausschließlich ``{type, stage, progress,
    message}``. Ein verspätetes ``rolling_back``, das NACH dem Start eines neuen
    Laufs eintrifft, beendet deshalb diesen neuen Lauf.

    Praktisch schmal: der Nachzügler ist auf dem Satelliten abgesetzt eingeplant
    und trifft binnen Millisekunden ein, während ein neuer Lauf einen
    ADMIN-Klick braucht. Sauber zu schließen wäre das nur mit einer Lauf-Kennung
    im Protokoll — eine Änderung an der Satelliten-Firmware, die hier nicht
    hingehört. Eine Heuristik wäre geraten und damit schlechter als die
    dokumentierte Grenze.

    Dieser Test hält das IST fest. Wird die Kennung eingeführt, muss er auf die
    dann gewünschte Erwartung umgeschrieben werden — er ist die Stelle, an der
    das auffällt.
    """
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite(status=US.FAILED, stage="failed", error="alt")
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage="downloading", progress=50)

    mgr.apply_update_progress("sat-1", stage="rolling_back", progress=0,
                              message="Rolling back...")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED  # die Grenze, nicht der Wunsch
    assert sat.update_error == "Rolling back..."


@pytest.mark.backend
@pytest.mark.unit
def test_completed_stage_ends_the_run_as_completed():
    """Der Satellit meldet ``completed`` als Fortschritt VOR dem Neustart, der
    das ``update_complete`` meist verschluckt. Bliebe der Lauf auf
    ``in_progress``, würde die Zeitgrenze ein ERFOLGREICHES Update später als
    gescheitert markieren."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="restarting", progress=90)
    mgr.apply_update_progress("sat-1", stage="completed", progress=100,
                              message="Updated to 1.4.7")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.COMPLETED
    assert sat.update_error is None
    assert sat.update_started_at is None


@pytest.mark.backend
@pytest.mark.unit
def test_clear_update_status_also_clears_the_start_mark():
    """Sonst erbt der NÄCHSTE Lauf eine abgelaufene Frist und wird sofort
    vom Kehraus erschlagen."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.clear_update_status("sat-1")

    assert mgr.get_satellite("sat-1").update_started_at is None


# ==========================================================================
# 6. Feindliche oder defekte Geräte
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_non_string_fields_do_not_poison_the_admin_list():
    """``stage``/``message`` kommen als JSON von einem LAN-Gerät. Ein falscher
    Typ darf nicht in den Zustand gelangen: die Antwortmodelle typisieren sie
    als ``str``, Pydantic wandelt nicht, und ``list_satellites`` hat keine
    Absicherung je Eintrag — EIN vergifteter Eintrag legt die ganze
    Admin-Liste lahm."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="initiating", progress=0)
    mgr.apply_update_progress("sat-1", stage={"boom": 1}, progress="viel",
                              message={"auch": "boom"})

    sat = mgr.get_satellite("sat-1")
    assert isinstance(sat.update_stage, str)
    assert isinstance(sat.update_progress, int)


@pytest.mark.backend
@pytest.mark.unit
def test_oversized_message_is_capped():
    """Der WS-Rahmen erlaubt 1 MB. So viel Gerätetext darf nicht dauerhaft im
    Verzeichnis liegen bleiben."""
    from ha_glue.services.satellite_manager import SatelliteManager
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress("sat-1", stage="failed", progress=0, message="x" * 100_000)

    sat = mgr.get_satellite("sat-1")
    assert len(sat.update_error) == SatelliteManager._MAX_UPDATE_ERROR_CHARS


# ==========================================================================
# 7. Kalibrierung: der Kehraus darf nichts Gesundes abräumen
# ==========================================================================


def _session(mgr, sat_id="sat-1", state=None, age=0.0):
    """Hängt dem Satelliten eine Sitzung im gewünschten Zustand und Alter an."""
    import time as _t

    from ha_glue.services.satellite_manager import SatelliteSession, SatelliteState

    sess = SatelliteSession(
        session_id="sess-1",
        satellite_id=sat_id,
        room="Fitnessraum",
        state=state or SatelliteState.LISTENING,
    )
    sess.started_at = _t.time() - age
    mgr.sessions["sess-1"] = sess
    mgr.satellites[sat_id].current_session_id = "sess-1"
    return sess


@pytest.mark.backend
@pytest.mark.asyncio
async def test_recording_timeout_only_hits_a_listening_session():
    """Die Frist ist eine maximale AUFNAHME-Dauer. Sie beginnt beim Weckwort,
    und der ganze Zug läuft inline in derselben Empfangsschleife — würde sie
    den Zug messen, zerstörte sie die Sitzung mitten in der Antwort, und
    ``send_tts_audio`` verwürfe sie stumm."""
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.PROCESSING, age=100_000)

    await mgr.cleanup_stale()

    assert "sess-1" in mgr.sessions, "ein denkender Zug darf nicht abgeräumt werden"


@pytest.mark.backend
@pytest.mark.asyncio
async def test_recording_timeout_ends_an_overlong_recording():
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.LISTENING, age=100_000)

    await mgr.cleanup_stale()

    assert "sess-1" not in mgr.sessions


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_satellite_in_a_live_turn_is_not_evicted():
    """Seine Lebenszeichen liegen ungelesen im Puffer, weil der Zug inline
    verarbeitet wird — es ist kein totes Gerät."""
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.PROCESSING, age=1.0)
    mgr.satellites["sat-1"].last_heartbeat = 0.0

    await mgr.cleanup_stale()

    assert "sat-1" in mgr.satellites


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_satellite_installing_an_update_is_not_evicted():
    """Der Installer blockiert die Ereignisschleife des Pi bis zu 150s gegen
    eine 60s-Frist. Räumte der Kehraus ihn, verlöre er die Startmarke — und
    entwaffnete damit die OTA-Zeitgrenze genau im Fall, für den sie da ist."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.satellites["sat-1"].last_heartbeat = 0.0

    await mgr.cleanup_stale()

    assert "sat-1" in mgr.satellites
    assert mgr.get_satellite("sat-1").update_started_at is not None


@pytest.mark.backend
@pytest.mark.asyncio
async def test_eviction_closes_the_socket():
    """Ohne Verbindungsschluss läuft die Empfangsschleife weiter und bestätigt
    weiter Heartbeats: das Gerät sieht eine gesunde Leitung, meldet sich nie
    neu an und ist dauerhaft stumm."""
    mgr = _manager_with_satellite()
    ws = mgr.satellites["sat-1"].websocket = AsyncMock()
    mgr.satellites["sat-1"].last_heartbeat = 0.0

    with patch("api.websocket.kiosk_handler.broadcast_kiosk_event", new=AsyncMock()):
        await mgr.cleanup_stale()

    assert "sat-1" not in mgr.satellites
    ws.close.assert_awaited()


# ==========================================================================
# 8. Die Verdrahtung im WebSocket-Handler
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_websocket_loop_calls_apply_update_progress():
    """Die Regeln oben sind wertlos, wenn die Empfangsschleife sie nie aufruft.
    Statisch geprüft, weil die Schleife sonst Anmeldung, Authentifizierung und
    Raumabgleich verlangte (gleiches Vorgehen wie
    test_satellite_memory_extraction.py)."""
    import ast
    from pathlib import Path

    import ha_glue.api.websocket.satellite_handler as sh

    tree = ast.parse(Path(sh.__file__).read_text())
    target = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and n.name == "satellite_websocket"),
        None,
    )
    assert target is not None, "satellite_websocket nicht gefunden"
    # Der Aufruf ist ein Attribut (satellite_manager.apply_update_progress),
    # kein freistehender Name — sonst prüfte der Test nichts.
    called = {
        n.func.attr
        for n in ast.walk(target)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "apply_update_progress" in called, (
        "der Fortschrittszweig muss über apply_update_progress laufen — sonst "
        "greifen weder Endstufen-Erkennung noch die Sperre gegen Rückschritte"
    )


@pytest.mark.backend
@pytest.mark.unit
def test_non_string_message_on_a_terminal_stage_is_coerced():
    """Auch der Fehlertext kommt als JSON vom Gerät. Ein Objekt darf nicht in
    ``update_error`` landen — die Antwortmodelle typisieren ihn als ``str``."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress("sat-1", stage="failed", progress=0,
                              message={"boom": [1, 2, 3]})

    assert isinstance(mgr.get_satellite("sat-1").update_error, str)


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_failing_close_does_not_derail_the_sweep():
    """Ein kaputter Socket darf den Kehraus nicht anhalten — sonst bliebe alles
    dahinter (auch die OTA-Zeitgrenze) an einem einzigen Gerät hängen."""
    mgr = _manager_with_satellite()
    ws = mgr.satellites["sat-1"].websocket = AsyncMock()
    ws.close = AsyncMock(side_effect=RuntimeError("Socket ist weg"))
    mgr.satellites["sat-1"].last_heartbeat = 0.0

    with patch("api.websocket.kiosk_handler.broadcast_kiosk_event", new=AsyncMock()):
        await mgr.cleanup_stale()

    assert "sat-1" not in mgr.satellites


@pytest.mark.backend
@pytest.mark.asyncio
async def test_derostered_heartbeat_is_closed_not_acked():
    """Der Kern des Zombie-Befunds: nach einer Räumung darf der Heartbeat NICHT
    mehr bestätigt werden, sonst hält das Gerät die Leitung für gesund."""
    from ha_glue.api.websocket.satellite_handler import _reject_derostered_heartbeat

    manager = MagicMock()
    manager.is_connected.return_value = False
    ws = AsyncMock()

    assert await _reject_derostered_heartbeat(ws, "sat-1", manager) is True
    ws.close.assert_awaited()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_registered_heartbeat_passes_through():
    from ha_glue.api.websocket.satellite_handler import _reject_derostered_heartbeat

    manager = MagicMock()
    manager.is_connected.return_value = True
    ws = AsyncMock()

    assert await _reject_derostered_heartbeat(ws, "sat-1", manager) is False
    ws.close.assert_not_awaited()


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_failing_close_in_the_handler_is_swallowed():
    from ha_glue.api.websocket.satellite_handler import _reject_derostered_heartbeat

    manager = MagicMock()
    manager.is_connected.return_value = False
    ws = AsyncMock()
    ws.close = AsyncMock(side_effect=RuntimeError("weg"))

    assert await _reject_derostered_heartbeat(ws, "sat-1", manager) is True


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_broken_scheduler_does_not_break_ha_glue_startup():
    """Der Takt ist wichtig, aber nicht wichtiger als der Start der übrigen
    Untersysteme."""
    import ha_glue.bootstrap as bootstrap

    led = MagicMock()
    led.return_value.initialize = AsyncMock()

    with patch.object(bootstrap, "_schedule_satellite_cleanup",
                      side_effect=RuntimeError("kein Takt")), \
         patch.object(bootstrap, "_schedule_ha_keywords_preload"), \
         patch.object(bootstrap, "_init_zeroconf", new=AsyncMock()), \
         patch("ha_glue.utils.config.ha_glue_settings.presence_enabled", False), \
         patch("ha_glue.services.led_dimming_service.get_led_dimming_service", led):
        await bootstrap.ha_glue_on_startup(app=MagicMock())  # darf nicht werfen


@pytest.mark.backend
@pytest.mark.unit
def test_a_terminal_frame_without_a_message_leaves_no_error():
    """Lässt der Satellit ``message`` weg, liefert ``data.get`` einen leeren
    String. Der darf nicht als Begründung abgelegt werden: die Oberfläche zeigt
    den Fehlerblock nur bei gesetztem ``update_error``, ein leerer Text ergäbe
    also einen sichtbaren, aber inhaltslosen Kasten."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.IN_PROGRESS, stage="installing", progress=70)
    mgr.apply_update_progress("sat-1", stage="failed", progress=0, message="")

    sat = mgr.get_satellite("sat-1")
    assert sat.update_status == US.FAILED
    assert sat.update_error is None


# ==========================================================================
# 9. Härtung am EINZIGEN Schreiber — nicht je Aufrufer
# ==========================================================================


@pytest.mark.backend
@pytest.mark.unit
def test_set_update_status_coerces_regardless_of_caller():
    """Drei WS-Zweige speisen diesen Schreiber (update_progress,
    update_complete, update_failed). Nur einer war gehärtet — die beiden
    anderen reichten rohes Geräte-JSON bis in die Antwortmodelle durch, wo
    Pydantic nicht wandelt und ``list_satellites`` ohne Absicherung je Eintrag
    aus EINEM schlechten Eintrag einen 500 für die ganze Liste macht."""
    from ha_glue.services.satellite_manager import UpdateStatus as US

    mgr = _manager_with_satellite()
    mgr.set_update_status("sat-1", US.FAILED, stage={"boom": 1},
                          progress="viel", error={"auch": "boom"})

    sat = mgr.get_satellite("sat-1")
    assert isinstance(sat.update_stage, str)
    assert isinstance(sat.update_progress, int)
    assert isinstance(sat.update_error, str)


@pytest.mark.backend
@pytest.mark.unit
def test_set_version_is_bounded_and_never_empty():
    """``update_complete`` schrieb ``sat.version`` direkt am Schreiber vorbei —
    und ``version`` ist auf den Antwortmodellen ein blankes ``str``."""
    mgr = _manager_with_satellite()

    mgr.set_version("sat-1", {"nicht": "ein string"})
    assert isinstance(mgr.get_satellite("sat-1").version, str)

    mgr.set_version("sat-1", "x" * 10_000)
    assert len(mgr.get_satellite("sat-1").version) <= 64

    mgr.set_version("sat-1", None)
    assert mgr.get_satellite("sat-1").version == "unknown"


# ==========================================================================
# 10. Verwaiste Sitzungen — die Kehrseite der Aufnahme-Einschränkung
# ==========================================================================


@pytest.mark.backend
@pytest.mark.asyncio
async def test_orphaned_session_is_swept_when_the_satellite_is_gone():
    mgr = _manager_with_satellite()
    _session(mgr, state=None, age=1.0)
    del mgr.satellites["sat-1"]

    await mgr.cleanup_stale()

    assert "sess-1" not in mgr.sessions


@pytest.mark.backend
@pytest.mark.asyncio
async def test_orphaned_session_is_swept_after_a_fast_reconnect():
    """``unregister`` steigt bei schneller Wiederverbindung VOR dem
    Sitzungsabbau aus — die Sitzung des sterbenden Sockets bleibt liegen.
    Vorher fegte der unbedingte Durchlauf sie weg; seit der Einschränkung auf
    ``listening`` täte das niemand mehr, und sie hält ihr gepuffertes Audio."""
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.PROCESSING, age=1.0)
    # Neuer Socket hat sich registriert und eine neue Sitzung begonnen.
    mgr.satellites["sat-1"].current_session_id = "sess-2"

    await mgr.cleanup_stale()

    assert "sess-1" not in mgr.sessions


@pytest.mark.backend
@pytest.mark.asyncio
async def test_a_live_session_is_not_swept_as_orphaned():
    """Gegenprobe: der Aufräumer darf keine lebende Sitzung mitnehmen."""
    from ha_glue.services.satellite_manager import SatelliteState

    mgr = _manager_with_satellite()
    _session(mgr, state=SatelliteState.PROCESSING, age=1.0)

    await mgr.cleanup_stale()

    assert "sess-1" in mgr.sessions
