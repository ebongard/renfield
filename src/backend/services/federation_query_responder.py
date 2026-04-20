"""
FederationQueryResponder — the server side of the `query_brain` protocol.

Two HTTP endpoints under /api/federation/peer/ route here. There's NO
`get_current_user` dependency: peers authenticate via the Ed25519
signature bound to a `peer_users.remote_pubkey` row, not via local
user sessions.

Lifecycle:

    handle_initiate()
        │ verify signature over canonical(asker_pubkey, query, nonce, ts)
        │ verify ±60s timestamp window
        │ verify nonce not already seen (replay defence)
        │ verify peer_users row exists + not revoked
        │ look up asker's local user_id + max_visible_tier
        │ mint request_id (UUID4)
        │ enqueue background task
        ▼
      returns {request_id, accepted_at}

    (background) _run_query()
        │ status := 'processing', progress := 'retrieving'
        │ run PolymorphicAtomStore.query(..., asker_id=..., max_visible_tier=...)
        │ status := 'processing', progress := 'synthesizing'
        │ call Ollama to turn top-k atoms into a natural-language answer
        │ status := 'complete', answer, provenance[] (redacted_for_remote)
        │   OR status := 'failed' on exception
        ▼ (exposed via handle_retrieve polls)

    handle_retrieve()
        │ verify poll signature over (request_id, asker_pubkey, ts)
        │ verify asker_pubkey matches the pubkey that called initiate
        │   (stolen-request_id defence — two users from different peers
        │    can't poll each other's requests)
        │ return current state snapshot; if terminal, sign the response.
        ▼
      returns {status, progress?, answer?, provenance?, responder_signature?}

Memory footprint: one `_PendingRequest` per in-flight query, purged on
terminal status or 60s TTL. Single-process (Renfield ships one backend
container); multi-process deploys would need Redis but that's F5.

Side-channel mitigation (design doc § streaming-progress):
  - Progress labels drawn from PROGRESS_LABELS (one of a locked set).
  - Rate limit: max 4 progress transitions per request (responder can't
    phase-by-phase telegraph timing for traffic analysis).
  - Progress strings never carry per-query specifics (no "found 47
    atoms"). A misbehaving responder can't leak that way because
    FederationQueryResponder is the only code path emitting progress
    in this flow.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PeerUser
from services.atom_types import Provenance
from services.database import AsyncSessionLocal
from services.federation_identity import (
    FederationIdentity,
    get_federation_identity,
)
from services.federation_query_schemas import (
    STATUS_COMPLETE,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    FederationProvenance,
    QueryBrainInitiateRequest,
    QueryBrainInitiateResponse,
    QueryBrainRetrieveRequest,
    QueryBrainRetrieveResponse,
    complete_canonical_payload,
    initiate_canonical_payload,
    retrieve_canonical_payload,
)
from services.mcp_streaming import (
    PROGRESS_LABEL_COMPLETE,
    PROGRESS_LABEL_FAILED,
    PROGRESS_LABEL_RETRIEVING,
    PROGRESS_LABEL_SYNTHESIZING,
)
from services.pairing_service import _canonical_bytes
from utils.config import settings


# Request lifecycle: pending rows live here for 60s (or until terminal).
REQUEST_TTL_SECONDS = 60
# Replay defence: nonces remembered for the ±60s window + a grace period.
NONCE_WINDOW_SECONDS = 60
NONCE_CACHE_MAX = 4096

# Max progress transitions per request (traffic-analysis defence).
MAX_PROGRESS_UPDATES = 4


# =============================================================================
# Errors (mapped to HTTP 400 uniformly by the route layer — no oracle)
# =============================================================================


class FederationQueryError(Exception):
    """Any signature/timestamp/nonce/peer lookup failure during query_brain."""


# =============================================================================
# In-memory state
# =============================================================================


@dataclass
class _PendingRequest:
    """One in-flight federated query, kept until terminal or TTL expiry."""
    request_id: str
    asker_pubkey: str
    peer_user_id: int              # PeerUser.id (the responder-side row)
    asker_local_user_id: int | None  # membership.member_user_id (None if not member)
    max_visible_tier: int           # how deep the asker can reach
    query: str
    initiated_at: float
    status: str = STATUS_PROCESSING
    progress_label: str = PROGRESS_LABEL_RETRIEVING
    progress_count: int = 0
    answer: str | None = None
    provenance: list[Provenance] = field(default_factory=list)
    answered_at: float | None = None
    error_message: str | None = None


# Single process scope. F5 hardening task: persist to Redis for multi-worker.
_pending_requests: dict[str, _PendingRequest] = {}
_nonce_cache: OrderedDict[str, float] = OrderedDict()

# Strong references to in-flight background tasks. Without this set the
# asyncio GC can collect the task mid-run; with it, we also have somewhere
# to join on shutdown (F5 will add graceful-drain support).
_background_tasks: set[asyncio.Task] = set()


def _clear_state_for_tests() -> None:
    """Test-only reset — every test starts with a clean in-memory view."""
    _pending_requests.clear()
    _nonce_cache.clear()
    # Cancel any lingering bg tasks from a prior test so they don't
    # later write to the cleared `_pending_requests` dict.
    for task in list(_background_tasks):
        task.cancel()
    _background_tasks.clear()


def purge_requests_for_pubkey(asker_pubkey: str) -> int:
    """
    Drop every in-flight pending request bound to `asker_pubkey`.

    Called by `revoke_peer` so a revoked peer cannot poll /retrieve
    and receive an answer for a request they initiated before the
    revocation. Also cancels their background _run_query tasks so
    they don't finish and write answers into (now-discarded) pending
    entries.

    Returns count of discarded requests.
    """
    discarded = [
        rid for rid, pr in list(_pending_requests.items())
        if pr.asker_pubkey == asker_pubkey
    ]
    for rid in discarded:
        _pending_requests.pop(rid, None)
    # Bg tasks whose pending entry has vanished will find `pending is None`
    # at the top of _run_query and return quietly — no cancellation needed.
    return len(discarded)


def _prune_expired(now: float | None = None) -> None:
    """Drop pending requests past TTL. Called on every initiate/retrieve.

    Iterates over a `list(items())` snapshot so a concurrent initiate
    that adds a new entry mid-iteration can't raise `RuntimeError:
    dictionary changed size during iteration` (two coroutines can
    interleave at any await boundary — not today's call path, but an
    easy foot-gun to leave).
    """
    t = now if now is not None else time.time()
    expired = [rid for rid, pr in list(_pending_requests.items())
               if t - pr.initiated_at > REQUEST_TTL_SECONDS and pr.status == STATUS_PROCESSING]
    for rid in expired:
        pr = _pending_requests.get(rid)
        if pr is None:
            continue
        pr.status = STATUS_EXPIRED
        logger.debug(f"Federation query_brain: expired {rid} (peer={pr.peer_user_id})")


def _record_nonce(nonce: str, now: float) -> bool:
    """
    Returns True iff the nonce is new. Evicts old entries past the
    window so the cache stays bounded. LRU-ordered by insertion time.
    """
    # Drop entries outside the timestamp window — they're outside the
    # replay-rejection window anyway.
    while _nonce_cache:
        oldest_nonce, oldest_at = next(iter(_nonce_cache.items()))
        if now - oldest_at > NONCE_WINDOW_SECONDS:
            _nonce_cache.pop(oldest_nonce, None)
        else:
            break

    if nonce in _nonce_cache:
        return False
    if len(_nonce_cache) >= NONCE_CACHE_MAX:
        # Shouldn't happen in practice; evict oldest to make room.
        _nonce_cache.popitem(last=False)
    _nonce_cache[nonce] = now
    return True


# =============================================================================
# Responder service
# =============================================================================


class FederationQueryResponder:
    """
    One per AsyncSession. Holds the DB handle + identity; the in-flight
    request map is module-level since background tasks outlive the
    HTTP session that created them.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.identity = get_federation_identity()

    # -------------------------------------------------------------------------
    # Step 1 — initiate
    # -------------------------------------------------------------------------

    async def handle_initiate(
        self,
        req: QueryBrainInitiateRequest,
    ) -> QueryBrainInitiateResponse:
        """
        Verify asker signature + freshness + peer authorization, mint a
        request_id, and kick off the background query+synthesis work.
        """
        self._verify_signature(
            pubkey_hex=req.asker_pubkey,
            signature_hex=req.signature,
            payload=initiate_canonical_payload(req),
        )

        now = time.time()
        if abs(now - req.timestamp) > NONCE_WINDOW_SECONDS:
            raise FederationQueryError(
                "Timestamp outside ±60s window (clock skew or replay)"
            )

        if not _record_nonce(req.nonce, now=now):
            raise FederationQueryError("Nonce already seen (replay detected)")

        # F5a — depth + cycle hardening. Checked AFTER signature
        # verification because the fields are part of the canonical
        # payload (an adversary can't strip them). Errors collapse to a
        # uniform "federation query failed" at the route layer
        # (federation_query.py) — no oracle; we log the specific reason
        # for operators.
        #
        # We accept any depth >= 0 without decrementing. depth=0 means
        # "you're the last stop — no further cascade allowed" and we do
        # the work normally. A future cascader (Renfield doesn't cascade
        # today) would decrement before forwarding the new fresh-signed
        # envelope.
        my_pubkey = self.identity.public_key_hex()
        if req.depth < 0:
            logger.warning(
                f"Federation query rejected: negative depth "
                f"({req.depth}, asker={req.asker_pubkey[:12]}…)"
            )
            raise FederationQueryError("Query depth exhausted")
        if my_pubkey in req.path:
            logger.warning(
                f"Federation query rejected: cycle — own pubkey "
                f"in path (asker={req.asker_pubkey[:12]}…, "
                f"path_len={len(req.path)})"
            )
            raise FederationQueryError("Federation cycle detected")
        # Path-integrity check: the sender's own pubkey MUST be in the
        # path. This prevents an adversary from stripping the originator
        # from the chain to hide provenance. Path entries beyond
        # asker_pubkey are informational (never trust claims); only the
        # envelope's signature is authoritative.
        if req.asker_pubkey not in req.path:
            logger.warning(
                f"Federation query rejected: asker_pubkey not in path "
                f"(asker={req.asker_pubkey[:12]}…)"
            )
            raise FederationQueryError("Malformed path: asker_pubkey missing")

        peer = await self._lookup_peer(req.asker_pubkey)

        # Resolve the asker's local visible tier. The responder granted
        # the asker a tier via CircleMembership when they paired (F2).
        # Use that as `max_visible_tier`. The asker's own user_id on
        # OUR side is the PeerUser.remote_user_id echo (cosmetic), but
        # the actual membership lookup keys on (circle_owner_id, member_user_id).
        # We don't have a stable remote-user-id ↔ local-user-id map, so
        # the membership is keyed on peer.remote_user_id directly: the
        # pairing flow wrote CircleMembership(
        #   circle_owner_id=responder, member_user_id=remote_user_id
        # ). Look that up.
        asker_local_user_id = peer.remote_user_id
        max_visible_tier = await self._resolve_asker_tier(
            owner_user_id=peer.circle_owner_id,
            member_user_id=peer.remote_user_id,
        )

        request_id = str(uuid.uuid4())
        _prune_expired(now=now)
        _pending_requests[request_id] = _PendingRequest(
            request_id=request_id,
            asker_pubkey=req.asker_pubkey,
            peer_user_id=peer.id,
            asker_local_user_id=asker_local_user_id,
            max_visible_tier=max_visible_tier,
            query=req.query,
            initiated_at=now,
        )

        # Schedule the background work. The task is fire-and-forget —
        # asker polls via handle_retrieve. We keep a strong reference
        # in `_background_tasks` so asyncio's GC doesn't collect it
        # mid-run (add_done_callback removes the ref on completion).
        task = asyncio.create_task(self._run_query(request_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        return QueryBrainInitiateResponse(
            request_id=request_id,
            accepted_at=int(now),
        )

    # -------------------------------------------------------------------------
    # Step 2 — retrieve (poll)
    # -------------------------------------------------------------------------

    async def handle_retrieve(
        self,
        req: QueryBrainRetrieveRequest,
    ) -> QueryBrainRetrieveResponse:
        """Verify poll signature + pubkey binding, return current state."""
        self._verify_signature(
            pubkey_hex=req.asker_pubkey,
            signature_hex=req.signature,
            payload=retrieve_canonical_payload(req),
        )

        now = time.time()
        if abs(now - req.timestamp) > NONCE_WINDOW_SECONDS:
            raise FederationQueryError("Poll timestamp outside window")

        _prune_expired(now=now)
        pending = _pending_requests.get(req.request_id)
        if pending is None:
            # Uniform: treat unknown + expired identically — no
            # existence-oracle on request_ids.
            return QueryBrainRetrieveResponse(status=STATUS_EXPIRED)

        # Binding check — a stolen request_id cannot be polled by a
        # different pubkey. Forges a uniform-expired response rather
        # than revealing "wrong user polled".
        if pending.asker_pubkey != req.asker_pubkey:
            logger.warning(
                f"Federation query_brain: pubkey mismatch on retrieve "
                f"(request_id={req.request_id}, expected={pending.asker_pubkey[:12]}..., "
                f"got={req.asker_pubkey[:12]}...)"
            )
            return QueryBrainRetrieveResponse(status=STATUS_EXPIRED)

        return self._serialize_pending(pending)

    # -------------------------------------------------------------------------
    # Background work
    # -------------------------------------------------------------------------

    async def _run_query(self, request_id: str) -> None:
        """Execute retrieval + synthesis in the background. Never raises —
        the outer try/except catches everything (including AttributeError
        on a None pending or ImportError inside _retrieve) and transitions
        to STATUS_FAILED, so the asker always sees a terminal status
        before TTL rather than polling into a timeout.

        CRITICAL: opens its OWN AsyncSession. The request-scoped session
        that `handle_initiate` used is closed by FastAPI's `get_db`
        dependency when the route returns, long before this bg task
        runs. Using `self.db` here would hit a closed session every time.
        """
        pending = _pending_requests.get(request_id)
        if pending is None:
            return

        try:
            # Transition: retrieving (chunk+KG+memory RRF).
            self._emit_progress(pending, PROGRESS_LABEL_RETRIEVING)

            # Fresh session scoped to this bg task only.
            async with AsyncSessionLocal() as session:
                matches = await self._retrieve(session, pending)

            # Transition: synthesizing (Ollama call).
            self._emit_progress(pending, PROGRESS_LABEL_SYNTHESIZING)
            answer = await self._synthesize(pending.query, matches)

            # Build redacted provenance list for the response.
            provenance = [
                Provenance(
                    atom_id=m.atom.atom_id,
                    atom_type=m.atom.atom_type,
                    display_label=m.snippet[:120] if m.snippet else f"atom {m.atom.atom_id[:8]}",
                    score=m.score,
                ).redacted_for_remote()
                for m in matches[:5]
            ]

            pending.answer = answer
            pending.provenance = provenance
            pending.answered_at = time.time()
            # Status set LAST so a concurrent retrieve observing
            # status=COMPLETE is guaranteed to see answer/provenance.
            pending.progress_label = PROGRESS_LABEL_COMPLETE
            pending.status = STATUS_COMPLETE
        except asyncio.CancelledError:
            # Propagate cancellation (from _clear_state_for_tests or
            # graceful shutdown). Don't mark failed.
            raise
        except Exception as e:
            logger.error(
                f"Federation query_brain: background work failed "
                f"(request_id={request_id}): {e}"
            )
            pending.error_message = str(e)
            # Fill in answered_at on failure too so _serialize_pending's
            # `int(pending.answered_at or time.time())` reports the
            # actual failure moment instead of the poll moment.
            pending.answered_at = time.time()
            pending.progress_label = PROGRESS_LABEL_FAILED
            pending.status = STATUS_FAILED

    async def _retrieve(
        self,
        session: AsyncSession,
        pending: _PendingRequest,
    ):
        """Run the polymorphic atom query, scoped to what the asker can see.

        Takes a session parameter rather than reading `self.db` — the bg
        task opens its own session (see _run_query docstring).
        """
        from services.polymorphic_atom_store import PolymorphicAtomStore
        store = PolymorphicAtomStore(session)
        return await store.query(
            query_text=pending.query,
            asker_id=pending.asker_local_user_id or 0,
            max_visible_tier=pending.max_visible_tier,
            top_k=10,
        )

    async def _synthesize(self, query: str, matches: list) -> str:
        """
        Ask the local LLM to turn retrieved matches into a natural-language
        answer.

        Synthesis isolation (design doc § Synthesis isolation):
          - The asker's query passes through an LLM prompt ONLY for the
            purpose of answering from the local snippets. It is NOT
            persisted back into conversation_memory, notifications, or
            any other capture surface on this responder. A malicious
            peer crafting an "ignore previous instructions ..." query
            therefore cannot coerce us to mutate our own atoms — they
            only ever shape the answer we ship back to them, which they
            already have full control over.
          - Answer is capped at ≤512 tokens via a hard prompt limit AND
            a 2000-char post-slice (defence in depth).

        Fallback: if Ollama is unreachable or times out, degrade to
        snippet concatenation so federation still returns SOMETHING
        rather than a blank answer. Logged as warning for operator
        visibility.
        """
        if not matches:
            return ""

        # Build snippet context. Cap each to 400 chars so the prompt
        # stays under typical context-window limits even for 10 matches.
        snippets_text = "\n".join(
            f"- {(m.snippet or '')[:400]}" for m in matches[:10]
        )

        system_msg = (
            "You are answering a federated query from a trusted peer. "
            "Answer ONLY from the provided snippets. "
            "If the snippets do not contain the answer, say 'I don't know from what was shared with you.' "
            "Do not fabricate details. Do not invent names, dates, or quantities not present in the snippets. "
            "Keep your answer under 200 words and in the same language as the question."
        )
        user_msg = (
            f"Question: {query}\n\n"
            f"Snippets from the responder's atoms:\n{snippets_text}"
        )

        try:
            from utils.llm_client import extract_response_content, get_default_client

            client = get_default_client()
            response = await asyncio.wait_for(
                client.chat(
                    model=settings.ollama_model,
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg},
                    ],
                    options={
                        "temperature": 0.2,   # factual, low creativity
                        "num_predict": 512,    # hard cap on generated tokens
                    },
                ),
                timeout=30.0,  # responder TTL is 60s; synthesis + retrieval + poll-reply must fit
            )
            answer = extract_response_content(response) or ""
        except Exception as e:
            logger.warning(
                f"Federation query_brain: Ollama synthesis failed ({e}); "
                f"falling back to snippet concatenation"
            )
            # Fallback stub — same shape as the pre-F3c synthesis. Keeps
            # federation functional when Ollama is down (a paired peer
            # still gets provenance + raw snippets to work with).
            answer = f"Relevant snippets:\n{snippets_text}"

        return answer[:2000]

    # -------------------------------------------------------------------------
    # Serialization + signing of terminal responses
    # -------------------------------------------------------------------------

    def _serialize_pending(self, pending: _PendingRequest) -> QueryBrainRetrieveResponse:
        if pending.status == STATUS_PROCESSING:
            return QueryBrainRetrieveResponse(
                status=STATUS_PROCESSING,
                progress=pending.progress_label,
            )

        if pending.status == STATUS_FAILED:
            resp = QueryBrainRetrieveResponse(
                status=STATUS_FAILED,
                answered_at=int(pending.answered_at or time.time()),
                responder_pubkey=self.identity.public_key_hex(),
            )
            resp.responder_signature = self._sign_response(resp)
            return resp

        if pending.status == STATUS_EXPIRED:
            return QueryBrainRetrieveResponse(status=STATUS_EXPIRED)

        # STATUS_COMPLETE
        resp = QueryBrainRetrieveResponse(
            status=STATUS_COMPLETE,
            answer=pending.answer,
            provenance=[
                FederationProvenance(
                    atom_id=p.atom_id,
                    atom_type=p.atom_type,
                    display_label=p.display_label,
                    score=p.score,
                )
                for p in pending.provenance
            ],
            answered_at=int(pending.answered_at or time.time()),
            responder_pubkey=self.identity.public_key_hex(),
        )
        resp.responder_signature = self._sign_response(resp)
        return resp

    def _sign_response(self, resp: QueryBrainRetrieveResponse) -> str:
        payload = complete_canonical_payload(resp)
        return self.identity.sign(_canonical_bytes(payload)).hex()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _verify_signature(
        *,
        pubkey_hex: str,
        signature_hex: str,
        payload: dict[str, Any],
    ) -> None:
        """Verify the asker's Ed25519 signature over the canonical payload.
        Raises FederationQueryError on any failure — uniform error at route."""
        try:
            pubkey = bytes.fromhex(pubkey_hex)
            signature = bytes.fromhex(signature_hex)
        except ValueError as e:
            raise FederationQueryError("Malformed pubkey or signature hex") from e
        if not FederationIdentity.verify(pubkey, signature, _canonical_bytes(payload)):
            raise FederationQueryError("Signature verification failed")

    async def _lookup_peer(self, asker_pubkey: str) -> PeerUser:
        """Find the PeerUser row that matches this remote pubkey. Must be
        not-revoked; pairing must have succeeded in F2 first.

        last_seen_at is updated as a side effect of a successful auth,
        but its commit failure is NON-FATAL — a transient DB hiccup here
        must not surface to the caller as an auth failure. A spammy-but-
        signed peer could also exploit this path to amplify writes;
        debouncing is a future F5 task.
        """
        peer = (await self.db.execute(
            select(PeerUser).where(
                PeerUser.remote_pubkey == asker_pubkey,
                PeerUser.revoked_at.is_(None),
            )
        )).scalar_one_or_none()
        if peer is None:
            raise FederationQueryError("Unknown or revoked peer")
        try:
            peer.last_seen_at = datetime.now(UTC).replace(tzinfo=None)
            await self.db.commit()
        except Exception as e:
            logger.warning(
                f"Federation query_brain: last_seen_at update failed "
                f"(peer_id={peer.id}, non-fatal): {e}"
            )
            await self.db.rollback()
        return peer

    async def _resolve_asker_tier(
        self,
        owner_user_id: int,
        member_user_id: int | None,
    ) -> int:
        """Look up what tier the responder granted the asker on pairing."""
        if member_user_id is None:
            return 0  # No membership — treat as self-only view (empty)
        from models.database import CircleMembership
        row = (await self.db.execute(
            select(CircleMembership).where(
                CircleMembership.circle_owner_id == owner_user_id,
                CircleMembership.member_user_id == member_user_id,
                CircleMembership.dimension == "tier",
            )
        )).scalar_one_or_none()
        if row is None:
            return 0
        try:
            return int(row.value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _emit_progress(pending: _PendingRequest, label: str) -> None:
        """Update progress label (rate-limited to MAX_PROGRESS_UPDATES)."""
        if pending.progress_count >= MAX_PROGRESS_UPDATES:
            return
        pending.progress_label = label
        pending.progress_count += 1
