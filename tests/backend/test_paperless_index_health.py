"""Paperless search-index health check + self-heal (Fix B), backend side.

What this encodes: detection + healing live in the Paperless MCP
(``search_index_health``, tested there); the backend owns the runtime gates, the
call shape, the persisted probe proof, the per-document heal ledger, the page
cursor, and — load-bearing — WHEN to raise. Raising is the alerting integration
(the scheduled-task engine turns a streak of raises into one ops_alert), so:

* a proven, unhealed degradation raises and KEEPS the cursor on that page;
* a probe proven once is remembered, so an index that lost its OLD documents
  (every old page entirely missing) is still reported in check mode;
* a heal blocked by Paperless workflows raises;
* a document that cannot be healed is retried a bounded number of times, then
  given up, reported directly, and the walk moves on;
* weak signals (inconclusive) never raise; a foreign response never reads healthy.
"""
import json
from types import SimpleNamespace

import pytest

import services.paperless_index_health as pih


class _FakeRedis:
    def __init__(self, *, broken=False):
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.sets: dict[str, set[str]] = {}
        self.broken = broken

    def _check(self):
        if self.broken:
            raise ConnectionError("redis down")

    async def get(self, key):
        self._check()
        return self.kv.get(key)

    async def set(self, key, value, ex=None):
        self._check()
        self.kv[key] = value
        if ex is not None:
            self.ttl[key] = ex

    async def hincrby(self, key, field, amount):
        self._check()
        h = self.hashes.setdefault(key, {})
        h[field] = h.get(field, 0) + amount
        return h[field]

    async def hdel(self, key, *fields):
        self._check()
        for f in fields:
            self.hashes.get(key, {}).pop(f, None)

    async def sadd(self, key, *members):
        self._check()
        self.sets.setdefault(key, set()).update(members)

    async def srem(self, key, *members):
        self._check()
        for m in members:
            self.sets.get(key, set()).discard(m)

    async def smembers(self, key):
        self._check()
        return set(self.sets.get(key, set()))

    async def expire(self, key, seconds):
        self._check()
        self.ttl[key] = seconds


class _MCP:
    def __init__(self, *inners, transport_error=False):
        self.inners = list(inners) or [{}]
        self.transport_error = transport_error
        self.calls: list[tuple[str, dict, dict]] = []

    async def execute_tool(self, tool, params, **kw):
        self.calls.append((tool, params, kw))
        if self.transport_error:
            return {"success": False, "message": "boom"}
        inner = self.inners[min(len(self.calls) - 1, len(self.inners) - 1)]
        return {"success": True, "message": json.dumps(inner)}


