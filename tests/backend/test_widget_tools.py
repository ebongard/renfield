"""
Tests for ``services.widget_tools`` — the Gen-UI render/weather internal tools.

Each tool returns a validated Lane-A artifact in ``data["artifacts"]`` (the
chat handler collects these and emits them). These pin the arg coercion
(real arrays AND JSON-string args), the validation reject path, and the
Open-Meteo → ``weather`` artifact mapping (via a fake MCP manager).
"""
import json

import pytest

from services.widget_tools import render_list, render_table, weather_widget


# --- render_table -----------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_render_table_with_arrays():
    out = await render_table({
        "columns": ["Name", "Preis"],
        "rows": [["Apfel", "1€"], ["Birne", "2€"]],
        "title": "Obst",
    })
    assert out["success"] is True
    art = out["data"]["artifacts"][0]
    assert art["kind"] == "table"
    assert art["title"] == "Obst"
    assert art["data"]["columns"] == ["Name", "Preis"]
    assert len(art["data"]["rows"]) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_render_table_with_json_string_args():
    # The model may encode complex args as JSON strings — coerce both.
    out = await render_table({
        "columns": json.dumps(["A", "B"]),
        "rows": json.dumps([["1", "2"]]),
    })
    assert out["success"] is True
    assert out["data"]["artifacts"][0]["data"]["rows"] == [["1", "2"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_render_table_missing_rows_rejected():
    out = await render_table({"columns": ["A"]})
    assert out["success"] is False
    assert "data" not in out  # no artifact emitted


@pytest.mark.unit
@pytest.mark.asyncio
async def test_render_table_numbers_coerced_to_strings():
    # validate_artifact stringifies numeric cells — the agent may emit raw ints.
    out = await render_table({"columns": ["n"], "rows": [[42], [3.5]]})
    assert out["success"] is True
    assert out["data"]["artifacts"][0]["data"]["rows"] == [["42"], ["3.5"]]


# --- render_list ------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_render_list_with_array_and_ordered_string():
    out = await render_list({"items": ["eins", "zwei"], "ordered": "true"})
    assert out["success"] is True
    art = out["data"]["artifacts"][0]
    assert art["kind"] == "list"
    assert art["data"]["items"] == ["eins", "zwei"]
    assert art["data"]["ordered"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_render_list_json_string_items():
    out = await render_list({"items": json.dumps(["a", "b", "c"])})
    assert out["success"] is True
    assert out["data"]["artifacts"][0]["data"]["items"] == ["a", "b", "c"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_render_list_missing_items_rejected():
    out = await render_list({"title": "x"})
    assert out["success"] is False
    assert "data" not in out


# --- weather_widget ---------------------------------------------------------

class _FakeMCP:
    """Minimal mcp_manager double: returns a get_weather-shaped payload wrapped
    the way MCPManager.execute_tool does (content blocks under ``data``)."""

    def __init__(self, payload: dict, *, success: bool = True):
        self._payload = payload
        self._success = success
        self.calls: list = []

    async def execute_tool(self, intent: str, params: dict) -> dict:
        self.calls.append((intent, params))
        return {
            "success": self._success,
            "data": [{"type": "text", "text": json.dumps(self._payload)}],
        }


_OPEN_METEO = {
    "location": {"name": "Berlin", "country": "Germany"},
    "current": {
        "temperature": 18.2, "weather_code": 3, "weather_description": "Bedeckt",
        "feels_like": 16.9, "humidity": 72, "wind_speed": 14.0,
    },
    "daily": [
        {"date": "2026-06-20", "temp_max": 20, "temp_min": 11, "weather_code": 3,
         "weather_description": "Bedeckt", "precipitation_probability": 20},
        {"date": "2026-06-21", "temp_max": 23, "temp_min": 12, "weather_code": 1,
         "weather_description": "Heiter", "precipitation_probability": 5},
        {"date": "2026-06-22", "temp_max": 19, "temp_min": 13, "weather_code": 80,
         "weather_description": "Schauer", "precipitation_probability": 60},
    ],
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weather_widget_maps_open_meteo_to_artifact():
    mcp = _FakeMCP(_OPEN_METEO)
    out = await weather_widget({"location": "Berlin"}, mcp_manager=mcp)
    assert out["success"] is True
    # It requested celsius from the MCP.
    assert mcp.calls[0][0] == "mcp.weather.get_weather"
    assert mcp.calls[0][1]["temperature_unit"] == "celsius"
    art = out["data"]["artifacts"][0]
    assert art["kind"] == "weather"
    cur = art["data"]["current"]
    assert cur["temp"] == 18.2
    assert cur["code"] == 3
    assert cur["condition"] == "Bedeckt"
    assert cur["feelsLike"] == 16.9 and cur["humidity"] == 72 and cur["windSpeed"] == 14.0
    # Today's high/low come from daily[0].
    assert cur["high"] == 20 and cur["low"] == 11
    # Forecast = daily[1:] (today skipped) → 2 days here.
    assert [d["date"] for d in art["data"]["forecast"]] == ["2026-06-21", "2026-06-22"]
    assert art["data"]["forecast"][1]["precipChance"] == 60
    # Raw data also returned for the agent's prose.
    assert out["data"]["weather"]["location"]["name"] == "Berlin"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weather_widget_requires_location():
    out = await weather_widget({}, mcp_manager=_FakeMCP(_OPEN_METEO))
    assert out["success"] is False
    assert "data" not in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weather_widget_no_mcp():
    out = await weather_widget({"location": "Berlin"}, mcp_manager=None)
    assert out["success"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weather_widget_location_error_surfaced():
    mcp = _FakeMCP({"error": "Location 'Xyzzy' not found"})
    out = await weather_widget({"location": "Xyzzy"}, mcp_manager=mcp)
    assert out["success"] is False
    assert "not found" in out["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weather_widget_no_current_falls_back_to_raw():
    # No current block → no artifact, but the raw data is handed back so the
    # agent can still answer in prose.
    mcp = _FakeMCP({"location": {"name": "Berlin"}, "daily": []})
    out = await weather_widget({"location": "Berlin"}, mcp_manager=mcp)
    assert out["success"] is True
    assert "artifacts" not in out["data"]
    assert out["data"]["weather"]["location"]["name"] == "Berlin"


# --- chat_handler collection helper ----------------------------------------

@pytest.mark.unit
def test_collect_tool_artifacts_flattens_tool_data():
    from api.websocket.chat_handler import _collect_tool_artifacts

    results = [
        ("internal.render_list", {"artifacts": [{"id": "a", "kind": "list"}]}),
        ("mcp.search.web_search", {"results": []}),  # no artifacts → ignored
        ("internal.weather_widget", {"artifacts": [{"id": "w", "kind": "weather"}], "weather": {}}),
        ("internal.x", None),  # tolerated
    ]
    arts = _collect_tool_artifacts(results)
    assert [a["id"] for a in arts] == ["a", "w"]
