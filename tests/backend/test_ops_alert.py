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


@pytest.mark.unit
class TestRetryBackoff:
    def test_deferred_key_is_closed_until_the_backoff_passed(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(ops_alert, "_now", lambda: clock[0])
        monkeypatch.setattr(ops_alert.settings, "mcp_health_realert_seconds", 21600.0)

        assert ops_alert.should_alert("k") is True
        ops_alert.defer_alert("k", 600.0)
        clock[0] += 120.0  # next monitor tick
        assert ops_alert.should_alert("k") is False
        clock[0] += 481.0  # backoff passed
        assert ops_alert.should_alert("k") is True
        # ...and a successful attempt is then held by the normal TTL again.
        clock[0] += 700.0
        assert ops_alert.should_alert("k") is False

    def test_deferred_keys_are_visible_to_the_recovery_sweep_and_clearable(self):
        ops_alert.should_alert("planea:x:down")
        ops_alert.defer_alert("planea:x:down", 600.0)
        assert ops_alert.alerted_keys("planea:") == ["planea:x:down"]
        ops_alert.clear_alert("planea:x:down")
        assert ops_alert.alerted_keys("planea:") == []
        assert ops_alert.should_alert("planea:x:down") is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestPersistedCountsAsTold:
    async def test_failure_after_persist_is_told(self, monkeypatch):
        """process_webhook commits the row, then delivery raises. The admin already
        sees the notification — reporting 'not delivered' made the monitor store a
        new row every tick."""
        monkeypatch.setattr(ops_alert.settings, "proactive_enabled", True)
        monkeypatch.setattr(ops_alert, "resolve_admin_user_id", _fake_admin)
        _install_notification_service(monkeypatch, outcome=RuntimeError("push failed"))

        async def _persisted(**_kw):
            return True

        monkeypatch.setattr(ops_alert, "_persisted_since", _persisted)
        assert await ops_alert.notify_admin(title="t", message="m", dedup_key="k") is True

    async def test_failure_before_persist_is_not_told(self, monkeypatch):
        monkeypatch.setattr(ops_alert.settings, "proactive_enabled", True)
        monkeypatch.setattr(ops_alert, "resolve_admin_user_id", _fake_admin)
        _install_notification_service(monkeypatch, outcome=RuntimeError("db down"))

        seen = {}

        async def _persisted(**kw):
            seen.update(kw)
            return False

        monkeypatch.setattr(ops_alert, "_persisted_since", _persisted)
        assert await ops_alert.notify_admin(
            title="t", message="m", dedup_key="k", source="mcp_health_monitor"
        ) is False
        # The persist check looks for exactly this notification, for this admin.
        assert (seen["title"], seen["message"], seen["source"], seen["target_user_id"]) == (
            "t", "m", "mcp_health_monitor", 1,
        )


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
