"""
Zeroconf Service Advertisement for Renfield Backend

Advertises the Renfield server on the local network so satellites
can automatically discover and connect to it.

Service Type: _renfield._tcp.local
"""

import socket

from loguru import logger

try:
    from zeroconf import IPVersion, ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    logger.warning("zeroconf not installed. Auto-discovery disabled.")
    logger.warning("Install with: pip install zeroconf")


# Service type for Renfield
SERVICE_TYPE = "_renfield._tcp.local."
SERVICE_NAME = "Renfield Voice Assistant._renfield._tcp.local."


def get_advertise_address() -> tuple[str, str | None]:
    """
    Get the address to advertise for this service.

    Returns:
        Tuple of (ip_address or None, hostname or None)
        At least one will be set.
    """
    import os

    # Option 1: Explicit IP override
    env_ip = os.environ.get("ADVERTISE_IP") or os.environ.get("HOST_IP")
    if env_ip:
        return env_ip, None

    # Option 2: Explicit hostname override (preferred for Docker)
    env_host = os.environ.get("ADVERTISE_HOST")
    if env_host:
        # Ensure it ends with .local for mDNS
        if not env_host.endswith(".local"):
            env_host = f"{env_host}.local"
        return None, env_host

    # Option 3: Auto-detect (works when not in Docker)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        # Check if this looks like a Docker internal IP
        if ip.startswith("172.") or ip.startswith("10."):
            # Likely Docker - fall back to hostname
            hostname = socket.gethostname()
            return None, f"{hostname}.local"
        return ip, None
    except Exception:
        return "127.0.0.1", None


class ZeroconfService:
    """
    Manages zeroconf service advertisement for the Renfield backend.

    Allows satellites to automatically discover the server without
    manual configuration.
    """

    def __init__(
        self,
        port: int = 8000,
        name: str = "Renfield Voice Assistant",
    ):
        """
        Initialize zeroconf service.

        Args:
            port: Port the backend is running on
            name: Human-readable service name
        """
        self.port = port
        self.name = name
        self._zeroconf: AsyncZeroconf | None = None
        self._service_info: ServiceInfo | None = None
        self._registered = False

    async def start(self):
        """Start advertising the service"""
        if not ZEROCONF_AVAILABLE:
            logger.warning("Zeroconf not available - skipping service advertisement")
            return

        if self._registered:
            return

        try:
            # Get address to advertise
            advertise_ip, advertise_host = get_advertise_address()

            # Determine server hostname and addresses
            if advertise_host:
                # Use hostname (mDNS will resolve it)
                server_name = advertise_host if advertise_host.endswith(".") else f"{advertise_host}."
                addresses = []  # Let mDNS resolve the hostname
                logger.info(f"Advertising hostname: {advertise_host}")
            else:
                # Use IP address directly
                server_name = f"{socket.gethostname()}.local."
                addresses = [socket.inet_aton(advertise_ip)]
                logger.info(f"Advertising IP address: {advertise_ip}")

            # Create service info
            self._service_info = ServiceInfo(
                type_=SERVICE_TYPE,
                name=f"{self.name}.{SERVICE_TYPE}",
                port=self.port,
                properties={
                    "version": "1.0.0",
                    "ws_path": "/ws/satellite",
                    "api_version": "1",
                },
                server=server_name,
                addresses=addresses if addresses else None,
            )

            # Create and start zeroconf
            self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
            await self._zeroconf.async_register_service(self._service_info)

            self._registered = True
            logger.info(f"✅ Zeroconf service registered: {SERVICE_TYPE}")
            logger.info(f"   Name: {self.name}")
            if advertise_host:
                logger.info(f"   Host: {advertise_host}:{self.port}")
            else:
                logger.info(f"   Address: {advertise_ip}:{self.port}")
            logger.info("   Satellites can now auto-discover this server")

        except Exception as e:
            logger.error(f"❌ Failed to register zeroconf service: {e}")

    async def stop(self):
        """Stop advertising the service"""
        if not self._registered or not self._zeroconf:
            return

        try:
            if self._service_info:
                await self._zeroconf.async_unregister_service(self._service_info)
            await self._zeroconf.async_close()
            self._registered = False
            logger.info("👋 Zeroconf service unregistered")

        except Exception as e:
            logger.error(f"❌ Failed to unregister zeroconf service: {e}")

    @property
    def is_registered(self) -> bool:
        """Check if service is registered"""
        return self._registered


# Global singleton
_zeroconf_service: ZeroconfService | None = None


def get_zeroconf_service(port: int = 8000) -> ZeroconfService:
    """Get or create the global ZeroconfService instance"""
    global _zeroconf_service
    if _zeroconf_service is None:
        _zeroconf_service = ZeroconfService(port=port)
    return _zeroconf_service
