"""
Federation endpoint guard -- which URL may be stored as, and used as, a peer's
address.

A peer's endpoint comes FROM THE FAR SIDE (the pairing offer / response). It is
signed, but by the very party the adversary-peer threat model is about, and
this instance later sends requests to it on its own initiative: the query, the
reachability ping every few minutes, the TLS probe at pairing. Unchecked, a
pairing partner picks the target of those requests -- the classic server-side
request forgery seam.

Two checks, used at two moments:

  `validate_endpoint_url`   FORM. Scheme, no credentials, no query, no
                            fragment, sane length, a host that is a DNS name or
                            an IP literal, and no literal that is obviously not
                            another instance (loopback, link-local incl. the
                            cloud metadata addresses, unspecified, multicast).
  `endpoint_refusal`        FORM, then the TARGET: the host is resolved and
                            refused if ANY address it resolves to is one of
                            the above. A harmless-looking name pointing at
                            169.254.169.254 is the standard way around a
                            literal-only check.

Both run at pairing (`pairing_service`) AND before every outbound request
(`federation_query_asker`) -- the second because a stored row may be older
than this guard, may have been written by hand, and because a name can be
re-pointed after pairing.

What is deliberately ALLOWED: private LAN ranges (10/8, 172.16/12, 192.168/16,
fc00::/7), `.local` names and single-label names. Household instances pair
across a LAN; refusing those would refuse the product.

Plain `http://`: refused unless `FEDERATION_ALLOW_HTTP_ENDPOINTS` is on. The
envelope signature protects integrity, not the confidentiality of the question
and the answer; and without TLS there is no certificate pin either.

What remains (also in docs/FEDERATION_PAIRING.md, "Known limits"):
  - Resolve-then-connect is not atomic. A name can answer the check with one
    address and the connection with another (DNS rebinding). What that buys is
    bounded: a POST of a fixed-shape signed envelope to a fixed path, whose
    answer is discarded unless it carries the paired key's signature -- and
    with a certificate pin the connection is refused before anything is sent.
  - A host the resolver DEFINITELY does not know (`EAI_NONAME`) passes the
    target check: nothing can be sent to it, and the transport reports that in
    its own words. Every other resolution failure REFUSES -- see
    `endpoint_refusal`. During a DNS outage a pairing therefore reads "no
    usable endpoint" rather than "unreachable".
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlsplit

from utils.config import settings

#: Longest endpoint URL accepted. A real one is a scheme, a host and a port.
ENDPOINT_URL_MAX_CHARS = 512
#: Most endpoint entries one pairing message may carry. The handshake writes
#: exactly one; the bound keeps a signed list of thousands out of the resolver.
ENDPOINTS_MAX = 8
#: How long the target check waits for name resolution. On expiry the
#: endpoint is REFUSED: the far side owns its DNS, and "no answer yet" is not
#: "no such name" (see `endpoint_refusal`).
RESOLVE_TIMEOUT_SECONDS = 3.0

_DNS_NAME_MAX_CHARS = 253
_DNS_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")
#: Names every resolver maps to this machine.
_LOOPBACK_NAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
})
#: IPv6 metadata endpoint of a large cloud; unique-local, so not caught by the
#: link-local test that catches its IPv4 sibling 169.254.169.254.
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
#: `::a.b.c.d`, the deprecated IPv4-compatible form. `::` and `::1` live in
#: this block too and are judged as what they are.
_V4_COMPATIBLE = ipaddress.ip_network("::/96")
#: "This network". Linux delivers the whole block to the local host, not only
#: 0.0.0.0.
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")


class EndpointRefused(ValueError):
    """The URL is not acceptable as a peer endpoint. `str()` is the reason --
    for the log; callers answer with their own uniform error."""


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address stands for, if it is such a wrapper."""
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address in _V4_COMPATIBLE and int(address) > 1:
        return ipaddress.IPv4Address(int(address))
    return None


