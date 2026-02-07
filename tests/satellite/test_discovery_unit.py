"""
Service Discovery Unit Tests

Tests for DiscoveredServer dataclass and ServiceDiscovery helper behavior,
including the ws_url property and availability checks when zeroconf is absent.
"""

import pytest
from unittest.mock import patch

from renfield_satellite.network.discovery import DiscoveredServer, ServiceDiscovery


class TestDiscoveredServer:
    """Tests for DiscoveredServer dataclass"""

    @pytest.mark.satellite
    def test_ws_url_property(self):
        """Test: ws_url returns correct WebSocket URL from host/port/ws_path"""
        server = DiscoveredServer(
            name="renfield-backend",
            host="192.168.1.100",
            port=8000,
            ws_path="/ws/satellite",
            properties={},
        )

        assert server.ws_url == "ws://192.168.1.100:8000/ws/satellite"

    @pytest.mark.satellite
    def test_ws_url_with_custom_path(self):
        """Test: ws_url works with a non-default ws_path"""
        server = DiscoveredServer(
            name="renfield-backend",
            host="10.0.0.5",
            port=9000,
            ws_path="/ws/device",
            properties={},
        )

        assert server.ws_url == "ws://10.0.0.5:9000/ws/device"

    @pytest.mark.satellite
    def test_str_includes_url(self):
        """Test: __str__() includes the WebSocket URL"""
        server = DiscoveredServer(
            name="renfield-backend",
            host="192.168.1.100",
            port=8000,
            ws_path="/ws/satellite",
            properties={},
        )

        text = str(server)
        assert "ws://192.168.1.100:8000/ws/satellite" in text
        assert "renfield-backend" in text

    @pytest.mark.satellite
    def test_str_matches_format(self):
        """Test: __str__() follows 'name at ws_url' format"""
        server = DiscoveredServer(
            name="my-server",
            host="192.168.1.50",
            port=8080,
            ws_path="/ws/satellite",
            properties={"version": "1.0"},
        )

        assert str(server) == "my-server at ws://192.168.1.50:8080/ws/satellite"


class TestServiceDiscoveryAvailability:
    """Tests for ServiceDiscovery.available property"""

    @pytest.mark.satellite
    def test_available_false_when_zeroconf_not_installed(self):
        """Test: available returns False when ZEROCONF_AVAILABLE is False"""
        with patch(
            "renfield_satellite.network.discovery.ZEROCONF_AVAILABLE", False
        ):
            discovery = ServiceDiscovery()
            assert discovery.available is False

    @pytest.mark.satellite
    def test_available_true_when_zeroconf_installed(self):
        """Test: available returns True when ZEROCONF_AVAILABLE is True"""
        with patch(
            "renfield_satellite.network.discovery.ZEROCONF_AVAILABLE", True
        ):
            discovery = ServiceDiscovery()
            assert discovery.available is True

    @pytest.mark.satellite
    def test_servers_list_initially_empty(self):
        """Test: servers list is empty on initialization"""
        discovery = ServiceDiscovery()
        assert discovery.servers == []
