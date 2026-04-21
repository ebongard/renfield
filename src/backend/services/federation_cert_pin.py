"""
TLS certificate fingerprint pinning for federation peers (F5d).

When `PeerUser.transport_config.tls_fingerprint` is set, the asker
verifies the responder's leaf TLS certificate matches the pinned
SHA-256 hex BEFORE issuing any federation request. Mismatch aborts
the query with a clear error.

Threat model:
  An adversary with a CA-issued cert for a domain they control could
  MITM a federation call. The Ed25519 pair-anchor binding on the
  response payload IS the cryptographic ground truth — a MITM can't
  forge a valid responder signature without the responder's private
  key. But pinning is defense-in-depth: it catches the MITM at the
  transport layer before any application-level data flows.

Implementation:
  We do a one-shot pre-flight TLS probe (asyncio.open_connection +
  custom SSLContext) that retrieves the leaf cert in DER form,
  computes SHA-256, and compares to the pin. The probe uses
  `verify_mode=CERT_NONE` because we're doing our own validation —
  pinned-cert deploys are typically self-signed where CA validation
  would fail anyway. The actual federation request that follows uses
  `verify=False` on the httpx client (set by `_tls_verify_for_peer`).

Hex format:
  Fingerprints are stored case-insensitive, with optional `:`
  separators (matches `openssl x509 -fingerprint -sha256` output).
  The compare normalizes both sides.
"""
from __future__ import annotations

import asyncio
import hashlib
import ssl
from urllib.parse import urlparse

from loguru import logger


def _normalize_fingerprint(s: str) -> str:
    """Strip `:` separators and `sha256:` algorithm prefix, lowercase.
    Tolerates the openssl `AA:BB:CC:...` format, the no-separator
    `aabbcc...` form, and the curl/SPKI-style `sha256:AA:BB:...`."""
    s = s.lower().strip()
    if s.startswith("sha256:"):
        s = s[len("sha256:"):]
    return s.replace(":", "").replace(" ", "")


async def probe_peer_cert_fingerprint(
    endpoint_url: str,
    *,
    timeout: float = 5.0,
) -> str | None:
    """
    Open a one-shot TLS connection to `endpoint_url` and return the
    peer's leaf certificate SHA-256 as lowercase hex (no separators).

    Returns None for:
      - non-HTTPS endpoints (nothing to pin)
      - TCP/TLS failures (host down, refused, timeout)
      - peers that don't present a certificate or aren't speaking TLS

    Used for two purposes:
      - F5d verification — caller compares against a stored pin.
      - F5e TOFU auto-pinning — caller stores the return value in
        `PeerUser.transport_config.tls_fingerprint` at pairing time,
        making every subsequent query verify against it.
    """
    parsed = urlparse(endpoint_url)
    if parsed.scheme != "https":
        return None
    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        logger.warning(f"Federation cert-pin: malformed endpoint {endpoint_url}")
        return None

    # CERT_NONE because we're either doing our own fingerprint
    # validation (F5d) or TOFU-capturing whatever cert is presented
    # (F5e). CA validity isn't the question we're answering.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError) as e:
        logger.warning(
            f"Federation cert-pin: could not connect to {host}:{port} for "
            f"pre-flight: {e}"
        )
        return None

    try:
        ssl_obj = writer.get_extra_info("ssl_object")
        if ssl_obj is None:
            logger.warning(
                f"Federation cert-pin: no SSL context on connection to "
                f"{host}:{port} (server speaks plain TCP?)"
            )
            return None
        cert_der = ssl_obj.getpeercert(binary_form=True)
        if not cert_der:
            logger.warning(
                f"Federation cert-pin: peer {host}:{port} did not "
                f"present a certificate"
            )
            return None
        return hashlib.sha256(cert_der).hexdigest().lower()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            # Tearing down the probe socket cleanly is best-effort;
            # any error here doesn't affect the verification result.
            pass


async def verify_peer_cert_fingerprint(
    endpoint_url: str,
    expected_fingerprint_hex: str,
    *,
    timeout: float = 5.0,
) -> tuple[bool, str | None]:
    """
    Open a TLS connection to `endpoint_url`, fetch the peer's leaf
    cert, compute SHA-256, and compare to `expected_fingerprint_hex`.

    Returns `(matched, actual_hex)`:
      - `(True, actual)` — fingerprint matches the pin.
      - `(False, actual)` — fingerprint mismatch; `actual` is what we
        observed so the caller can log it for forensics.
      - `(False, None)` — connection failed entirely (network error,
        timeout, non-TLS endpoint). Treated as mismatch by the caller.

    Non-https endpoints (http://) return `(True, None)` — there's no
    TLS to pin, and the federation Ed25519 binding still secures the
    payload. Caller should still warn-log this case.
    """
    parsed = urlparse(endpoint_url)
    if parsed.scheme != "https":
        # Nothing to pin on plain HTTP; let the request proceed and
        # let the Ed25519 response signature do the integrity work.
        return True, None

    actual = await probe_peer_cert_fingerprint(endpoint_url, timeout=timeout)
    if actual is None:
        # probe_peer_cert_fingerprint already logged the specific failure.
        return False, None
    expected_norm = _normalize_fingerprint(expected_fingerprint_hex)
    return actual == expected_norm, actual
