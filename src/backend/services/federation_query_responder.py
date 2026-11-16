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
        │ verify peer_users row exists + not revoked
        │ refuse a nonce already seen (read-only peek, spends nothing)
        │ take a token from the pairing's rate bucket
        │ record the nonce (replay defence, the atomic claim)
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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import PeerUser
from services.atom_types import Provenance
from services.circle_sql import PeerScope
from services.database import AsyncSessionLocal
from services.federation_audit import (
    STAGE_AFTER_ANSWER,
    STAGE_AFTER_READ,
    STAGE_BEFORE_READ,
    outcome_note,
)
from services.federation_identity import (
    FederationIdentity,
    get_federation_identity,
)
from services.federation_instance_name import (
    remember_remote_instance_name,
    sign_name_attachment,
    stored_instance_name,
    verify_name_attachment,
)
from services.federation_pending_store import (
    NONCE_WINDOW_SECONDS,
    _PendingRequest,
    get_pending_store,
)
from services.federation_query_schemas import (
    PING_STATUS_OK,
    STATUS_COMPLETE,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    FederationProvenance,
    PeerPingRequest,
    PeerPingResponse,
    QueryBrainInitiateRequest,
    QueryBrainInitiateResponse,
    QueryBrainRetrieveRequest,
    QueryBrainRetrieveResponse,
    complete_canonical_payload,
    initiate_canonical_payload,
    ping_canonical_payload,
    pong_canonical_payload,
    retrieve_canonical_payload,
)
from services.mcp_streaming import (
    PROGRESS_LABEL_COMPLETE,
    PROGRESS_LABEL_FAILED,
    PROGRESS_LABEL_RETRIEVING,
    PROGRESS_LABEL_SYNTHESIZING,
)
from services.pairing_service import _canonical_bytes
from services.polymorphic_atom_store import SourceSessions
from utils.config import settings
from utils.log_safety import error_text_without_content

# Max progress transitions per request (traffic-analysis defence).
MAX_PROGRESS_UPDATES = 4

# One instance for the whole process: the bound on concurrent statements of
# the fan-outs (`SourceSessions`) only holds across retrievals that share it. The
# factory is looked up at call time, not bound here, so it is always the same
# name `_run_query` opens its own session from.
_SOURCE_SESSIONS = SourceSessions(lambda: AsyncSessionLocal())


# =============================================================================
# Errors (mapped to HTTP 400 uniformly by the route layer — no oracle)
# =============================================================================


class FederationQueryError(Exception):
    """Any signature/timestamp/nonce/peer lookup failure during query_brain."""


# =============================================================================
# In-memory state
# =============================================================================


# Strong references to in-flight background tasks. Without this set the
# asyncio GC can collect the task mid-run; with it, we also have somewhere
# to join on shutdown (F5 will add graceful-drain support).
_background_tasks: set[asyncio.Task] = set()


# `_PendingRequest` is now defined in services.federation_pending_store
# (shared by both the in-memory and Redis backends). Re-exported above
# for type-hint compatibility with code that imports it from here.


async def _clear_state_for_tests() -> None:
    """Test-only reset — every test starts with a clean view of the
    pending store + nonce cache, regardless of which backend is in use."""
    await get_pending_store().clear_for_tests()
    # Cancel any lingering bg tasks from a prior test so they don't
    # later write to the (now-cleared) store.
    for task in list(_background_tasks):
        task.cancel()
    _background_tasks.clear()


async def purge_requests_for_pubkey(asker_pubkey: str) -> int:
    """
    Drop every in-flight pending request bound to `asker_pubkey`.

    Called by `revoke_peer` so a revoked peer cannot poll /retrieve
    and receive an answer for a request they initiated before the
    revocation. A background task that is already running finds the
    record gone at its next check (`_refusal_for`) and
    cannot bring it back: `save` only updates an existing record.

    Returns count of discarded requests.
    """
    store = get_pending_store()
    pending = await store.list_for_pubkey(asker_pubkey)
    rids = [pr.request_id for pr in pending]
    await store.delete_many(rids)
    return len(rids)


async def _prune_expired(now: float | None = None) -> None:
    """Drop pending requests past TTL. Called on every initiate/retrieve.

    With the Redis backend, Redis EXPIRE handles eviction directly;
    this becomes a no-op there. The in-memory backend still does
    explicit pruning.
    """
    await get_pending_store().prune_expired(now=now)


class PendingScopeError(Exception):
    """A pending record that cannot be interpreted unambiguously."""


