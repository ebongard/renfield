"""Weather MCP server — the whole path from intent to formatted answer (#122).

Wiring: ``config/mcp_servers.yaml`` declares ``weather`` as a stdio server
(``python -m renfield_mcp_weather``, the pinned package from
``requirements.txt``) with ``prompt_tools: [get_weather]``; the agent's intent
``mcp.weather.get_weather`` goes ActionExecutor → MCPManager.execute_tool →
the MCP session.

Two layers:

* ``unit`` — the backend's half against a mocked MCP session: the config
  stanza, tool-name resolution (incl. the fallback for a hallucinated name),
  parameter pass-through, response parsing, and the error shapes the real
  server produces (unknown place, upstream timeout, MCP-call timeout).
* ``integration`` — the REAL server spawned over stdio by MCPManager from the
  real config stanza, with Open-Meteo replaced by a local stub HTTP server
  (the package reads ``OPEN_METEO_API_URL_<KEY>``). No external network: the
  stub listens on 127.0.0.1, so the test runs offline. It is skipped when the
  package is not installed (outside the backend image).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from services.action_executor import ActionExecutor
from services.mcp_client import (
    MCPManager,
    MCPServerConfig,
    MCPServerState,
    MCPToolInfo,
    MCPTransportType,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "mcp_servers.yaml"

BERLIN_ANSWER = {
    "location": {"name": "Berlin", "country": "Deutschland", "latitude": 52.52, "longitude": 13.41},
    "current": {"temperature": 12.3, "weather_code": 3, "weather_description": "Bedeckt"},
    "daily": [{"date": "2026-11-06", "temp_max": 14.0, "temp_min": 6.5}],
}


def _text_result(payload, *, is_error: bool = False):
    content = MagicMock()
    content.type = "text"
    content.text = payload if isinstance(payload, str) else json.dumps(payload)
    result = MagicMock()
    result.isError = is_error
    result.content = [content]
    return result


def _weather_manager(call_tool) -> tuple[MCPManager, AsyncMock]:
    """A manager holding the weather server the way discovery leaves it:
    every tool discovered, only the prompt_tools in the index."""
    manager = MCPManager()
    session = AsyncMock()
    session.call_tool = call_tool
    get_weather = MCPToolInfo("weather", "get_weather", "mcp.weather.get_weather", "Forecast")
    air = MCPToolInfo("weather", "get_air_quality", "mcp.weather.get_air_quality", "Air quality")
    state = MCPServerState(
        config=MCPServerConfig(name="weather", transport=MCPTransportType.STDIO,
                               prompt_tools=["get_weather"]),
        connected=True,
        session=session,
    )
    state.all_discovered_tools = [get_weather, air]
    manager._servers["weather"] = state
    manager._tool_index["mcp.weather.get_weather"] = get_weather
    return manager, session


async def _run(manager: MCPManager, intent: str, parameters: dict) -> dict:
    return await ActionExecutor(mcp_manager=manager).execute(
        {"intent": intent, "parameters": parameters},
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_config_stanza_is_the_stdio_package_and_dark_by_default():
    entries = yaml.safe_load(CONFIG.read_text())["servers"]
    weather = next(e for e in entries if e["name"] == "weather")
    assert weather["transport"] == "stdio"
    assert weather["command"] == "python"
    assert weather["args"] == ["-m", "renfield_mcp_weather"]
    assert weather["enabled"] == "${WEATHER_ENABLED:-false}"
    assert weather["prompt_tools"] == ["get_weather"]
    assert weather["example_intent"] == "mcp.weather.get_weather"


@pytest.mark.unit
def test_load_config_honours_the_flag(monkeypatch):
    monkeypatch.delenv("WEATHER_ENABLED", raising=False)
    off = MCPManager()
    off.load_config(str(CONFIG), only={"weather"}, overlay_dir="/nonexistent")
    assert "weather" not in off._servers

    monkeypatch.setenv("WEATHER_ENABLED", "true")
    on = MCPManager()
    on.load_config(str(CONFIG), only={"weather"}, overlay_dir="/nonexistent")
    cfg = on._servers["weather"].config
    assert cfg.transport == MCPTransportType.STDIO
    assert cfg.command == "python" and cfg.args == ["-m", "renfield_mcp_weather"]
    assert cfg.prompt_tools == ["get_weather"]


# ---------------------------------------------------------------------------
# Name resolution + parameters + parsing (mocked session)
# ---------------------------------------------------------------------------


class TestWeatherThroughTheExecutor:
    @pytest.mark.unit
    async def test_happy_path_passes_city_and_units_unchanged(self):
        manager, session = _weather_manager(AsyncMock(return_value=_text_result(BERLIN_ANSWER)))
        params = {"location": "Berlin", "days": 2, "temperature_unit": "fahrenheit",
                  "wind_speed_unit": "ms", "language": "en"}

        result = await _run(manager, "mcp.weather.get_weather", params)

        assert result["success"] is True
        session.call_tool.assert_awaited_once_with("get_weather", params)
        parsed = json.loads(result["message"])
        assert parsed["location"]["name"] == "Berlin"
        assert parsed["current"]["temperature"] == 12.3
        assert result["data"][0]["type"] == "text"

    @pytest.mark.unit
    async def test_a_discovered_but_unadvertised_tool_still_resolves(self):
        manager, session = _weather_manager(AsyncMock(return_value=_text_result({"aqi": 21})))
        result = await _run(manager, "mcp.weather.get_air_quality", {"location": "Berlin"})
        assert result["success"] is True
        session.call_tool.assert_awaited_once_with("get_air_quality", {"location": "Berlin"})

    @pytest.mark.unit
    async def test_a_hallucinated_tool_name_falls_back_to_get_weather(self):
        manager, session = _weather_manager(AsyncMock(return_value=_text_result(BERLIN_ANSWER)))
        result = await _run(manager, "mcp.weather.get_current_weather", {"location": "Berlin"})
        assert result["success"] is True
        assert session.call_tool.await_args.args[0] == "get_weather"

    @pytest.mark.unit
    async def test_an_unknown_server_is_refused(self):
        manager, session = _weather_manager(AsyncMock())
        result = await _run(manager, "mcp.wetter.get_weather", {"location": "Berlin"})
        assert result["success"] is False
        assert "Unknown MCP tool" in result["message"]
        session.call_tool.assert_not_awaited()

    @pytest.mark.unit
    async def test_an_unknown_place_is_a_failure_with_the_servers_text(self):
        # Exactly what renfield_mcp_weather returns: MCP-level OK, inner error.
        answer = {"error": "Ort nicht gefunden: Atlantis", "suggestion": "Versuche eine größere Stadt in der Nähe"}
        manager, _ = _weather_manager(AsyncMock(return_value=_text_result(answer)))
        result = await _run(manager, "mcp.weather.get_weather", {"location": "Atlantis"})
        assert result["success"] is False
        assert "Atlantis" in result["message"]

    @pytest.mark.unit
    async def test_an_upstream_api_timeout_is_a_failure(self):
        answer = {"error": "Weather API timeout - please try again"}
        manager, _ = _weather_manager(AsyncMock(return_value=_text_result(answer)))
        result = await _run(manager, "mcp.weather.get_weather", {"location": "Berlin"})
        assert result["success"] is False
        assert "timeout" in result["message"].lower()

    @pytest.mark.unit
    async def test_an_mcp_level_error_is_a_failure(self):
        manager, _ = _weather_manager(
            AsyncMock(return_value=_text_result("validation error: location required", is_error=True)))
        result = await _run(manager, "mcp.weather.get_weather", {})
        assert result["success"] is False
        assert "location required" in result["message"]

    @pytest.mark.unit
    async def test_a_hanging_call_times_out(self):
        async def hang(*_a, **_kw):
            await asyncio.sleep(30)

        manager, _ = _weather_manager(hang)
        with patch("services.mcp_client.settings") as s:
            s.mcp_call_timeout = 0.05
            result = await _run(manager, "mcp.weather.get_weather", {"location": "Berlin"})
        assert result["success"] is False
        assert "Timeout" in result["message"]


# ---------------------------------------------------------------------------
# The real package, in process: error mapping without network
# ---------------------------------------------------------------------------

_HAS_PACKAGE = importlib.util.find_spec("renfield_mcp_weather") is not None
needs_package = pytest.mark.skipif(not _HAS_PACKAGE, reason="renfield_mcp_weather not installed")


@needs_package
class TestPackageErrorMapping:
    @pytest.mark.unit
    async def test_upstream_timeout_becomes_an_error_envelope(self):
        import httpx
        from renfield_mcp_weather import server

        geo = {"latitude": 52.5, "longitude": 13.4, "name": "Berlin", "country": "DE",
               "timezone": "Europe/Berlin", "population": 1}
        with patch.object(server, "geocode", AsyncMock(return_value=geo)), \
             patch.object(server, "fetch_api", AsyncMock(side_effect=httpx.ReadTimeout("slow"))):
            out = await server.get_weather("Berlin")
        assert out == {"error": "Weather API timeout - please try again"}

    @pytest.mark.unit
    async def test_unknown_place_becomes_an_error_envelope(self):
        from renfield_mcp_weather import server

        with patch.object(server, "geocode", AsyncMock(side_effect=ValueError("nope"))):
            out = await server.get_weather("Atlantis", language="en")
        assert "Atlantis" in out["error"]
        assert "suggestion" in out


# ---------------------------------------------------------------------------
# The real server over stdio, Open-Meteo stubbed on localhost
# ---------------------------------------------------------------------------


class _OpenMeteoStub:
    """Geocoding + forecast endpoints, recording what reached them."""

    def __init__(self) -> None:
        self.forecast_queries: list[dict] = []
        self.fail_forecast_with: int | None = None
        self.runner = None
        self.base = ""

    async def start(self) -> None:
        from aiohttp import web

        async def search(request):
            name = request.query.get("name", "")
            if name != "Berlin":
                return web.json_response({})
            return web.json_response({"results": [{
                "name": "Berlin", "country": "Deutschland", "latitude": 52.52,
                "longitude": 13.41, "timezone": "Europe/Berlin", "population": 3_600_000,
            }]})

        async def forecast(request):
            self.forecast_queries.append(dict(request.query))
            if self.fail_forecast_with:
                return web.Response(status=self.fail_forecast_with)
            return web.json_response({
                "current": {"temperature_2m": 54.1, "weather_code": 3, "wind_speed_10m": 4.0},
                "daily": {"time": ["2026-11-06"], "temperature_2m_max": [57.2],
                          "temperature_2m_min": [43.7], "weather_code": [3]},
            })

        app = web.Application()
        app.router.add_get("/v1/search", search)
        app.router.add_get("/v1/forecast", forecast)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()


@needs_package
@pytest.mark.integration
class TestRealServerOverStdio:
    async def _connect(self, monkeypatch, stub: _OpenMeteoStub) -> MCPManager:
        monkeypatch.setenv("WEATHER_ENABLED", "true")
        manager = MCPManager()
        manager.load_config(str(CONFIG), only={"weather"}, overlay_dir="/nonexistent")
        state = manager._servers["weather"]
        # The subprocess env is a whitelist plus the stanza's env — so the stub
        # URLs go in there, exactly as an operator would point it elsewhere.
        state.config.env = {
            **(state.config.env or {}),
            "OPEN_METEO_API_URL_GEOCODING": f"{stub.base}/v1/search",
            "OPEN_METEO_API_URL_FORECAST": f"{stub.base}/v1/forecast",
        }
        await manager.connect_all()
        assert state.connected, "the real weather server did not come up over stdio"
        return manager

    async def test_end_to_end(self, monkeypatch):
        stub = _OpenMeteoStub()
        await stub.start()
        manager = None
        try:
            manager = await self._connect(monkeypatch, stub)

            # Discovery: many tools, only get_weather advertised.
            state = manager._servers["weather"]
            discovered = {t.original_name for t in state.all_discovered_tools}
            assert {"get_weather", "get_air_quality", "geocode_location"} <= discovered
            assert "mcp.weather.get_weather" in manager._tool_index
            assert "mcp.weather.get_air_quality" not in manager._tool_index

            # Happy path, units passed through to the upstream query.
            ok = await _run(manager, "mcp.weather.get_weather", {
                "location": "Berlin", "days": 1, "temperature_unit": "fahrenheit", "language": "en",
            })
            assert ok["success"] is True, ok["message"]
            body = json.loads(ok["message"])
            assert body["location"]["name"] == "Berlin"
            assert body["current"]["temperature"] == 54.1
            assert body["daily"][0]["temp_max"] == 57.2
            q = stub.forecast_queries[-1]
            assert q["temperature_unit"] == "fahrenheit"
            assert q["latitude"] == "52.52" and q["forecast_days"] == "1"
            assert q["timezone"] == "Europe/Berlin"

            # Unknown place: no forecast request, a failure naming the place.
            before = len(stub.forecast_queries)
            missing = await _run(manager, "mcp.weather.get_weather", {"location": "Atlantis", "language": "en"})
            assert missing["success"] is False
            assert "Atlantis" in missing["message"]
            assert len(stub.forecast_queries) == before

            # Upstream failure: a failure, not a crash of the server.
            stub.fail_forecast_with = 503
            down = await _run(manager, "mcp.weather.get_weather", {"location": "Berlin"})
            assert down["success"] is False
            assert "503" in down["message"]
        finally:
            if manager is not None:
                await manager.shutdown()
            await stub.stop()
