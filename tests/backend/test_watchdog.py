"""External HTTP watchdog (A3).

What this encodes: a system that is down cannot report itself. The watchdog is
the floor under that — it probes OTHER endpoints and raises when one is gone, so
the scheduled-task failure-streak machinery does the alerting. The tests pin the
target parsing (one typo must not disarm the other targets), the probe verdict
(non-2xx IS the signal — /health/ready answers 503 with a dead dependency), and
the raise-on-failure contract the alerting depends on.
"""
import pytest

from services import watchdog


@pytest.mark.unit
class TestParseTargets:
    def test_parses_name_url_pairs(self):
        targets = watchdog.parse_targets(
            "xidra=http://a.svc:8000/health/ready, paperless=http://192.168.1.162:8000/"
        )
        assert [(t.name, t.url) for t in targets] == [
            ("xidra", "http://a.svc:8000/health/ready"),
            ("paperless", "http://192.168.1.162:8000/"),
        ]

    def test_empty_is_inert(self):
        assert watchdog.parse_targets("") == []
        assert watchdog.parse_targets(None) == []

    def test_malformed_entry_is_dropped_not_fatal(self):
        """One typo in an env var must not take down the watch on everything else."""
        targets = watchdog.parse_targets("broken, ok=http://a/health/ready, =http://b")
        assert [t.name for t in targets] == ["ok"]

    def test_non_http_scheme_rejected(self):
        assert watchdog.parse_targets("weird=ftp://a/") == []

    def test_duplicate_name_ignored(self):
        targets = watchdog.parse_targets("a=http://one/, a=http://two/")
        assert [t.url for t in targets] == ["http://one/"]


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    """Stands in for httpx.AsyncClient; maps url → status code or exception."""

    def __init__(self, responses: dict):
        self._responses = responses
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        self.calls.append(url)
        outcome = self._responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


@pytest.fixture
def fake_http(monkeypatch):
    def _install(responses: dict):
        client = _FakeClient(responses)
        monkeypatch.setattr(watchdog.httpx, "AsyncClient", lambda **kw: client)
        return client

    return _install


@pytest.mark.unit
@pytest.mark.asyncio
class TestRunWatchdog:
    async def test_no_targets_is_a_clean_noop(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "watchdog_targets", "")
        assert "keine Ziele" in await watchdog.run_watchdog()

    async def test_all_reachable_returns_detail(self, monkeypatch, fake_http):
        from utils.config import settings

        monkeypatch.setattr(settings, "watchdog_targets", "peer=http://peer/health/ready")
        fake_http({"http://peer/health/ready": 200})

        assert await watchdog.run_watchdog() == "1 Ziel(e) erreichbar"

    async def test_connection_error_raises_naming_the_target(self, monkeypatch, fake_http):
        """The raise IS the alerting integration — the engine's failure-streak
        machinery turns it into exactly one notification."""
        from utils.config import settings

        monkeypatch.setattr(settings, "watchdog_targets", "xidra=http://peer/health/ready")
        fake_http({"http://peer/health/ready": ConnectionError("refused")})

        with pytest.raises(RuntimeError) as exc:
            await watchdog.run_watchdog()
        assert "xidra" in str(exc.value)

    async def test_503_counts_as_down(self, monkeypatch, fake_http):
        """/health/ready answers 503 with a JSON body naming the dead dependency.
        That is the 2026-09-11 signal — the plain /health would have said 'ok'."""
        from utils.config import settings

        monkeypatch.setattr(settings, "watchdog_targets", "peer=http://peer/health/ready")
        fake_http({"http://peer/health/ready": 503})

        with pytest.raises(RuntimeError) as exc:
            await watchdog.run_watchdog()
        assert "HTTP 503" in str(exc.value)

    async def test_one_dead_among_healthy_still_raises(self, monkeypatch, fake_http):
        from utils.config import settings

        monkeypatch.setattr(
            settings, "watchdog_targets", "a=http://a/ready,b=http://b/ready"
        )
        fake_http({"http://a/ready": 200, "http://b/ready": 500})

        with pytest.raises(RuntimeError) as exc:
            await watchdog.run_watchdog()
        assert "b" in str(exc.value) and "a (" not in str(exc.value)

    async def test_every_target_is_probed_even_after_a_failure(self, monkeypatch, fake_http):
        """A dead first target must not hide a dead second one from the message."""
        from utils.config import settings

        monkeypatch.setattr(
            settings, "watchdog_targets", "a=http://a/ready,b=http://b/ready"
        )
        client = fake_http({"http://a/ready": 500, "http://b/ready": 500})

        with pytest.raises(RuntimeError) as exc:
            await watchdog.run_watchdog()
        assert client.calls == ["http://a/ready", "http://b/ready"]
        assert "a (" in str(exc.value) and "b (" in str(exc.value)


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandlerGate:
    async def test_handler_self_gates_on_flag(self, monkeypatch):
        """H4 discipline: the runtime flag gates the work in-handler, so a
        ConfigMap flip disarms it without touching the task row."""
        from types import SimpleNamespace

        from services.scheduled_tasks.builtins import _watchdog_handler
        from utils.config import settings

        monkeypatch.setattr(settings, "watchdog_enabled", False)
        result = await _watchdog_handler(SimpleNamespace(state=SimpleNamespace()), {})
        assert "skipped" in result
