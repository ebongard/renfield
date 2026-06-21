"""
Tests for ``services.artifact_service`` — the backend half of chat artifacts
(Lane A). Per the locked design the backend validates ONLY the DoS gate
(kind allowlist + size/row/series/point caps); the frontend zod schema owns
shape. These tests pin the caps + the persistence round-trip + the chart
numeric (NaN/Infinity) guard.
"""
import pytest

from services.artifact_service import (
    ALLOWED_KINDS,
    MAX_CHART_POINTS,
    MAX_CHART_SERIES,
    MAX_KEYVALUE_PAIRS,
    MAX_LIST_ITEMS,
    MAX_TABLE_ROWS,
    ArtifactRejected,
    merge_artifacts_into_metadata,
    validate_artifact,
    validate_artifacts,
)


def _table(rows):
    return {"id": "art_t", "kind": "table", "data": {"columns": ["A", "B"], "rows": rows}}


# --- kind allowlist ---------------------------------------------------------

@pytest.mark.unit
def test_allowed_kinds_are_exactly_lane_a():
    assert ALLOWED_KINDS == frozenset({"table", "list", "keyvalue", "chart", "weather"})


# --- weather widget (Gen-UI kind) ------------------------------------------

def _weather(**over):
    data = {
        "location": "Berlin",
        "current": {"temp": 18.0, "unit": "°C", "code": 3, "condition": "Bedeckt"},
    }
    data.update(over)
    return {"id": "art_w", "kind": "weather", "data": data}


@pytest.mark.unit
def test_weather_minimal_valid():
    out = validate_artifact(_weather())
    assert out["kind"] == "weather"
    assert out["data"]["location"] == "Berlin"
    assert out["data"]["current"]["temp"] == 18.0
    assert out["data"]["current"]["code"] == 3
    # No optional fields / forecast when not supplied.
    assert "forecast" not in out["data"]
    assert "feelsLike" not in out["data"]["current"]


@pytest.mark.unit
def test_weather_optional_current_fields_and_forecast():
    out = validate_artifact(_weather(
        current={
            "temp": 18, "unit": "°C", "code": 61, "condition": "Regen",
            "feelsLike": 16, "humidity": 80, "windSpeed": 12, "high": 20, "low": 11,
        },
        forecast=[
            {"date": "2026-06-21", "code": 1, "high": 22, "low": 12,
             "condition": "Heiter", "precipChance": 10},
            {"date": "2026-06-22", "code": 80, "high": 19, "low": 13},
        ],
    ))
    cur = out["data"]["current"]
    assert (cur["feelsLike"], cur["humidity"], cur["windSpeed"]) == (16, 80, 12)
    assert (cur["high"], cur["low"]) == (20, 11)
    assert len(out["data"]["forecast"]) == 2
    assert out["data"]["forecast"][0]["precipChance"] == 10
    # A forecast entry without optional condition/precipChance keeps them absent.
    assert "condition" not in out["data"]["forecast"][1]


@pytest.mark.unit
def test_weather_missing_current_rejected():
    with pytest.raises(ArtifactRejected):
        validate_artifact({"id": "x", "kind": "weather", "data": {"location": "Berlin"}})


@pytest.mark.unit
def test_weather_nonfinite_temp_rejected():
    with pytest.raises(ArtifactRejected):
        validate_artifact(_weather(current={"temp": float("nan"), "unit": "°C", "code": 0, "condition": "x"}))


@pytest.mark.unit
def test_weather_forecast_cap():
    from services.artifact_service import MAX_FORECAST_DAYS
    days = [{"date": f"2026-06-{d:02d}", "code": 0, "high": 20, "low": 10}
            for d in range(1, MAX_FORECAST_DAYS + 3)]
    with pytest.raises(ArtifactRejected):
        validate_artifact(_weather(forecast=days))


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["html", "svg", "iframe", "script", "Table", "", None])
def test_unknown_kind_rejected(kind):
    with pytest.raises(ArtifactRejected):
        validate_artifact({"id": "x", "kind": kind, "data": {}})


@pytest.mark.unit
def test_missing_id_rejected():
    with pytest.raises(ArtifactRejected):
        validate_artifact({"kind": "list", "data": {"items": []}})


# --- size caps (the DoS gate) ----------------------------------------------

@pytest.mark.unit
def test_table_10k_rows_rejected_before_emit():
    big = [["1", "2"] for _ in range(10_000)]
    with pytest.raises(ArtifactRejected):
        validate_artifact(_table(big))


@pytest.mark.unit
def test_table_at_cap_accepted_over_cap_rejected():
    ok = validate_artifact(_table([["1", "2"]] * MAX_TABLE_ROWS))
    assert len(ok["data"]["rows"]) == MAX_TABLE_ROWS
    with pytest.raises(ArtifactRejected):
        validate_artifact(_table([["1", "2"]] * (MAX_TABLE_ROWS + 1)))


@pytest.mark.unit
def test_list_over_cap_rejected():
    with pytest.raises(ArtifactRejected):
        validate_artifact({"id": "l", "kind": "list", "data": {"items": ["x"] * (MAX_LIST_ITEMS + 1)}})


@pytest.mark.unit
def test_keyvalue_over_cap_rejected():
    pairs = [{"key": "k", "value": "v"}] * (MAX_KEYVALUE_PAIRS + 1)
    with pytest.raises(ArtifactRejected):
        validate_artifact({"id": "kv", "kind": "keyvalue", "data": {"pairs": pairs}})


@pytest.mark.unit
def test_chart_too_many_series_rejected():
    series = [{"label": str(i), "points": [{"x": 0, "y": 1}]} for i in range(MAX_CHART_SERIES + 1)]
    with pytest.raises(ArtifactRejected):
        validate_artifact({"id": "c", "kind": "chart", "data": {"chartType": "bar", "series": series}})


