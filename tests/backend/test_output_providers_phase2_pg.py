"""Phase 2 (generic output providers): additive (output_provider, output_target_id)
pair + dual-read + backfill, against REAL Postgres (``pg_db_session``, gated on
RENFIELD_TEST_PG_URL).

Covers:
  - the migration's backfill LOGIC (pc20260610) — the CASE/COALESCE UPDATE that
    fills the pair from the three legacy columns. (The full ``alembic upgrade
    head`` run is verified on the .159 build box; this exercises the SQL.)
  - dual-read on RoomOutputDevice.target_id / target_type (prefer the pair, fall
    back to legacy columns).
  - OutputRoutingService.add_output_device dual-write + explicit-pair (samsung)
    path + validation.

pg_db_session wraps one outer txn rolled back on teardown: flush(), never
commit(). Services that commit are patched commit→flush (see _commit_as_flush).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from ha_glue.models.database import Room, RoomDevice, RoomOutputDevice
from ha_glue.services.output_routing_service import OutputRoutingService

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


# Mirrors pc20260610_output_provider_target.upgrade()'s backfill UPDATE. Kept in
# sync with the migration; the authoritative full-upgrade run is on .159.
_BACKFILL_SQL = text(
    """
    UPDATE room_output_devices
    SET output_provider = CASE
            WHEN renfield_device_id IS NOT NULL THEN 'renfield'
            WHEN ha_entity_id      IS NOT NULL THEN 'homeassistant'
            WHEN dlna_renderer_name IS NOT NULL THEN 'dlna'
        END,
        output_target_id = COALESCE(renfield_device_id, ha_entity_id, dlna_renderer_name)
    WHERE output_provider IS NULL
    """
)


def _commit_as_flush(db, monkeypatch):
    monkeypatch.setattr(db, "commit", db.flush)
    monkeypatch.setattr(db, "rollback", db.flush)


async def _make_room(db, name: str) -> Room:
    room = Room(name=name)
    db.add(room)
    await db.flush()
    return room


# --- backfill LOGIC (pre-migration rows get the pair filled) ----------------


class TestBackfill:
    async def test_backfill_fills_pair_for_all_three_legacy_types(self, pg_db_session):
        room = await _make_room(pg_db_session, "bf_room")
        dev = RoomDevice(room_id=room.id, device_id="sat-bf-1", capabilities={})
        pg_db_session.add(dev)
        await pg_db_session.flush()

        # Insert legacy-only rows (the pair left NULL, as if pre-migration).
        rows = [
            RoomOutputDevice(room_id=room.id, output_type="audio", renfield_device_id="sat-bf-1"),
            RoomOutputDevice(room_id=room.id, output_type="audio", ha_entity_id="media_player.kitchen"),
            RoomOutputDevice(room_id=room.id, output_type="visual", dlna_renderer_name="Wohnzimmer TV"),
        ]
        for r in rows:
            pg_db_session.add(r)
        await pg_db_session.flush()

        await pg_db_session.execute(_BACKFILL_SQL)
        for r in rows:
            await pg_db_session.refresh(r)

        by_target = {r.output_target_id: r.output_provider for r in rows}
        assert by_target == {
            "sat-bf-1": "renfield",
            "media_player.kitchen": "homeassistant",
            "Wohnzimmer TV": "dlna",
        }

    async def test_backfill_idempotent_skips_already_paired(self, pg_db_session):
        room = await _make_room(pg_db_session, "bf_idem")
        # A row already carrying a (samsung) pair must NOT be touched (WHERE
        # output_provider IS NULL).
        r = RoomOutputDevice(
            room_id=room.id, output_type="visual",
            output_provider="samsung", output_target_id="192.168.1.47",
        )
        pg_db_session.add(r)
        await pg_db_session.flush()
        await pg_db_session.execute(_BACKFILL_SQL)
        await pg_db_session.refresh(r)
        assert r.output_provider == "samsung"
        assert r.output_target_id == "192.168.1.47"


# --- dual-read properties (pure; prefer pair, fall back to legacy) -----------


class TestDualRead:
    def test_prefers_pair(self):
        d = RoomOutputDevice(output_provider="samsung", output_target_id="192.168.1.47")
        assert d.target_type == "samsung"
        assert d.target_id == "192.168.1.47"

    def test_falls_back_to_legacy_when_pair_absent(self):
        d = RoomOutputDevice(dlna_renderer_name="Wohnzimmer TV")
        assert d.target_type == "dlna"
        assert d.target_id == "Wohnzimmer TV"

    def test_pair_wins_over_legacy_when_both_present(self):
        d = RoomOutputDevice(
            ha_entity_id="media_player.old",
            output_provider="homeassistant",
            output_target_id="media_player.new",
        )
        assert d.target_type == "homeassistant"
        assert d.target_id == "media_player.new"

    def test_empty_defaults(self):
        d = RoomOutputDevice()
        assert d.target_id == ""
        assert d.target_type == "renfield"


# --- add_output_device dual-write + explicit pair + validation --------------


class TestAddOutputDevice:
    async def test_legacy_arg_dual_writes_pair(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        room = await _make_room(pg_db_session, "add_legacy")
        svc = OutputRoutingService(pg_db_session)
        dev = await svc.add_output_device(
            room_id=room.id, output_type="visual", dlna_renderer_name="Wohnzimmer TV"
        )
        assert dev.dlna_renderer_name == "Wohnzimmer TV"
        assert dev.output_provider == "dlna"
        assert dev.output_target_id == "Wohnzimmer TV"

    async def test_explicit_pair_path_samsung(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        room = await _make_room(pg_db_session, "add_samsung")
        svc = OutputRoutingService(pg_db_session)
        dev = await svc.add_output_device(
            room_id=room.id, output_type="visual",
            output_provider="samsung", output_target_id="192.168.1.47",
        )
        # No legacy column populated for a brand without one.
        assert dev.renfield_device_id is None
        assert dev.ha_entity_id is None
        assert dev.dlna_renderer_name is None
        assert dev.output_provider == "samsung"
        assert dev.output_target_id == "192.168.1.47"
        assert dev.device_name == "192.168.1.47"  # auto from target id
        assert dev.target_type == "samsung"

    async def test_rejects_pair_plus_legacy(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        room = await _make_room(pg_db_session, "add_both")
        svc = OutputRoutingService(pg_db_session)
        with pytest.raises(ValueError, match="not both"):
            await svc.add_output_device(
                room_id=room.id, output_type="visual",
                dlna_renderer_name="X", output_provider="samsung", output_target_id="Y",
            )

    async def test_rejects_half_pair(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        room = await _make_room(pg_db_session, "add_half")
        svc = OutputRoutingService(pg_db_session)
        with pytest.raises(ValueError, match="together"):
            await svc.add_output_device(
                room_id=room.id, output_type="visual", output_provider="samsung"
            )

    async def test_rejects_nothing(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        room = await _make_room(pg_db_session, "add_none")
        svc = OutputRoutingService(pg_db_session)
        with pytest.raises(ValueError):
            await svc.add_output_device(room_id=room.id, output_type="audio")
