"""The fleet watchdog: the settle period, the right signal, no false alarm.

The most important test here is `test_a_connected_satellite_is_never_reported`.
It holds the insight that shaped the whole watchdog: `last_authenticated_at` is
NOT fit as a sign of life. The column is set when the CONNECTION IS
ESTABLISHED, so its age measures "time since the last reconnect". A watchdog
built on it would on 2026-09-26 have reported the three HEALTHY satellites
(they had just re-authenticated, so they were fresh) and said nothing about the
three DEAD ones.
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
    """Settle period over -- otherwise the task does not judge at all."""
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
        # 🛑 Without the settle period EVERY rollout reported the whole fleet
        # as dead: the registry lives in memory and is empty at startup. A
        # false alarm of that size makes every real report worthless.
        monkeypatch.setattr(wd, "_STARTED_AT", time.monotonic())
        n, st = _Notify(), _State("a")
        out = await _run(notify=n, connected=[],
                         rows=_rows(("a", "Kueche", 99)), state=st)
        assert out is None
        assert n.calls == []
        # And the marker stays UNTOUCHED. If the settle period cleared it,
        # the next run after every restart would be a "change" -- so one
        # report per rollout after all.
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
        """Even when its last connection was established 30 DAYS ago.

        This is the core. A stably connected satellite does not
        re-authenticate -- its column ages while the device itself is very much
        alive.
        """
        n = _Notify()
        out = await _run(notify=n, connected=["sat-a"],
                         rows=_rows(("sat-a", "Kueche", 24 * 30)), state=_State())
        assert out is None, "ein VERBUNDENER Satellit darf nie gemeldet werden"
        assert n.calls == []

    async def test_a_freshly_authenticated_but_absent_one_is_reported(self, settled):
        # The other direction: just authenticated, but not in the registry
        # -- for instance because the connection dropped again immediately.
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
        """🛑 The core of this version, and a correction to myself.

        The first version relied on `notify_admin`'s `dedup_key` and I claimed
        that this prevented an hourly repeat report. Operation disproved that
        within THREE HOURS: three identical notifications at 13:59, 14:59,
        15:59, because `proactive_suppression_window` is 60 seconds. On a
        30-day outage that would be 24 reports a day.
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
        # A second room drops out -> new situation, new message.
        await _run(notify=n, connected=[],
                   rows=_rows(("a", "Kueche", 5), ("b", "Bad", 1)), state=st)
        assert len(n.calls) == 2

    async def test_the_marker_ignores_row_order(self, settled):
        # Otherwise any other ordering of the query would be a "change".
        n, st = _Notify(), _State()
        await _run(notify=n, connected=[],
                   rows=_rows(("b", "Bad", 5), ("a", "Kueche", 5)), state=st)
        await _run(notify=n, connected=[],
                   rows=_rows(("a", "Kueche", 5), ("b", "Bad", 5)), state=st)
        assert len(n.calls) == 1

    async def test_a_return_is_reported_and_CLEARS_the_marker(self, settled):
        """A return is a message of its own -- and the marker MUST be cleared.

        If it stays, the next outage of the same rooms would be silent.
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

        # ...and the same outage afterwards reports again.
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
        # The task still reports its result into the run log.
        assert out is not None

    async def test_an_empty_fleet_is_silent(self, settled):
        n = _Notify()
        out = await _run(notify=n, connected=[], rows=[], state=_State())
        assert out is None
        assert n.calls == []


async def _async(value):
    return value


class TestTheFeatureGate:
    """The inner gate -- the path `manager=None` takes.

    🛑 This block was missing, and that was no accident: every other test here
    goes through `_run()`, and that ALWAYS feeds in a manager. So
    `manager is None` was never true and the whole branch was dead code as far
    as the suite was concerned -- including the `ha_glue` import it is meant to
    shield. That very import is the reason `test_ha_glue_boundary.py` lets this
    file through. An allowlist entry whose condition is unchecked is not a
    justification but an assertion.
    """

    async def test_without_the_satellite_feature_it_never_touches_ha_glue(
        self, monkeypatch, settled
    ):
        from utils.config import settings

        monkeypatch.setattr(settings, "feature_satellites", False)
        assert settings.features.get("satellites") is False

        # Were the gate gone, the code would run into the import and from
        # there into the real registry. A failure here means the boundary is
        # open.
        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=None,
            fetch_enrolled=lambda: _async(_rows(("a", "Kueche", 5))),
            read_state=_State().read, write_state=_State().write,
        )
        assert out is None
        assert n.calls == []

    async def test_with_the_feature_on_it_asks_the_registry(self, monkeypatch, settled):
        """The other direction: with the feature ON the manager is fetched.

        The import is replaced so the test runs without an `ha_glue` registry
        -- what is checked is THAT it happens, not what lies behind it.
        """
        from utils.config import settings

        monkeypatch.setattr(settings, "feature_satellites", True)
        asked = []

        class _Mgr:
            satellites: dict = {}

        def _get():
            asked.append(1)
            return _Mgr()

        import sys
        import types

        mod = types.ModuleType("ha_glue.services.satellite_manager")
        mod.get_satellite_manager = _get
        monkeypatch.setitem(sys.modules, "ha_glue.services.satellite_manager", mod)

        n = _Notify()
        out = await wd.check_satellite_fleet(
            notify=n, manager=None,
            fetch_enrolled=lambda: _async(_rows(("a", "Kueche", 5))),
            read_state=_State().read, write_state=_State().write,
        )
        assert asked == [1], "mit Satellitenmerkmal MUSS die Registratur gefragt werden"
        assert out is not None, "der Satellit fehlt, also muss gemeldet werden"