def hostile_address_reason(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> str | None:
    """Why `address` cannot be another instance -- or `None` if it can."""
    if isinstance(address, ipaddress.IPv6Address):
        inner = _embedded_ipv4(address)
        if inner is not None:
            address = inner
        elif address == _METADATA_V6:
            return "cloud metadata address"
        elif address.is_site_local:
            # fec0::/10, deprecated. A LAN uses fc00::/7, which stays allowed.
            return "deprecated site-local address"
    if address.is_loopback:
        return "loopback address"
    if address.is_link_local:
        return "link-local address"
    if address.is_unspecified:
        return "unspecified address"
    if isinstance(address, ipaddress.IPv4Address) and address in _THIS_NETWORK:
        return "this-network address"
    if address.is_multicast:
        return "multicast address"
    if isinstance(address, ipaddress.IPv4Address) and address.is_reserved:
        return "reserved address"
    return None


def _check_host(host: str) -> None:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = hostile_address_reason(literal)
        if reason:
            raise EndpointRefused(f"host is a {reason}")
        return

    name = host[:-1] if host.endswith(".") else host
    if not name or len(name) > _DNS_NAME_MAX_CHARS:
        raise EndpointRefused("host name is empty or too long")
    labels = name.split(".")
    if not all(_DNS_LABEL.match(label) for label in labels):
        raise EndpointRefused("host is not a DNS name")
    # `127.1`, `2130706433`, `0x7f.0.0.1`: not IP literals to `ipaddress`, but
    # the C resolver reads them as addresses. A real name never ends in a
    # label like that.
    last = labels[-1]
    if last.isdigit() or last.startswith("0x"):
        raise EndpointRefused("host is a numeric form that is not a plain IP literal")
    if name in _LOOPBACK_NAMES or name.endswith(".localhost"):
        raise EndpointRefused("host names this machine")


def validate_endpoint_url(url: Any) -> str:
    """Return `url` if its FORM is acceptable as a peer endpoint, else raise
    `EndpointRefused`. Resolves nothing and connects to nothing."""
    if not isinstance(url, str) or not url:
        raise EndpointRefused("endpoint is not a non-empty string")
    if len(url) > ENDPOINT_URL_MAX_CHARS:
        raise EndpointRefused("endpoint URL is too long")
    if any(ch <= " " or ch == "\x7f" or ch == "\\" for ch in url):
        raise EndpointRefused("endpoint URL contains whitespace, a control character or a backslash")
    # Checked on the raw text: `urlsplit` reports an EMPTY query or fragment
    # (`https://host/?`) as absent.
    if "?" in url or "#" in url:
        raise EndpointRefused("endpoint URL carries a query or a fragment")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise EndpointRefused(f"endpoint URL does not parse ({e})") from None

    scheme = parts.scheme.lower()
    if scheme == "http":
        if not settings.federation_allow_http_endpoints:
            raise EndpointRefused("plain http is not allowed (FEDERATION_ALLOW_HTTP_ENDPOINTS)")
    elif scheme != "https":
        raise EndpointRefused("scheme is not https")
    if "@" in parts.netloc:
        raise EndpointRefused("endpoint URL carries credentials")
    if port == 0:
        raise EndpointRefused("port 0")
    host = parts.hostname
    if not host:
        raise EndpointRefused("endpoint URL has no host")
    _check_host(host.lower())
    return url


def endpoint_url_of(entry: Any) -> Any:
    """The URL of one endpoint entry, in the shapes a pairing may carry: a
    bare string, `{"url": ...}` or `{"endpoint_url": ...}`."""
    if isinstance(entry, dict):
        return entry.get("url") or entry.get("endpoint_url")
    return entry


def _resolve(host: str) -> list[str]:
    """Every address `host` resolves to. Blocking -- call through a thread."""
    return [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


async def endpoint_refusal(url: Any) -> str | None:
    """Why no request may be sent to `url` -- or `None` if one may.

    Form first, then every address the host resolves to. Never raises.

    🛑 Only a DEFINITE "no such name" (`EAI_NONAME`) passes unresolved. A
    lookup that times out, fails temporarily (`EAI_AGAIN`) or fails in any
    other way REFUSES. The far side owns its DNS: a server that answers after
    the timeout here -- with a loopback, metadata or in-cluster address --
    would otherwise pass this check every time, and the HTTP client and the
    certificate probe then resolve the name afresh with longer timeouts of
    their own. "No answer yet" says nothing about where the name points.
    """
    try:
        validate_endpoint_url(url)
    except EndpointRefused as e:
        return str(e)

    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return None  # a literal: `validate_endpoint_url` has judged it
    except ValueError:
        pass
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(_resolve, host), timeout=RESOLVE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return "host did not resolve in time"
    except socket.gaierror as e:
        if e.errno == socket.EAI_NONAME:
            return None
        return f"host resolution failed ({e.errno})"
    except (OSError, UnicodeError):
        return "host resolution failed"
    for raw in addresses:
        try:
            # A scoped IPv6 address comes back as `fe80::1%eth0`.
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            continue
        reason = hostile_address_reason(address)
        if reason:
            return f"host resolves to a {reason}"
    return None


async def check_peer_endpoints(endpoints: Any) -> None:
    """Refuse a pairing message's endpoint list unless EVERY entry is an
    acceptable target. An empty list passes (a peer may advertise nothing)."""
    if endpoints is None:
        return
    if not isinstance(endpoints, list):
        raise EndpointRefused("endpoints is not a list")
    if len(endpoints) > ENDPOINTS_MAX:
        raise EndpointRefused("too many endpoints")
    for entry in endpoints:
        reason = await endpoint_refusal(endpoint_url_of(entry))
        if reason:
            raise EndpointRefused(reason)
