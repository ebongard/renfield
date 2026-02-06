"""Tests for ZeroconfService.

Tests zeroconf service advertisement lifecycle, configuration,
service info creation, and error handling.
"""
import sys
from unittest.mock import MagicMock

# Pre-mock modules not available in test environment
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "zeroconf", "zeroconf.asyncio",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from unittest.mock import AsyncMock, patch

import pytest

from services.zeroconf_service import (
    SERVICE_NAME,
    SERVICE_TYPE,
    ZeroconfService,
    get_advertise_address,
    get_zeroconf_service,
)

# ============================================================================
# Initialization Tests
# ============================================================================

@pytest.mark.unit
class TestZeroconfServiceInit:

    def test_init_defaults(self):
        """Service initializes with correct defaults."""
        svc = ZeroconfService()
        assert svc.port == 8000
        assert svc.name == "Renfield Voice Assistant"
        assert svc._zeroconf is None
        assert svc._service_info is None
        assert svc._registered is False

    def test_init_custom_port(self):
        """Service accepts custom port."""
        svc = ZeroconfService(port=9000)
        assert svc.port == 9000

    def test_init_custom_name(self):
        """Service accepts custom name."""
        svc = ZeroconfService(name="My Assistant")
        assert svc.name == "My Assistant"

    def test_is_registered_initially_false(self):
        """is_registered is False before start."""
        svc = ZeroconfService()
        assert svc.is_registered is False


# ============================================================================
# Constants Tests
# ============================================================================

@pytest.mark.unit
class TestConstants:

    def test_service_type_format(self):
        """SERVICE_TYPE follows mDNS convention."""
        assert SERVICE_TYPE == "_renfield._tcp.local."
        assert SERVICE_TYPE.endswith(".")

    def test_service_name_includes_type(self):
        """SERVICE_NAME includes the service type."""
        assert SERVICE_TYPE in SERVICE_NAME


# ============================================================================
# Start Lifecycle Tests
# ============================================================================

@pytest.mark.unit
class TestStartLifecycle:

    @pytest.mark.asyncio
    async def test_start_registers_service(self):
        """start() creates zeroconf instance and registers service."""
        mock_async_zc = MagicMock()
        mock_async_zc.async_register_service = AsyncMock()

        with patch("services.zeroconf_service.ZEROCONF_AVAILABLE", True), \
             patch("services.zeroconf_service.AsyncZeroconf", return_value=mock_async_zc), \
             patch("services.zeroconf_service.ServiceInfo"), \
             patch("services.zeroconf_service.get_advertise_address", return_value=("192.168.1.100", None)):
            svc = ZeroconfService(port=8000)
            await svc.start()

        assert svc._registered is True
        assert svc._zeroconf is mock_async_zc
        mock_async_zc.async_register_service.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_skips_when_not_available(self):
        """start() does nothing when zeroconf is not installed."""
        with patch("services.zeroconf_service.ZEROCONF_AVAILABLE", False):
            svc = ZeroconfService()
            await svc.start()

        assert svc._registered is False
        assert svc._zeroconf is None

    @pytest.mark.asyncio
    async def test_start_skips_when_already_registered(self):
        """start() is idempotent — does not re-register."""
        mock_async_zc = MagicMock()
        mock_async_zc.async_register_service = AsyncMock()

        with patch("services.zeroconf_service.ZEROCONF_AVAILABLE", True), \
             patch("services.zeroconf_service.AsyncZeroconf", return_value=mock_async_zc), \
             patch("services.zeroconf_service.ServiceInfo"), \
             patch("services.zeroconf_service.get_advertise_address", return_value=("192.168.1.100", None)):
            svc = ZeroconfService()
            await svc.start()
            mock_async_zc.async_register_service.reset_mock()

            # Second start should be a no-op
            await svc.start()

        mock_async_zc.async_register_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_handles_exception(self):
        """start() handles errors gracefully without raising."""
        with patch("services.zeroconf_service.ZEROCONF_AVAILABLE", True), \
             patch("services.zeroconf_service.get_advertise_address", side_effect=RuntimeError("network error")):
            svc = ZeroconfService()
            await svc.start()  # Should not raise

        assert svc._registered is False

    @pytest.mark.asyncio
    async def test_start_with_hostname(self):
        """start() uses hostname when get_advertise_address returns hostname."""
        mock_async_zc = MagicMock()
        mock_async_zc.async_register_service = AsyncMock()
        mock_service_info = MagicMock()

        with patch("services.zeroconf_service.ZEROCONF_AVAILABLE", True), \
             patch("services.zeroconf_service.AsyncZeroconf", return_value=mock_async_zc), \
             patch("services.zeroconf_service.ServiceInfo", return_value=mock_service_info) as mock_si_cls, \
             patch("services.zeroconf_service.get_advertise_address", return_value=(None, "renfield.local")):
            svc = ZeroconfService(port=8000)
            await svc.start()

        assert svc._registered is True
        # Verify ServiceInfo was called with hostname-based server
        call_kwargs = mock_si_cls.call_args[1]
        assert call_kwargs["server"] == "renfield.local."