async def _write_responder_audit(
    pending: _PendingRequest,
    *,
    success: bool,
    error: str | None = None,
    aborted: bool = False,
) -> None:
    """The RESPONDER records which identity it answered on.

    🛑 Until 2026-10-29 only the asker side wrote (`mcp_client.py`, a single
    caller). But the identity decision is made HERE -- so nothing recorded
    whether an answer came about with the linked person's full reach or only
    on the granted tier. For the first successful federated query in this
    project that could no longer be established afterwards, only inferred:
    the pending record that carries `mapped` lives in memory and is gone once
    the query ends.

    🛑 A record on the ASKER side would be no substitute. It would be the far
    side's claim about its own access decision; the instance whose data was
    read has to record the access itself.

    `user_id` always means "the local person whose reach was used": mapped,
    the linked person; in the fallback, the owner of the pairing. Both cases
    have one, which is why the column stays NOT NULL.

    Write failures are swallowed in `write_federation_audit` -- an audit row
    must not retroactively overturn a request that was already answered.
    """
    from services.federation_audit import write_federation_audit

    user_id = pending.asker_local_user_id if pending.mapped else pending.peer_owner_id
    if user_id is None:
        # Neither mapped nor with an owner -- a record from an older version,
        # which `_peer_scope_for` rejects anyway. Nothing to record.
        return
    await write_federation_audit(
        user_id=user_id,
        peer_user_id=pending.peer_user_id,
        peer_pubkey_snapshot=pending.asker_pubkey,
        peer_display_name_snapshot=pending.peer_display_name,
        peer_instance_name_snapshot=pending.peer_instance_name or None,
        query_text=pending.query,
        initiated_at=datetime.fromtimestamp(pending.initiated_at, tz=UTC).replace(tzinfo=None),
        # 🛑 The SHAPE matters, not just the content: `_classify_final` reads
        # `final.get("success")`. A record without that key is falsy and would
        # be logged as "failed / Unknown failure" -- a successful answer as a
        # failure. And `None` there means "aborted", not "failed"; the real
        # reason would be lost.
        final_item=(
            {"success": True, "message": pending.answer or ""}
            if success
            # 🛑 `aborted` is a status of its own, not `failed`: a cancellation
            # AFTER the retrieval is not a failed access but a successful one
            # without delivery. Buried among the failures the log would look
            # calmer than it was.
            else {"success": False, "aborted": aborted,
                  "message": error or "unbekannter Fehler"}
        ),
        direction="answered",
        identity_basis="mapped" if pending.mapped else "peer_tier",
        request_id=pending.request_id,
    )


#: Why a request is no longer served. `_GONE` is all the code knows when only
#: the pending record is missing: purged by a revocation (the purge also hits
#: the other requests of the same far side) or lapsed -- it cannot tell which.
_REFUSED_GONE = "gone"
_REFUSED_PAIRING = "pairing_revoked"
_REFUSED_LINK = "link_revoked"
#: The background task was cancelled (shutdown, a test reset).
_CANCELLED = "cancelled"

#: The German sentence for each code -- presentation. The code itself leads
#: the stored text (`federation_audit.outcome_note`) and is what tests and
#: later readers of the log go by.
_OUTCOME_TEXT = {
    _REFUSED_GONE: "Anfrage nicht mehr vorhanden (verworfen oder abgelaufen)",
    _REFUSED_PAIRING: "Kopplung widerrufen",
    _REFUSED_LINK: "Verknüpfung besteht nicht mehr",
    _CANCELLED: "Abgebrochen",
}


def _unserved_note(code: str, pending: _PendingRequest) -> str:
    """The audit text for an access that stopped being served: the code, how
    far it had got, and the sentence saying the same.

    `retrieved_count` is the dividing line: `None` = nothing was read, a
    number = foreign data sat in memory, even though no answer went out.
    """
    what = _OUTCOME_TEXT[code]
    if pending.retrieved_count is None:
        return outcome_note(
            code, f"{what} vor dem Abruf — es wurden keine Daten gelesen.",
            stage=STAGE_BEFORE_READ)
    return outcome_note(
        code,
        f"{what} nach dem Abruf — {pending.retrieved_count} Treffer "
        "gelesen, keine Antwort ausgeliefert.",
        stage=STAGE_AFTER_READ)


def _undelivered_note(code: str, pending: _PendingRequest) -> str:
    """The audit text for an answer that was MADE and never handed over."""
    return outcome_note(
        code,
        f"{_OUTCOME_TEXT[code]} nach der Antwort — {pending.retrieved_count} "
        "Treffer gelesen, Antwort erzeugt, aber nicht ausgeliefert.",
        stage=STAGE_AFTER_ANSWER)


async def _record_undelivered_answer(
    pending: _PendingRequest, why: str, *, sole_writer: bool = False,
) -> None:
    """The answer of `pending` exists and will not be handed over: put that
    into the row that so far says it was answered.

    `sole_writer` (the background task, which is the only one working on its
    request): if there is no row to correct -- its write is best-effort --
    the access is recorded the way every other unserved one is; better a row
    that says "read, not delivered" than none. A POLL must not do that: two
    polls can be refused at the same moment, the second finds the row
    already corrected, and a fallback would add a second row.
    """
    from services.federation_audit import mark_answer_not_handed_over

    corrected = await mark_answer_not_handed_over(
        request_id=pending.request_id,
        error_message=_undelivered_note(why, pending),
    )
    if not corrected and sole_writer:
        await _record_refused_access(pending, why)