def _result(**over) -> dict:
    base = {
        "index_check": True, "verdict": "healthy", "db_total": 100, "page": 1,
        "next_page": 2, "sampled": 50, "found": 50, "missing": 0, "found_ids": [],
        "missing_ids": [], "control_id": None, "control_found": False,
        "probe_proven": False, "heal_attempted": False, "heal_blocked": None,
        "blocking_workflows": 0, "touched": 0, "healed": 0, "healed_ids": [],
        "still_missing_ids": [], "skipped_ids": [], "excluded_ids": [],
        "unverified_ids": [], "complete": True, "message": "",
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
    monkeypatch.setattr(settings, "paperless_index_heal_allow_workflows", False)
    monkeypatch.setattr(settings, "paperless_index_heal_max_attempts", 3)
    return settings


@pytest.fixture
def alerts(monkeypatch):
    from services import ops_alert

    ops_alert.reset_ledger()
    sent: list[dict] = []

    async def _notify(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(ops_alert, "notify_admin", _notify)
    yield sent
    ops_alert.reset_ledger()


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
        redis.kv[pih.CURSOR_KEY] = "3"
        mcp = _MCP(_result())
        await pih.run_index_health_check(mcp)
        tool, params, kw = mcp.calls[0]
        assert params == {
            "sample_size": 40, "page": 3, "heal": False, "max_touch": 7,
            "min_age_seconds": 600, "probe_proven": False, "exclude_ids": [],
            "allow_workflows": False,
        }
        assert kw["truncate"] is False
        assert kw["call_timeout"] > 30

    async def test_heal_and_workflow_override_passed_through(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        monkeypatch.setattr(flags, "paperless_index_heal_allow_workflows", True)
        mcp = _MCP(_result())
        await pih.run_index_health_check(mcp)
        assert mcp.calls[0][1]["heal"] is True
        assert mcp.calls[0][1]["allow_workflows"] is True


@pytest.mark.unit
class TestProbeProof:
    async def test_proof_is_remembered_and_sent_on_later_runs(self, flags, redis):
        """The 2026-08 shape: page 1 proves the probe; the old pages further down are
        entirely missing. The proof must survive to those later runs."""
        mcp = _MCP(
            _result(probe_proven=True, next_page=2),
            _result(verdict="healthy", next_page=3),
        )
        await pih.run_index_health_check(mcp)
        await pih.run_index_health_check(mcp)
        assert mcp.calls[0][1]["probe_proven"] is False
        assert mcp.calls[1][1]["probe_proven"] is True
        assert redis.ttl[pih.PROOF_KEY] == pih.PROOF_TTL_SECONDS  # expires on its own

    async def test_old_pages_missing_in_check_mode_raise(self, flags, redis):
        """Check mode (heal off): an entirely-missing old page that the MCP judged
        degraded thanks to the proof/control MUST raise, and keep the page."""
        redis.kv[pih.CURSOR_KEY] = "40"
        redis.kv[pih.PROOF_KEY] = "1"
        mcp = _MCP(_result(
            page=40, verdict="degraded", found=0, missing=50, sampled=50,
            control_found=True, probe_proven=True, next_page=41,
        ))
        with pytest.raises(RuntimeError, match="unvollständig"):
            await pih.run_index_health_check(mcp)
        assert mcp.calls[0][1]["probe_proven"] is True
        assert redis.kv[pih.CURSOR_KEY] == "40"


@pytest.mark.unit
class TestVerdicts:
    async def test_healthy_advances_cursor(self, flags, redis):
        out = await pih.run_index_health_check(_MCP(_result(next_page=2)))
        assert "verdict=healthy" in out
        assert redis.kv[pih.CURSOR_KEY] == "2"

    async def test_last_page_wraps_to_start(self, flags, redis):
        redis.kv[pih.CURSOR_KEY] = "9"
        await pih.run_index_health_check(_MCP(_result(page=9, next_page=None)))
        assert redis.kv[pih.CURSOR_KEY] == "1"

    async def test_degraded_detect_only_raises_and_keeps_page(self, flags, redis):
        redis.kv[pih.CURSOR_KEY] = "4"
        with pytest.raises(RuntimeError, match="unvollständig"):
            await pih.run_index_health_check(
                _MCP(_result(verdict="degraded", missing=3, found=47, next_page=5))
            )
        assert redis.kv[pih.CURSOR_KEY] == "4"

    async def test_degraded_fully_healed_is_ok_and_advances(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.kv[pih.CURSOR_KEY] = "4"
        out = await pih.run_index_health_check(_MCP(_result(
            verdict="degraded", missing=3, found=47, heal_attempted=True,
            touched=3, healed=3, healed_ids=[1, 2, 3], next_page=5,
        )))
        assert "healed=3" in out
        assert redis.kv[pih.CURSOR_KEY] == "5"

    async def test_heal_budget_exhausted_is_ok_but_keeps_page(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.kv[pih.CURSOR_KEY] = "4"
        out = await pih.run_index_health_check(_MCP(_result(
            verdict="degraded", missing=30, found=20, heal_attempted=True,
            touched=25, healed=25, healed_ids=list(range(25)), next_page=5,
        )))
        assert "fortgesetzt" in out
        assert redis.kv[pih.CURSOR_KEY] == "4"

    async def test_unverified_touches_keep_page_without_raising(self, flags, redis, monkeypatch):
        """Re-saved but not re-probed (time budget) is not a failed heal."""
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.kv[pih.CURSOR_KEY] = "4"
        out = await pih.run_index_health_check(_MCP(_result(
            verdict="degraded", missing=3, found=47, heal_attempted=True, touched=3,
            healed_ids=[1], unverified_ids=[2, 3], complete=False, next_page=5,
        )))
        assert "fortgesetzt" in out
        assert redis.kv[pih.CURSOR_KEY] == "4"
        assert pih.ATTEMPTS_KEY not in redis.hashes or not redis.hashes[pih.ATTEMPTS_KEY]

    async def test_index_error_raises(self, flags, redis):
        redis.kv[pih.CURSOR_KEY] = "2"
        with pytest.raises(RuntimeError, match="document_index reindex"):
            await pih.run_index_health_check(_MCP(_result(verdict="index_error")))
        assert redis.kv[pih.CURSOR_KEY] == "2"

    async def test_inconclusive_never_raises(self, flags, redis):
        out = await pih.run_index_health_check(
            _MCP(_result(verdict="inconclusive", found=0, missing=50, next_page=2))
        )
        assert "inconclusive" in out
        assert redis.kv[pih.CURSOR_KEY] == "2"

    async def test_empty_page_advances(self, flags, redis):
        out = await pih.run_index_health_check(
            _MCP(_result(verdict="empty", sampled=0, found=0, next_page=None))
        )
        assert "verdict=empty" in out
        assert redis.kv[pih.CURSOR_KEY] == "1"


@pytest.mark.unit
class TestWorkflowBlock:
    async def test_active_workflows_block_raises(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.kv[pih.CURSOR_KEY] = "4"
        with pytest.raises(RuntimeError, match="blockiert.*2 aktive"):
            await pih.run_index_health_check(_MCP(_result(
                verdict="degraded", missing=3, found=47, heal_blocked="workflows",
                blocking_workflows=2, next_page=5,
            )))
        assert redis.kv[pih.CURSOR_KEY] == "4"

    async def test_unverifiable_workflows_block_raises(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        with pytest.raises(RuntimeError, match="nicht prüfen"):
            await pih.run_index_health_check(_MCP(_result(
                verdict="degraded", missing=3, found=47,
                heal_blocked="workflows_unverifiable",
            )))


@pytest.mark.unit
class TestHealLedger:
    async def test_ineffective_heal_below_limit_raises_and_counts(self, flags, redis, alerts, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.kv[pih.CURSOR_KEY] = "4"
        with pytest.raises(RuntimeError, match="wirkungslos"):
            await pih.run_index_health_check(_MCP(_result(
                verdict="degraded", missing=2, found=48, heal_attempted=True,
                touched=2, healed_ids=[1], still_missing_ids=[11], next_page=5,
            )))
        assert redis.hashes[pih.ATTEMPTS_KEY] == {"11": 1}
        assert redis.kv[pih.CURSOR_KEY] == "4"
        assert alerts == []

    async def test_given_up_after_max_attempts_alerts_and_walk_continues(
        self, flags, redis, alerts, monkeypatch
    ):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.kv[pih.CURSOR_KEY] = "4"
        redis.hashes[pih.ATTEMPTS_KEY] = {"11": 2}
        out = await pih.run_index_health_check(_MCP(_result(
            verdict="degraded", missing=1, found=49, heal_attempted=True,
            touched=1, still_missing_ids=[11], next_page=5,
        )))
        assert redis.sets[pih.EXHAUSTED_KEY] == {"11"}
        assert redis.kv[pih.CURSOR_KEY] == "5"  # the walk continues
        assert len(alerts) == 1 and "11" in alerts[0]["message"]
        assert "verdict=degraded" in out

    async def test_given_up_ids_are_excluded_and_not_retried(self, flags, redis, alerts, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.sets[pih.EXHAUSTED_KEY] = {"11", "12"}
        redis.kv[pih.CURSOR_KEY] = "4"
        mcp = _MCP(_result(
            verdict="degraded", missing=1, found=49, heal_attempted=False,
            excluded_ids=[11], next_page=5,
        ))
        await pih.run_index_health_check(mcp)
        assert mcp.calls[0][1]["exclude_ids"] == [11, 12]
        assert redis.kv[pih.CURSOR_KEY] == "5"
        assert len(alerts) == 1  # still listed in the alert

    async def test_unhealable_alert_is_rate_limited(self, flags, redis, alerts, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.sets[pih.EXHAUSTED_KEY] = {"11"}
        page = _result(verdict="degraded", missing=1, found=49, excluded_ids=[11], next_page=None)
        await pih.run_index_health_check(_MCP(page))
        await pih.run_index_health_check(_MCP(page))
        assert len(alerts) == 1

    async def test_found_and_healed_ids_clear_the_ledger(self, flags, redis, monkeypatch):
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.hashes[pih.ATTEMPTS_KEY] = {"11": 2, "12": 1, "13": 1}
        redis.sets[pih.EXHAUSTED_KEY] = {"11"}
        await pih.run_index_health_check(_MCP(_result(
            verdict="degraded", missing=1, found=49, found_ids=[11], heal_attempted=True,
            touched=1, healed_ids=[12], next_page=5,
        )))
        assert redis.hashes[pih.ATTEMPTS_KEY] == {"13": 1}
        assert redis.sets[pih.EXHAUSTED_KEY] == set()

    async def test_skipped_documents_are_not_a_failed_heal(self, flags, redis, alerts, monkeypatch):
        """A doc deleted between listing and re-save (404) is skipped, not counted."""
        monkeypatch.setattr(flags, "paperless_index_heal_enabled", True)
        redis.kv[pih.CURSOR_KEY] = "4"
        out = await pih.run_index_health_check(_MCP(_result(
            verdict="degraded", missing=2, found=48, heal_attempted=True, touched=1,
            healed_ids=[1], skipped_ids=[2], next_page=5,
        )))
        assert "skipped=1" in out
        assert redis.kv[pih.CURSOR_KEY] == "5"
        assert not redis.hashes.get(pih.ATTEMPTS_KEY)


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
class TestRedisResilience:
    async def test_redis_down_still_runs_with_conservative_defaults(self, flags, monkeypatch):
        monkeypatch.setattr(pih, "get_redis", lambda: _FakeRedis(broken=True))
        mcp = _MCP(_result(probe_proven=True))
        out = await pih.run_index_health_check(mcp)
        params = mcp.calls[0][1]
        assert params["page"] == 1
        assert params["probe_proven"] is False
        assert params["exclude_ids"] == []
        assert "verdict=healthy" in out

    async def test_garbage_cursor_resets(self, flags, redis):
        redis.kv[pih.CURSOR_KEY] = "-3"
        mcp = _MCP(_result())
        await pih.run_index_health_check(mcp)
        assert mcp.calls[0][1]["page"] == 1