# ============================================================================
# Stop Lifecycle Tests
# ============================================================================

@pytest.mark.unit
class TestStopLifecycle:

    @pytest.mark.asyncio
    async def test_stop_unregisters_service(self):
        """stop() unregisters service and closes zeroconf."""
        mock_async_zc = MagicMock()
        mock_async_zc.async_register_service = AsyncMock()
        mock_async_zc.async_unregister_service = AsyncMock()
        mock_async_zc.async_close = AsyncMock()

        with patch("services.zeroconf_service.ZEROCONF_AVAILABLE", True), \
             patch("services.zeroconf_service.AsyncZeroconf", return_value=mock_async_zc), \
             patch("services.zeroconf_service.ServiceInfo"), \
             patch("services.zeroconf_service.get_advertise_address", return_value=("192.168.1.100", None)):
            svc = ZeroconfService()
            await svc.start()
            await svc.stop()

        assert svc._registered is False
        mock_async_zc.async_unregister_service.assert_awaited_once()
        mock_async_zc.async_close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_noop_when_not_registered(self):
        """stop() does nothing when service is not registered."""
        svc = ZeroconfService()
        await svc.stop()  # Should not raise
        assert svc._registered is False

    @pytest.mark.asyncio
    async def test_stop_handles_exception(self):
        """stop() handles errors gracefully without raising."""
        mock_async_zc = MagicMock()
        mock_async_zc.async_register_service = AsyncMock()
        mock_async_zc.async_unregister_service = AsyncMock(side_effect=RuntimeError("zc error"))
        mock_async_zc.async_close = AsyncMock()

        with patch("services.zeroconf_service.ZEROCONF_AVAILABLE", True), \
             patch("services.zeroconf_service.AsyncZeroconf", return_value=mock_async_zc), \
             patch("services.zeroconf_service.ServiceInfo"), \
             patch("services.zeroconf_service.get_advertise_address", return_value=("192.168.1.100", None)):
            svc = ZeroconfService()
            await svc.start()
            await svc.stop()  # Should not raise


# ============================================================================
# get_advertise_address Tests
# ============================================================================

@pytest.mark.unit
class TestGetAdvertiseAddress:

    def test_explicit_ip_override(self):
        """Uses ADVERTISE_IP env var when set."""
        with patch.dict("os.environ", {"ADVERTISE_IP": "10.0.0.5"}, clear=False):
            ip, host = get_advertise_address()
        assert ip == "10.0.0.5"
        assert host is None

    def test_host_ip_fallback(self):
        """Uses HOST_IP env var when ADVERTISE_IP is not set."""
        with patch.dict("os.environ", {"HOST_IP": "10.0.0.6"}, clear=False), \
             patch.dict("os.environ", {}, clear=False) as env:
            env.pop("ADVERTISE_IP", None)
            ip, host = get_advertise_address()
        assert ip == "10.0.0.6"
        assert host is None

    def test_advertise_host_adds_local_suffix(self):
        """ADVERTISE_HOST gets .local suffix if missing."""
        with patch.dict("os.environ", {"ADVERTISE_HOST": "renfield"}, clear=False) as env:
            env.pop("ADVERTISE_IP", None)
            env.pop("HOST_IP", None)
            ip, host = get_advertise_address()
        assert ip is None
        assert host == "renfield.local"

    def test_advertise_host_keeps_local_suffix(self):
        """ADVERTISE_HOST keeps .local suffix if already present."""
        with patch.dict("os.environ", {"ADVERTISE_HOST": "renfield.local"}, clear=False) as env:
            env.pop("ADVERTISE_IP", None)
            env.pop("HOST_IP", None)
            ip, host = get_advertise_address()
        assert ip is None
        assert host == "renfield.local"


# ============================================================================
# Singleton Tests
# ============================================================================

@pytest.mark.unit
class TestSingleton:

    def test_get_zeroconf_service_returns_instance(self):
        """get_zeroconf_service returns a ZeroconfService."""
        with patch("services.zeroconf_service._zeroconf_service", None):
            svc = get_zeroconf_service()
        assert isinstance(svc, ZeroconfService)

    def test_get_zeroconf_service_uses_port(self):
        """get_zeroconf_service passes port to constructor."""
        with patch("services.zeroconf_service._zeroconf_service", None):
            svc = get_zeroconf_service(port=9090)
        assert svc.port == 9090

    def test_get_zeroconf_service_returns_same_instance(self):
        """get_zeroconf_service returns singleton."""
        with patch("services.zeroconf_service._zeroconf_service", None):
            svc1 = get_zeroconf_service()
            svc2 = get_zeroconf_service()
        assert svc1 is svc2