async def _record_cancelled_access(pending: _PendingRequest) -> None:
    """Write the record for a cancelled foreign access.

    A separate function so the cancellation branch above stays readable and
    the write is testable without staging a race.
    """
    reason = _unserved_note(_CANCELLED, pending)
    try:
        # `shield`: a SECOND cancel() ends the wait here, not the write
        # itself. Against a closing event loop nothing helps -- then there is
        # no row, i.e. exactly the old behaviour.
        await asyncio.shield(
            _write_responder_audit(pending, success=False, error=reason,
                                   aborted=True)
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # pragma: no cover -- must never mask the cancellation
        logger.warning(f"Federation: cancellation record not written: {error_text_without_content(e)}")


async def _record_refused_access(pending: _PendingRequest, why: str) -> None:
    """Write the record for a query that stopped being served under way.

    `aborted`, like a cancellation, and for the same reason: after the
    retrieval this is not a failed access but one that happened and was not
    delivered.

    The record says what the code KNOWS (`why`): a revoked pairing and a link
    that no longer stands were read from the database; a record that is merely
    gone is called gone, not revoked.
    """
    await _write_responder_audit(
        pending, success=False, error=_unserved_note(why, pending), aborted=True)


async def _pairing_is_active(db: AsyncSession, peer_user_id: int) -> bool:
    """Is the pairing a pending record was admitted on still not revoked?"""
    return (await db.execute(
        select(PeerUser.id).where(
            PeerUser.id == peer_user_id,
            PeerUser.revoked_at.is_(None),
        )
    )).scalar_one_or_none() is not None


async def _standing_refusal(db: AsyncSession, pending: _PendingRequest) -> str | None:
    """What the DATABASE says against serving `pending` -- or `None`.

    Both things a request was admitted on are asked again, each by its id:

    * the pairing is not revoked;
    * for a MAPPED request, the person link it was admitted on still stands
      (`link_still_lends`: the row exists, names the same account, and the
      account is active).

    🛑 A link that is gone REFUSES the request. It does not fall back to the
    pairing's tier: the far side asked as a person and was admitted as that
    person; answering the same request with another reach would be an answer
    nobody asked for, and the audit row would have to describe two identities.
    A new request from the far side takes the fallback at `initiate`, openly.

    A mapped record without `link_id` (a row from an older version) cannot be
    re-checked and is refused for the same reason.
    """
    if not await _pairing_is_active(db, pending.peer_user_id):
        return _REFUSED_PAIRING
    if pending.mapped:
        from services.federation_identity_link import link_still_lends

        if (
            pending.link_id is None
            or pending.asker_local_user_id is None
            or not await link_still_lends(
                db, link_id=pending.link_id, peer_id=pending.peer_user_id,
                local_user_id=pending.asker_local_user_id,
            )
        ):
            return _REFUSED_LINK
    return None


async def _refusal_for(pending: _PendingRequest) -> str | None:
    """Why the background task must stop working for this request -- or
    `None` while it may go on.

    Independent signals, and any one ends it:

    * the pairing is revoked, or the person link of a mapped request no
      longer stands, in the DATABASE (`_standing_refusal`). A revocation
      purges AFTER it commits, so for a moment the record is still there --
      and a record `put` after the purge (the `initiate` race: link resolved,
      link revoked and purged, record stored) is never purged at all. The
      database is asked every time, so neither order gets an answer;
    * the pending record is GONE -- purged or lapsed; `save` never brings one
      back.

    The database is asked even when the record is gone: it is what tells a
    revocation from a record that merely lapsed, and the audit row says which.

    Opens a short session of its own: the task has no request session.
    """
    store = get_pending_store()
    gone = await store.get(pending.request_id) is None
    async with AsyncSessionLocal() as session:
        why = await _standing_refusal(session, pending)
    if why is not None and not gone:
        await store.delete_many([pending.request_id])
    return why or (_REFUSED_GONE if gone else None)


def _peer_scope_for(pending: _PendingRequest) -> PeerScope | None:
    """The `PeerScope` for a pending record -- or a refusal.

    🛑 Nothing is GUESSED here. A record is either mapped (`mapped=True`, runs as
    the linked person, no peer filter) or a fallback (`mapped=False`, requires
    `peer_owner_id`). Anything else is contradictory and gets refused.

    The case this catches: a row from an OLDER version that knows neither
    `mapped` nor `peer_owner_id`. With the Redis store that is the normal case
    during a rolling deploy (see `_from_jsonable`). It carries
    `asker_local_user_id = peer.remote_user_id` -- the borrowed foreign id
    that F-ID-3 abolished -- and without this check would look like a valid
    mapped record. With auth off it would even hit the single-user shortcut.

    Better a refused request than an answered one with the wrong reach.
    """
    if pending.mapped:
        return None
    if pending.peer_owner_id is None:
        raise PendingScopeError(
            f"Fallback without an owner (request_id={pending.request_id}) -- "
            "probably a record from an older version; refused"
        )
    return PeerScope(owner_id=pending.peer_owner_id, tier=pending.max_visible_tier)


async def _record_nonce(nonce: str, now: float) -> bool:
    """
    Returns True iff the nonce is new. Delegates to the configured
    store so multi-worker deploys (Redis backend) get cross-worker
    replay defense.
    """
    return await get_pending_store().record_nonce(nonce, now)


async def _nonce_seen(nonce: str, now: float) -> bool:
    """Read-only: has this nonce been recorded already? Writes nothing."""
    return await get_pending_store().nonce_seen(nonce, now)


async def _admit_paired_envelope(
    *,
    asker_pubkey: str,
    nonce: str,
    now: float,
    acquire_token: Callable[[str], Awaitable[bool]],
    replay_error: str,
    rate_error: str,
) -> None:
    """Replay peek, rate bucket, nonce record -- for a PAIRED key only.

    🛑 Callers run this AFTER signature, timestamp and the pairing lookup, and
    both envelope kinds (query, ping) share it so the two halves cannot drift
    apart again. The order inside is the point:

      1. Peek (read-only). A replay of a recorded envelope is refused here,
         BEFORE the bucket: whoever can record a pairing's traffic could
         otherwise spend that pairing's budget with replays and starve its
         real requests. The peek writes nothing, so it opens no way to fill
         the capped nonce store, and it tells the caller nothing new: only an
         envelope that verified for a paired key gets this far, and the
         refusal is the same uniform one as every other.
      2. Bucket. Only a fresh nonce of a paired key spends a token.
      3. Record (the atomic claim). Only a paired key within its budget
         writes into the nonce store -- an unpaired key never does.

    What remains: two copies of the SAME fresh envelope racing each other can
    both pass the peek; the second then loses at the record and has spent one
    token. That needs the replay to arrive within the handling of the
    original and costs at most one token per original -- it cannot be
    repeated against a recorded envelope.
    """
    if await _nonce_seen(nonce, now=now):
        raise FederationQueryError(replay_error)
    if not await acquire_token(asker_pubkey):
        raise FederationQueryError(rate_error)
    if not await _record_nonce(nonce, now=now):
        raise FederationQueryError(replay_error)


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

        F5c — any `RedisError` raised by the pending store (when Redis
        backend is enabled and Redis is unreachable) is translated to
        `FederationQueryError("Federation store unavailable")` so the
        route layer returns the same uniform 400 as any other
        rejection — no storage-state oracle surfaced to the peer.
        """
        try:
            return await self._handle_initiate(req)
        except RedisError as e:
            logger.error(f"Federation pending-store unavailable on initiate: {error_text_without_content(e)}")
            raise FederationQueryError("Federation store unavailable") from e

    async def _handle_initiate(
        self,
        req: QueryBrainInitiateRequest,
    ) -> QueryBrainInitiateResponse:
        """Real initiate logic — separated so the outer wrapper can
        translate storage errors without re-indenting the body."""
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

        # 🛑 The pairing is looked up BEFORE anything is written. Until
        # 2026-11-14 the nonce was recorded and a rate bucket created first, so
        # a key nobody paired with, signing its own requests, could fill the
        # capped nonce store (pushing real peers' nonces out of it, and with
        # them the replay protection) and grow the bucket registry by one
        # entry per key. Same order as the ping now: signature, timestamp,
        # pairing, rate bucket, nonce. An unpaired request leaves nothing.
        #
        # `last_seen_at` is NOT moved here: it means "last real request", and
        # a replay or a request over budget is refused below. It is touched
        # once the request is admitted.
        peer = await self._lookup_peer(req.asker_pubkey, touch_last_seen=False)

        from services.federation_rate_limits import acquire_responder_token
        await _admit_paired_envelope(
            asker_pubkey=req.asker_pubkey, nonce=req.nonce, now=now,
            acquire_token=acquire_responder_token,
            replay_error="Nonce already seen (replay detected)",
            rate_error="Rate limit exceeded for this asker",
        )

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

        await self._touch_last_seen(peer)
        # The envelope verified and the key belongs to a non-revoked pairing:
        # only now may the name it carries be believed.
        learned_name = await self._learn_instance_name(
            peer,
            name=req.instance_name,
            signature_hex=req.instance_name_signature,
            envelope_signature_hex=req.signature,
        )

        # F-ID-1 (person-scoped federation): if the envelope names a person
        # (`querier_ref`) AND we have a link mapping it to a LOCAL user, serve
        # the query AS that user — full circle reach (their own atoms + grants +
        # memberships), NOT the peer-scoped public/guest fallback. Gated dark by
        # `federation_identity_links_enabled`; a stripped/absent ref, a missing
        # link, or a NULLed local user all fail closed to the fallback below.
        # Design: docs/design/federation-identity-mapping.md.
        # The mapped path (enforce_circles=False = run AS the local user) is only
        # meaningful — and only SAFE — when auth is ON. Under AUTH_ENABLED=false
        # there are no per-user circles to enforce, and enforce_circles=False
        # would hit the single-user `return ("TRUE", {})` bypass in every
        # retrieval module → the WHOLE brain (every member, every tier), not the
        # mapped user's reach. That is exactly the leak the fallback path (#957)
        # was built to prevent, so on an auth-off responder we refuse to map and
        # serve the peer-scoped fallback instead.
        mapped_user_id: int | None = None
        link_id: int | None = None
        if (
            settings.federation_identity_links_enabled
            and settings.auth_enabled
            and req.querier_ref
        ):
            from services.federation_identity_link import resolve_link
            link = await resolve_link(
                self.db, peer_id=peer.id, querier_ref=req.querier_ref,
            )
            if link is not None:
                link_id, mapped_user_id = link

        if mapped_user_id is not None:
            # MAPPED — run as the local user with full circle semantics.
            asker_local_user_id = mapped_user_id
            max_visible_tier = 0          # not read on the mapped path
            peer_owner_id = None
            mapped = True
        else:
            # FALLBACK -- peer-scoped. Since F-ID-3 the tier comes from the
            # PAIRING (`peer_users.granted_tier`), no longer from a
            # `CircleMembership` keyed on `peer.remote_user_id`.
            #
            # 🛑 `asker_local_user_id` is NO LONGER set here. It used to hold
            # `peer.remote_user_id` -- a number from the FAR SIDE that the
            # filter read as a local person. On the peer-scoped branch there is
            # no asking local person any more, so there is no id anyone could
            # confuse either.
            asker_local_user_id = None
            max_visible_tier = peer.granted_tier
            peer_owner_id = peer.circle_owner_id
            mapped = False

        request_id = str(uuid.uuid4())
        await _prune_expired(now=now)
        pending = _PendingRequest(
            request_id=request_id,
            asker_pubkey=req.asker_pubkey,
            peer_user_id=peer.id,
            asker_local_user_id=asker_local_user_id,
            max_visible_tier=max_visible_tier,
            query=req.query,
            initiated_at=now,
            peer_owner_id=peer_owner_id,
            mapped=mapped,
            # 🛑 The link is resolved above and the record stored below; a
            # revocation that commits AND purges in between finds no record
            # to purge. That window is closed by the background task, which
            # asks the database for THIS link before it reads anything
            # (`_refusal_for`), and by the poll, which asks again.
            link_id=link_id if mapped else None,
            peer_display_name=peer.remote_display_name or "",
            # 🛑 A verified name or nothing -- never the endpoint host: this
            # is what the audit row of the access stores for good.
            peer_instance_name=learned_name or stored_instance_name(peer) or "",
        )
        await get_pending_store().put(pending)

        # Schedule the background work. The task is fire-and-forget —
        # asker polls via handle_retrieve. We keep a strong reference
        # in `_background_tasks` so asyncio's GC doesn't collect it
        # mid-run (add_done_callback removes the ref on completion).
        task = asyncio.create_task(self._run_query(request_id, pending))
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
        """Verify poll signature + pubkey binding, return current state.

        F5c — same RedisError translation as handle_initiate.
        """
        try:
            return await self._handle_retrieve(req)
        except RedisError as e:
            logger.error(f"Federation pending-store unavailable on retrieve: {error_text_without_content(e)}")
            raise FederationQueryError("Federation store unavailable") from e

    async def _handle_retrieve(
        self,
        req: QueryBrainRetrieveRequest,
    ) -> QueryBrainRetrieveResponse:
        """Real retrieve logic — see `_handle_initiate` for the split."""
        self._verify_signature(
            pubkey_hex=req.asker_pubkey,
            signature_hex=req.signature,
            payload=retrieve_canonical_payload(req),
        )

        now = time.time()
        if abs(now - req.timestamp) > NONCE_WINDOW_SECONDS:
            raise FederationQueryError("Poll timestamp outside window")

        await _prune_expired(now=now)
        pending = await get_pending_store().get(req.request_id)
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

        # 🛑 The pairing is looked up AGAIN, and so is the person link of a
        # mapped request. Until 2026-11-15 only `initiate` checked either, and
        # the purge on revocation was the sole protection of the poll -- a
        # record stored after the purge, or brought back by the background
        # task, was handed out to a revoked key. The refusal is the one an
        # unknown request gets: nothing tells the far side that there was a
        # request and a revocation.
        why = await _standing_refusal(self.db, pending)
        if why is not None:
            await get_pending_store().delete_many([pending.request_id])
            logger.warning(
                f"Federation query_brain: poll refused ({why}) "
                f"(request_id={req.request_id}, peer_id={pending.peer_user_id})"
            )
            # 🛑 An answer that was ready and is refused HERE stood in the
            # audit log as delivered: its row was written when the answer was
            # made. The row is corrected -- once: the record is deleted
            # above, so the far side's next poll (it polls twice a second)
            # finds nothing and writes nothing, and the correction itself
            # only matches a row that still says `success`. An answer that
            # had already been handed over stays recorded as delivered,
            # because it was. A request still PROCESSING is recorded by its
            # background task at the task's next check.
            if pending.status == STATUS_COMPLETE and pending.handed_over_at is None:
                await _record_undelivered_answer(pending, why)
            return QueryBrainRetrieveResponse(status=STATUS_EXPIRED)

        # The first hand-over of a finished answer is noted on the pending
        # record (a store update, no database write): it is what a later
        # refused poll needs to know.
        if pending.status == STATUS_COMPLETE and pending.handed_over_at is None:
            pending.handed_over_at = now
            await get_pending_store().save(pending)

        return self._serialize_pending(pending)

    # -------------------------------------------------------------------------
    # Background work
    # -------------------------------------------------------------------------

    async def _run_query(
        self, request_id: str, pending: _PendingRequest | None = None,
    ) -> None:
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
        store = get_pending_store()
        # `handle_initiate` hands the record over. A task that only knew the
        # id could not even RECORD a request that was purged while it waited
        # to start -- the record is the only place its identity is kept.
        if pending is None:
            pending = await store.get(request_id)
        if pending is None:
            return

        try:
            # 🛑 A revoked pairing or person link gets no answer -- and no
            # retrieval either, where that can still be avoided. The task may have waited (event
            # loop, model server) since `initiate` admitted the request; the
            # check at its start alone left everything after it unguarded.
            if why := await _refusal_for(pending):
                await _record_refused_access(pending, why)
                return

            # Transition: retrieving (chunk+KG+memory RRF).
            await self._emit_progress(pending, PROGRESS_LABEL_RETRIEVING)

            # Fresh session scoped to this bg task only.
            #
            # 🛑 ONE deadline over the whole step. Each source of the fan-out
            # has its own, but graph expansion runs after it on this session
            # with none, and the far side stops polling when the request
            # window closes. Over the deadline the query FAILS (a terminal
            # status the far side sees) instead of being answered into a
            # window that has closed; the session is closed on the way out.
            async with AsyncSessionLocal() as session:
                async with asyncio.timeout(settings.federation_retrieve_deadline):
                    matches = await self._retrieve(session, pending)
            # 🛑 From here on foreign data sat in memory. If the query is
            # cancelled now that is still an access -- and only this record
            # tells it apart afterwards from a cancellation before it.
            pending.retrieved_count = len(matches)

            # A revocation during the retrieval: the model is not handed what
            # was read, and its call (up to the synthesis timeout) is saved.
            if why := await _refusal_for(pending):
                await _record_refused_access(pending, why)
                return

            # Transition: synthesizing (Ollama call).
            await self._emit_progress(pending, PROGRESS_LABEL_SYNTHESIZING)
            answer = await self._synthesize(pending.query, matches)

            # Retrieval and synthesis take seconds; a revocation in that time
            # must not be answered. Checked BEFORE the result is stored.
            if why := await _refusal_for(pending):
                await _record_refused_access(pending, why)
                return

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
            # 🛑 The row is written BEFORE the answer becomes collectable.
            # The other order left a moment in which a poll could be refused
            # (and find no row to correct) while this task then wrote
            # `success` for an answer nobody got.
            await _write_responder_audit(pending, success=True)
            if not await store.save(pending):
                # Gone between the check above and this line: `save` does
                # not create, so nothing can be polled. The row just written
                # is corrected: an answer was made, not handed over.
                await _record_undelivered_answer(
                    pending, _REFUSED_GONE, sole_writer=True)
                return
        except asyncio.CancelledError:
            # 🛑 A cancellation is no reason to keep the access quiet.
            #
            # Until 2026-10-29 this was just `raise`. The ASKER side has
            # written its record inside a `finally` since F4d, explicitly so
            # that "cancellation, caller-side aclose(), or a consumer raising
            # mid-iteration still produces one audit row" -- this side came
            # later and did not adopt it. And this is the side that records a
            # FOREIGN access to one's own data.
            #
            # The status is NOT `unknown`: this side knows how far it got.
            # `retrieved_count` says whether foreign data was already in
            # memory.
            #
            # The write inside the cancellation branch can itself be cancelled
            # (a second `cancel()`, or the event loop closing) -- then it falls
            # back to the old behaviour, never to anything worse. Hence
            # `shield`: a second cancellation ends the wait, not the write.
            await _record_cancelled_access(pending)
            raise
        except Exception as e:
            logger.error(
                f"Federation query_brain: background work failed "
                f"(request_id={request_id}): {error_text_without_content(e)}"
            )
            # 🛑 The text goes into the pending record AND, for good, into the
            # audit row. A database error here (graph expansion runs on this
            # task's session) would carry `[SQL: ...] [parameters: ...]` --
            # the far side's question and local rows -- so it is cut like
            # every log line. The far side never sees it: a FAILED poll
            # carries no message.
            reason = f"{type(e).__name__}: {error_text_without_content(e)}"
            pending.error_message = reason
            # Fill in answered_at on failure too so _serialize_pending's
            # `int(pending.answered_at or time.time())` reports the
            # actual failure moment instead of the poll moment.
            pending.answered_at = time.time()
            pending.progress_label = PROGRESS_LABEL_FAILED
            pending.status = STATUS_FAILED
            await store.save(pending)
            await _write_responder_audit(pending, success=False, error=reason)

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

        # 🛑 `session` is FRESH (see `_run_query`), and the store fans out at
        # once. Eight sources on one cold session is the defect that ran live
        # until 2026-11-15: all of them try to provision the same connection,
        # one wins, the rest end in `InvalidRequestError` -- "2/8 sources
        # delivered, 3 crashed" on every inbound query. So no source touches
        # `session`: each statement of each source runs on a session of its
        # own. `session` itself serves only what runs after the fan-out
        # (graph expansion).
        store = PolymorphicAtomStore(session, source_sessions=_SOURCE_SESSIONS)
        return await store.query(
            query_text=pending.query,
            asker_id=pending.asker_local_user_id or 0,
            max_visible_tier=pending.max_visible_tier,
            top_k=10,
            # FALLBACK: `peer` set -> peer-scoped filter, independent of
            # `settings.auth_enabled`. Without it an auth-off instance would
            # hit the single-user shortcut and give away its whole brain.
            # The tier comes from the pairing, the owner binds it to that
            # owner's rows -- both together, or the peer would see its tier
            # across every owner.
            # MAPPED (F-ID-1): `peer=None` -> the request runs AS the linked
            # person with that person's full reach.
            peer=_peer_scope_for(pending),
            # 🛑 The path is STATED, not guessed from `peer`. There was a bug
            # exactly here: the log line in the atom store derived it from
            # `peer is not None` -- and `_peer_scope_for` returns `None` in the
            # MAPPED case. So the one run the marker was built for stayed
            # unmarked: the cold one, where the session is fresh and seven of
            # eight sources break off.
            federated=True,
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
                timeout=settings.federation_synthesis_timeout,  # fits the request window together with the retrieval: validated in config
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
            self._attach_instance_name(resp)
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
        self._attach_instance_name(resp)
        return resp

    def _sign_response(self, resp: QueryBrainRetrieveResponse) -> str:
        payload = complete_canonical_payload(resp)
        return self.identity.sign(_canonical_bytes(payload)).hex()

    def _attach_instance_name(
        self, resp: QueryBrainRetrieveResponse | PeerPingResponse,
    ) -> None:
        """Add OUR instance name to a response we just signed.

        An attachment bound to `responder_signature`, outside the signed
        payload -- an asker one release behind drops both fields and verifies
        the answer as before (see services/federation_instance_name.py).
        """
        attachment = sign_name_attachment(self.identity, resp.responder_signature or "")
        if attachment:
            resp.instance_name = attachment["instance_name"]
            resp.instance_name_signature = attachment["instance_name_signature"]

    # -------------------------------------------------------------------------
    # Ping -- the reachability round trip
    # -------------------------------------------------------------------------

    async def handle_ping(self, req: PeerPingRequest) -> PeerPingResponse:
        """Answer a paired peer's "are you there, and do you still know me?".

        Checked in this order: signature, ±60s window, non-revoked pairing,
        then `_admit_paired_envelope` (replay peek, rate limit, nonce record)
        -- and refused at any of them with the same
        `FederationQueryError`, which the route turns into the same uniform
        400. An unpaired or revoked caller therefore learns nothing here that
        a refused query would not tell it.

        🛑 The nonce is recorded LAST, after the pairing and the bucket. The
        nonce store is capped; a key nobody paired with, signing its own pings,
        could otherwise fill it and push the nonces of real peers out of it
        (and with them the replay protection). An unpaired ping leaves no state
        behind at all: no nonce, no bucket, no name, no row.

        🛑 What a ping does NOT do, each on purpose:
          - It does not move `last_seen_at`. That column means "the far side
            last sent us a real REQUEST"; a wall display that pings every few
            minutes would turn it into "the far side's kiosk is switched on".
          - It writes no audit row and no pending record. The audit view lists
            accesses to this instance's data, and a ping reads none.
          - It spends no query token: pings have a bucket of their own, so a
            pinging peer cannot starve its own queries (or the reverse).
        """
        try:
            return await self._handle_ping(req)
        except RedisError as e:
            logger.error(f"Federation pending-store unavailable on ping: {error_text_without_content(e)}")
            raise FederationQueryError("Federation store unavailable") from e

    async def _handle_ping(self, req: PeerPingRequest) -> PeerPingResponse:
        self._verify_signature(
            pubkey_hex=req.asker_pubkey,
            signature_hex=req.signature,
            payload=ping_canonical_payload(req),
        )

        now = time.time()
        if abs(now - req.timestamp) > NONCE_WINDOW_SECONDS:
            raise FederationQueryError("Ping timestamp outside ±60s window")

        peer = await self._lookup_peer(req.asker_pubkey, touch_last_seen=False)

        # Only a paired key gets a bucket or writes a nonce: a stranger with a
        # fresh key per request is refused above and leaves nothing behind.
        from services.federation_rate_limits import acquire_ping_responder_token
        await _admit_paired_envelope(
            asker_pubkey=req.asker_pubkey, nonce=req.nonce, now=now,
            acquire_token=acquire_ping_responder_token,
            replay_error="Ping nonce already seen (replay detected)",
            rate_error="Ping rate limit exceeded for this asker",
        )

        await self._learn_instance_name(
            peer,
            name=req.instance_name,
            signature_hex=req.instance_name_signature,
            envelope_signature_hex=req.signature,
        )

        resp = PeerPingResponse(
            status=PING_STATUS_OK,
            nonce=req.nonce,
            answered_at=int(now),
            responder_pubkey=self.identity.public_key_hex(),
        )
        resp.responder_signature = self.identity.sign(
            _canonical_bytes(pong_canonical_payload(resp))
        ).hex()
        self._attach_instance_name(resp)
        return resp

    async def _learn_instance_name(
        self,
        peer: PeerUser,
        *,
        name: str | None,
        signature_hex: str | None,
        envelope_signature_hex: str,
    ) -> str | None:
        """Verify the name attachment of an authenticated envelope and store it.

        🛑 Callers pass a `peer` that `_lookup_peer` returned for the envelope's
        key -- i.e. the envelope signature verified AND the key is paired and
        not revoked. That order is the whole security property: the name of an
        unknown or revoked key is never looked at, let alone stored.

        Returns the verified name (stored or already known), else `None`.
        """
        verified = verify_name_attachment(
            name=name,
            signature_hex=signature_hex,
            signer_pubkey_hex=peer.remote_pubkey,
            envelope_signature_hex=envelope_signature_hex,
        )
        if verified is None:
            return None
        current = stored_instance_name(peer)
        if verified != current:
            await remember_remote_instance_name(peer.id, verified, current=current)
        return verified

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

    async def _lookup_peer(
        self, asker_pubkey: str, *, touch_last_seen: bool = True,
    ) -> PeerUser:
        """Find the PeerUser row that matches this remote pubkey. Must be
        not-revoked; pairing must have succeeded in F2 first.

        `touch_last_seen=False` is for the ping: `last_seen_at` records the
        far side's last real REQUEST, and a ping is not one.

        last_seen_at is updated as a side effect of a successful auth,
        but its commit failure is NON-FATAL — a transient DB hiccup here
        must not surface to the caller as an auth failure. A spammy-but-
        signed peer could also exploit this path to amplify writes;
        debouncing is a future F5 task.
        """
        # 🛑 A pairing is identified by `(circle_owner_id, remote_pubkey)` --
        # that is how the unique index `uq_peer_users_owner_pubkey` states it.
        # Here it was looked up by the key alone until 2026-10-28, and with
        # `scalar_one_or_none()`. Two local people pairing with the SAME far
        # side are permitted by the index -- and would then make every
        # federated request end as a 500 with `MultipleResultsFound`. The
        # household was one step away from this on 2026-10-28: an active
        # row for account 1 and a revoked one for account 2, both with
        # x-idra's key; pairing again would have revived the second.
        candidates = (await self.db.execute(
            select(PeerUser).where(
                PeerUser.remote_pubkey == asker_pubkey,
                PeerUser.revoked_at.is_(None),
            ).order_by(PeerUser.id)
        )).scalars().all()
        if not candidates:
            raise FederationQueryError("Unknown or revoked peer")
        if len(candidates) > 1:
            # 🛑 REFUSE, do not pick. There is an obvious way out -- let the
            # `querier_ref` decide which pairing is meant. But it comes FROM
            # THE FAR SIDE. Letting it determine whose circle the request runs
            # in would be the same class of bug F-ID-3 just removed: a
            # foreign value deciding local reach.
            logger.warning(
                f"Federation query_brain: {len(candidates)} active pairings with "
                f"the same key (owners "
                f"{[k.circle_owner_id for k in candidates]}) -- refused. "
                f"Exactly one of them may be active."
            )
            raise FederationQueryError("Unknown or revoked peer")
        peer = candidates[0]
        if touch_last_seen:
            await self._touch_last_seen(peer)
        return peer

    async def _touch_last_seen(self, peer: PeerUser) -> None:
        """Record "the far side just sent a real request". Never fatal."""
        try:
            peer.last_seen_at = datetime.now(UTC).replace(tzinfo=None)
            await self.db.commit()
        except Exception as e:
            logger.warning(
                f"Federation query_brain: last_seen_at update failed "
                f"(peer_id={peer.id}, non-fatal): {error_text_without_content(e)}"
            )
            await self.db.rollback()

    # 🛑 `_resolve_asker_tier` was REMOVED on 2026-10-27 (F-ID-3). It looked up a
    # `CircleMembership` via `member_user_id = peer.remote_user_id` -- a foreign
    # number in a column with a foreign key onto the local `users.id` -- and did
    # so with `scalar_one_or_none()`, which would have raised for two peers
    # under one owner. The tier now comes from `peer.granted_tier`.

    @staticmethod
    async def _emit_progress(pending: _PendingRequest, label: str) -> None:
        """Update progress label (rate-limited to MAX_PROGRESS_UPDATES).
        Writes through to the pending store so other workers polling
        /retrieve see the new label.

        Single-writer invariant: only the bg `_run_query` task that
        owns this request mutates `pending`. Other workers READ via
        `store.get()` but never call this. If a future "admin cancel"
        path needs to mutate from a non-owner worker, it must use a
        store-level CAS rather than the in-memory mutate-then-save
        sequence here.
        """
        if pending.progress_count >= MAX_PROGRESS_UPDATES:
            return
        pending.progress_label = label
        pending.progress_count += 1
        await get_pending_store().save(pending)
