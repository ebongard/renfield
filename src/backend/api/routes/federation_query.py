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

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from services.database import get_db
from services.federation_query_responder import (
    FederationQueryError,
    FederationQueryResponder,
)
from services.federation_query_schemas import (
    QueryBrainInitiateRequest,
    QueryBrainInitiateResponse,
    QueryBrainRetrieveRequest,
    QueryBrainRetrieveResponse,
)


router = APIRouter()


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
        return await responder.handle_retrieve(body)
    except FederationQueryError as e:
        logger.warning(
            f"Federation query_brain/retrieve rejected "
            f"(asker={body.asker_pubkey[:12]}..., request_id={body.request_id}): {e}"
        )
        raise HTTPException(status_code=400, detail="Federation query failed")
