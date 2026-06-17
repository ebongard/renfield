"""Generic output providers — POST-cleanup (legacy columns DROPPED), against
REAL Postgres (``pg_db_session``, gated on RENFIELD_TEST_PG_URL).

The additive phase (migration ``pc20260610``) added the
``(output_provider, output_target_id)`` pair and dual-wrote/dual-read it
alongside the three legacy brand columns. After the prod soak, migration
``pc20260617b_drop_outlegacy`` DROPPED ``renfield_device_id`` /
``ha_entity_id`` / ``dlna_renderer_name``. This file verifies the post-cleanup
contract:

  - the three legacy columns no longer exist on ``room_output_devices``.
  - the pair is the SOLE persisted target identity; ``target_id`` / ``target_type``
    read ONLY the pair (no legacy fallback).
  - ``OutputRoutingService.add_output_device`` still accepts the legacy kwargs as
    INPUT ADAPTERS, mapping them onto the pair (it no longer writes a column).
  - the explicit-pair (samsung) path is unchanged.

The full ``alembic upgrade head`` (incl. this drop) is verified on the .159
build box; this exercises the resulting schema + the service logic.

pg_db_session wraps one outer txn rolled back on teardown: flush(), never
commit(). Services that commit are patched commit→flush (see _commit_as_flush).
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect, select, text

from ha_glue.models.database import Room, RoomDevice, RoomOutputDevice
from ha_glue.services.output_routing_service import OutputRoutingService

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _commit_as_flush(db, monkeypatch):
    monkeypatch.setattr(db, "commit", db.flush)
    monkeypatch.setattr(db, "rollback", db.flush)


async def _make_room(db, name: str) -> Room:
    room = Room(name=name)
    db.add(room)
    await db.flush()
    return room


# --- the legacy columns are GONE ---------------------------------------------


class TestLegacyColumnsDropped:
    async def test_legacy_columns_absent_from_table(self, pg_db_session):
        """The three brand columns no longer exist on room_output_devices."""
        cols = await pg_db_session.execute(
            text(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'room_output_devices'
                """
            )
        )
        names = {row[0] for row in cols.fetchall()}
        assert "renfield_device_id" not in names
        assert "ha_entity_id" not in names
        assert "dlna_renderer_name" not in names
        # The generic pair survives.
        assert "output_provider" in names
        assert "output_target_id" in names

    async def test_renfield_device_fk_dropped(self, pg_db_session):
        """The FK from renfield_device_id → room_devices.device_id is gone."""
        fks = await pg_db_session.execute(
            text(
                """
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'room_output_devices'::regclass AND contype = 'f'
                """
            )
        )
        names = {row[0] for row in fks.fetchall()}
        assert "room_output_devices_renfield_device_id_fkey" not in names

    def test_model_has_no_legacy_attributes(self):
        """The ORM mapper no longer exposes the dropped columns."""
        mapper_cols = {c.key for c in sa_inspect(RoomOutputDevice).columns}
        assert "renfield_device_id" not in mapper_cols
        assert "ha_entity_id" not in mapper_cols
        assert "dlna_renderer_name" not in mapper_cols
        assert {"output_provider", "output_target_id"} <= mapper_cols


# --- target identity reads ONLY the pair -------------------------------------


class TestPairOnlyIdentity:
    def test_pair_resolves(self):
        d = RoomOutputDevice(output_provider="samsung", output_target_id="192.168.1.47")
        assert d.target_type == "samsung"
        assert d.target_id == "192.168.1.47"
        assert d.is_renfield_device is False
        assert d.is_ha_device is False
        assert d.is_dlna_device is False

    def test_dlna_pair(self):
        d = RoomOutputDevice(output_provider="dlna", output_target_id="Wohnzimmer TV")
        assert d.target_type == "dlna"
        assert d.target_id == "Wohnzimmer TV"
        assert d.is_dlna_device is True

    def test_renfield_pair(self):
        d = RoomOutputDevice(output_provider="renfield", output_target_id="sat-1")
        assert d.target_type == "renfield"
        assert d.is_renfield_device is True

    def test_homeassistant_pair(self):
        d = RoomOutputDevice(output_provider="homeassistant", output_target_id="media_player.x")
        assert d.target_type == "homeassistant"
        assert d.is_ha_device is True

    def test_empty_defaults(self):
        d = RoomOutputDevice()
        assert d.target_id == ""
        assert d.target_type == "renfield"


# --- add_output_device: legacy kwargs map onto the pair (no column write) ----


class TestAddOutputDevice:
    async def test_legacy_arg_maps_to_pair(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        room = await _make_room(pg_db_session, "add_legacy")
        svc = OutputRoutingService(pg_db_session)
        dev = await svc.add_output_device(
            room_id=room.id, output_type="visual", dlna_renderer_name="Wohnzimmer TV"
        )
        assert dev.output_provider == "dlna"
        assert dev.output_target_id == "Wohnzimmer TV"
        assert dev.target_type == "dlna"

    async def test_explicit_pair_path_samsung(self, pg_db_session, monkeypatch):
        _commit_as_flush(pg_db_session, monkeypatch)
        room = await _make_room(pg_db_session, "add_samsung")
        svc = OutputRoutingService(pg_db_session)
        dev = await svc.add_output_device(
            room_id=room.id, output_type="visual",
            output_provider="samsung", output_target_id="192.168.1.47",
        )
        assert dev.output_provider == "samsung"
        assert dev.output_target_id == "192.168.1.47"
        assert dev.device_name == "192.168.1.47"  # auto from target id
        assert dev.target_type == "samsung"

    async def test_explicit_pair_legacy_provider(self, pg_db_session, monkeypatch):
        # A legacy provider added via the unified picker (pair-only) resolves
        # entirely off the pair — no brand column to back-fill anymore.
        _commit_as_flush(pg_db_session, monkeypatch)
        room = await _make_room(pg_db_session, "add_pair_dlna")
        svc = OutputRoutingService(pg_db_session)
        dev = await svc.add_output_device(
            room_id=room.id, output_type="visual",
            output_provider="dlna", output_target_id="Wohnzimmer TV",
        )
        assert dev.output_provider == "dlna"
        assert dev.output_target_id == "Wohnzimmer TV"
        assert dev.target_type == "dlna"
        ha = await svc.add_output_device(
            room_id=room.id, output_type="audio",
            output_provider="homeassistant", output_target_id="media_player.x",
        )
        assert ha.output_provider == "homeassistant"
        assert ha.output_target_id == "media_player.x"

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
