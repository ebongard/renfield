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


class _State:
    """Der dauerhafte Marker, in-memory nachgebildet."""

    def __init__(self, initial: str = ""):
        self.value = initial
        self.writes: list[str] = []

    async def read(self):
        return self.value

    async def write(self, v):
        self.value = v
        self.writes.append(v)


async def _run(*, notify, connected, rows, state):
    return await wd.check_satellite_fleet(
        notify=notify, manager=_Manager(connected),
        fetch_enrolled=lambda: _async(rows),
        read_state=state.read, write_state=state.write,
    )


class TestTheSettlePeriod:
    async def test_it_does_not_judge_right_after_a_restart(self, monkeypatch):
        # 🛑 Ohne die Karenz meldete JEDER Rollout die ganze Flotte als tot: die
        # Registratur lebt im Arbeitsspeicher und ist beim Start leer. Ein
        # Fehlalarm dieser Groesse macht jede echte Meldung wertlos.
        monkeypatch.setattr(wd, "_STARTED_AT", time.monotonic())
        n, st = _Notify(), _State("a")
        out = await _run(notify=n, connected=[],
                         rows=_rows(("a", "Kueche", 99)), state=st)
        assert out is None
        assert n.calls == []
        # Und der Marker bleibt UNBERUEHRT. Wuerde die Karenz ihn leeren, waere
        # nach jedem Neustart der naechste Lauf eine "Aenderung" — also doch
        # wieder eine Meldung je Rollout.
        assert st.writes == []
        assert st.value == "a"

    async def test_after_the_settle_period_it_judges(self, settled):
        n = _Notify()
        out = await _run(notify=n, connected=[],
                         rows=_rows(("a", "Kueche", 5)), state=_State())
        assert out is not None
        assert len(n.calls) == 1


class TestTheRightSignal:
    async def test_a_connected_satellite_is_never_reported(self, settled):
        """Auch wenn sein letzter Verbindungsaufbau 30 TAGE her ist.

        Das ist der Kern. Ein stabil verbundener Satellit meldet sich nicht neu
        an — seine Spalte altert, er selbst ist quicklebendig.
        """
        n = _Notify()
        out = await _run(notify=n, connected=["sat-a"],
                         rows=_rows(("sat-a", "Kueche", 24 * 30)), state=_State())
        assert out is None, "ein VERBUNDENER Satellit darf nie gemeldet werden"
        assert n.calls == []

    async def test_a_freshly_authenticated_but_absent_one_is_reported(self, settled):
        # Die Gegenrichtung: gerade erst angemeldet, aber nicht in der
        # Registratur — etwa weil die Verbindung sofort wieder abriss.
        n = _Notify()
        out = await _run(notify=n, connected=[],
                         rows=_rows(("sat-a", "Kueche", 0)), state=_State())
        assert out is not None
        assert len(n.calls) == 1

    async def test_only_the_absent_ones_appear(self, settled):
        n = _Notify()
        out = await _run(notify=n, connected=["sat-a", "sat-b"],
                         rows=_rows(
                ("sat-a", "Kueche", 1), ("sat-b", "Bad", 1), ("sat-c", "Flur", 50)), state=_State())
        assert "Flur" in out
        assert "Kueche" not in out and "Bad" not in out


class TestTheMessage:
    async def test_an_unchanged_situation_is_not_reported_again(self, settled):
        """🛑 Der Kern dieser Fassung, und eine Berichtigung an mir selbst.

        Die erste Fassung verliess sich auf den `dedup_key` von `notify_admin`
        und ich behauptete, damit werde nicht stuendlich erneut gemeldet. Der
        Betrieb hat das in DREI STUNDEN widerlegt: drei identische
        Benachrichtigungen um 13:59, 14:59, 15:59, weil
        `proactive_suppression_window` 60 Sekunden ist. Bei einem 30-Tage-Ausfall
        waeren das 24 Meldungen am Tag.
        """
        n, st = _Notify(), _State()
        rows = _rows(("a", "Kueche", 5), ("b", "Bad", 5))
        for _ in range(4):
            out = await _run(notify=n, connected=[], rows=rows, state=st)
            assert out is not None, "die Lage steht weiter im Laufprotokoll"
        assert len(n.calls) == 1, "nur die ERSTE Aenderung wird gemeldet"

    async def test_a_changed_situation_is_reported_again(self, settled):
        n, st = _Notify(), _State()
        await _run(notify=n, connected=[], rows=_rows(("a", "Kueche", 5)), state=st)
        # Ein zweiter Raum fallt aus -> neue Lage, neue Nachricht.
        await _run(notify=n, connected=[],
                   rows=_rows(("a", "Kueche", 5), ("b", "Bad", 1)), state=st)
        assert len(n.calls) == 2

    async def test_the_marker_ignores_row_order(self, settled):
        # Sonst waere jede andere Sortierung der Abfrage eine "Aenderung".
        n, st = _Notify(), _State()
        await _run(notify=n, connected=[],
                   rows=_rows(("b", "Bad", 5), ("a", "Kueche", 5)), state=st)
        await _run(notify=n, connected=[],
                   rows=_rows(("a", "Kueche", 5), ("b", "Bad", 5)), state=st)
        assert len(n.calls) == 1

    async def test_a_return_is_reported_and_CLEARS_the_marker(self, settled):
        """Rueckkehr ist eine eigene Nachricht — und der Marker MUSS leer werden.

        Bleibt er stehen, waere der naechste Ausfall derselben Raeume stumm.
        """
        n, st = _Notify(), _State()
        rows = _rows(("a", "Kueche", 5))
        await _run(notify=n, connected=[], rows=rows, state=st)
        assert st.value == "a"

        out = await _run(notify=n, connected=["a"], rows=rows, state=st)
        assert out is None
        assert st.value == "", "der Marker muss geleert werden"
        assert len(n.calls) == 2
        assert "wieder verbunden" in n.calls[1]["title"]

        # …und derselbe Ausfall danach meldet wieder.
        await _run(notify=n, connected=[], rows=rows, state=st)
        assert len(n.calls) == 3

    async def test_all_present_without_a_prior_outage_stays_silent(self, settled):
        n, st = _Notify(), _State()
        out = await _run(notify=n, connected=["a"], rows=_rows(("a", "Kueche", 1)),
                         state=st)
        assert out is None
        assert n.calls == [], "keine Rueckkehr melden, wenn nie etwas fehlte"

    async def test_days_instead_of_hours_once_it_gets_long(self, settled):
        n = _Notify()
        out = await _run(notify=n, connected=[],
                         rows=_rows(("a", "BensZimmer", 24 * 30)), state=_State())
        assert "30 Tagen" in out

    async def test_never_connected_says_so(self, settled):
        n = _Notify()
        out = await _run(notify=n, connected=[],
                         rows=_rows(("a", "Neu", None)), state=_State())
        assert "nie verbunden" in out


class TestSurvival:
    async def test_a_failing_notification_does_not_break_the_task(self, settled):
        async def boom(**_kw):
            raise RuntimeError("Zustellung abgelehnt")

        out = await _run(notify=boom, connected=[],
                         rows=_rows(("a", "Kueche", 5)), state=_State())
        # Die Aufgabe meldet trotzdem ihr Ergebnis ins Laufprotokoll.
        assert out is not None

    async def test_an_empty_fleet_is_silent(self, settled):
        n = _Notify()
        out = await _run(notify=n, connected=[], rows=[], state=_State())
        assert out is None
        assert n.calls == []


async def _async(value):
    return value
