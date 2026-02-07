"""
Zeroconf Service Discovery for Renfield Satellite

Automatically discovers the Renfield backend server on the local network.
No manual URL configuration needed.
"""

import asyncio
import socket
from dataclasses import dataclass
from typing import Callable, List, Optional

try:
    from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    print("Warning: zeroconf not installed. Auto-discovery disabled.")
    print("Install with: pip install zeroconf")


# Service type to look for
SERVICE_TYPE = "_renfield._tcp.local."


@dataclass
class DiscoveredServer:
    """Information about a discovered Renfield server"""
    name: str
    host: str
    port: int
    ws_path: str
    properties: dict

    @property
    def ws_url(self) -> str:
        """Get the WebSocket URL for this server"""
        return f"ws://{self.host}:{self.port}{self.ws_path}"

    def __str__(self) -> str:
        return f"{self.name} at {self.ws_url}"


class ServiceDiscovery:
    """
    Discovers Renfield backend servers on the local network using zeroconf.

    Usage:
        discovery = ServiceDiscovery()
        server = await discovery.find_server(timeout=10.0)
        if server:
            print(f"Found server: {server.ws_url}")
    """

    def __init__(self):
        self._zeroconf: Optional["AsyncZeroconf"] = None
        self._servers: List[DiscoveredServer] = []
        self._on_found: Optional[Callable[[DiscoveredServer], None]] = None
        self._on_removed: Optional[Callable[[str], None]] = None

    @property
    def available(self) -> bool:
        """Check if zeroconf is available"""
        return ZEROCONF_AVAILABLE

    @property
    def servers(self) -> List[DiscoveredServer]:
        """Get list of discovered servers"""
        return self._servers.copy()

    def on_server_found(self, callback: Callable[[DiscoveredServer], None]):
        """Register callback for when a server is found"""
        self._on_found = callback

    def on_server_removed(self, callback: Callable[[str], None]):
        """Register callback for when a server is removed"""
        self._on_removed = callback

    async def find_server(self, timeout: float = 10.0) -> Optional[DiscoveredServer]:
        """
        Find a Renfield server on the network.

        Args:
            timeout: Maximum time to search in seconds

        Returns:
            DiscoveredServer if found, None otherwise
        """
        if not ZEROCONF_AVAILABLE:
            print("Zeroconf not available for auto-discovery")
            return None

        print(f"Searching for Renfield server ({timeout}s timeout)...")

        # Event to signal when we find a server
        found_event = asyncio.Event()
        found_server: List[DiscoveredServer] = []

        class Listener(ServiceListener):
            def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda: asyncio.create_task(self._handle_add(zc, type_, name))
                )

            async def _handle_add(self, zc: Zeroconf, type_: str, name: str):
                info = zc.get_service_info(type_, name)
                if info:
                    server = self._parse_service_info(info)
                    if server:
                        found_server.append(server)
                        found_event.set()

            def _parse_service_info(self, info) -> Optional[DiscoveredServer]:
                try:
                    # Get address
                    if info.addresses:
                        host = socket.inet_ntoa(info.addresses[0])
                    else:
                        host = info.server.rstrip(".")

                    # Get properties
                    props = {}
                    if info.properties:
                        for key, value in info.properties.items():
                            if isinstance(key, bytes):
                                key = key.decode("utf-8")
                            if isinstance(value, bytes):
                                value = value.decode("utf-8")
                            props[key] = value

                    return DiscoveredServer(
                        name=info.name.replace(f".{SERVICE_TYPE}", ""),
                        host=host,
                        port=info.port,
                        ws_path=props.get("ws_path", "/ws/satellite"),
                        properties=props,
                    )
                except Exception as e:
                    print(f"Failed to parse service info: {e}")
                    return None

            def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                pass

            def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                pass

        try:
            # Create zeroconf and browser
            zc = Zeroconf(ip_version=IPVersion.V4Only)
            listener = Listener()
            browser = ServiceBrowser(zc, SERVICE_TYPE, listener)

            # Wait for a server or timeout
            try:
                await asyncio.wait_for(found_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

            # Clean up
            browser.cancel()
            zc.close()

            if found_server:
                server = found_server[0]
                print(f"Found Renfield server: {server}")
                print(f"⚠️  Accepting first discovered server: {server.host}:{server.port} — verify this is expected")
                self._servers.append(server)
                return server
            else:
                print("No Renfield server found on the network")
                return None

        except Exception as e:
            print(f"Discovery error: {e}")
            return None

    async def start_continuous_discovery(self):
        """
        Start continuous discovery in the background.

        Servers will be added/removed from self.servers automatically.
        Use on_server_found/on_server_removed callbacks for notifications.
        """
        if not ZEROCONF_AVAILABLE:
            return

        self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)

        class Listener(ServiceListener):
            def __init__(self, parent: "ServiceDiscovery"):
                self.parent = parent

            def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                info = zc.get_service_info(type_, name)
                if info:
                    server = self._parse_info(info)
                    if server:
                        self.parent._servers.append(server)
                        if self.parent._on_found:
                            self.parent._on_found(server)
                        print(f"Discovered server: {server}")

            def _parse_info(self, info) -> Optional[DiscoveredServer]:
                try:
                    if info.addresses:
                        host = socket.inet_ntoa(info.addresses[0])
                    else:
                        host = info.server.rstrip(".")

                    props = {}
                    if info.properties:
                        for key, value in info.properties.items():
                            if isinstance(key, bytes):
                                key = key.decode("utf-8")
                            if isinstance(value, bytes):
                                value = value.decode("utf-8")
                            props[key] = value

                    return DiscoveredServer(
                        name=info.name.replace(f".{SERVICE_TYPE}", ""),
                        host=host,
                        port=info.port,
                        ws_path=props.get("ws_path", "/ws/satellite"),
                        properties=props,
                    )
                except Exception:
                    return None

            def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                clean_name = name.replace(f".{SERVICE_TYPE}", "")
                self.parent._servers = [
                    s for s in self.parent._servers if s.name != clean_name
                ]
                if self.parent._on_removed:
                    self.parent._on_removed(clean_name)
                print(f"Server removed: {clean_name}")

            def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                pass

        self._browser = AsyncServiceBrowser(
            self._zeroconf.zeroconf,
            SERVICE_TYPE,
            Listener(self),
        )

        print("Started continuous server discovery")

    async def stop_continuous_discovery(self):
        """Stop continuous discovery"""
        if hasattr(self, "_browser"):
            self._browser.cancel()
        if self._zeroconf:
            await self._zeroconf.async_close()
            self._zeroconf = None
        print("Stopped continuous server discovery")


async def discover_server(timeout: float = 10.0) -> Optional[str]:
    """
    Convenience function to discover a Renfield server.

    Args:
        timeout: Maximum time to search

    Returns:
        WebSocket URL if found, None otherwise
    """
    discovery = ServiceDiscovery()
    server = await discovery.find_server(timeout=timeout)
    return server.ws_url if server else None
