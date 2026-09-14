"""/health/ready — the backend readiness probe (MCP self-detection Phase 3).

k8s/backend.yaml now points readiness at /health/ready. Two properties matter for a
probe the kubelet calls every 10 s:

- a black-holed DB must yield a prompt 503, not a request held for the driver's
  pool/connect timeout (probes would pile up behind it);
- only the DB decides the status code — a slow LLM or a missing Redis must not drop
  every replica out of the Service.
"""
import asyncio
import json

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _HangingSession:
    async def __aenter__(self):
        await asyncio.sleep(3600)

    async def __aexit__(self, *exc):
        return False


class _OkSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_k):
        return None


class _DeadRedis:
    async def ping(self):
        raise ConnectionError("redis down")


async def test_a_hanging_database_answers_503_within_the_bound(monkeypatch):
    import main

    monkeypatch.setattr(main.settings, "health_ready_db_timeout_seconds", 0.05)
    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: _HangingSession())
    monkeypatch.setattr(main, "_get_health_redis_client", lambda: _DeadRedis())

    response = await asyncio.wait_for(main.readiness_check(), timeout=5.0)

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["checks"]["database"]["status"] == "unhealthy"


async def test_liveness_touches_no_dependency(monkeypatch):
    """k8s liveness points at /health/live. If it ever grew a DB check, a DB outage
    would restart every replica — so a dead DB must not change its answer."""
    import main

    def _boom():
        raise AssertionError("liveness must not open a DB session")

    monkeypatch.setattr(main, "AsyncSessionLocal", _boom)
    body = await main.liveness_check()
    assert body["status"] == "alive"


async def test_redis_down_alone_does_not_fail_readiness(monkeypatch):
    import main

    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: _OkSession())
    monkeypatch.setattr(main, "_get_health_redis_client", lambda: _DeadRedis())

    response = await main.readiness_check()

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["checks"]["redis"]["status"] == "degraded"
