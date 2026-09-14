"""Paperless search-index health check + self-heal (Fix B), backend side.

What this encodes: detection + healing live in the Paperless MCP
(``search_index_health``, tested there); the backend owns the runtime gates, the
call shape, the page cursor, and — load-bearing — WHEN to raise. Raising is the
whole alerting integration (the scheduled-task engine turns a streak of raises
into one ops_alert), so these tests pin that:

* a proven, unhealed degradation raises and KEEPS the cursor on that page, so the
  next run re-checks it and the streak can build;
* a successfully healed page does not raise and moves on;
* weak signals (inconclusive) never raise;
* a foreign / old-MCP response never reads as healthy.
"""
import json
from types import SimpleNamespace

import pytest

import services.paperless_index_health as pih


class _FakeRedis:
    def __init__(self, value=None, *, broken=False):
        self.value = value
        self.broken = broken

    async def get(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        return self.value

    async def set(self, key, value):
        if self.broken:
            raise ConnectionError("redis down")
        self.value = value


class _MCP:
    def __init__(self, inner=None, *, transport_error=False):
        self.inner = inner or {}
        self.transport_error = transport_error
        self.calls: list[tuple[str, dict, dict]] = []

    async def execute_tool(self, tool, params, **kw):
        self.calls.append((tool, params, kw))
        if self.transport_error:
            return {"success": False, "message": "boom"}
        return {"success": True, "message": json.dumps(self.inner)}


def _result(**over) -> dict:
    base = {
        "index_check": True, "verdict": "healthy", "db_total": 100, "page": 1,
        "next_page": 2, "sampled": 50, "found": 50, "missing": 0, "missing_ids": [],
        "heal_attempted": False, "touched": 0, "healed": 0, "still_missing_ids": [],
        "complete": True, "message": "",
    }
    base.update(over)
    return base


@pytest.fixture
def redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(pih, "get_redis", lambda: r)
    return r


@pytest.fixture
def flags(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "paperless_index_check_enabled", True)
    monkeypatch.setattr(settings, "paperless_index_heal_enabled", False)
    return settings


@pytest.mark.unit
class TestHandlerGate:
    async def test_gate_off_makes_no_mcp_call(self, monkeypatch):
        from services.scheduled_tasks import builtins
        from utils.config import settings

        monkeypatch.setattr(settings, "paperless_index_check_enabled", False)
        mcp = _MCP(_result())
        app = SimpleNamespace(state=SimpleNamespace(mcp_manager=mcp))
        out = await builtins._paperless_index_health_handler(app, {})
        assert "skipped" in out
        assert mcp.calls == []

    async def test_no_mcp_manager_skips(self, flags):
        from services.scheduled_tasks import builtins

        out = await builtins._paperless_index_health_handler(
            SimpleNamespace(state=SimpleNamespace()), {}
        )
        assert "skipped" in out

    async def test_enabled_runs_the_check(self, flags, redis):
        from services.scheduled_tasks import builtins

        mcp = _MCP(_result())
        app = SimpleNamespace(state=SimpleNamespace(mcp_manager=mcp))
        out = await builtins._paperless_index_health_handler(app, {})
        assert "verdict=healthy" in out
        assert mcp.calls[0][0] == "mcp.paperless.search_index_health"


@pytest.mark.unit
class TestCallShape:
    async def test_params_and_transport_options(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_check_sample_size", 40)
        monkeypatch.setattr(flags, "paperless_index_heal_max_touch", 7)
        monkeypatch.setattr(flags, "paperless_index_check_min_age_seconds", 600)
        redis.value = "3"
        mcp = _MCP(_result())
        await pih.run_index_health_check(mcp)
        tool, params, kw = mcp.calls[0]
        assert params == {
            "sample_size": 40, "page": 3, "heal": False, "max_touch": 7,
            "min_age_seconds": 600,
        }
        assert kw["truncate"] is False
        assert kw["call_timeout"] > 30

    async def test_heal_flag_passed_through_each_run(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        mcp = _MCP(_result())
        await pih.run_index_health_check(mcp)
        assert mcp.calls[0][1]["heal"] is True


@pytest.mark.unit
class TestVerdicts:
    async def test_healthy_advances_cursor(self, flags, redis):
        out = await pih.run_index_health_check(_MCP(_result(next_page=2)))
        assert "verdict=healthy" in out
        assert redis.value == "2"

    async def test_last_page_wraps_to_start(self, flags, redis):
        redis.value = "9"
        await pih.run_index_health_check(_MCP(_result(page=9, next_page=None)))
        assert redis.value == "1"

    async def test_degraded_detect_only_raises_and_keeps_page(self, flags, redis):
        redis.value = "4"
        with pytest.raises(RuntimeError, match="unvollständig"):
            await pih.run_index_health_check(
                _MCP(_result(verdict="degraded", missing=3, found=47, next_page=5))
            )
        assert redis.value == "4"  # re-check the same page → the streak builds

    async def test_degraded_fully_healed_is_ok_and_advances(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.value = "4"
        out = await pih.run_index_health_check(_MCP(_result(
            verdict="degraded", missing=3, found=47, heal_attempted=True,
            touched=3, healed=3, still_missing_ids=[], next_page=5,
        )))
        assert "healed=3" in out
        assert redis.value == "5"

    async def test_heal_ineffective_raises_and_keeps_page(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.value = "4"
        with pytest.raises(RuntimeError, match="wirkungslos"):
            await pih.run_index_health_check(_MCP(_result(
                verdict="degraded", missing=3, found=47, heal_attempted=True,
                touched=3, healed=1, still_missing_ids=[11, 12], next_page=5,
            )))
        assert redis.value == "4"

    async def test_heal_budget_exhausted_is_ok_but_keeps_page(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.value = "4"
        out = await pih.run_index_health_check(_MCP(_result(
            verdict="degraded", missing=30, found=20, heal_attempted=True,
            touched=25, healed=25, still_missing_ids=[], next_page=5,
        )))
        assert "fortgesetzt" in out
        assert redis.value == "4"

    async def test_index_error_raises(self, flags, redis):
        redis.value = "2"
        with pytest.raises(RuntimeError, match="document_index reindex"):
            await pih.run_index_health_check(_MCP(_result(verdict="index_error")))
        assert redis.value == "2"

    async def test_inconclusive_never_raises(self, flags, redis):
        """A weak signal is logged and reported, never escalated to an alert."""
        out = await pih.run_index_health_check(
            _MCP(_result(verdict="inconclusive", found=0, missing=50, next_page=2))
        )
        assert "inconclusive" in out
        assert redis.value == "2"

    async def test_empty_page_advances(self, flags, redis):
        out = await pih.run_index_health_check(
            _MCP(_result(verdict="empty", sampled=0, found=0, next_page=None))
        )
        assert "verdict=empty" in out
        assert redis.value == "1"


@pytest.mark.unit
class TestNeverFalseGreen:
    async def test_old_mcp_fuzzy_fallback_raises(self, flags, redis):
        mcp = _MCP({"summary": {"total_matching": 0}, "results": []})
        with pytest.raises(RuntimeError, match="nicht verfügbar"):
            await pih.run_index_health_check(mcp)

    async def test_tool_error_raises(self, flags, redis):
        with pytest.raises(RuntimeError, match="fehlgeschlagen"):
            await pih.run_index_health_check(_MCP({"error": "PAPERLESS_API_URL not configured"}))

    async def test_transport_failure_raises(self, flags, redis):
        with pytest.raises(RuntimeError):
            await pih.run_index_health_check(_MCP(transport_error=True))

    def test_marker_requires_index_check_true(self):
        assert pih.looks_like_index_result({"index_check": True, "verdict": "healthy"})
        assert not pih.looks_like_index_result({"verdict": "healthy"})
        assert not pih.looks_like_index_result({"index_check": True})


@pytest.mark.unit
class TestCursorResilience:
    async def test_redis_down_starts_at_page_one_and_still_runs(self, flags, monkeypatch):
        monkeypatch.setattr(pih, "get_redis", lambda: _FakeRedis(broken=True))
        mcp = _MCP(_result())
        out = await pih.run_index_health_check(mcp)
        assert mcp.calls[0][1]["page"] == 1
        assert "verdict=healthy" in out

    async def test_garbage_cursor_resets(self, flags, redis):
        redis.value = "-3"
        mcp = _MCP(_result())
        await pih.run_index_health_check(mcp)
        assert mcp.calls[0][1]["page"] == 1
