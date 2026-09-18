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
    mgr.apply_update_progress("sat-1", stage="rolling_back", progress=0, message="")

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
