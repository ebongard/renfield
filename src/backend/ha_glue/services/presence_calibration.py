"""
Placement v2 calibration — stage 2, part B.

The receiver numbers and open-plan zones placement v2 decides with live in
tables (``presence_receivers``, ``presence_zones``, ``presence_zone_rooms``):

  * **Source:** the tables (owner's decision, 2026-11-25). On a start with BOTH
    tables empty the JSON settings ``PRESENCE_PLACEMENT_V2_RECEIVERS`` /
    ``…_ZONES`` are copied in once; after that the JSON is ignored, and a start
    that still finds it set says so with one WARNING.
  * **Writes** go through this module, which validates exactly like the JSON
    parser (``presence_placement.parse_*``) and hands the new numbers to the
    running decision (``PlacementShadow.set_calibration``) — no restart.
  * **Proposal** (the guided flow on ``/admin/presence``): the person stands in
    the receiver's room with the device; the readings that receiver took of
    that device since the start become a proposed in-room bound by the rule the
    labelled walk was scored with (``IN_ROOM_PERCENTILE``). Nothing is saved
    until an admin confirms. The readings are the ones the presence service
    keeps anyway (``_sightings``, the deadline window) — no second recorder.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ha_glue.services.presence_placement import (
    IN_ROOM_PERCENTILE,
    RSSI_MAX_DBM,
    RSSI_MIN_DBM,
    PlacementConfigError,
    ReceiverParams,
    parse_receiver_params,
    parse_zones,
    percentile,
)
from ha_glue.utils.config import ha_glue_settings
from utils.log_safety import error_text_without_content

if TYPE_CHECKING:
    from ha_glue.services.presence_service import PresenceService

LOG_PREFIX = "Presence calibration:"

# Fewest readings a proposal rests on. A choice: the satellites that cut at
# -80 re-send their own smoothed value for 20 s, so a handful of readings may
# be one measurement (design doc, "the labelled walk").
MIN_SAMPLES = 10

# Set once the tables are the source (seeded or written by an admin). Without
# it, "both tables empty" could not tell "never seeded" from "an admin removed
# everything" — and a restart would silently bring the JSON numbers back.
MARKER_KEY = "presence.calibration_in_tables"

SOURCE_DATABASE = "database"
SOURCE_SEEDED = "seeded_from_settings"
SOURCE_EMPTY = "empty"


class CalibrationError(ValueError):
    """A calibration write or proposal that must be refused.

    ``code`` is stable and machine-readable (the admin UI translates it),
    ``params`` the numbers the translation shows. The message is for logs."""

    def __init__(self, code: str, message: str, **params: object):
        super().__init__(message)
        self.code = code
        self.params = params

    def detail(self) -> dict[str, object]:
        return {"code": self.code, **self.params}


@dataclass(frozen=True)
class Calibration:
    params: dict[str, ReceiverParams]
    zones: dict[str, frozenset[int]]
    source: str
    # The JSON settings are set but ignored because the tables hold values.
    settings_ignored: bool = False


@dataclass(frozen=True)
class Proposal:
    satellite_id: str
    samples: int
    in_room_dbm: float
    min_dbm: float
    median_dbm: float
    max_dbm: float
    # The cutoff the satellite reports, else the receiver's stored one; None
    # when neither is known (an older satellite, uncalibrated).
    cutoff_dbm: float | None
    offset_db: float
    # "satellite" | "stored" | None — where ``cutoff_dbm`` comes from.
    cutoff_source: str | None = None


def _settings_set() -> bool:
    return bool(
        (ha_glue_settings.presence_placement_v2_receivers or "").strip()
        or (ha_glue_settings.presence_placement_v2_zones or "").strip()
    )


# ---------------------------------------------------------------------------
# Reading and seeding
# ---------------------------------------------------------------------------


async def _marked(db: AsyncSession) -> bool:
    from models.database import SystemSetting

    return await db.get(SystemSetting, MARKER_KEY) is not None


async def _mark(db: AsyncSession) -> None:
    """Record that the tables are the source (in the caller's transaction)."""
    from models.database import SystemSetting

    if await db.get(SystemSetting, MARKER_KEY) is None:
        db.add(SystemSetting(key=MARKER_KEY, value="true"))


async def read_calibration(db: AsyncSession) -> Calibration:
    """The tables as they are (no seeding). ``source`` is ``database`` once the
    tables have been seeded or written, even when an admin has emptied them."""
    from ha_glue.models.database import PresenceReceiver, PresenceZone, PresenceZoneRoom

    rows = (await db.execute(select(PresenceReceiver))).scalars().all()
    params = {
        r.satellite_id: ReceiverParams(
            offset_db=r.offset_db, in_room_dbm=r.in_room_dbm, cutoff_dbm=r.cutoff_dbm
        )
        for r in rows
    }
    zone_rows = (await db.execute(select(PresenceZone))).scalars().all()
    members = (await db.execute(select(PresenceZoneRoom))).scalars().all()
    by_zone: dict[int, set[int]] = {}
    for m in members:
        by_zone.setdefault(m.zone_id, set()).add(m.room_id)
    zones = {z.name: frozenset(by_zone.get(z.id, set())) for z in zone_rows}
    # A zone left with fewer than two rooms (a room was deleted) is no zone.
    zones = {name: rooms for name, rooms in zones.items() if len(rooms) >= 2}
    in_tables = bool(params or zones) or await _marked(db)
    source = SOURCE_DATABASE if in_tables else SOURCE_EMPTY
    return Calibration(params, zones, source, settings_ignored=in_tables and _settings_set())


async def load_calibration(db: AsyncSession) -> Calibration:
    """At startup: the tables, seeded once from the JSON settings when both are
    empty. A seed that cannot be written leaves the tables empty and says why;
    it never stops the backend."""
    current = await read_calibration(db)
    if current.source != SOURCE_EMPTY:
        if current.settings_ignored:
            logger.warning(
                f"{LOG_PREFIX} PRESENCE_PLACEMENT_V2_RECEIVERS/_ZONES are set but IGNORED — "
                "the calibration tables are the source (edit them on /admin/presence)."
            )
        return current
    if not _settings_set():
        return current
    try:
        params = parse_receiver_params(ha_glue_settings.presence_placement_v2_receivers)
        zones = parse_zones(ha_glue_settings.presence_placement_v2_zones)
    except PlacementConfigError as e:
        logger.error(
            f"{LOG_PREFIX} the JSON settings are unusable, nothing seeded: "
            f"{error_text_without_content(e)}"
        )
        return current
    try:
        for satellite_id, p in params.items():
            await _upsert_receiver(db, satellite_id, p, user_id=None)
        await _write_zones(db, zones)
        await _mark(db)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(
            f"{LOG_PREFIX} seeding from the JSON settings failed ({type(e).__name__}); "
            "the tables stay empty."
        )
        return current
    logger.info(
        f"{LOG_PREFIX} seeded {len(params)} receiver(s) and {len(zones)} zone(s) from the "
        "JSON settings; the tables are the source from now on."
    )
    return Calibration(params, zones, SOURCE_SEEDED)


def apply_calibration(presence: PresenceService, calibration: Calibration) -> None:
    """Hand the numbers to the running decision (shadow or state), if any.

    An EMPTY calibration (never seeded: no settings, or a seed that failed or
    was refused) is not handed over: the decision keeps the numbers it was
    built with from the settings instead of losing them."""
    if calibration.source == SOURCE_EMPTY:
        return
    engine = getattr(presence, "_placement_shadow", None)
    if engine is not None:
        engine.set_calibration(calibration.params, calibration.zones)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def validate_receiver(offset_db: object, in_room_dbm: object, cutoff_dbm: object) -> ReceiverParams:
    """The JSON parser's rules for one receiver, as an admin-facing error."""
    entry = {"offset_db": offset_db, "in_room_dbm": in_room_dbm, "cutoff_dbm": cutoff_dbm}
    try:
        return parse_receiver_params(json.dumps({"receiver": entry}, allow_nan=True))["receiver"]
    except (PlacementConfigError, ValueError) as e:
        raise CalibrationError("invalid_receiver", str(e).replace("receiver 'receiver'", "receiver")) from e


def validate_zones(zones: Mapping[str, list[int]]) -> dict[str, frozenset[int]]:
    try:
        return parse_zones(json.dumps(dict(zones)))
    except PlacementConfigError as e:
        raise CalibrationError("invalid_zones", str(e)) from e


async def _upsert_receiver(
    db: AsyncSession, satellite_id: str, params: ReceiverParams, user_id: int | None
) -> None:
    from ha_glue.models.database import PresenceReceiver

    row = await db.get(PresenceReceiver, satellite_id)
    if row is None:
        row = PresenceReceiver(satellite_id=satellite_id)
        db.add(row)
    row.offset_db = params.offset_db
    row.in_room_dbm = params.in_room_dbm
    row.cutoff_dbm = params.cutoff_dbm
    row.updated_by_user_id = user_id


async def _write_zones(db: AsyncSession, zones: Mapping[str, frozenset[int]]) -> None:
    """Replace every zone. Unknown room ids are refused, not dropped."""
    from ha_glue.models.database import PresenceZone, PresenceZoneRoom, Room

    wanted = {r for rooms in zones.values() for r in rooms}
    if wanted:
        known = set((await db.execute(select(Room.id).where(Room.id.in_(wanted)))).scalars())
        unknown = sorted(wanted - known)
        if unknown:
            raise CalibrationError("unknown_rooms", f"unknown room id(s): {unknown}", room_ids=unknown)
    await db.execute(delete(PresenceZoneRoom))
    await db.execute(delete(PresenceZone))
    await db.flush()
    for name, rooms in zones.items():
        zone = PresenceZone(name=name)
        db.add(zone)
        await db.flush()
        for room_id in sorted(rooms):
            db.add(PresenceZoneRoom(room_id=room_id, zone_id=zone.id))


async def save_receiver(
    db: AsyncSession, presence: PresenceService | None, satellite_id: str,
    params: ReceiverParams, user_id: int | None,
) -> Calibration:
    if not satellite_id.strip():
        raise CalibrationError("invalid_satellite", "empty satellite id")
    await _upsert_receiver(db, satellite_id, params, user_id)
    await _mark(db)
    await db.commit()
    return await _reload(db, presence)


async def delete_receiver(
    db: AsyncSession, presence: PresenceService | None, satellite_id: str
) -> Calibration:
    from ha_glue.models.database import PresenceReceiver

    row = await db.get(PresenceReceiver, satellite_id)
    if row is None:
        raise CalibrationError("not_calibrated", "no calibration for this receiver")
    await db.delete(row)
    await _mark(db)
    await db.commit()
    return await _reload(db, presence)


async def replace_zones(
    db: AsyncSession, presence: PresenceService | None, zones: Mapping[str, frozenset[int]]
) -> Calibration:
    try:
        await _write_zones(db, zones)
        await _mark(db)
        await db.commit()
    except CalibrationError:
        await db.rollback()
        raise
    return await _reload(db, presence)


async def _reload(db: AsyncSession, presence: PresenceService | None) -> Calibration:
    calibration = await read_calibration(db)
    if presence is not None:
        apply_calibration(presence, calibration)
    logger.info(
        f"{LOG_PREFIX} {len(calibration.params)} receiver(s), {len(calibration.zones)} zone(s) in effect"
    )
    return calibration


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------


def _finite_level(value: object) -> bool:
    return (
        isinstance(value, int | float) and not isinstance(value, bool)
        and math.isfinite(value) and RSSI_MIN_DBM <= value <= RSSI_MAX_DBM
    )


def propose(
    presence: PresenceService,
    device_key: str,
    satellite_id: str,
    since: float,
    now: float,
    current: ReceiverParams | None,
) -> Proposal:
    """Propose this receiver's numbers from its readings of ONE device since
    ``since`` (the start of the guided measurement). ``device_key`` is the
    redacted key the live-readings view shows — the raw key never reaches the
    browser. Raises ``CalibrationError`` with the reason it cannot propose."""
    from ha_glue.services.presence_debug import redact_device_key

    if since > now:
        raise CalibrationError("not_started", "the measurement has not started yet")
    if now - since > float(ha_glue_settings.presence_stale_timeout):
        raise CalibrationError(
            "too_old", "the measurement is older than the reading window; start it again",
            window_s=float(ha_glue_settings.presence_stale_timeout),
        )
    key = next((k for k in presence._sightings if redact_device_key(k) == device_key), None)
    if key is None:
        raise CalibrationError("device_not_heard", "this device has no readings")

    by_time: dict[float, float] = {}
    for s in presence._sightings.get(key, ()):
        if (
            s.satellite_id == satellite_id
            and since <= s.timestamp <= now
            and _finite_level(s.rssi)
            and getattr(s, "comparable", True)  # Classic BT: no level (DeviceSighting)
        ):
            by_time[s.timestamp] = float(s.rssi)  # one reading per instant
    values = list(by_time.values())
    if len(values) < MIN_SAMPLES:
        raise CalibrationError(
            "too_few_readings",
            f"only {len(values)} reading(s) of this device by this receiver; at least {MIN_SAMPLES} needed",
            samples=len(values), needed=MIN_SAMPLES,
        )

    in_room = round(percentile(values, IN_ROOM_PERCENTILE), 1)
    # The cutoff is a property of the satellite's firmware, not of this
    # measurement: what the satellite reports (it filters there, so it wins),
    # else the receiver's stored value, else unknown (the admin enters it).
    # The weakest reading in the window is NOT a cutoff — with every device
    # close it sits far above it.
    status = getattr(presence, "_scanner_status", {}).get(satellite_id)
    if status is not None and status.cutoff_dbm is not None:
        cutoff, cutoff_source = status.cutoff_dbm, "satellite"
    elif current is not None:
        cutoff, cutoff_source = current.cutoff_dbm, "stored"
    else:
        cutoff, cutoff_source = None, None
    if cutoff is not None and not in_room > cutoff:
        raise CalibrationError(
            "bound_not_above_cutoff",
            f"the proposed bound {in_room} dBm is not above the cutoff {cutoff} dBm",
            bound_dbm=in_room, cutoff_dbm=cutoff,
        )
    return Proposal(
        satellite_id=satellite_id,
        samples=len(values),
        in_room_dbm=in_room,
        min_dbm=min(values),
        median_dbm=round(percentile(values, 50.0), 1),
        max_dbm=max(values),
        cutoff_dbm=cutoff,
        offset_db=current.offset_db if current is not None else 0.0,
        cutoff_source=cutoff_source,
    )
