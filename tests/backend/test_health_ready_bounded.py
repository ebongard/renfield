"""/health/ready — the backend readiness probe (MCP self-detection Phase 3).

k8s/backend.yaml points readiness at /health/ready. What matters for a probe the
kubelet calls every 10 s on every replica at once:

- only DB REACHABILITY decides — on the probe's OWN connection, so an exhausted app
  pool (the 2026-07-01 case) reads as slow, not as 503 on every replica;
- a black-holed DB yields a prompt 503 within the bound;
- a hanging Redis or a hanging device hook is bounded and never fails readiness;
- liveness never touches a dependency.
"""
import asyncio
import json
import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _HangingSession:
    async def __aenter__(self):
        await asyncio.sleep(3600)

    async def __aexit__(self, *exc):
        return False


class _OkConnection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        return None


class _OkEngine:
    def connect(self):
        return _OkConnection()


class _HangingEngine:
    def connect(self):
        return _HangingSession()


class _OkRedis:
    async def ping(self):
        return True


class _HangingRedis:
    async def ping(self):
        await asyncio.sleep(3600)


@pytest.fixture
def hc(monkeypatch):
    from services import health_check

    monkeypatch.setattr(health_check, "_engine", None)
    monkeypatch.setattr(health_check, "_redis_client", None)
    monkeypatch.setattr(health_check.settings, "health_ready_db_timeout_seconds", 0.2)
    monkeypatch.setattr(health_check.settings, "health_ready_aux_timeout_seconds", 0.1)
    monkeypatch.setattr(health_check, "_get_redis_client", lambda: _OkRedis())

    async def _no_hooks(*_a, **_k):
        return []

    monkeypatch.setattr("utils.hooks.run_hooks", _no_hooks)
    return health_check


async def _ready():
    import main

    response = await asyncio.wait_for(main.readiness_check(), timeout=5.0)
    return response.status_code, json.loads(response.body)


async def test_exhausted_app_pool_with_a_reachable_db_stays_ready(hc, monkeypatch):
    """The decided behaviour: pool saturation is 'slow', not 'down'. The app pool
    hangs, the DB itself answers → 200."""
    import main

    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: _HangingSession(), raising=False)
    monkeypatch.setattr("services.database.AsyncSessionLocal", lambda: _HangingSession())
    monkeypatch.setattr(hc, "_get_engine", lambda: _OkEngine())

    status, body = await _ready()
    assert status == 200
    assert body["checks"]["database"]["status"] == "healthy"


async def test_a_black_holed_database_answers_503_within_the_bound(hc, monkeypatch):
    monkeypatch.setattr(hc, "_get_engine", lambda: _HangingEngine())

    started = time.monotonic()
    status, body = await _ready()
    assert status == 503
    assert body["checks"]["database"]["status"] == "unhealthy"
    assert time.monotonic() - started < 2.0


async def test_an_unreachable_database_fails_fast_on_a_real_engine(hc, monkeypatch):
    """A real asyncpg NullPool engine against a closed port: refused → 503, bounded."""
    monkeypatch.setattr(
        hc.settings, "database_url", "postgresql://u:p@127.0.0.1:1/none"
    )
    monkeypatch.setattr(hc.settings, "health_ready_db_timeout_seconds", 2.0)

    started = time.monotonic()
    status, _ = await _ready()
    assert status == 503
    assert time.monotonic() - started < 4.0
    await hc.dispose()


async def test_the_probe_engine_is_created_once_and_uses_nullpool(hc, monkeypatch):
    from sqlalchemy.pool import NullPool

    monkeypatch.setattr(hc.settings, "database_url", "sqlite+aiosqlite:///:memory:")
    first = hc._get_engine()
    assert hc._get_engine() is first
    assert isinstance(first.pool, NullPool)
    await hc.check_database()  # a real SELECT 1 on its own connection
    await hc.dispose()
    assert hc._engine is None


async def test_a_hanging_redis_is_bounded_and_does_not_fail_readiness(hc, monkeypatch):
    monkeypatch.setattr(hc, "_get_engine", lambda: _OkEngine())
    monkeypatch.setattr(hc, "_get_redis_client", lambda: _HangingRedis())

    started = time.monotonic()
    status, body = await _ready()
    assert status == 200
    assert body["checks"]["redis"]["status"] == "degraded"
    assert time.monotonic() - started < 2.0


async def test_a_hanging_device_hook_is_bounded_and_does_not_fail_readiness(hc, monkeypatch):
    monkeypatch.setattr(hc, "_get_engine", lambda: _OkEngine())

    async def _hanging_hooks(*_a, **_k):
        await asyncio.sleep(3600)

    monkeypatch.setattr("utils.hooks.run_hooks", _hanging_hooks)

    started = time.monotonic()
    status, body = await _ready()
    assert status == 200
    assert body["checks"]["devices"]["status"] == "unknown"
    assert time.monotonic() - started < 2.0


async def test_the_redis_client_carries_socket_timeouts(monkeypatch):
    """Belt and braces under asyncio.timeout: the client itself must not wait for a
    socket longer than the aux bound either."""
    import redis.asyncio as aioredis

    from services import health_check

    captured = {}

    def _from_url(url, **kwargs):
        captured.update(kwargs)
        return _OkRedis()

    monkeypatch.setattr(aioredis, "from_url", _from_url)
    monkeypatch.setattr(health_check, "_redis_client", None)
    monkeypatch.setattr(health_check.settings, "health_ready_aux_timeout_seconds", 0.7)

    health_check._get_redis_client()

    assert captured["socket_connect_timeout"] == 0.7
    assert captured["socket_timeout"] == 0.7


async def test_liveness_touches_no_dependency(monkeypatch):
    """k8s liveness points at /health/live. If it ever grew a DB check, a DB outage
    would restart every replica — so a dead DB must not change its answer."""
    import main

    from services import health_check

    def _boom():
        raise AssertionError("liveness must not open a DB connection")

    monkeypatch.setattr(health_check, "_get_engine", _boom)
    body = await main.liveness_check()
    assert body["status"] == "alive"
