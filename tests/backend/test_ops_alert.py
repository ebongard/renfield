"""The shared operator-alert path (services/ops_alert.py).

Extracted from mcp_health_monitor so three subsystems stop growing their own
version. The contract worth pinning is the one the failure-streak alerting
depends on: **notify_admin reports whether it actually handed off**, because a
caller that keeps a durable "already told them" marker must not stamp it for an
alert that never left the building.
"""
import pytest

from services import ops_alert


@pytest.fixture(autouse=True)
def _reset():
    ops_alert.reset_ledger()
    yield
    ops_alert.reset_ledger()


@pytest.mark.unit
class TestLedger:
    def test_first_alert_passes_then_is_rate_limited(self, monkeypatch):
        monkeypatch.setattr(ops_alert.settings, "mcp_health_realert_seconds", 3600.0)
        assert ops_alert.should_alert("k") is True
        assert ops_alert.should_alert("k") is False

    def test_ttl_override_per_call(self):
        assert ops_alert.should_alert("k", realert_seconds=0.0) is True
        # A zero TTL means "always re-alert" — the elapsed check is >=.
        assert ops_alert.should_alert("k", realert_seconds=0.0) is True

    def test_clear_rearms(self, monkeypatch):
        monkeypatch.setattr(ops_alert.settings, "mcp_health_realert_seconds", 3600.0)
        assert ops_alert.should_alert("k") is True
        ops_alert.clear_alert("k")
        assert ops_alert.should_alert("k") is True

    def test_alerted_keys_filters_by_prefix(self):
        ops_alert.should_alert("planea:x")
        ops_alert.should_alert("planeb:y")
        assert ops_alert.alerted_keys("planea:") == ["planea:x"]


@pytest.mark.unit
@pytest.mark.asyncio
class TestDeliveryVerdict:
    async def test_proactive_off_reports_not_delivered(self, monkeypatch):
        """Not a silent no-op any more: the caller must learn nothing was sent,
        or it records a six-hour 'already told them' for an alert nobody saw."""
        monkeypatch.setattr(ops_alert.settings, "proactive_enabled", False)
        assert await ops_alert.notify_admin(title="t", message="m", dedup_key="k") is False

    async def test_success_reports_delivered(self, monkeypatch):
        monkeypatch.setattr(ops_alert.settings, "proactive_enabled", True)
        monkeypatch.setattr(ops_alert, "resolve_admin_user_id", _fake_admin)
        _install_notification_service(monkeypatch, outcome=None)

        assert await ops_alert.notify_admin(title="t", message="m", dedup_key="k") is True

    async def test_pipeline_dedup_counts_as_delivered(self, monkeypatch):
        """A ValueError means an equivalent notification already exists — the
        admin HAS been told. Retrying that forever would be the wrong reading."""
        monkeypatch.setattr(ops_alert.settings, "proactive_enabled", True)
        monkeypatch.setattr(ops_alert, "resolve_admin_user_id", _fake_admin)
        _install_notification_service(monkeypatch, outcome=ValueError("duplicate"))

        assert await ops_alert.notify_admin(title="t", message="m", dedup_key="k") is True

    async def test_pipeline_failure_reports_not_delivered(self, monkeypatch):
        monkeypatch.setattr(ops_alert.settings, "proactive_enabled", True)
        monkeypatch.setattr(ops_alert, "resolve_admin_user_id", _fake_admin)
        _install_notification_service(monkeypatch, outcome=RuntimeError("pipeline down"))

        assert await ops_alert.notify_admin(title="t", message="m", dedup_key="k") is False

    async def test_never_raises_into_the_caller(self, monkeypatch):
        """A failing alert must not break the tick that produced it."""
        monkeypatch.setattr(ops_alert.settings, "proactive_enabled", True)
        monkeypatch.setattr(ops_alert, "resolve_admin_user_id", _fake_admin)
        _install_notification_service(monkeypatch, outcome=RuntimeError("boom"))

        await ops_alert.notify_admin(title="t", message="m", dedup_key="k")  # no raise


async def _fake_admin(db):
    return 1


def _install_notification_service(monkeypatch, *, outcome):
    """Stub services.notification_service + services.database so notify_admin's
    lazy imports resolve without a real DB."""
    import sys
    import types

    class _Svc:
        def __init__(self, db):
            pass

        async def process_webhook(self, **kw):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    ns_mod = types.ModuleType("services.notification_service")
    ns_mod.NotificationService = _Svc
    monkeypatch.setitem(sys.modules, "services.notification_service", ns_mod)

    db_mod = sys.modules.get("services.database") or types.ModuleType("services.database")
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: _Session(), raising=False)
    monkeypatch.setitem(sys.modules, "services.database", db_mod)
