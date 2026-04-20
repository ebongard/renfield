"""
Federation query_brain wire-format schemas.

Split from the responder service so the asker-side client (F3b) can
import the same shapes without pulling in the responder's runtime
dependencies (Ollama, AtomStore, etc.).

Protocol summary (design doc § query_brain MCP tool, Two-step protocol):

    ASKER                                     RESPONDER
      │                                           │
      │   POST /peer/query_brain/initiate         │
      │   {asker_pubkey, query, nonce,            │
      │    timestamp, signature}                  │
      │──────────────────────────────────────────▶│
      │                                           │
      │                                           │ verify sig + ± window
      │                                           │ + peer_users lookup
      │                                           │ + mint request_id (UUID4)
      │                                           │ + enqueue background
      │                                           │   work
      │                                           │
      │◀──────────────────────────────────────────│
      │   {request_id, accepted_at}               │
      │                                           │
      │   (poll loop)                             │
      │   POST /peer/query_brain/retrieve         │
      │   {request_id, asker_pubkey, signature}   │
      │──────────────────────────────────────────▶│
      │                                           │ verify sig + pubkey
      │                                           │ BOUND to initiator
      │                                           │ + return progress
      │                                           │ label (one of a
      │                                           │ locked vocabulary —
      │                                           │ see mcp_streaming)
      │                                           │
      │◀──────────────────────────────────────────│
      │   {status: 'processing', progress: str}   │
      │   ... (repeat until complete/failed/      │
      │        expired) ...                       │
      │   {status: 'complete', answer,            │
      │    provenance[], responder_signature}     │
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# =============================================================================
# Status discriminator for retrieve responses
# =============================================================================

STATUS_PROCESSING = "processing"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

STATUS_VALUES = frozenset({STATUS_PROCESSING, STATUS_COMPLETE, STATUS_FAILED, STATUS_EXPIRED})


# =============================================================================
# Initiate
# =============================================================================


class QueryBrainInitiateRequest(BaseModel):
    """
    Asker-signed request to kick off a federated query.

    Signature covers the canonical-JSON encoding of every non-signature
    field (version, asker_pubkey, query, nonce, timestamp, depth, path).
    Responder MUST reject requests whose timestamp falls outside a ±60s
    window, must remember the nonce to reject replays, and (F5a) must
    reject if `depth < 0` or its own pubkey already appears in `path`
    (cycle).

    F5a design — `depth` and `path` are hardening fields only. They do
    NOT grant cross-peer trust:
      - The envelope's `asker_pubkey` IS the sender, signed with the
        sender's key, bound to the receiver's pair-anchor. That's the
        only trust anchor.
      - `path` is a provenance / cycle-defense marker. Entries beyond
        `asker_pubkey` (if cascade is ever wired) are informational —
        a receiver never verifies them against its peer table.
      - Cascade mechanics (if/when added): the cascader signs a fresh
        envelope AS THE NEW asker (pair-anchor requires it). The
        original originator's pubkey lives on in `path[0]` for audit
        and cycle detection, but the new receiver trusts only its
        directly paired peer.

    Version 2 of the envelope adds `depth` + `path` for F5a. Because the
    federation network is pre-production the bump is hard — v1 peers
    will be rejected at signature verification (the canonical payload
    shape differs). No backwards-compat shim.
    """
    version: int = 2
    asker_pubkey: str = Field(..., min_length=64, max_length=64)  # hex
    query: str = Field(..., max_length=4000)
    nonce: str = Field(..., min_length=16)  # 128-bit, hex-encoded
    timestamp: int  # unix seconds; responder rejects > ±60s from its clock
    # F5a — remaining hop budget (resource guard). Asker sets to
    # settings.federation_max_depth. Responder rejects depth<0. A
    # future cascader decrements before forwarding. depth=0 means
    # "you're the last stop — no further cascade allowed but do your
    # own work"; the responder accepts and processes normally.
    depth: int = Field(default=3, ge=0, le=10)
    # F5a — cycle-defense + provenance marker. Carries asker pubkeys
    # already in the call chain. First-hop queries carry
    # `[originator_pubkey]`. A future cascader appends its own pubkey
    # when minting the DOWNSTREAM (fresh-signed) envelope, so the
    # receiver sees the chain. Receivers reject when their own pubkey
    # is already in the list (cycle) — but DO NOT treat path entries
    # as trust claims; only the envelope's `asker_pubkey` signature
    # is authoritative. Max 10 at schema level bounds path growth.
    path: list[str] = Field(default_factory=list, max_length=10)
    signature: str = Field(..., min_length=128, max_length=128)  # hex


class QueryBrainInitiateResponse(BaseModel):
    """Responder acknowledgement — opaque request_id the asker polls with."""
    request_id: str  # UUID4
    accepted_at: int  # unix seconds


# =============================================================================
# Retrieve (poll)
# =============================================================================


class QueryBrainRetrieveRequest(BaseModel):
    """
    Asker-signed poll for an in-flight request. Signature covers
    (request_id, asker_pubkey, timestamp) so a stolen request_id
    alone can't be polled by an eavesdropper.
    """
    version: int = 1
    request_id: str
    asker_pubkey: str = Field(..., min_length=64, max_length=64)
    timestamp: int
    signature: str = Field(..., min_length=128, max_length=128)


class FederationProvenance(BaseModel):
    """
    Redacted source attribution — matches `Provenance.redacted_for_remote()`
    output. Responder runs that redaction before serializing. Asker stores
    these alongside the answer for display ("from Mom's recipes") but
    cannot correlate them back to the responder's atom IDs (UUID4 per
    call, not stable).
    """
    atom_id: str
    atom_type: str
    display_label: str
    score: float


class QueryBrainRetrieveResponse(BaseModel):
    """
    Poll response. Status discriminator controls which other fields
    are populated. Every terminal status (complete/failed/expired)
    also carries `responder_signature` over the full response body
    so the asker can verify the responder actually produced it.
    """
    version: int = 1
    status: str  # one of STATUS_VALUES
    progress: str | None = None  # present when status='processing'
    answer: str | None = None    # present when status='complete'
    provenance: list[FederationProvenance] = Field(default_factory=list)
    answered_at: int | None = None
    responder_pubkey: str | None = None
    responder_signature: str | None = None  # terminal statuses only


# =============================================================================
# Helpers
# =============================================================================


def initiate_canonical_payload(req: QueryBrainInitiateRequest) -> dict[str, Any]:
    """
    Return the dict over which `signature` is Ed25519-signed by the asker.
    Shared by signer and verifier to avoid byte drift (same pattern as
    pairing_service._canonical_bytes).

    `depth` and `path` are signed so an adversary can't strip them
    after the fact to bypass cycle detection / hop budget. `path` is
    encoded in order (it's a call chain, not a set).
    """
    return {
        "version": req.version,
        "asker_pubkey": req.asker_pubkey,
        "query": req.query,
        "nonce": req.nonce,
        "timestamp": req.timestamp,
        "depth": req.depth,
        "path": list(req.path),  # copy so later mutations don't alias
    }


def retrieve_canonical_payload(req: QueryBrainRetrieveRequest) -> dict[str, Any]:
    """Dict over which the asker signs their poll request."""
    return {
        "version": req.version,
        "request_id": req.request_id,
        "asker_pubkey": req.asker_pubkey,
        "timestamp": req.timestamp,
    }


def complete_canonical_payload(resp: QueryBrainRetrieveResponse) -> dict[str, Any]:
    """
    Dict over which the RESPONDER signs a terminal retrieve response.
    Asker verifies this signature against responder_pubkey before
    accepting the answer into their agent loop.
    """
    return {
        "version": resp.version,
        "status": resp.status,
        "answer": resp.answer,
        "provenance": [
            {
                "atom_id": p.atom_id,
                "atom_type": p.atom_type,
                "display_label": p.display_label,
                "score": p.score,
            }
            for p in resp.provenance
        ],
        "answered_at": resp.answered_at,
        "responder_pubkey": resp.responder_pubkey,
    }