@pytest.mark.unit
def test_chart_too_many_points_rejected():
    points = [{"x": i, "y": i} for i in range(MAX_CHART_POINTS + 1)]
    with pytest.raises(ArtifactRejected):
        validate_artifact({"id": "c", "kind": "chart",
                           "data": {"chartType": "line", "series": [{"label": "s", "points": points}]}})


# --- chart numeric validation (NaN / Infinity → viewBox DoS) ----------------

@pytest.mark.unit
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "5", None, [1]])
def test_chart_non_finite_coordinate_rejected(bad):
    with pytest.raises(ArtifactRejected):
        validate_artifact({"id": "c", "kind": "chart",
                           "data": {"chartType": "line", "series": [{"label": "s", "points": [{"x": 0, "y": bad}]}]}})


@pytest.mark.unit
def test_chart_finite_coordinates_accepted_and_coerced_to_float():
    art = validate_artifact({"id": "c", "kind": "chart",
                             "data": {"chartType": "bar", "series": [{"label": "s", "points": [{"x": 1, "y": 2}]}]}})
    pt = art["data"]["series"][0]["points"][0]
    assert isinstance(pt["x"], float) and isinstance(pt["y"], float)


# --- scalar coercion + per-cell cap -----------------------------------------

@pytest.mark.unit
def test_numbers_and_bools_stringified_in_cells():
    art = validate_artifact({"id": "t", "kind": "table",
                             "data": {"columns": ["n"], "rows": [[42], [True], [3.5]]}})
    assert art["data"]["rows"] == [["42"], ["True"], ["3.5"]]


@pytest.mark.unit
def test_structural_cell_rejected():
    with pytest.raises(ArtifactRejected):
        validate_artifact({"id": "t", "kind": "table", "data": {"columns": ["n"], "rows": [[{"nested": 1}]]}})


@pytest.mark.unit
def test_oversized_cell_rejected():
    huge = "x" * 100_000
    with pytest.raises(ArtifactRejected):
        validate_artifact({"id": "l", "kind": "list", "data": {"items": [huge]}})


# --- valid artifacts (round shape) ------------------------------------------

@pytest.mark.unit
def test_valid_table_keeps_title_and_partial():
    art = validate_artifact({"id": "t", "kind": "table", "title": "Plan", "partial": True,
                             "data": {"columns": ["A"], "rows": [["1"]]}})
    assert art["id"] == "t" and art["title"] == "Plan" and art["partial"] is True
    assert art["kind"] == "table"


@pytest.mark.unit
def test_list_ordered_flag_preserved():
    art = validate_artifact({"id": "l", "kind": "list", "data": {"ordered": True, "items": ["a", "b"]}})
    assert art["data"]["ordered"] is True and art["data"]["items"] == ["a", "b"]


# --- validate_artifacts (list level: drop rejects, cap count) ---------------

@pytest.mark.unit
def test_validate_artifacts_drops_bad_keeps_good():
    out = validate_artifacts([
        {"id": "ok", "kind": "list", "data": {"items": ["a"]}},
        {"id": "bad", "kind": "html", "data": {}},        # unknown kind → dropped
        {"id": "ok2", "kind": "keyvalue", "data": {"pairs": [{"key": "k", "value": "v"}]}},
    ])
    assert [a["id"] for a in out] == ["ok", "ok2"]


@pytest.mark.unit
def test_validate_artifacts_non_list_returns_empty():
    assert validate_artifacts(None) == []
    assert validate_artifacts({"id": "x"}) == []


# --- persistence: message_metadata["artifacts"] array, keyed by id ----------

@pytest.mark.unit
def test_merge_persists_array_keyed_by_id():
    meta: dict = {}
    a1 = validate_artifact({"id": "t", "kind": "table", "data": {"columns": ["A"], "rows": [["1"]]}})
    a2 = validate_artifact({"id": "c", "kind": "chart",
                            "data": {"chartType": "bar", "series": [{"label": "s", "points": [{"x": 0, "y": 1}]}]}})
    merge_artifacts_into_metadata(meta, [a1, a2])
    assert [a["id"] for a in meta["artifacts"]] == ["t", "c"]


@pytest.mark.unit
def test_merge_same_id_replaces_not_duplicates():
    meta: dict = {"artifacts": [{"id": "t", "kind": "table", "data": {"columns": [], "rows": [["1"]]}}]}
    newer = validate_artifact({"id": "t", "kind": "table", "data": {"columns": ["A"], "rows": [["1"], ["2"]]}})
    merge_artifacts_into_metadata(meta, [newer])
    assert len(meta["artifacts"]) == 1
    assert len(meta["artifacts"][0]["data"]["rows"]) == 2


@pytest.mark.unit
def test_merge_empty_is_noop():
    meta: dict = {}
    merge_artifacts_into_metadata(meta, [])
    assert "artifacts" not in meta


@pytest.mark.unit
def test_persistence_round_trip_shape():
    """An artifact survives a metadata write → read as the same dict shape that
    the frontend `historyToUiMessage` rehydrates from ``metadata.artifacts``."""
    meta: dict = {"intent": "x", "agent_role": "general"}
    art = validate_artifact({"id": "t", "kind": "list", "title": "Einkauf",
                             "data": {"items": ["Milch", "Brot"]}})
    merge_artifacts_into_metadata(meta, [art])
    # simulate JSON round-trip (DB jsonb)
    import json
    restored = json.loads(json.dumps(meta))
    assert restored["artifacts"][0] == {
        "id": "t", "kind": "list", "title": "Einkauf", "partial": False,
        "data": {"items": ["Milch", "Brot"]},
    }
