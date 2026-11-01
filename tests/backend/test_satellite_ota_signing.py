"""Security review H6 — backend forwards the signed OTA manifest (does NOT sign).

The backend reads the offline-signed RELEASE_MANIFEST.json + .sig from its
bundled satellite source and forwards them verbatim in the update_request; with
`satellite_ota_require_signature` it refuses to push an unsigned update.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ha_glue.services.satellite_update_service as svc_mod
from ha_glue.services.satellite_manager import UpdateStatus
from ha_glue.services.satellite_update_service import SatelliteUpdateService
from ha_glue.utils.config import ha_glue_settings


@pytest.mark.backend
class TestLoadReleaseSignature:
    def test_returns_none_when_absent(self, tmp_path):
        svc = SatelliteUpdateService()
        svc.satellite_source_path = tmp_path
        assert svc._load_release_signature() == (None, None)

    def test_reads_manifest_and_sig_verbatim(self, tmp_path):
        (tmp_path / "RELEASE_MANIFEST.json").write_text('{"version":"9.9.9","files":{}}')
        (tmp_path / "RELEASE_MANIFEST.json.sig").write_text("c2lnbmF0dXJl\n")
        svc = SatelliteUpdateService()
        svc.satellite_source_path = tmp_path
        manifest, sig = svc._load_release_signature()
        assert manifest == '{"version":"9.9.9","files":{}}'  # verbatim, no trailing newline added
        assert sig == "c2lnbmF0dXJl"  # stripped


def _fake_manager(sat):
    return SimpleNamespace(
        get_satellite=lambda sid: sat,
        set_update_status=lambda *a, **k: None,
        begin_update_run=lambda sid: "run-x",
    )


@pytest.mark.backend
class TestInitiateUpdateForwarding:
    async def test_forwards_manifest_and_signature(self, monkeypatch):
        svc = SatelliteUpdateService()
        monkeypatch.setattr(svc, "is_update_available", lambda v: True)
        monkeypatch.setattr(
            svc, "get_package_info",
            lambda: {
                "version": "9.9.9", "checksum": "sha256:abc", "size": 10,
                "manifest": '{"version":"9.9.9","files":{}}', "signature": "c2ln",
            },
        )
        ws = AsyncMock()
        sat = SimpleNamespace(version="1.0.0", update_status=UpdateStatus.NONE, websocket=ws)
        monkeypatch.setattr(svc_mod, "get_satellite_manager", lambda: _fake_manager(sat))

        res = await svc.initiate_update("sat-x")
        assert res["success"] is True
        sent = ws.send_json.call_args.args[0]
        assert sent["type"] == "update_request"
        assert sent["manifest"] == '{"version":"9.9.9","files":{}}'
        assert sent["signature"] == "c2ln"
        assert sent["run_id"] == "run-x"

    async def test_require_signature_refuses_unsigned(self, monkeypatch):
        svc = SatelliteUpdateService()
        monkeypatch.setattr(svc, "is_update_available", lambda v: True)
        monkeypatch.setattr(
            svc, "get_package_info",
            lambda: {"version": "9.9.9", "checksum": "sha256:abc", "size": 10,
                     "manifest": None, "signature": None},
        )
        ws = AsyncMock()
        sat = SimpleNamespace(version="1.0.0", update_status=UpdateStatus.NONE, websocket=ws)
        monkeypatch.setattr(svc_mod, "get_satellite_manager", lambda: _fake_manager(sat))
        monkeypatch.setattr(ha_glue_settings, "satellite_ota_require_signature", True)

        res = await svc.initiate_update("sat-x")
        assert res["success"] is False
        ws.send_json.assert_not_called()  # never pushed an unsigned update
