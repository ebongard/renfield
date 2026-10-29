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
from services.federation_identity import (
    FederationIdentity,
    get_federation_identity,
)
from services.federation_pending_store import (
    NONCE_WINDOW_SECONDS,
    _PendingRequest,
    get_pending_store,
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
    revocation. Bg tasks whose pending entry has vanished will find
    `pending is None` at the top of _run_query and return quietly —
    no cancellation needed.

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
    """Ein Pending-Satz, der sich nicht eindeutig deuten laesst."""


async def _write_responder_audit(
    pending: _PendingRequest,
    *,
    erfolg: bool,
    fehler: str | None = None,
    abgebrochen: bool = False,
) -> None:
    """Der ANTWORTENDE haelt fest, auf welcher Identitaet er geantwortet hat.

    🛑 Bis 2026-10-29 schrieb nur die Fragerseite (`mcp_client.py`, ein
    einziger Aufrufer). Die Entscheidung ueber die Identitaet faellt aber
    HIER — und damit war nirgends festgehalten, ob eine Antwort mit der vollen
    Reichweite der verknuepften Person oder nur auf der gewaehrten Stufe
    zustande kam. Beim ersten erfolgreichen foederierten Zug der
    Projektgeschichte liess sich das hinterher nicht mehr feststellen, nur
    erschliessen: der Pending-Satz, der `mapped` traegt, liegt im
    Arbeitsspeicher und ist nach dem Zug weg.

    🛑 Ein Vermerk auf der FRAGERSEITE waere kein Ersatz. Er waere die
    Behauptung der Gegenseite ueber ihre eigene Zugriffsentscheidung; die
    Instanz, deren Daten gelesen wurden, muss den Zugriff selbst verzeichnen.

    `user_id` bedeutet durchgehend „die lokale Person, deren Reichweite
    benutzt wurde": gemappt die verknuepfte Person, im Rueckfall der
    Eigentuemer der Paarung. Beide Faelle haben eine, darum bleibt die Spalte
    NOT NULL.

    Fehlschlaege beim Schreiben werden in `write_federation_audit`
    geschluckt — ein Protokolleintrag darf eine beantwortete Anfrage nicht
    nachtraeglich umwerfen.
    """
    from services.federation_audit import write_federation_audit

    user_id = pending.asker_local_user_id if pending.mapped else pending.peer_owner_id
    if user_id is None:
        # Weder gemappt noch mit Eigentuemer — ein Satz aus einer aelteren
        # Fassung, den `_peer_scope_for` ohnehin abweist. Nichts zu verzeichnen.
        return
    await write_federation_audit(
        user_id=user_id,
        peer_user_id=pending.peer_user_id,
        peer_pubkey_snapshot=pending.asker_pubkey,
        peer_display_name_snapshot=pending.peer_display_name,
        query_text=pending.query,
        initiated_at=datetime.fromtimestamp(pending.initiated_at, tz=UTC).replace(tzinfo=None),
        # 🛑 Die FORM zaehlt, nicht nur der Inhalt: `_classify_final` liest
        # `final.get("success")`. Ein Satz ohne diesen Schluessel ist falsy und
        # wuerde als „failed / Unknown failure" verzeichnet — eine geglueckte
        # Antwort als Fehlschlag. Und `None` bedeutet dort „abgebrochen", nicht
        # „fehlgeschlagen"; der echte Grund ginge verloren.
        final_item=(
            {"success": True, "message": pending.answer or ""}
            if erfolg
            # 🛑 `aborted` ist ein eigener Status, kein `failed`: ein Abbruch
            # NACH dem Abruf ist kein fehlgeschlagener Zugriff, sondern ein
            # geglueckter ohne Auslieferung. Unter den Fehlschlaegen begraben
            # saehe das Protokoll ruhiger aus, als es war.
            else {"success": False, "aborted": abgebrochen,
                  "message": fehler or "unbekannter Fehler"}
        ),
        direction="answered",
        identity_basis="mapped" if pending.mapped else "peer_tier",
    )


async def _abgebrochenen_zugriff_verzeichnen(pending: _PendingRequest) -> None:
    """Den Vermerk fuer einen abgebrochenen fremden Zugriff schreiben.

    Getrennte Funktion, damit der Abbruchzweig oben lesbar bleibt und der
    Schreibvorgang testbar ist, ohne eine Nebenlaeufigkeit nachzustellen.
    """
    if pending.retrieved_count is None:
        grund = "Abgebrochen vor dem Abruf — es wurden keine Daten gelesen."
    else:
        grund = (
            f"Abgebrochen nach dem Abruf — {pending.retrieved_count} Treffer "
            "gelesen, keine Antwort ausgeliefert."
        )
    try:
        # `shield`: ein ZWEITES cancel() beendet das Warten hier, nicht den
        # Schreibvorgang selbst. Gegen einen schliessenden Ereignisring hilft
        # nichts — dann gibt es keine Zeile, also genau das alte Verhalten.
        await asyncio.shield(
            _write_responder_audit(pending, erfolg=False, fehler=grund,
                                   abgebrochen=True)
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # pragma: no cover — darf den Abbruch nie ueberdecken
        logger.warning(f"Federation: Abbruch-Vermerk nicht geschrieben: {e}")


def _peer_scope_for(pending: _PendingRequest) -> PeerScope | None:
    """Die `PeerScope` zu einem Pending-Satz — oder ein Fehlschlag.

    🛑 Hier wird NICHT geraten. Ein Satz ist entweder gemappt (`mapped=True`,
    laeuft als die verknuepfte Person, kein Peer-Filter) oder ein Rueckfall
    (`mapped=False`, verlangt `peer_owner_id`). Alles andere ist
    widerspruechlich und wird abgewiesen.

    Der Fall, den das abfaengt: eine Zeile aus einer AELTEREN Fassung, die
    weder `mapped` noch `peer_owner_id` kennt. Beim Redis-Speicher ist das
    waehrend eines rollenden Deploys der Normalfall (siehe `_from_jsonable`).
    Sie traegt `asker_local_user_id = peer.remote_user_id` — die geliehene
    fremde Kennung, die F-ID-3 abgeschafft hat — und saehe ohne diese Pruefung
    aus wie ein gueltiger gemappter Satz. Unter Auth-aus faende sie dann sogar
    die Einzelnutzer-Abkuerzung.

    Lieber eine abgewiesene Anfrage als eine beantwortete mit falscher Reichweite.
    """
    if pending.mapped:
        return None
    if pending.peer_owner_id is None:
        raise PendingScopeError(
            f"Rueckfall ohne Eigentuemer (request_id={pending.request_id}) — "
            "vermutlich ein Satz aus einer aelteren Fassung; abgewiesen"
        )
    return PeerScope(owner_id=pending.peer_owner_id, tier=pending.max_visible_tier)


async def _record_nonce(nonce: str, now: float) -> bool:
    """
    Returns True iff the nonce is new. Delegates to the configured
    store so multi-worker deploys (Redis backend) get cross-worker
    replay defense.
    """
    return await get_pending_store().record_nonce(nonce, now)


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
            logger.error(f"Federation pending-store unavailable on initiate: {e}")
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

        if not await _record_nonce(req.nonce, now=now):
            raise FederationQueryError("Nonce already seen (replay detected)")

        # F5b — inbound rate limit keyed by asker_pubkey. Checked AFTER
        # signature + timestamp + nonce so an unauthenticated flood
        # can't probe the limiter state by pubkey. A valid signature
        # is required to land in the rate-limit bucket at all, so the
        # bucket's state is private to each authenticated peer.
        from services.federation_rate_limits import acquire_responder_token
        if not await acquire_responder_token(req.asker_pubkey):
            logger.warning(
                f"Federation responder rate limit hit for asker "
                f"{req.asker_pubkey[:12]}…"
            )
            raise FederationQueryError("Rate limit exceeded for this asker")

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
        if (
            settings.federation_identity_links_enabled
            and settings.auth_enabled
            and req.querier_ref
        ):
            from services.federation_identity_link import resolve_linked_user
            mapped_user_id = await resolve_linked_user(
                self.db, peer_id=peer.id, querier_ref=req.querier_ref,
            )

        if mapped_user_id is not None:
            # MAPPED — run as the local user with full circle semantics.
            asker_local_user_id = mapped_user_id
            max_visible_tier = 0          # im gemappten Pfad nicht gelesen
            peer_owner_id = None
            mapped = True
        else:
            # FALLBACK — peer-bezogen. Die Stufe kommt seit F-ID-3 aus der
            # PAARUNG (`peer_users.granted_tier`), nicht mehr aus einer
            # `CircleMembership`, die auf `peer.remote_user_id` keyte.
            #
            # 🛑 `asker_local_user_id` wird hier NICHT mehr gesetzt. Es stand
            # dort `peer.remote_user_id` — eine Zahl der GEGENSTELLE, die der
            # Filter als lokale Person las. Im peer-bezogenen Zweig gibt es
            # keine fragende lokale Person mehr, also auch keine Kennung, die
            # jemand verwechseln koennte.
            asker_local_user_id = None
            max_visible_tier = peer.granted_tier
            peer_owner_id = peer.circle_owner_id
            mapped = False

        request_id = str(uuid.uuid4())
        await _prune_expired(now=now)
        await get_pending_store().put(_PendingRequest(
            request_id=request_id,
            asker_pubkey=req.asker_pubkey,
            peer_user_id=peer.id,
            asker_local_user_id=asker_local_user_id,
            max_visible_tier=max_visible_tier,
            query=req.query,
            initiated_at=now,
            peer_owner_id=peer_owner_id,
            mapped=mapped,
            peer_display_name=peer.remote_display_name or "",
        ))

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
        """Verify poll signature + pubkey binding, return current state.

        F5c — same RedisError translation as handle_initiate.
        """
        try:
            return await self._handle_retrieve(req)
        except RedisError as e:
            logger.error(f"Federation pending-store unavailable on retrieve: {e}")
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
        store = get_pending_store()
        pending = await store.get(request_id)
        if pending is None:
            return

        try:
            # Transition: retrieving (chunk+KG+memory RRF).
            await self._emit_progress(pending, PROGRESS_LABEL_RETRIEVING)

            # Fresh session scoped to this bg task only.
            async with AsyncSessionLocal() as session:
                matches = await self._retrieve(session, pending)
            # 🛑 Ab hier lagen fremde Daten im Speicher. Wird der Zug jetzt
            # abgebrochen, ist das trotzdem ein Zugriff — und nur dieser
            # Vermerk unterscheidet ihn hinterher von einem Abbruch davor.
            pending.retrieved_count = len(matches)

            # Transition: synthesizing (Ollama call).
            await self._emit_progress(pending, PROGRESS_LABEL_SYNTHESIZING)
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
            await store.save(pending)
            await _write_responder_audit(pending, erfolg=True)
        except asyncio.CancelledError:
            # 🛑 Ein Abbruch ist kein Grund, den Zugriff zu verschweigen.
            #
            # Hier stand bis zum 2026-10-29 nur `raise`. Die FRAGERSEITE
            # schreibt ihren Vermerk seit F4d in einem `finally`, ausdruecklich
            # damit „cancellation, caller-side aclose(), or a consumer raising
            # mid-iteration still produces one audit row" — diese Seite kam
            # spaeter und hat es nicht uebernommen. Ausgerechnet sie
            # verzeichnet aber den FREMDEN Zugriff auf eigene Daten.
            #
            # Der Status ist NICHT `unknown`: diese Seite weiss, wie weit sie
            # gekommen ist. `retrieved_count` sagt, ob fremde Daten schon im
            # Speicher lagen.
            #
            # Dass der Schreibvorgang im Abbruchzweig selbst abgebrochen wird,
            # bleibt moeglich (ein zweites `cancel()`, oder der Ereignisring
            # schliesst gerade) — dann faellt es auf das alte Verhalten
            # zurueck, nie auf etwas Schlechteres. Darum `shield`: ein zweiter
            # Abbruch beendet das Warten, nicht den Schreibvorgang.
            await _abgebrochenen_zugriff_verzeichnen(pending)
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
            await store.save(pending)
            await _write_responder_audit(pending, erfolg=False, fehler=str(e))

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
            # FALLBACK: `peer` gesetzt → peer-bezogener Filter, unabhaengig von
            # `settings.auth_enabled`. Ohne ihn liefe eine auth-aus-Instanz in
            # die Einzelnutzer-Abkuerzung und gaebe ihr ganzes Gehirn preis.
            # Die Stufe stammt aus der Paarung, der Eigentuemer bindet sie an
            # dessen Zeilen — beides zusammen, sonst saehe der Peer seine Stufe
            # bei jedem Eigentuemer.
            # MAPPED (F-ID-1): `peer=None` → die Anfrage laeuft ALS die
            # verknuepfte Person mit deren voller Reichweite.
            peer=_peer_scope_for(pending),
            # 🛑 Der Pfad wird GESAGT, nicht aus `peer` geraten. Genau hier lag
            # ein Fehler: die Protokollzeile im Atom-Store leitete ihn aus
            # `peer is not None` ab — und `_peer_scope_for` gibt im GEMAPPTEN
            # Fall `None` zurueck. Damit blieb der eine Lauf unmarkiert, fuer
            # den die Markierung gebaut wurde: der kalte, auf dem die Sitzung
            # frisch ist und sieben von acht Quellen abbrechen.
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
                timeout=settings.federation_synthesis_timeout,  # constraint validated in config: must be <60 (responder TTL)
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
        # 🛑 Die Kennung einer Paarung ist `(circle_owner_id, remote_pubkey)` —
        # so steht es im Eindeutigkeitsindex `uq_peer_users_owner_pubkey`. Hier
        # wurde bis 2026-10-28 allein ueber den Schluessel gesucht, und zwar mit
        # `scalar_one_or_none()`. Zwei lokale Menschen, die sich mit DERSELBEN
        # Gegenstelle paaren, sind laut Index erlaubt — und liessen dann jede
        # foederierte Anfrage mit `MultipleResultsFound` als 500 enden. Im
        # Haushalt stand das am 2026-10-28 unmittelbar bevor: eine aktive Zeile
        # fuer Konto 1 und eine widerrufene fuer Konto 2, beide mit x-idras
        # Schluessel; ein erneutes Paaren haette die zweite wiederbelebt.
        candidates = (await self.db.execute(
            select(PeerUser).where(
                PeerUser.remote_pubkey == asker_pubkey,
                PeerUser.revoked_at.is_(None),
            ).order_by(PeerUser.id)
        )).scalars().all()
        if not candidates:
            raise FederationQueryError("Unknown or revoked peer")
        if len(candidates) > 1:
            # 🛑 ABWEISEN, nicht waehlen. Es gaebe einen naheliegenden Ausweg —
            # den `querier_ref` entscheiden lassen, welche Paarung gemeint ist.
            # Der kommt aber VON DER GEGENSTELLE. Ihn bestimmen zu lassen, in
            # wessen Kreis die Anfrage laeuft, waere dieselbe Klasse von Fehler,
            # die F-ID-3 gerade beseitigt hat: eine fremde Angabe entscheidet
            # ueber lokale Reichweite.
            logger.warning(
                f"Federation query_brain: {len(candidates)} aktive Paarungen mit "
                f"demselben Schluessel (Eigentuemer "
                f"{[k.circle_owner_id for k in candidates]}) — abgewiesen. "
                f"Genau eine davon darf aktiv sein."
            )
            raise FederationQueryError("Unknown or revoked peer")
        peer = candidates[0]
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

    # 🛑 `_resolve_asker_tier` ist am 2026-10-27 ENTFALLEN (F-ID-3). Sie suchte
    # eine `CircleMembership` ueber `member_user_id = peer.remote_user_id` — eine
    # fremde Zahl in einer Spalte mit Fremdschluessel auf die lokale `users.id` —
    # und tat das mit `scalar_one_or_none()`, das bei zwei Peers unter einem
    # Eigentuemer geworfen haette. Die Stufe kommt jetzt aus `peer.granted_tier`.

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
