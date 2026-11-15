"""
Federation query_brain routes — peer-authenticated endpoints.

Mounted under /api/federation/peer/. NO `get_current_user` dependency:
these are peer-to-peer endpoints, authenticated entirely by the
Ed25519 signature on each request (verified against peer_users rows).

Uniform 400 "federation query failed" on every FederationQueryError —
no oracle telegraphing signature-vs-nonce-vs-timestamp failure modes.
Logs carry the specific reason for operator debugging.

Every body on these routes is read by `_read_capped`: bounded in bytes AND in
time, because the signature can only be checked once the body is here and the
caller is therefore still a stranger while it is being read.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
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

#: Bodies larger than this are not a ping. Checked before parsing, so the
#: route cannot be used to make the backend parse megabytes.
_PING_BODY_MAX_BYTES = 4096

#: Bodies larger than this are not a query envelope. The largest valid one is
#: an `initiate` whose 4000-character query consists of characters JSON escapes
#: as two `\uXXXX` units (12 bytes each, 48 KiB) plus about 2 KiB of envelope.
_QUERY_BODY_MAX_BYTES = 64 * 1024

_UNIFORM_REFUSAL = "Federation query failed"


async def _read_capped(
    request: Request, cap: int, *, deadline_seconds: float | None = None,
) -> bytes | None:
    """The request body -- or `None` when it is, or claims to be, larger than
    `cap`, or has not arrived in full within the deadline.

    🛑 These routes are reachable WITHOUT a pairing: the signature can only be
    checked once the body is here. `request.body()` buffers whatever is sent,
    so an unpaired caller could have this process hold megabytes per request.
    A declared `Content-Length` above the cap is refused before a byte is
    read; a body without one (chunked) is read only up to the cap.

    🛑 The cap alone bounds bytes, not TIME: a caller sending one byte every
    few seconds stays inside any cap and holds the connection (and this
    coroutine) for as long as it likes. So the WHOLE read has one deadline
    (`FEDERATION_INBOUND_READ_TIMEOUT_SECONDS`), not one per chunk -- a
    per-chunk timeout is exactly what a slow drip satisfies.

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
    if deadline_seconds is None:
        deadline_seconds = settings.federation_inbound_read_timeout_seconds
    received = bytearray()
    try:
        async with asyncio.timeout(deadline_seconds):
            async for chunk in request.stream():
                received += chunk
                if len(received) > cap:
                    return None
    except TimeoutError:
        logger.warning(
            f"Federation inbound body not complete after {deadline_seconds:g}s "
            f"({len(received)} bytes received) -- refused"
        )
        return None
    return bytes(received)


class _BoundedBodyRequest(Request):
    """A request whose `body()` is capped and deadlined like a ping's.

    FastAPI reads a declared body parameter through `Request.body()` BEFORE
    the route function (or any dependency) runs, so the bound has to sit in
    the request object itself.
    """

    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            raw = await _read_capped(self, _QUERY_BODY_MAX_BYTES)
            if raw is None:
                logger.warning("Federation query rejected: body too large or too slow")
                raise HTTPException(status_code=400, detail=_UNIFORM_REFUSAL)
            self._body = raw
        return self._body


class _BoundedBodyRoute(APIRoute):
    """Hands every route of this router a `_BoundedBodyRequest`."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            return await handler(_BoundedBodyRequest(request.scope, request.receive))

        return bounded


router = APIRouter(route_class=_BoundedBodyRoute)


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


_PING_PATH = "/peer/ping"


# 🛑 Not in the OpenAPI schema: with the flag off this route must not be
# readable from `/openapi.json` either.
@router.post(_PING_PATH, response_model=PeerPingResponse, include_in_schema=False)
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


async def _peer_ping_any_other_method(request: Request) -> Response:
    """Every method but POST on the ping path.

    🛑 With reachability off the path must answer like one that was never
    built, and that is not only a statement about POST: a registered route
    answers a GET with 405 + `Allow: POST`, an unregistered path with 404.
    Until 2026-11-14 a stranger could read "this version has the ping, and it
    is switched off" from that difference. With the flag on nothing changes
    here -- the refusal of a POST gives the route away anyway (see
    docs/FEDERATION_PAIRING.md, "Known limits").
    """
    if not settings.federation_reachability_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    raise HTTPException(
        status_code=405, detail="Method Not Allowed", headers={"Allow": "POST"},
    )


async def _peer_ping_with_trailing_slash(request: Request) -> Response:
    """`/peer/ping/` -- 404, whatever the flag says.

    The router would otherwise redirect it to the registered path (307), which
    an unregistered path never gets: the same tell as the 405 above.
    """
    raise HTTPException(status_code=404, detail="Not Found")


# `methods=[]` matches EVERY method (Starlette treats an empty set as "no
# restriction"), custom ones included. The POST route above is registered
# first and wins for POST; a method mismatch there is only a partial match, so
# the router goes on to these.
router.add_route(_PING_PATH, _peer_ping_any_other_method, methods=[], include_in_schema=False)
router.add_route(
    _PING_PATH + "/", _peer_ping_with_trailing_slash, methods=[], include_in_schema=False,
)
