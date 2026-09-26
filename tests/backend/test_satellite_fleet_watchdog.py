"""Die Flottenwache: Karenz, richtiges Signal, kein Fehlalarm.

Der wichtigste Test hier ist `test_a_connected_satellite_is_never_reported`. Er
haelt die Einsicht fest, die den ganzen Waechter geformt hat: `last_authenticated_at`
taugt NICHT als Lebenszeichen. Die Spalte wird beim VERBINDUNGSAUFBAU gesetzt,
ihr Alter misst also „Zeit seit dem letzten Neuverbinden". Ein Waechter darauf
haette am 2026-09-26 die drei GESUNDEN Satelliten gemeldet (sie hatten sich
gerade neu angemeldet, waren also frisch) und die drei TOTEN verschwiegen.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from services import satellite_fleet_watchdog as wd

pytestmark = [pytest.mark.unit]


class _Manager:
    def __init__(self, connected):
        self.satellites = {s: object() for s in connected}


def _rows(*specs):
    """(satellite_id, room, Alter des letzten Verbindungsaufbaus in Stunden)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return [(sid, room, None if hours is None else now - timedelta(hours=hours))
            for sid, room, hours in specs]


@pytest.fixture
def settled(monkeypatch):
    """Karenz vorbei — sonst urteilt die Aufgabe gar nicht."""
    monkeypatch.setattr(wd, "_STARTED_AT", time.monotonic() - wd.SETTLE_SECONDS - 1)


class _Notify:
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        return True


class TestTheSettlePeriod:
    async def test_it_does_not_judge_right_after_a_restart(self, monkeypatch):
        # 🛑 Ohne die Karenz meldete JEDER Rollout die ganze Flotte als tot: die
        # Registratur lebt im Arbeitsspeicher und ist beim Start leer. Ein
        # Fehlalarm dieser Groesse macht jede echte Meldung wertlos.
        monkeypatch.setattr(wd, "_STARTED_AT", time.monotonic())
        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=_Manager([]),
            fetch_enrolled=lambda: _async(_rows(("a", "Kueche", 99))),
        )
        assert out is None
        assert n.calls == []

    async def test_after_the_settle_period_it_judges(self, settled):
        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=_Manager([]),
            fetch_enrolled=lambda: _async(_rows(("a", "Kueche", 5))),
        )
        assert out is not None
        assert len(n.calls) == 1


class TestTheRightSignal:
    async def test_a_connected_satellite_is_never_reported(self, settled):
        """Auch wenn sein letzter Verbindungsaufbau 30 TAGE her ist.

        Das ist der Kern. Ein stabil verbundener Satellit meldet sich nicht neu
        an — seine Spalte altert, er selbst ist quicklebendig.
        """
        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=_Manager(["sat-a"]),
            fetch_enrolled=lambda: _async(_rows(("sat-a", "Kueche", 24 * 30))),
        )
        assert out is None, "ein VERBUNDENER Satellit darf nie gemeldet werden"
        assert n.calls == []

    async def test_a_freshly_authenticated_but_absent_one_is_reported(self, settled):
        # Die Gegenrichtung: gerade erst angemeldet, aber nicht in der
        # Registratur — etwa weil die Verbindung sofort wieder abriss.
        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=_Manager([]),
            fetch_enrolled=lambda: _async(_rows(("sat-a", "Kueche", 0))),
        )
        assert out is not None
        assert len(n.calls) == 1

    async def test_only_the_absent_ones_appear(self, settled):
        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=_Manager(["sat-a", "sat-b"]),
            fetch_enrolled=lambda: _async(_rows(
                ("sat-a", "Kueche", 1), ("sat-b", "Bad", 1), ("sat-c", "Flur", 50))),
        )
        assert "Flur" in out
        assert "Kueche" not in out and "Bad" not in out


class TestTheMessage:
    async def test_the_dedup_key_follows_the_SITUATION_not_the_run(self, settled):
        # Solange dieselben Raeume fehlen, soll nicht stuendlich erneut gemeldet
        # werden. Aendert sich die Menge, ist es eine neue Lage und eine neue
        # Nachricht.
        n = _Notify()
        for _ in range(2):
            await wd.check_satellite_fleet(
                notify=n, manager=_Manager([]),
                fetch_enrolled=lambda: _async(_rows(("b", "Bad", 3), ("a", "Kueche", 3))),
            )
        assert n.calls[0]["dedup_key"] == n.calls[1]["dedup_key"]
        # sortiert, damit die Reihenfolge der Zeilen den Schluessel nicht aendert
        assert n.calls[0]["dedup_key"] == "satellite_fleet_offline:a,b"

        await wd.check_satellite_fleet(
            notify=n, manager=_Manager([]),
            fetch_enrolled=lambda: _async(_rows(("a", "Kueche", 3))),
        )
        assert n.calls[2]["dedup_key"] != n.calls[0]["dedup_key"]

    async def test_days_instead_of_hours_once_it_gets_long(self, settled):
        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=_Manager([]),
            fetch_enrolled=lambda: _async(_rows(("a", "BensZimmer", 24 * 30))),
        )
        assert "30 Tagen" in out

    async def test_never_connected_says_so(self, settled):
        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=_Manager([]),
            fetch_enrolled=lambda: _async(_rows(("a", "Neu", None))),
        )
        assert "nie verbunden" in out


class TestSurvival:
    async def test_a_failing_notification_does_not_break_the_task(self, settled):
        async def boom(**_kw):
            raise RuntimeError("Zustellung abgelehnt")

        out = await wd.check_satellite_fleet(
            notify=boom, manager=_Manager([]),
            fetch_enrolled=lambda: _async(_rows(("a", "Kueche", 5))),
        )
        # Die Aufgabe meldet trotzdem ihr Ergebnis ins Laufprotokoll.
        assert out is not None

    async def test_an_empty_fleet_is_silent(self, settled):
        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=_Manager([]), fetch_enrolled=lambda: _async([]))
        assert out is None
        assert n.calls == []


async def _async(value):
    return value
