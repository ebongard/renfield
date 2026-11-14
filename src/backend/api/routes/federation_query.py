"""
Federation query_brain routes — peer-authenticated endpoints.

Mounted under /api/federation/peer/. NO `get_current_user` dependency:
these are peer-to-peer endpoints, authenticated entirely by the
Ed25519 signature on each request (verified against peer_users rows).

Uniform 400 "federation query failed" on every FederationQueryError —
no oracle telegraphing signature-vs-nonce-vs-timestamp failure modes.
Logs carry the specific reason for operator debugging.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from services.database import get_db
from services.federation_instance_name import wire_body
from services.federation_query_responder import (
    FederationQueryError,
    FederationQueryResponder,
)
from services.federation_query_schemas import (
    PeerPingRequest,
    PeerPingResponse,
    QueryBrainInitiateRequest,
    QueryBrainInitiateResponse,
    QueryBrainRetrieveRequest,
    QueryBrainRetrieveResponse,
)
from utils.config import settings

router = APIRouter()

#: Bodies larger than this are not a ping. Checked before parsing, so the
#: route cannot be used to make the backend parse megabytes.
_PING_BODY_MAX_BYTES = 4096


@router.post("/peer/query_brain/initiate", response_model=QueryBrainInitiateResponse)
async def peer_query_brain_initiate(
    body: QueryBrainInitiateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Responder step 1 — accept a signed query and enqueue background work."""
    responder = FederationQueryResponder(db)
    try:
        return await responder.handle_initiate(body)
    except FederationQueryError as e:
        logger.warning(
            f"Federation query_brain/initiate rejected "
            f"(asker={body.asker_pubkey[:12]}...): {e}"
        )
        raise HTTPException(status_code=400, detail="Federation query failed")


@router.post("/peer/query_brain/retrieve", response_model=QueryBrainRetrieveResponse)
async def peer_query_brain_retrieve(
    body: QueryBrainRetrieveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Responder step 2 — return current state (or final signed answer)."""
    responder = FederationQueryResponder(db)
    try:
        resp = await responder.handle_retrieve(body)
    except FederationQueryError as e:
        logger.warning(
            f"Federation query_brain/retrieve rejected "
            f"(asker={body.asker_pubkey[:12]}..., request_id={body.request_id}): {e}"
        )
        raise HTTPException(status_code=400, detail="Federation query failed")
    # Serialised by hand so that the name attachment is OMITTED when this
    # instance has none -- every other key goes out as before, `null`s
    # included, which `response_model_exclude_none` would not preserve.
    return JSONResponse(content=wire_body(resp))


async def _read_capped(request: Request, cap: int) -> bytes | None:
    """The request body -- or `None` when it is, or claims to be, larger than `cap`.

    🛑 This route is reachable WITHOUT a pairing: the signature can only be
    checked once the body is here. `request.body()` buffers whatever is sent,
    so an unpaired caller could have this process hold megabytes per request.
    A declared `Content-Length` above the cap is refused before a byte is
    read; a body without one (chunked) is read only up to the cap.

    A body that declares a `Content-Encoding` is refused unread as well. The
    stream below is the raw body -- nothing here inflates it -- and it has to
    stay that way: the cap counts bytes on the wire, and a decompressing
    layer put in front of this route would turn 4 KiB into megabytes.
    """
    encoding = request.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        return None
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > cap):
        return None
    received = bytearray()
    async for chunk in request.stream():
        received += chunk
        if len(received) > cap:
            return None
    return bytes(received)


@router.post("/peer/ping", response_model=PeerPingResponse)
async def peer_ping(request: Request, db: AsyncSession = Depends(get_db)):
    """Reachability round trip -- a signed acknowledgement, nothing else.

    🛑 The body is read by hand instead of being declared as a parameter, for
    two reasons that are both about not telling an outsider anything new:

      - With reachability off this route must be INDISTINGUISHABLE from a
        version that does not have it: 404 with FastAPI's own body, whatever
        was posted. A declared body would be validated first and answer 422.
      - A body that does not parse gets the same uniform 400 as a bad
        signature, an unknown key or a revoked pairing. An unpaired caller
        sees exactly what a refused query shows it.
    """
    if not settings.federation_reachability_enabled:
        raise HTTPException(status_code=404, detail="Not Found")

    raw = await _read_capped(request, _PING_BODY_MAX_BYTES)
    try:
        if raw is None:
            raise ValueError("ping body too large")
        body = PeerPingRequest.model_validate_json(raw)
    except (ValidationError, ValueError) as e:
        logger.warning(f"Federation ping rejected: malformed body ({type(e).__name__})")
        raise HTTPException(status_code=400, detail="Federation query failed") from None

    responder = FederationQueryResponder(db)
    try:
        resp = await responder.handle_ping(body)
    except FederationQueryError as e:
        logger.warning(
            f"Federation ping rejected (asker={body.asker_pubkey[:12]}...): {e}"
        )
        raise HTTPException(status_code=400, detail="Federation query failed")
    return JSONResponse(content=wire_body(resp))
