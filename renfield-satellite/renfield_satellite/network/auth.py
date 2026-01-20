"""
Authentication helper for Renfield Satellite

Handles WebSocket token retrieval from the Renfield backend server.
Only needed when server has WS_AUTH_ENABLED=true.
"""

import asyncio
from typing import Optional, Tuple

# Try to import aiohttp, fall back to urllib for basic functionality
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    AIOHTTP_AVAILABLE = False

try:
    import urllib.request
    import urllib.parse
    import json
    URLLIB_AVAILABLE = True
except ImportError:
    URLLIB_AVAILABLE = False


async def fetch_ws_token(
    server_base_url: str,
    satellite_id: str,
    device_type: str = "satellite"
) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetch WebSocket authentication token from server.

    Args:
        server_base_url: HTTP base URL (e.g., http://192.168.1.100:8000)
        satellite_id: Satellite ID for identification
        device_type: Device type string (default: "satellite")

    Returns:
        Tuple of (token, protocol_version) or (None, None) if auth disabled/failed
    """
    if AIOHTTP_AVAILABLE:
        return await _fetch_with_aiohttp(server_base_url, satellite_id, device_type)
    elif URLLIB_AVAILABLE:
        # Run sync urllib in thread pool
        return await asyncio.get_event_loop().run_in_executor(
            None, _fetch_with_urllib, server_base_url, satellite_id, device_type
        )
    else:
        print("⚠️ Neither aiohttp nor urllib available for token fetch")
        return None, None


async def _fetch_with_aiohttp(
    server_base_url: str,
    satellite_id: str,
    device_type: str
) -> Tuple[Optional[str], Optional[str]]:
    """Fetch token using aiohttp (preferred async method)."""
    try:
        # Ensure URL doesn't end with slash
        base_url = server_base_url.rstrip("/")
        url = f"{base_url}/api/ws/token"

        params = {
            "device_id": satellite_id,
            "device_type": device_type
        }

        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    token = data.get("token")
                    protocol_version = data.get("protocol_version", "1.0")

                    if token:
                        expires_in = data.get("expires_in", "?")
                        print(f"✅ Obtained WS token (expires in {expires_in}s)")
                        return token, protocol_version
                    else:
                        print("ℹ️ WS auth disabled on server")
                        return None, protocol_version
                else:
                    print(f"⚠️ Failed to get WS token: HTTP {response.status}")
                    return None, None

    except asyncio.TimeoutError:
        print("⚠️ Token fetch timed out")
        return None, None
    except aiohttp.ClientError as e:
        print(f"⚠️ Token fetch connection error: {e}")
        return None, None
    except Exception as e:
        print(f"⚠️ Token fetch error: {e}")
        return None, None


def _fetch_with_urllib(
    server_base_url: str,
    satellite_id: str,
    device_type: str
) -> Tuple[Optional[str], Optional[str]]:
    """Fetch token using urllib (sync fallback)."""
    try:
        base_url = server_base_url.rstrip("/")
        params = urllib.parse.urlencode({
            "device_id": satellite_id,
            "device_type": device_type
        })
        url = f"{base_url}/api/ws/token?{params}"

        req = urllib.request.Request(url, method="POST")
        req.add_header("Content-Type", "application/json")

        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                token = data.get("token")
                protocol_version = data.get("protocol_version", "1.0")

                if token:
                    expires_in = data.get("expires_in", "?")
                    print(f"✅ Obtained WS token (expires in {expires_in}s)")
                    return token, protocol_version
                else:
                    print("ℹ️ WS auth disabled on server")
                    return None, protocol_version
            else:
                print(f"⚠️ Failed to get WS token: HTTP {response.status}")
                return None, None

    except Exception as e:
        print(f"⚠️ Token fetch error: {e}")
        return None, None


def ws_url_from_http(http_url: str, endpoint: str = "/ws/satellite") -> str:
    """
    Convert HTTP URL to WebSocket URL.

    Args:
        http_url: HTTP URL (e.g., http://192.168.1.100:8000)
        endpoint: WebSocket endpoint path

    Returns:
        WebSocket URL (e.g., ws://192.168.1.100:8000/ws/satellite)
    """
    ws_url = http_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = ws_url.rstrip("/")

    if not ws_url.endswith(endpoint):
        ws_url = f"{ws_url}{endpoint}"

    return ws_url


def http_url_from_ws(ws_url: str) -> str:
    """
    Convert WebSocket URL to HTTP URL (for token fetch).

    Args:
        ws_url: WebSocket URL (e.g., ws://192.168.1.100:8000/ws/satellite)

    Returns:
        HTTP base URL (e.g., http://192.168.1.100:8000)
    """
    http_url = ws_url.replace("wss://", "https://").replace("ws://", "http://")

    # Remove WebSocket endpoint paths
    for endpoint in ["/ws/satellite", "/ws/device", "/ws"]:
        if endpoint in http_url:
            http_url = http_url.split(endpoint)[0]
            break

    return http_url.rstrip("/")
