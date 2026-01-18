"""Network communication module"""
from .websocket_client import WebSocketClient
from .discovery import ServiceDiscovery, DiscoveredServer, discover_server

__all__ = ["WebSocketClient", "ServiceDiscovery", "DiscoveredServer", "discover_server"]
