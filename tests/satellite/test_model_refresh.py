"""A wake word model that changed on the server replaces the satellite's copy.

The satellite used to download a model only when no file of that name
existed, so a retrained renfield_de never reached a satellite that had the
previous one. The config push now carries the server's SHA-256 per model; a
differing local file is replaced (verified, atomically) and reloaded, and a
failed update keeps the running model — a room must not go deaf over it.
"""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from renfield_satellite import satellite as sat_mod
from renfield_satellite.network import model_downloader as md_mod
from renfield_satellite.network import websocket_client as wsc
from renfield_satellite.network.model_downloader import ModelDownloader
from renfield_satellite.network.websocket_client import ServerConfig
from renfield_satellite.satellite import Satellite
from renfield_satellite.wakeword.detector import WakeWordDetector


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- downloader: stale detection -------------------------------------------


@pytest.mark.satellite
def test_a_differing_local_file_is_stale(tmp_path):
    (tmp_path / "renfield_de.onnx").write_bytes(b"v3")
    dl = ModelDownloader(models_path=str(tmp_path))

    assert dl.is_model_stale("renfield_de", _sha(b"v5")) is True
    assert dl.is_model_stale("renfield_de", _sha(b"v3")) is False


@pytest.mark.satellite
def test_without_a_server_checksum_nothing_is_stale(tmp_path):
    """An older server sends no checksum: behave as before ("exists?")."""
    (tmp_path / "renfield_de.onnx").write_bytes(b"v3")
    dl = ModelDownloader(models_path=str(tmp_path))

    assert dl.is_model_stale("renfield_de", None) is False
    assert dl.is_model_stale("renfield_de", "") is False


@pytest.mark.satellite
def test_a_missing_file_is_not_stale_but_missing(tmp_path):
    dl = ModelDownloader(models_path=str(tmp_path))

    assert dl.is_model_stale("renfield_de", _sha(b"v5")) is False
    assert dl.is_model_available("renfield_de") is False


# --- downloader: verified, atomic replacement -------------------------------


