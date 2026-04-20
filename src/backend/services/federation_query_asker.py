"""
FederationQueryAsker — the client side of the `query_brain` protocol.

Drives the 2-step async flow against a remote Renfield peer:

    PeerUser (paired in F2)
        │
        ▼
    query_peer(peer, query_text)  →  AsyncIterator[ProgressChunk | FinalResult]
        │
        ├─ Step 1: POST /api/federation/peer/query_brain/initiate
        │         body: asker-signed {query, nonce, ts}
        │         → {request_id, accepted_at}
        │
        ├─ Step 2: poll POST /query_brain/retrieve
        │         body: asker-signed {request_id, ts}
        │         → {status, progress?, answer?, provenance?, sig?}
        │
        │         On each 'processing' poll:
        │              yield ProgressChunk(label=progress, ...)
        │
        │         On terminal:
        │              verify responder_signature covers complete_canonical_payload
        │              yield FinalResult dict
        │
        └─ On timeout / transport error / signature mismatch:
             yield FinalResult(success=False, message=...)

Exposed as an AsyncIterator so F3c can plug this straight into
`MCPManager.execute_tool_streaming` — the agent loop sees one tool
(`query_brain`) with live progress chunks flowing to the chat UI.

Side-channel posture (mirrors F3a responder):
  - Progress labels from the locked PROGRESS_LABELS vocabulary are
    passed through to the chat layer; unknown labels fall back to
    `tool_running` (defence against a misbehaving responder emitting
    arbitrary strings).
  - Responder signature is mandatory on terminal responses — an
    unsigned or invalidly-signed answer is treated as failure. The
    asker never trusts the responder's answer without that check.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from loguru import logger

from models.database import PeerUser
from services.federation_identity import (
    FederationIdentity,
    get_federation_identity,
)
from services.federation_query_schemas import (
    STATUS_COMPLETE,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    QueryBrainInitiateRequest,
    QueryBrainInitiateResponse,
    QueryBrainRetrieveRequest,
    QueryBrainRetrieveResponse,
    complete_canonical_payload,
    initiate_canonical_payload,
    retrieve_canonical_payload,
)
from services.mcp_streaming import (
    PROGRESS_LABEL_FAILED,
    PROGRESS_LABEL_TOOL_RUNNING,
    PROGRESS_LABELS,
    ProgressChunk,
)
from services.pairing_service import _canonical_bytes


# Timeouts (seconds) — responder-side TTL is 60s (F3a), so we cap here.
POLL_INTERVAL_SECONDS = 0.5
MAX_POLL_DURATION_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10


class FederationQueryError(Exception):
    """Any asker-side failure — unreachable peer, bad signature, timeout."""


class FederationQueryAsker:
    """
    One per logical query. Holds this Renfield's identity + an HTTP
    client. Exposes `query_peer` as an AsyncIterator so consumers
    (F3c `MCPManager.execute_tool_streaming` shim) can surface
    progress chunks in real time.
    """

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.identity = get_federation_identity()
        # Injectable for tests. Production uses a fresh client per
        # query; long-running pooled client shared across queries is a
        # future optimization if query rate warrants it (it won't for
        # home-scale deployments).
        self._client = client

    async def query_peer(
        self, peer: PeerUser, query_text: str,
    ) -> AsyncIterator[ProgressChunk | dict[str, Any]]:
        """
        Drive a federated query against `peer`. Yields ProgressChunks
        during polling and a final FinalResult dict at the end.

        The FinalResult shape matches `MCPManager.execute_tool`'s
        contract — `{"success": bool, "message": str, "data": Any}` —
        so F3c can drop this into the existing tool-result plumbing
        without a translation layer.

        Cancellation: if the consumer breaks out of the iterator (chat
        WebSocket drops, agent loop cancels), the owned httpx client is
        closed via `async with` and any in-flight request is cancelled
        by asyncio cooperative cancellation.
        """
        endpoint = _select_endpoint(peer)
        if endpoint is None:
            yield _final_error("Peer has no usable transport endpoint")
            return

        if self._client is not None:
            # Injected client (test path) — don't own its lifecycle.
            async for item in self._run(self._client, peer, endpoint, query_text):
                yield item
            return

        # Owned client — `async with` guarantees cleanup even on
        # GeneratorExit / CancelledError propagating from a consumer abort.
        client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(HTTP_TIMEOUT_SECONDS)}
        verify = _tls_verify_for_peer(peer)
        if verify is not None:
            client_kwargs["verify"] = verify

        async with httpx.AsyncClient(**client_kwargs) as client:
            async for item in self._run(client, peer, endpoint, query_text):
                yield item

    async def _run(
        self,
        client: "httpx.AsyncClient | Any",
        peer: PeerUser,
        endpoint: str,
        query_text: str,
    ) -> AsyncIterator[ProgressChunk | dict[str, Any]]:
        """Shared query loop for both owned + injected client paths."""
        initiate_resp = await self._initiate(client, endpoint, query_text)
        if initiate_resp is None:
            yield _final_error("Peer rejected initiate")
            return

        request_id = initiate_resp.request_id
        logger.debug(
            f"Federation query_brain: initiated {request_id} → "
            f"{endpoint} (peer={peer.remote_display_name})"
        )

        # Poll loop — yield progress until terminal status.
        sequence = 0
        deadline = time.time() + MAX_POLL_DURATION_SECONDS
        last_progress: str | None = None

        while time.time() < deadline:
            poll_resp = await self._retrieve(client, endpoint, request_id)
            if poll_resp is None:
                yield _final_error("Peer poll request failed")
                return

            if poll_resp.status == STATUS_PROCESSING:
                # Only emit a ProgressChunk when the progress label
                # actually changed — avoid flooding the UI with
                # redundant "still retrieving" chunks on every poll.
                if poll_resp.progress and poll_resp.progress != last_progress:
                    sequence += 1
                    label = (
                        poll_resp.progress
                        if poll_resp.progress in PROGRESS_LABELS
                        else PROGRESS_LABEL_TOOL_RUNNING
                    )
                    yield ProgressChunk(
                        label=label,
                        detail={"peer": peer.remote_display_name},
                        sequence=sequence,
                    )
                    last_progress = poll_resp.progress
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Terminal — verify signature AND bind to paired pubkey.
            yield self._finalize(poll_resp, peer)
            return

        # Ran out of polls without a terminal status.
        yield _final_error(
            f"Federation query timed out after {MAX_POLL_DURATION_SECONDS}s"
        )

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    async def _initiate(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        query_text: str,
    ) -> QueryBrainInitiateResponse | None:
        """Sign + POST the initiate request. Returns None on HTTP error."""
        unsigned = {
            "version": 1,
            "asker_pubkey": self.identity.public_key_hex(),
            "query": query_text,
            "nonce": secrets.token_hex(16),
            "timestamp": int(time.time()),
        }
        signature = self.identity.sign(_canonical_bytes(unsigned)).hex()
        req = QueryBrainInitiateRequest(**unsigned, signature=signature)

        try:
            r = await client.post(
                _join(endpoint, "/api/federation/peer/query_brain/initiate"),
                json=req.model_dump(),
            )
        except httpx.HTTPError as e:
            logger.warning(f"Federation initiate transport error for {endpoint}: {e}")
            return None

        if r.status_code != 200:
            logger.warning(
                f"Federation initiate rejected by {endpoint}: "
                f"HTTP {r.status_code} {r.text[:200]}"
            )
            return None

        try:
            return QueryBrainInitiateResponse.model_validate(r.json())
        except Exception as e:
            logger.warning(f"Federation initiate: malformed response from {endpoint}: {e}")
            return None

    async def _retrieve(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        request_id: str,
    ) -> QueryBrainRetrieveResponse | None:
        """Sign + POST a retrieve poll. Returns None on HTTP error."""
        unsigned = {
            "version": 1,
            "request_id": request_id,
            "asker_pubkey": self.identity.public_key_hex(),
            "timestamp": int(time.time()),
        }
        signature = self.identity.sign(_canonical_bytes(unsigned)).hex()
        req = QueryBrainRetrieveRequest(**unsigned, signature=signature)

        try:
            r = await client.post(
                _join(endpoint, "/api/federation/peer/query_brain/retrieve"),
                json=req.model_dump(),
            )
        except httpx.HTTPError as e:
            logger.warning(f"Federation retrieve transport error for {endpoint}: {e}")
            return None

        if r.status_code != 200:
            logger.warning(
                f"Federation retrieve rejected by {endpoint}: HTTP {r.status_code}"
            )
            return None

        try:
            return QueryBrainRetrieveResponse.model_validate(r.json())
        except Exception as e:
            logger.warning(f"Federation retrieve: malformed response from {endpoint}: {e}")
            return None

    def _finalize(
        self,
        resp: QueryBrainRetrieveResponse,
        peer: PeerUser,
    ) -> dict[str, Any]:
        """
        Verify responder signature on terminal response + map to
        the MCPManager.execute_tool FinalResult shape.

        Rejects the answer if:
          - status is EXPIRED (responder discarded our request)
          - status is FAILED (responder hit an error)
          - status is COMPLETE but responder_pubkey / signature missing
          - status is COMPLETE but responder_pubkey != peer.remote_pubkey
            (PAIR-ANCHOR BINDING — see below)
          - status is COMPLETE but signature verification fails

        PAIR-ANCHOR BINDING (review CRITICAL #1 fix):
          The asker trusts `peer.remote_pubkey` — the pubkey the peer
          presented at F2 pairing time. We do NOT trust
          `resp.responder_pubkey` by itself; a MITM could replace the
          whole response with an attacker-signed message carrying the
          attacker's own pubkey, and Ed25519 verification would pass
          against that claimed key. The pair anchor closes this hole:
          only signatures made with the paired peer's key are accepted.
        """
        if resp.status == STATUS_EXPIRED:
            return _final_error("Responder discarded the request (STATUS_EXPIRED)")
        if resp.status == STATUS_FAILED:
            return _final_error("Responder reported failure")
        if resp.status != STATUS_COMPLETE:
            return _final_error(f"Unknown responder status: {resp.status}")

        if not resp.responder_pubkey or not resp.responder_signature:
            return _final_error(
                "Responder complete response missing pubkey or signature — "
                "refusing to trust the answer"
            )

        # PAIR-ANCHOR BINDING — the response's self-claimed pubkey must
        # match the pubkey we paired with. Otherwise an attacker-signed
        # response using THEIR own key would verify-against-itself.
        if resp.responder_pubkey != peer.remote_pubkey:
            logger.warning(
                f"Federation query_brain: responder_pubkey "
                f"({resp.responder_pubkey[:12]}...) does not match paired peer "
                f"({peer.remote_pubkey[:12]}...) — possible MITM or peer drift"
            )
            return _final_error(
                "Responder pubkey does not match paired peer — "
                "refusing to trust the answer"
            )

        try:
            pubkey = bytes.fromhex(resp.responder_pubkey)
            signature = bytes.fromhex(resp.responder_signature)
        except ValueError:
            return _final_error("Malformed responder pubkey or signature hex")

        payload = complete_canonical_payload(resp)
        if not FederationIdentity.verify(pubkey, signature, _canonical_bytes(payload)):
            return _final_error(
                "Responder signature verification failed — answer may be forged"
            )

        return {
            "success": True,
            "message": resp.answer or "",
            "data": {
                "provenance": [p.model_dump() for p in resp.provenance],
                "answered_at": resp.answered_at,
                "responder_pubkey": resp.responder_pubkey,
            },
        }


# =============================================================================
# Helpers
# =============================================================================


def _final_error(message: str) -> dict[str, Any]:
    """Build a FinalResult dict for an asker-side failure."""
    return {"success": False, "message": message, "data": None}


def _select_endpoint(peer: PeerUser) -> str | None:
    """
    Pick a reachable endpoint from the peer's transport_config.

    v1: return the first endpoint. Future F5 hardening may rank by
    Tailscale-vs-direct-LAN-vs-VPS-relay freshness signals. For now
    the pairing handshake (F2) writes exactly one endpoint per peer.
    """
    config = peer.transport_config or {}
    endpoints = config.get("endpoints") or config.get("accepted_endpoints") or []
    if not endpoints:
        # Fallback: if transport_config was written with a different
        # shape (early F2 drafts), accept a top-level url.
        url = config.get("endpoint_url")
        if url:
            return url
        return None
    first = endpoints[0]
    if isinstance(first, str):
        return first
    if isinstance(first, dict):
        return first.get("url") or first.get("endpoint_url")
    return None


def _join(base: str, path: str) -> str:
    """Join base URL + path without duplicating slashes."""
    return f"{base.rstrip('/')}{path}"


def _tls_verify_for_peer(peer: PeerUser) -> Any | None:
    """
    Resolve the `verify=` parameter for the httpx client based on
    `peer.transport_config.tls_fingerprint`.

    Policy (review SHOULD-FIX #2):
    - tls_fingerprint present → future: enforce cert pin via a custom
      SSLContext that validates against the pinned fingerprint. For v1
      we log that a pin was configured but use default verification;
      the Ed25519 pair-anchor binding on the response payload (see
      _finalize) is the cryptographic ground-truth anyway. Upgrading
      to real pinning is an F5 hardening task.
    - no fingerprint → default verification (CA-signed certs work,
      self-signed don't). Home deployments using Tailscale sidestep
      this; direct-LAN HTTPS peers will need the fingerprint.
    - http:// endpoints → httpx does no TLS at all; we log but allow
      because the Ed25519 response signature provides integrity.

    Returns None when the default should be used (caller omits
    `verify=` from the client kwargs).
    """
    config = peer.transport_config or {}
    fingerprint = config.get("tls_fingerprint")
    if fingerprint:
        logger.info(
            f"Federation peer {peer.remote_display_name} has tls_fingerprint "
            f"configured (not yet enforced — F5 hardening task)"
        )
    return None
