"""The Simba portal's upload limits, read from the MCP's check_connection (#1609)."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from services import simba_portal_limits as limits_mod
from services.simba_portal_limits import PortalLimits, parse_limits, portal_limits, too_large

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_cache():
    limits_mod.reset_for_tests()
    yield
    limits_mod.reset_for_tests()


def _envelope(**inner):
    return {"success": True, "message": json.dumps({"status": "ok", **inner})}


def _mgr(result=None, side_effect=None):
    m = AsyncMock()
    m.execute_tool = AsyncMock(return_value=result, side_effect=side_effect)
    return m


class TestParse:
    def test_the_portal_s_stated_limit(self):
        assert parse_limits(_envelope(maxDateigroesseMB=15)) == PortalLimits(max_file_mb=15.0)

    @pytest.mark.parametrize("result", [
        None,
        {"success": False, "message": "MCP server 'simba' not connected"},
        {"success": True, "message": "Anmeldung fehlgeschlagen"},          # an error text
        {"success": True, "message": json.dumps({"status": "ok"})},        # an older MCP
        _envelope(maxDateigroesseMB=0),
        _envelope(maxDateigroesseMB=-1),
        _envelope(maxDateigroesseMB=True),
        _envelope(maxDateigroesseMB="15"),
        {"success": True, "message": json.dumps({"status": "error", "maxDateigroesseMB": 15})},
    ])
    def test_no_usable_limit_is_unknown(self, result):
        assert parse_limits(result) is None


class TestTooLarge:
    def test_exactly_the_limit_fits(self):
        assert too_large(15 * 1024 * 1024, PortalLimits(15)) is None

    def test_one_byte_over_is_too_large(self):
        verdict = too_large(15 * 1024 * 1024 + 1, PortalLimits(15))
        assert verdict is not None and verdict.code == "too_large" and verdict.max_mb == 15

    def test_the_measured_case(self):
        """2026-11-22: a 16.2 MB PDF against the portal's 15 MB."""
        verdict = too_large(int(16.2 * 1024 * 1024), PortalLimits(15))
        assert verdict.size_mb == 16.2
        assert "16,2 MB" in verdict.message() and "15 MB" in verdict.message()

    def test_unknown_limits_never_block(self):
        assert too_large(10**12, None) is None


class TestCache:
    async def test_one_portal_login_serves_many_callers(self):
        mgr = _mgr(_envelope(maxDateigroesseMB=15))
        results = await asyncio.gather(*(portal_limits(mgr) for _ in range(5)))
        assert all(r == PortalLimits(15) for r in results)
        assert mgr.execute_tool.await_count == 1
        assert mgr.execute_tool.await_args.args[0] == "mcp.simba.check_connection"
        await portal_limits(mgr)
        assert mgr.execute_tool.await_count == 1

    async def test_a_known_limit_expires_after_its_ttl(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(limits_mod.time, "monotonic", lambda: clock[0])
        mgr = _mgr(_envelope(maxDateigroesseMB=15))
        await portal_limits(mgr)
        clock[0] += limits_mod._KNOWN_TTL_SECONDS - 1
        await portal_limits(mgr)
        assert mgr.execute_tool.await_count == 1
        clock[0] += 2
        await portal_limits(mgr)
        assert mgr.execute_tool.await_count == 2

    async def test_unknown_is_retried_sooner_than_known(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(limits_mod.time, "monotonic", lambda: clock[0])
        mgr = _mgr({"success": False, "message": "down"})
        assert await portal_limits(mgr) is None
        clock[0] += limits_mod._UNKNOWN_TTL_SECONDS + 1
        await portal_limits(mgr)
        assert mgr.execute_tool.await_count == 2
        assert limits_mod._UNKNOWN_TTL_SECONDS < limits_mod._KNOWN_TTL_SECONDS

    async def test_a_raising_mcp_fails_open(self):
        assert await portal_limits(_mgr(side_effect=RuntimeError("portal down"))) is None

    async def test_no_manager_is_unknown_without_a_call(self):
        assert await portal_limits(None) is None

    async def test_a_bounded_wait_answers_unknown_and_the_fetch_fills_the_cache(self):
        release = asyncio.Event()

        async def _slow(*_a, **_k):
            await release.wait()
            return _envelope(maxDateigroesseMB=15)

        mgr = _mgr(side_effect=_slow)
        assert await portal_limits(mgr, wait=0.01) is None
        release.set()
        await limits_mod._inflight
        assert await portal_limits(mgr) == PortalLimits(15)
        assert mgr.execute_tool.await_count == 1
