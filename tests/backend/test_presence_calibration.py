"""Placement v2 calibration in tables (#1551, stage 2 part B).

The tables are the source; the JSON settings seed them once when both are
empty and are ignored afterwards; writes validate like the JSON parser and
reach the running decision without a restart; the guided proposal derives the
in-room bound by the same rule the labelled walk was scored with, refuses
with a stable code, and never invents a cutoff. Routes: ADMIN, fail-closed,
404 with presence off.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from ha_glue.models.database import PresenceReceiver, PresenceZone, PresenceZoneRoom
from ha_glue.services import presence_calibration as cal
from ha_glue.services.presence_debug import redact_device_key
from ha_glue.services.presence_placement import (
    IN_ROOM_PERCENTILE,
    ReceiverParams,
    percentile,
)
from ha_glue.services.presence_service import DeviceSighting
from models.database import Room

RECEIVERS_JSON = (
    '{"sat-a": {"offset_db": 0, "in_room_dbm": -55, "cutoff_dbm": -80},'
    ' "sat-b": {"offset_db": 1.5, "in_room_dbm": -60, "cutoff_dbm": -100}}'
)
KEY = "AA:BB:CC:DD:EE:01"


def _settings(receivers: str = "", zones: str = ""):
    """Patch the two JSON settings the seed reads."""
    return patch.multiple(
        cal.ha_glue_settings,
        presence_placement_v2_receivers=receivers,
        presence_placement_v2_zones=zones,
    )


async def _rooms(db, *names: str) -> list[int]:
    rows = [Room(name=n, alias=n.lower()) for n in names]
    db.add_all(rows)
    await db.commit()
    return [r.id for r in rows]


async def _count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar()


class _Engine:
    """Records what the running decision is handed."""

    def __init__(self):
        self.calls: list[tuple[dict, dict]] = []

    def set_calibration(self, params, zones):
        self.calls.append((dict(params), dict(zones)))


def _presence(sightings: dict[str, list[DeviceSighting]] | None = None, engine=None,
              scanners: dict | None = None):
    presence = MagicMock()
    presence._sightings = sightings or {}
    presence._placement_shadow = engine
    presence._scanner_status = scanners or {}
    return presence


def _status(cutoff=-100.0, age=2.0, received_at=1030.0):
    from ha_glue.services.presence_service import ScannerStatus

    return ScannerStatus(received_at=received_at, mode="continuous", running=True,
                         cutoff_dbm=cutoff, last_advert_age_s=age)


# ---------------------------------------------------------------------------
# Seeding and reading
# ---------------------------------------------------------------------------


@pytest.mark.database
class TestLoad:
    async def test_empty_tables_and_no_settings_stay_empty(self, db_session):
        with _settings():
            calibration = await cal.load_calibration(db_session)
        assert calibration.source == cal.SOURCE_EMPTY
        assert calibration.params == {} and calibration.zones == {}
        assert await _count(db_session, PresenceReceiver) == 0

    async def test_empty_tables_are_seeded_once_from_the_settings(self, db_session):
        a, b = await _rooms(db_session, "Kitchen", "Dining")
        with _settings(RECEIVERS_JSON, f'{{"open-plan": [{a}, {b}]}}'):
            seeded = await cal.load_calibration(db_session)
            again = await cal.load_calibration(db_session)
        assert seeded.source == cal.SOURCE_SEEDED
        assert seeded.params["sat-b"] == ReceiverParams(offset_db=1.5, in_room_dbm=-60.0, cutoff_dbm=-100.0)
        assert seeded.zones == {"open-plan": frozenset({a, b})}
        # The second start reads the tables; the settings are now ignored.
        assert again.source == cal.SOURCE_DATABASE
        assert again.params == seeded.params and again.zones == seeded.zones
        assert again.settings_ignored is True
        assert await _count(db_session, PresenceReceiver) == 2

    async def test_tables_win_over_different_settings(self, db_session):
        db_session.add(PresenceReceiver(satellite_id="sat-a", offset_db=0, in_room_dbm=-70, cutoff_dbm=-80))
        await db_session.commit()
        with _settings(RECEIVERS_JSON):
            calibration = await cal.load_calibration(db_session)
        assert calibration.params == {"sat-a": ReceiverParams(0.0, -70.0, -80.0)}
        assert calibration.settings_ignored is True

    async def test_unusable_settings_seed_nothing(self, db_session):
        with _settings('{"sat-a": {"offset_db": 0, "in_room_dbm": -90, "cutoff_dbm": -80}}'):
            calibration = await cal.load_calibration(db_session)
        assert calibration.source == cal.SOURCE_EMPTY
        assert await _count(db_session, PresenceReceiver) == 0

    async def test_seed_with_an_unknown_room_writes_nothing(self, db_session):
        """All or nothing: a zone naming a deleted room aborts the seed,
        receivers included, instead of seeding half of it."""
        with _settings(RECEIVERS_JSON, '{"open-plan": [900001, 900002]}'):
            calibration = await cal.load_calibration(db_session)
        assert calibration.source == cal.SOURCE_EMPTY
        assert await _count(db_session, PresenceReceiver) == 0
        assert await _count(db_session, PresenceZone) == 0

    async def test_emptied_by_an_admin_is_not_seeded_again(self, db_session):
        """Once the tables are the source, removing every receiver is a
        decision: a restart with the JSON still set must not bring it back."""
        with _settings(RECEIVERS_JSON):
            await cal.load_calibration(db_session)
            for satellite_id in ("sat-a", "sat-b"):
                await cal.delete_receiver(db_session, None, satellite_id)
            after_restart = await cal.load_calibration(db_session)
        assert after_restart.source == cal.SOURCE_DATABASE
        assert after_restart.params == {}
        assert after_restart.settings_ignored is True
        assert await _count(db_session, PresenceReceiver) == 0

    async def test_a_first_admin_write_also_makes_the_tables_the_source(self, db_session):
        with _settings():
            await cal.save_receiver(db_session, None, "sat-a", ReceiverParams(0, -60, -80), None)
            await cal.delete_receiver(db_session, None, "sat-a")
        with _settings(RECEIVERS_JSON):
            calibration = await cal.load_calibration(db_session)
        assert calibration.params == {} and calibration.source == cal.SOURCE_DATABASE

    async def test_a_zone_left_with_one_room_is_no_zone(self, db_session):
        a, b = await _rooms(db_session, "Kitchen", "Dining")
        with _settings():
            await cal.replace_zones(db_session, None, {"open-plan": frozenset({a, b})})
        await db_session.delete(await db_session.get(Room, b))
        await db_session.commit()
        calibration = await cal.read_calibration(db_session)
        assert calibration.zones == {}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@pytest.mark.database
class TestWrites:
    async def test_save_reaches_the_running_decision(self, db_session):
        engine = _Engine()
        with _settings():
            calibration = await cal.save_receiver(
                db_session, _presence(engine=engine), "sat-a",
                ReceiverParams(0.0, -62.0, -80.0), user_id=None,
            )
        assert calibration.params == {"sat-a": ReceiverParams(0.0, -62.0, -80.0)}
        assert engine.calls == [({"sat-a": ReceiverParams(0.0, -62.0, -80.0)}, {})]

    async def test_save_overwrites_and_delete_removes(self, db_session):
        engine = _Engine()
        presence = _presence(engine=engine)
        with _settings():
            await cal.save_receiver(db_session, presence, "sat-a", ReceiverParams(0, -62, -80), None)
            await cal.save_receiver(db_session, presence, "sat-a", ReceiverParams(2, -58, -80), None)
            assert (await cal.read_calibration(db_session)).params["sat-a"] == ReceiverParams(2, -58, -80)
            await cal.delete_receiver(db_session, presence, "sat-a")
        assert engine.calls[-1] == ({}, {})
        with pytest.raises(cal.CalibrationError) as e:
            await cal.delete_receiver(db_session, presence, "sat-a")
        assert e.value.code == "not_calibrated"

    def test_validation_is_the_json_parsers(self):
        assert cal.validate_receiver(0, -60, -80) == ReceiverParams(0.0, -60.0, -80.0)
        for bad in [(0, -80, -80), (0, -90, -80), (0, float("nan"), -80), (0, -60, -200), ("x", -60, -80)]:
            with pytest.raises(cal.CalibrationError) as e:
                cal.validate_receiver(*bad)
            assert e.value.code == "invalid_receiver", bad

    async def test_zones_replace_all_and_refuse_unknown_rooms(self, db_session):
        a, b, c = await _rooms(db_session, "Kitchen", "Dining", "Hall")
        with _settings():
            await cal.replace_zones(db_session, None, {"open-plan": frozenset({a, b})})
            await cal.replace_zones(db_session, None, {"downstairs": frozenset({b, c})})
            assert (await cal.read_calibration(db_session)).zones == {"downstairs": frozenset({b, c})}
            with pytest.raises(cal.CalibrationError) as e:
                await cal.replace_zones(db_session, None, {"x": frozenset({a, 900001})})
        assert e.value.code == "unknown_rooms" and e.value.params == {"room_ids": [900001]}
        # The refused write changed nothing.
        assert (await cal.read_calibration(db_session)).zones == {"downstairs": frozenset({b, c})}
        assert await _count(db_session, PresenceZoneRoom) == 2

    def test_zone_validation(self):
        for bad in ({"a": [1]}, {"a": [1, 2], "b": [2, 3]}, {"a": [5, 5]}):
            with pytest.raises(cal.CalibrationError) as e:
                cal.validate_zones(bad)
            assert e.value.code == "invalid_zones", bad


# ---------------------------------------------------------------------------
# Proposal (pure: the readings the presence service keeps)
# ---------------------------------------------------------------------------


def _readings(values, *, satellite="sat-a", start=1000.0, step=2.0, key=KEY):
    return {key: [DeviceSighting(satellite, 1, v, start + i * step) for i, v in enumerate(values)]}


@pytest.mark.unit
class TestProposal:
    def test_bound_is_the_percentile_of_this_receivers_readings_since_the_start(self):
        values = [-60, -58, -66, -62, -59, -70, -61, -63, -57, -64, -65, -60]
        sightings = _readings(values)
        sightings[KEY] += [
            DeviceSighting("sat-a", 1, -40, 900.0),   # before the start
            DeviceSighting("sat-b", 2, -30, 1005.0),  # another receiver
        ]
        p = cal.propose(_presence(sightings), redact_device_key(KEY), "sat-a",
                        since=1000.0, now=1030.0, current=ReceiverParams(0.5, -55, -80))
        assert p.samples == len(values)
        assert p.in_room_dbm == round(percentile(values, IN_ROOM_PERCENTILE), 1)
        assert (p.min_dbm, p.max_dbm) == (-70, -57)
        assert p.cutoff_dbm == -80 and p.offset_db == 0.5

    def test_an_uncalibrated_receiver_gets_no_invented_cutoff(self):
        p = cal.propose(_presence(_readings([-50] * 12)), redact_device_key(KEY), "sat-a",
                        since=1000.0, now=1030.0, current=None)
        assert p.cutoff_dbm is None and p.offset_db == 0.0 and p.cutoff_source is None

    def test_the_cutoff_the_satellite_reports_wins(self):
        presence = _presence(_readings([-50] * 12), scanners={"sat-a": _status(cutoff=-100.0)})
        for current in (None, ReceiverParams(0.5, -55, -80)):
            p = cal.propose(presence, redact_device_key(KEY), "sat-a",
                            since=1000.0, now=1030.0, current=current)
            assert p.cutoff_dbm == -100.0 and p.cutoff_source == "satellite"
        stored = cal.propose(_presence(_readings([-50] * 12)), redact_device_key(KEY), "sat-a",
                             since=1000.0, now=1030.0, current=ReceiverParams(0.5, -55, -80))
        assert stored.cutoff_dbm == -80 and stored.cutoff_source == "stored"

    def test_classic_readings_are_not_measured(self):
        """A Classic-BT value is no dBm level; it must not become a bound."""
        sightings = _readings([-50] * 12)
        for s in sightings[KEY]:
            s.comparable = False
        with pytest.raises(cal.CalibrationError) as e:
            cal.propose(_presence(sightings), redact_device_key(KEY), "sat-a",
                        since=1000.0, now=1030.0, current=None)
        assert e.value.code == "too_few_readings"

    @pytest.mark.parametrize(
        ("since", "now", "values", "device", "code"),
        [
            (1050.0, 1030.0, [-50] * 12, KEY, "not_started"),
            (1000.0, 1000.0 + 121, [-50] * 12, KEY, "too_old"),
            (1000.0, 1030.0, [-50] * 12, "AA:BB:CC:DD:EE:99", "device_not_heard"),
            (1000.0, 1030.0, [-50] * 9, KEY, "too_few_readings"),
            (1000.0, 1030.0, [-85] * 12, KEY, "bound_not_above_cutoff"),
        ],
    )
    def test_refusals_carry_a_code(self, since, now, values, device, code):
        with patch.object(cal.ha_glue_settings, "presence_stale_timeout", 120):
            with pytest.raises(cal.CalibrationError) as e:
                cal.propose(_presence(_readings(values)), redact_device_key(device), "sat-a",
                            since=since, now=now, current=ReceiverParams(0, -60, -80))
        assert e.value.code == code
        assert e.value.detail()["code"] == code

    def test_the_raw_key_is_never_accepted(self):
        """The browser only ever sees the redacted key; the raw MAC finds nothing."""
        with pytest.raises(cal.CalibrationError) as e:
            cal.propose(_presence(_readings([-50] * 12)), KEY, "sat-a",
                        since=1000.0, now=1030.0, current=None)
        assert e.value.code == "device_not_heard"

    def test_out_of_range_levels_do_not_count(self):
        values = [-50] * 9 + [float("nan"), 25, -200]
        with pytest.raises(cal.CalibrationError) as e:
            cal.propose(_presence(_readings(values)), redact_device_key(KEY), "sat-a",
                        since=1000.0, now=1030.0, current=None)
        assert e.value.code == "too_few_readings" and e.value.params["samples"] == 9


# ---------------------------------------------------------------------------
# Engine reload
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_empty_calibration_never_clears_the_running_decision():
    """Never seeded (no settings, or a refused seed): the decision keeps the
    numbers it was built with; an admin-emptied calibration is applied."""
    engine = _Engine()
    cal.apply_calibration(_presence(engine=engine), cal.Calibration({}, {}, cal.SOURCE_EMPTY))
    assert engine.calls == []
    cal.apply_calibration(_presence(engine=engine), cal.Calibration({}, {}, cal.SOURCE_DATABASE))
    assert engine.calls == [({}, {})]


@pytest.mark.unit
def test_engine_takes_new_numbers_without_losing_its_state():
    from ha_glue.services.presence_placement import PlacementConfig
    from ha_glue.services.presence_placement_shadow import PlacementShadow

    shadow = PlacementShadow({}, {}, PlacementConfig(
        time_constant_s=6, fresh_s=20, margin_db=10, hysteresis_db=3,
        absent_after_s=120, receiver_alive_s=60,
    ), roster=dict)
    shadow._zone_since["k"] = (frozenset({1}), 5.0)
    shadow.set_calibration({"sat-a": ReceiverParams(0, -60, -80)}, {"z": frozenset({1, 2})})
    assert shadow._params == {"sat-a": ReceiverParams(0, -60, -80)}
    assert shadow._zones == {"z": frozenset({1, 2})}
    assert shadow._zone_since == {"k": (frozenset({1}), 5.0)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(db_session):
    from ha_glue.api.routes import presence as route
    from services.auth_service import get_current_user
    from services.database import get_db

    app = FastAPI()
    app.include_router(route.router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: None
    engine = _Engine()
    with (
        patch.object(route.ha_glue_settings, "presence_enabled", True),
        patch.object(route, "get_presence_service", lambda: _presence(_readings([-50] * 12), engine)),
        _settings(),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.app = app
            c.engine = engine
            yield c


@pytest.mark.database
class TestRoutes:
    async def test_crud_round_trip(self, client, db_session):
        a, b = await _rooms(db_session, "Kitchen", "Dining")
        r = await client.get("/api/presence/calibration")
        assert r.status_code == 200 and r.json()["source"] == "empty"
        assert r.json()["engine_active"] is True

        r = await client.put("/api/presence/calibration/receivers/sat-a",
                             json={"offset_db": 0, "in_room_dbm": -61, "cutoff_dbm": -80})
        assert r.status_code == 200
        assert r.json()["receivers"] == [
            {"satellite_id": "sat-a", "offset_db": 0, "in_room_dbm": -61, "cutoff_dbm": -80}
        ]
        assert client.engine.calls[-1][0] == {"sat-a": ReceiverParams(0, -61, -80)}

        r = await client.put("/api/presence/calibration/zones",
                             json={"zones": [{"name": "open-plan", "room_ids": [b, a]}]})
        assert r.status_code == 200
        assert r.json()["zones"] == [{"name": "open-plan", "room_ids": sorted([a, b])}]

        r = await client.delete("/api/presence/calibration/receivers/sat-a")
        assert r.status_code == 200 and r.json()["receivers"] == []
        r = await client.delete("/api/presence/calibration/receivers/sat-a")
        assert r.status_code == 404 and r.json()["detail"]["code"] == "not_calibrated"

    async def test_refusals_are_coded(self, client):
        r = await client.put("/api/presence/calibration/receivers/sat-a",
                             json={"offset_db": 0, "in_room_dbm": -90, "cutoff_dbm": -80})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_receiver"
        r = await client.put("/api/presence/calibration/zones",
                             json={"zones": [{"name": "z", "room_ids": [1, 2]},
                                             {"name": "z", "room_ids": [3, 4]}]})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_zones"
        r = await client.put("/api/presence/calibration/zones",
                             json={"zones": [{"name": "z" * 101, "room_ids": [1, 2]}]})
        assert r.status_code == 422  # refused before the database, not a 500

    async def test_the_response_lists_what_the_satellites_report(self, client):
        from ha_glue.api.routes import presence as route

        now = time.time()
        presence = _presence(scanners={
            "sat-a": _status(cutoff=-100.0, age=2.0, received_at=now),
            "sat-b": _status(cutoff=-80.0, age=50.0, received_at=now),
        })
        with patch.object(route, "get_presence_service", lambda: presence):
            body = (await client.get("/api/presence/calibration")).json()
        a, b = body["scanners"]
        assert a["satellite_id"] == "sat-a" and a["cutoff_dbm"] == -100.0 and a["hearing"] is True
        assert b["hearing"] is False and b["last_advert_age_s"] >= 50.0

    async def test_guided_measurement(self, client):
        r = await client.post("/api/presence/calibration/measurement")
        assert r.status_code == 200
        assert abs(r.json()["since"] - time.time()) < 5
        # The fake service holds readings from t=1000; a measurement since
        # then is long past the window, a fresh one has none yet.
        r = await client.post("/api/presence/calibration/proposal", json={
            "device_key": redact_device_key(KEY), "satellite_id": "sat-a", "since": 1000.0,
        })
        assert r.status_code == 422 and r.json()["detail"]["code"] == "too_old"

    async def test_presence_off_is_404(self, client):
        from ha_glue.api.routes import presence as route

        with patch.object(route.ha_glue_settings, "presence_enabled", False):
            for method, url in [
                ("get", "/api/presence/calibration"),
                ("post", "/api/presence/calibration/measurement"),
                ("delete", "/api/presence/calibration/receivers/sat-a"),
            ]:
                assert (await getattr(client, method)(url)).status_code == 404, url

    async def test_admin_only_when_auth_is_on(self, client):
        from services.auth_service import get_current_user
        from utils.config import settings

        member = MagicMock(id=7)
        member.has_permission = lambda perm: False
        with patch.object(settings, "auth_enabled", True):
            client.app.dependency_overrides[get_current_user] = lambda: None
            assert (await client.get("/api/presence/calibration")).status_code == 401
            client.app.dependency_overrides[get_current_user] = lambda: member
            for method, url, body in [
                ("get", "/api/presence/calibration", None),
                ("put", "/api/presence/calibration/zones", {"zones": []}),
                ("put", "/api/presence/calibration/receivers/sat-a",
                 {"offset_db": 0, "in_room_dbm": -60, "cutoff_dbm": -80}),
                ("post", "/api/presence/calibration/measurement", None),
                ("post", "/api/presence/calibration/proposal",
                 {"device_key": "mac:00000000", "satellite_id": "sat-a", "since": 0}),
                ("delete", "/api/presence/calibration/receivers/sat-a", None),
            ]:
                kwargs = {"json": body} if body is not None else {}
                r = await getattr(client, method)(url, **kwargs)
                assert r.status_code == 403, url
        assert client.engine.calls == []


# ---------------------------------------------------------------------------
# Startup and live-readings payload
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_payload_names_the_v2_mode():
    from ha_glue.services.presence_debug import build_sightings_payload

    presence = MagicMock()
    presence._sightings = {}
    presence._placement_shadow = object()
    presence._placement_state = presence._placement_shadow
    assert build_sightings_payload(presence)["placement_v2_mode"] == "state"
    presence._placement_state = None
    assert build_sightings_payload(presence)["placement_v2_mode"] == "shadow"
    presence._placement_shadow = None
    assert "placement_v2_mode" not in build_sightings_payload(presence)


class _Session:
    async def __aenter__(self):
        return "db"

    async def __aexit__(self, *exc):
        return False


@pytest.mark.unit
@pytest.mark.parametrize("fails", [False, True])
async def test_startup_loads_the_tables_into_the_decision(fails):
    """`_init_presence` hands the table numbers to the decision; a failing
    load is one ERROR line, leaves the JSON numbers in effect and never stops
    the start."""
    from unittest.mock import AsyncMock

    from ha_glue import bootstrap

    engine = _Engine()
    presence = _presence(engine=engine)
    presence.load_device_registry = AsyncMock()
    loaded = cal.Calibration({"sat-a": ReceiverParams(0, -60, -80)}, {}, cal.SOURCE_DATABASE)
    load = AsyncMock(side_effect=RuntimeError("db down") if fails else None, return_value=loaded)

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _RoomSession(_Session):
        async def __aenter__(self):
            session = MagicMock()
            session.execute = AsyncMock(return_value=_Result())
            return session

    with (
        patch("ha_glue.services.presence_analytics.register_presence_analytics_hooks"),
        patch("ha_glue.services.presence_webhook.register_presence_webhooks"),
        patch.object(bootstrap, "_schedule_presence_event_cleanup"),
        patch("ha_glue.services.presence_service.get_presence_service", lambda: presence),
        patch("services.database.AsyncSessionLocal", _RoomSession),
        patch.object(cal, "load_calibration", load),
        patch.object(bootstrap.logger, "error") as error,
    ):
        await bootstrap._init_presence(MagicMock())

    load.assert_awaited_once()
    if fails:
        assert engine.calls == []
        assert error.call_count == 1 and "db down" not in error.call_args[0][0]
    else:
        assert engine.calls == [({"sat-a": ReceiverParams(0, -60, -80)}, {})]
        error.assert_not_called()