class _Response:
    def __init__(self, body: bytes):
        self.status = 200
        self.headers = {"Content-Disposition": 'attachment; filename="renfield_de.onnx"'}
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, body: bytes):
        self._body = body

    def get(self, *_a, **_k):
        return _Response(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _downloader(tmp_path, served: bytes, monkeypatch) -> ModelDownloader:
    monkeypatch.setattr(md_mod.aiohttp, "ClientSession", lambda *a, **k: _Session(served))
    dl = ModelDownloader(models_path=str(tmp_path))
    dl.set_server_url("https://server/ws/satellite")
    return dl


@pytest.mark.satellite
async def test_a_matching_download_replaces_the_file(tmp_path, monkeypatch):
    (tmp_path / "renfield_de.onnx").write_bytes(b"v3")
    dl = _downloader(tmp_path, b"v5", monkeypatch)

    ok, err = await dl.download_model("renfield_de", expected_sha256=_sha(b"v5"))

    assert (ok, err) == (True, None)
    assert (tmp_path / "renfield_de.onnx").read_bytes() == b"v5"
    assert (tmp_path / "renfield_de.onnx.prev").read_bytes() == b"v3"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.satellite
async def test_restore_previous_puts_back_the_replaced_file(tmp_path, monkeypatch):
    (tmp_path / "renfield_de.onnx").write_bytes(b"v3")
    dl = _downloader(tmp_path, b"v5", monkeypatch)
    await dl.download_model("renfield_de", expected_sha256=_sha(b"v5"))

    assert dl.restore_previous("renfield_de") is True
    assert (tmp_path / "renfield_de.onnx").read_bytes() == b"v3"
    assert not (tmp_path / "renfield_de.onnx.prev").exists()


@pytest.mark.satellite
def test_restore_previous_without_a_replaced_file_does_nothing(tmp_path):
    (tmp_path / "renfield_de.onnx").write_bytes(b"v3")

    assert ModelDownloader(models_path=str(tmp_path)).restore_previous("renfield_de") is False
    assert (tmp_path / "renfield_de.onnx").read_bytes() == b"v3"


@pytest.mark.satellite
async def test_a_failed_write_leaves_no_part_file_and_keeps_the_model(tmp_path, monkeypatch):
    (tmp_path / "renfield_de.onnx").write_bytes(b"v3")
    dl = _downloader(tmp_path, b"v5", monkeypatch)
    real_replace = md_mod.os.replace

    def failing_replace(src, dst):
        if str(src).endswith(".part"):
            raise OSError("No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(md_mod.os, "replace", failing_replace)

    ok, err = await dl.download_model("renfield_de", expected_sha256=_sha(b"v5"))

    assert ok is False and "No space left" in err
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.satellite
async def test_a_download_that_does_not_match_keeps_the_current_file(tmp_path, monkeypatch):
    (tmp_path / "renfield_de.onnx").write_bytes(b"v3")
    dl = _downloader(tmp_path, b"corrupted", monkeypatch)

    ok, err = await dl.download_model("renfield_de", expected_sha256=_sha(b"v5"))

    assert ok is False and "Checksum mismatch" in err
    assert (tmp_path / "renfield_de.onnx").read_bytes() == b"v3"
    assert not list(tmp_path.glob("*.part"))


# --- the server's checksums reach ServerConfig ------------------------------


@pytest.mark.satellite
async def test_a_config_update_carries_the_checksums():
    client = object.__new__(wsc.WebSocketClient)
    client._server_config = ServerConfig(wake_words=["renfield_de"], threshold=0.5)
    client._on_config_update = MagicMock()

    await client._handle_message({
        "type": "config_update",
        "config": {"wake_words": ["renfield_de"], "threshold": 0.8, "model_sha256": {"renfield_de": "abc"}},
    })

    pushed = client._on_config_update.call_args.args[0]
    assert pushed.model_sha256 == {"renfield_de": "abc"}


@pytest.mark.satellite
async def test_a_config_update_from_an_older_server_has_no_checksums():
    client = object.__new__(wsc.WebSocketClient)
    client._server_config = ServerConfig(wake_words=["renfield_de"], threshold=0.5)
    client._on_config_update = MagicMock()

    await client._handle_message({"type": "config_update", "config": {"wake_words": ["renfield_de"]}})

    assert client._on_config_update.call_args.args[0].model_sha256 == {}


# --- the satellite applies it -----------------------------------------------


def _satellite(downloader, active=("renfield_de",)):
    sat = object.__new__(Satellite)
    sat.ws_client = MagicMock()
    sat.ws_client.server_url = "wss://server/ws/satellite"
    sat.ws_client.send_config_ack = AsyncMock()
    sat.config = MagicMock()
    sat.config.server.verify_tls = False
    sat._model_downloader_token = lambda: None
    sat.wakeword = MagicMock()
    sat.wakeword.active_keywords = list(active)
    sat.wakeword.update_config.return_value = True
    sat._test_downloader = downloader
    return sat


def _fake_downloader(stale: bool, download_ok: bool):
    dl = MagicMock()
    dl.is_model_stale.return_value = stale
    dl.is_model_available.return_value = True
    dl.download_model = AsyncMock(return_value=(download_ok, None if download_ok else "network down"))
    dl.restore_previous.return_value = True
    return dl


async def _apply(sat, shas):
    config = ServerConfig(wake_words=["renfield_de"], threshold=0.8, model_sha256=shas)
    with patch.object(sat_mod, "get_model_downloader", return_value=sat._test_downloader):
        await sat._apply_config_and_ack(config)


@pytest.mark.satellite
async def test_a_stale_model_is_downloaded_and_reloaded():
    dl = _fake_downloader(stale=True, download_ok=True)
    sat = _satellite(dl)

    await _apply(sat, {"renfield_de": "new"})

    dl.download_model.assert_awaited_once_with("renfield_de", expected_sha256="new")
    sat.wakeword.reload_keyword.assert_called_once_with("renfield_de")
    dl.restore_previous.assert_not_called()
    assert sat.ws_client.send_config_ack.await_args.kwargs["success"] is True


@pytest.mark.satellite
async def test_a_failed_update_keeps_the_running_model():
    dl = _fake_downloader(stale=True, download_ok=False)
    sat = _satellite(dl)

    await _apply(sat, {"renfield_de": "new"})

    sat.wakeword.reload_keyword.assert_not_called()
    sat.wakeword.remove_keyword.assert_not_called()
    ack = sat.ws_client.send_config_ack.await_args.kwargs
    assert ack["active_keywords"] == ["renfield_de"] and ack["failed_keywords"] == []
    # ... but the backend must see it: only an unsuccessful ack is logged there.
    assert ack["success"] is False
    assert "network down" in ack["error"]


@pytest.mark.satellite
async def test_an_update_that_does_not_load_restores_the_previous_file_and_is_reported():
    """Else the next restart loads the broken file and the room loses its wake word."""
    dl = _fake_downloader(stale=True, download_ok=True)
    sat = _satellite(dl)
    sat.wakeword.reload_keyword.return_value = False

    await _apply(sat, {"renfield_de": "new"})

    dl.restore_previous.assert_called_once_with("renfield_de")
    ack = sat.ws_client.send_config_ack.await_args.kwargs
    assert ack["active_keywords"] == ["renfield_de"]
    assert ack["success"] is False
    assert "does not load" in ack["error"]


@pytest.mark.satellite
async def test_a_current_model_is_left_alone():
    dl = _fake_downloader(stale=False, download_ok=True)
    sat = _satellite(dl)

    await _apply(sat, {"renfield_de": "same"})

    dl.download_model.assert_not_awaited()
    sat.wakeword.reload_keyword.assert_not_called()


# --- the detector swaps only a model that loaded -----------------------------


def _detector(loaded_model):
    det = object.__new__(WakeWordDetector)
    det._wake_models = {"renfield_de": "old-model"}
    det._discover_models = lambda: [{"id": "renfield_de"}]
    det._find_model = lambda kw, models: {"id": kw, "path": "/m/renfield_de.onnx"}
    det._load_model = lambda info: loaded_model
    return det


@pytest.mark.satellite
def test_reload_swaps_in_the_new_model():
    new = MagicMock(model_type="onnx")
    det = _detector(new)

    assert det.reload_keyword("renfield_de") is True
    assert det._wake_models["renfield_de"] is new


@pytest.mark.satellite
def test_reload_keeps_the_running_model_when_the_new_one_does_not_load():
    det = _detector(None)

    assert det.reload_keyword("renfield_de") is False
    assert det._wake_models["renfield_de"] == "old-model"


# --- the detector loads the file the update checks ---------------------------


def _lookup(ids, keyword):
    det = object.__new__(WakeWordDetector)
    found = det._find_model(keyword, [{"id": i} for i in ids])
    return found and found["id"]


@pytest.mark.satellite
def test_an_exact_model_id_wins_over_a_fuzzy_neighbour():
    """The downloader hashes renfield_de.<ext>; the detector must load that file."""
    assert _lookup(["renfield_de_v5", "renfield_de"], "renfield_de") == "renfield_de"
    assert _lookup(["hey_renfield", "renfield"], "renfield") == "renfield"


@pytest.mark.satellite
def test_the_fuzzy_match_remains_the_fallback():
    assert _lookup(["alexa_v0.1"], "alexa") == "alexa_v0.1"
    assert _lookup(["hey_jarvis_v0.1"], "hey-jarvis") == "hey_jarvis_v0.1"
    assert _lookup(["alexa_v0.1"], "renfield_de") is None
