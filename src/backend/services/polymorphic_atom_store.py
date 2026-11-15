"""
PolymorphicAtomStore — v1 implementation of the AtomStore Protocol.

Wraps the three Lane-A retrieval modules (RAGRetrieval, KGRetrieval,
MemoryRetrieval) and merges their results via reciprocal rank fusion (RRF).
Each retrieval module is responsible for its own circle-tier filter pushdown
to SQL (added in Lane C alongside the legacy-consumer rewrite).

ASCII query flow:

    PolymorphicAtomStore.query(text, asker_id, max_visible_tier, top_k=20)
        |
        +-- Build per-asker AccessContext (dimensions + memberships) once.
        |
        +-- Fan-out over EIGHT sources (asyncio.gather):
        |     +-- RAGRetrieval(s).search(text, top_k=top_k*3) -> kb_chunks
        |     +-- KGRetrieval(s).get_relevant_atoms(text, asker)
        |     +-- MemoryRetrieval(s).retrieve(text, asker_id)
        |     +-- LexicalRetrieval(s).search_chunks_lexical / _memories_lexical
        |     +-- SkillService(s).find_similar          (flag-gated)
        |     +-- DocumentFactRetrieval(s).search
        |     +-- NoteRetrieval(s).search               (flag-gated)
        |
        |   WHICH SESSION `s` IS decides whether this is parallel. An
        |   `AsyncSession` holds ONE connection; concurrent queries on it are
        |   not supported by SQLAlchemy. Measured against real Postgres with
        |   the project's own session factory, eight queries at ~50 ms PURE
        |   SQL time, three runs, stable (`bin/probe-atom-store-sessions.py`):
        |
        |       A) one session, COLD           1 of 8 delivered     52 ms
        |       B) one session, pre-warmed     8 of 8              408 ms  (serial)
        |       C) one session per source      8 of 8               56 ms
        |
        |   🛑 These numbers are NOT the duration in the log line. The line times
        |   the whole fan-out, embedding calls to Ollama/llama-server included,
        |   and those dominate it. The probe isolates SQL time, the line does
        |   not.
        |
        |   There are two regimes, chosen by the CALLER through the
        |   constructor:
        |
        |   * `source_sessions=None` -- every source runs on `self.db`. That
        |     is regime B and only correct on a WARM session. Warm means: a
        |     transaction is OPEN, so the connection is already there -- a
        |     commit or rollback makes the session cold again. The one caller
        |     is `GET /api/atoms`: its auth dependency has queried the same
        |     request session before the route body runs, without committing
        |     (`get_current_user` / `get_user_or_default`). A caller with a
        |     fresh session MUST NOT use this regime -- that is regime A, and
        |     it is what the federated path did until 2026-11-15 (live: "2/8
        |     sources delivered, 3 crashed" on every inbound query).
        |     Pre-warming is not the fix: it removes the error message by
        |     removing the concurrency.
        |
        |   * `source_sessions=SourceSessions(...)` -- no source touches
        |     `self.db` (regime C). The federated responder uses this. Each
        |     source is handed a `SourceReader`; every statement on it runs
        |     on a session of its own that lives exactly as long as the
        |     statement. While a source waits for a model server it holds
        |     nothing. At most `SOURCE_SESSION_LIMIT` statements -- and so
        |     connections -- are on the database at any time, however many
        |     fan-outs run; see `SourceSessions`. Every source also has a
        |     deadline there (`atom_fanout_source_timeout`).
        |
        |   A source in regime C has NO caller transaction to lean on, so
        |   each call tree was checked for writes (2026-11-15):
        |
        |     rag, kg, lexical_chunks, lexical_memories, document_fact, note
        |         read only; return plain dicts, no ORM instances.
        |     memory
        |         `retrieve` bumps `access_count` and only `flush()`es -- "the
        |         caller's transaction decides". Neither caller of this store
        |         commits, so in the fan-out the bump was never durable by
        |         design; it only held row locks until the session closed. In
        |         regime C it is switched off (`track_access=False`): the
        |         fan-out is a PURE READ there, no row locks across sessions,
        |         and a peer's question does not shift the recency ranking of
        |         local memories.
        |     skill
        |         `find_similar` writes the shadow log and COMMITS on its
        |         session (`skill_shadow_log_enabled`, default on). On a shared
        |         session that commit also decides for whatever the other
        |         sources have flushed. In regime C it is switched off
        |         (`record_shadow_log=False`): a paired instance's question
        |         is not a local person's turn and must not write curator
        |         rows under a local account.
        |
        |   With both switched off the fan-out in regime C is a pure read,
        |   and `SourceReader` enforces it: it has no `add`, `flush` or
        |   `commit`. None of the sources uses SET LOCAL, an advisory lock
        |   or a savepoint.
        |   Graph expansion runs AFTER the fan-out, alone, on `self.db`.
        |
        |   🛑 How many sources CAN report a failure is not the same as eight.
        |   `lexical_chunks`, `lexical_memories` and `note` re-raise to the
        |   fan-out (since 2026-10-29; they have no caller outside it), and so
        |   do `rag` and `skill`. `memory`, `document_fact` and `kg` catch
        |   some failures THEMSELVES and return empty -- they show up in the
        |   line as "empty", not as "crashed". Those are the three with
        |   external callers, and converting them is an open decision. Anyone
        |   reading the number off that line needs to know this.
        |
        +-- Wrap each source result list as AtomMatch[] with rank assigned.
        |
        +-- RRF merge across the eight sources:
        |     score = sum(weight / (k + rank + 1))
        |     where k = settings.rag_hybrid_rrf_k (default 60)
        |
        +-- Truncate to top_k, return AtomMatch[].

Per CEO Tension A acceptance, this is the v1 default; v3 KG-as-brain swaps
in KGAtomStore against the same Protocol without touching this module's
consumers.

NOT IN SCOPE for v1 PolymorphicAtomStore:
- Cross-source ranking is rank-only RRF (per Open Q 9 — eng-review accepted
  this trade-off rather than normalizing heterogeneous score scales)
- get_atom / upsert_atom / update_tier / soft_delete delegate to AtomService
  (PolymorphicAtomStore is primarily a query-side router)
"""
from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    ATOM_TYPE_CONVERSATION_MEMORY,
    ATOM_TYPE_KB_DOCUMENT,
    ATOM_TYPE_KG_EDGE,
    ATOM_TYPE_KG_NODE,
)
from services.atom_service import AtomService
from services.atom_types import Atom, AtomMatch
from services.circle_resolver import CircleResolver
from services.circle_sql import PeerScope
from utils.config import settings
from utils.log_safety import error_text_without_content

#: How many statements of read-only fan-outs may be on the database at the
#: same time in this process -- and with that, how many connections all of
#: them hold together. Eight = one fan-out runs its SQL fully parallel; a
#: second one arriving meanwhile shares the same eight slots instead of taking
#: eight more.
SOURCE_SESSION_LIMIT = 8


class SourceSessions:
    """Database access for the sources of a read-only fan-out -- bounded.

    One instance per process and session factory (the federated responder
    keeps one at module level); the bound only holds across fan-outs that
    share the instance.

    🛑 THE SLOT COVERS ONE STATEMENT, NOT ONE SOURCE. A source is handed a
    `SourceReader`, and every `execute` on it takes a slot, opens a session,
    runs the statement, buffers the result, closes the session and gives the
    slot back. Between two statements a source holds NOTHING -- no slot, no
    session, no connection, no open transaction.

    That is the point, because the sources do not only query. All of them
    but the two lexical ones and `document_fact` call a model server in
    between: `kg` asks an LLM for entity names BEFORE its first statement
    (read timeout 300 s) and embeds between statements, `rag` reranks
    between its search and its parent lookup, `memory`, `skill` and `note`
    embed first. The first version of this class held the slot for the whole
    source call. Eight sources waiting on a busy model server then held all
    eight slots without one connection among them, and every SQL source of
    every other inbound query queued behind them -- one slow model call
    delayed everybody, where before the change it delayed its own query.
    A deadline would only have bounded that; this removes it. It needs no
    knowledge of where a source happens to call its model, and a source
    that grows a new model call cannot bring the coupling back.

    Why one statement per session is no loss: the fan-out is a pure read,
    and Postgres' READ COMMITTED gives every statement a snapshot of its own
    anyway -- two statements of one source never shared a view of the data.
    No source uses SET LOCAL, an advisory lock, a savepoint or a temp table
    (checked 2026-11-15), so nothing lives on the connection between them.

    Pool arithmetic (`services/database.py`, `utils/config.py`): a backend
    process owns `db_pool_size + db_max_overflow` = 10 + 20 = 30 connections
    by default. Checked on 2026-11-15: the manifests in this repository's
    `k8s/` override neither, nor `max_connections` (Postgres default 100,
    shared with the workers' own pools); the second instance's manifests
    live in another repository and were not checked. All read-only fan-outs
    TOGETHER hold at most `limit` connections -- 8 of 30 -- whatever the
    number of inbound queries, and only while a statement runs. The price
    is a pool checkout per statement (pre-ping and the checkin hook
    included) instead of one per source.
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        limit: int = SOURCE_SESSION_LIMIT,
    ):
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._factory = factory
        self._limit = limit
        # An `asyncio.Semaphore` belongs to the event loop it first waited on,
        # and this object lives as long as the process. One semaphore per
        # loop keeps it usable from any loop that comes later; keyed weakly so
        # a closed loop frees its entry.
        self._slots: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Semaphore
        ] = weakref.WeakKeyDictionary()

    def _slots_for_this_loop(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        slots = self._slots.get(loop)
        if slots is None:
            slots = self._slots[loop] = asyncio.Semaphore(self._limit)
        return slots

    def reader(self) -> SourceReader:
        """What ONE source is handed in place of a session."""
        return SourceReader(self)

    @property
    def bind(self) -> Any:
        """The engine behind the factory -- the sources ask it for the
        dialect. A session that is never used takes no connection."""
        return getattr(self._factory(), "bind", None)

    async def execute(self, statement: Any, params: Any = None, **kwargs: Any) -> Any:
        """Run ONE statement inside a slot, on a session that lives exactly
        as long as the statement.

        The session is closed on return, error and cancellation alike
        (`async with`), and the slot is released after it. The result is
        frozen first: its rows are in memory, so it stays readable after the
        connection has gone back to the pool.
        """
        async with self._slots_for_this_loop():
            async with self._factory() as session:
                result = await session.execute(statement, params, **kwargs)
                return result.freeze()()


class SourceReader:
    """The read-only stand-in for a session that a source of a fan-out on
    `SourceSessions` works with.

    It has `execute` and `bind` and NOTHING ELSE -- no `add`, no `flush`, no
    `commit`. That is deliberate: the fan-out on own sessions is a pure
    read, and a source that tries to write there fails loudly (it shows up
    as crashed in the summary line) instead of writing on a connection
    nobody decides about. A source that has a write needs an explicit
    switch for it, as `track_access` and `record_shadow_log` are.
    """

    def __init__(self, sessions: SourceSessions):
        self._sessions = sessions

    @property
    def bind(self) -> Any:
        return self._sessions.bind

    async def execute(self, statement: Any, params: Any = None, **kwargs: Any) -> Any:
        return await self._sessions.execute(statement, params, **kwargs)


class PolymorphicAtomStore:
    """v1 AtomStore implementation. Fans out to the Lane-A retrieval modules."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        source_sessions: SourceSessions | None = None,
    ):
        """``source_sessions`` selects the fan-out regime (module docstring).

        ``None``: all sources share ``db`` -- only for a caller whose session
        has already run a statement. A caller that opens a fresh session and
        queries at once passes a ``SourceSessions``.
        """
        self.db = db
        self.resolver = CircleResolver(db)
        self.atom_service = AtomService(db, self.resolver)
        self._source_sessions = source_sessions

    async def _run_source(
        self, call: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        """Run one source of the fan-out on what its regime gives it.

        On own sessions every source also gets a DEADLINE
        (`atom_fanout_source_timeout`). A slow model server holds no slot
        (see `SourceSessions`), but it would still hold THIS fan-out: the kg
        source alone can wait 300 s for its LLM, long after the far side
        stopped polling. A source over its deadline is cancelled and counted
        as crashed (`TimeoutError`); the others still make the answer.
        """
        if self._source_sessions is None:
            return await call(self.db)
        async with asyncio.timeout(settings.atom_fanout_source_timeout):
            return await call(self._source_sessions.reader())

    async def query(
        self,
        query_text: str,
        *,
        asker_id: int,
        max_visible_tier: int,
        hybrid: bool = True,
        top_k: int = 20,
        peer: PeerScope | None = None,
        federated: bool = False,
    ) -> Sequence[AtomMatch]:
        """
        Query each source in parallel; merge with RRF; return top_k.

        Circle filtering (Lane C) is applied per source: rag / kg / memory /
        lexical / document-fact retrieval all receive ``asker_id`` and constrain
        results via ``circle_sql`` (owner + public-tier + explicit grants + tier
        reach), which computes the asker's reach per-owner. A ``None`` asker
        reduces every source to public-tier only. (RAG search previously omitted
        ``user_id`` here and returned unfiltered chunks — fixed in #695 so no
        source bypasses the circle filter.)

        ``enforce_circles`` (FEDERATION): when True, every source keeps its circle
        filter active EVEN WHEN ``settings.auth_enabled=False`` (single-user mode's
        auth-off bypass is suppressed) AND runs it PEER-SCOPED — the ``circle_sql``
        owner-equality and explicit-grant branches are dropped, leaving only
        ``public-tier OR pairing-tier-membership``. This closes the responder
        brain-leak: a federated ``asker_id`` originates from ``PeerUser.remote_user_id``
        and can collide with a real local owner id (the FK on
        ``circle_memberships.member_user_id`` structurally forces it to a local
        ``users.id`` — often the sole owner's). Default False → local callers
        (REST ``/api/atoms``, agent path) are byte-identical to today.

        🛑 ``max_visible_tier`` HAS been the effective filter since 2026-10-27 --
        this docstring said the opposite until then ("not consulted as a scalar
        ... retained for call-site compatibility"). The reason it was inert WAS
        the bug: the tier came from a ``CircleMembership`` keyed on the REMOTE
        user id. It now comes from the pairing (``peer_users.granted_tier``),
        travels together with the owner as a ``PeerScope``, and forms the
        condition ``granted_tier <= atom.circle_tier`` in the peer-scoped
        branch.
        """
        from services.document_fact_retrieval import DocumentFactRetrieval
        from services.kg_retrieval import KGRetrieval
        from services.lexical_retrieval import LexicalRetrieval
        from services.memory_retrieval import MemoryRetrieval
        from services.note_retrieval import NoteRetrieval
        from services.rag_retrieval import RAGRetrieval
        from services.skill_service import SkillService

        candidate_k = top_k * 3  # over-fetch for RRF fusion across sources

        # A source on a session of its own has no caller transaction that
        # could decide about its writes, so there the fan-out is a PURE READ.
        # It has two writes, and both are switched off explicitly: the memory
        # access bump (row locks) and the skill shadow log (rows inserted and
        # committed under the asking account). Module docstring, "memory" and
        # "skill".
        writes_allowed = self._source_sessions is None

        # 🛑 Every source is built from what it is HANDED (`s`: the caller's
        # session, or a `SourceReader`), and from nothing else. The scope --
        # `asker_id`, `peer` -- is closed over here, once, for both regimes:
        # the regime changes which connection a source reads on, never what
        # it is allowed to read.
        def rag(s: Any):
            return RAGRetrieval(s).search(
                query_text, top_k=candidate_k, user_id=asker_id, peer=peer
            )

        # Structured per-entity/-relation KG atoms (not the aggregated string):
        # each kg_node/kg_edge carries its source id so the detail drawer can
        # render the entity + edit its tier. The agent path still uses the
        # string form (get_relevant_context) elsewhere.
        def kg(s: Any):
            return KGRetrieval(s).get_relevant_atoms(
                query_text, user_id=asker_id, peer=peer
            )

        def memory(s: Any):
            return MemoryRetrieval(s).retrieve(
                query_text, user_id=asker_id, limit=candidate_k, peer=peer,
                track_access=writes_allowed,
            )

        # Lexical retrievers — keyword / name fallback for queries the
        # vector path mis-ranks (single tokens like "Jutta", short
        # German questions whose embedding sits below
        # memory_retrieval_threshold). Returns RAG-shape and memory-shape
        # dicts so the existing _wrap_rag_results / _wrap_memory_results
        # helpers consume them unchanged. Duplicate atom_ids across
        # vector + lexical lists get an RRF boost from both contributions
        # — exactly the "this is both semantic AND keyword match"
        # behaviour we want.
        def lexical_chunks(s: Any):
            return LexicalRetrieval(s).search_chunks_lexical(
                query_text, asker_id=asker_id, top_k=candidate_k, peer=peer,
            )

        def lexical_memories(s: Any):
            return LexicalRetrieval(s).search_memories_lexical(
                query_text, asker_id=asker_id, top_k=candidate_k, peer=peer,
            )

        # Schicht A document facts — a sixth source so structured facts
        # (Steuernummer, issuer, obligation) surface in /brain search via the
        # same RRF fusion. Keyword/identifier retrieval, not vector (facts are
        # short structured strings; the parent chunk is already embedded).
        def document_fact(s: Any):
            return DocumentFactRetrieval(s).search(
                query_text, asker_id=asker_id, top_k=candidate_k, peer=peer,
            )

        # Self-learning Phase 1: procedural skills are atoms too. Gated on
        # skills_enabled so the fusion is unchanged for deployments that
        # haven't opted in.
        def skill(s: Any):
            return SkillService(s).find_similar(
                query_text, asker_id=asker_id, top_k=candidate_k, peer=peer,
                record_shadow_log=writes_allowed,
            )

        # Phase 4B: hand-authored notes are atoms too (FTS keyword retrieval; a
        # note's parent has no separate embedding). Gated on notes_enabled.
        def note(s: Any):
            return NoteRetrieval(s).search(
                query_text, asker_id=asker_id, top_k=candidate_k, peer=peer,
            )

        # Which sources were not asked AT ALL. They must not count as
        # "empty" -- otherwise an instance with `NOTES_ENABLED=false` looks
        # like a permanent partial outage. They take no session and no slot.
        off: set[str] = set()
        if not settings.skills_enabled:
            off.add("skill")
        if not settings.notes_enabled:
            off.add("note")

        async def _not_asked() -> list:
            return []

        calls = [
            ("rag", rag), ("kg", kg), ("memory", memory),
            ("lexical_chunks", lexical_chunks),
            ("lexical_memories", lexical_memories),
            ("skill", skill), ("document_fact", document_fact), ("note", note),
        ]

        # Per PR #402 review SHOULD-FIX #9: gather(return_exceptions=True) does NOT raise,
        # so the previous try/except wrapper around it was dead code that swallowed
        # programmer errors (e.g., import failure on the lazy-imported retrieval modules).
        # Removing the wrapper — exceptions in retrieval modules are converted to []
        # by the _wrap_* helpers, and any actual programmer error now bubbles to FastAPI.
        #
        # One crashing source does not take the others down, and in regime C
        # it cannot even disturb them: they share no session. A cancellation
        # of this task cancels all eight; a statement in flight closes its
        # session on the way.
        _t0 = time.perf_counter()
        (
            rag_results, kg_atoms, memory_results,
            lexical_chunks_results, lexical_memories_results, skill_results,
            document_fact_results, note_results,
        ) = await asyncio.gather(
            *(
                _not_asked() if name in off else self._run_source(call)
                for name, call in calls
            ),
            return_exceptions=True,
        )

        # 🛑 Wrap AND classify in one pass. Emptiness must not be decided with
        # `not value`: `kg` returns `{"entities": [], "relations": []}` when it
        # finds nothing, and a dict with two keys is TRUTHY. The count would be
        # permanently one too high -- found in review on 2026-10-29, and I had
        # even written that bug into the test as the expected value.
        #
        # The `_wrap_*` helpers are the only place that knows the shape of
        # EVERY source. So they decide emptiness, and all that happens here is
        # catching the exception first.
        (
            rag_matches, kg_matches, memory_matches,
            lexical_chunk_matches, lexical_memory_matches, skill_matches,
            document_fact_matches, note_matches,
        ), _ = _wrap_and_count(
            [
                ("rag", rag_results, _wrap_rag_results),
                ("kg", kg_atoms, _wrap_kg_atoms),
                ("memory", memory_results, _wrap_memory_results),
                ("lexical_chunks", lexical_chunks_results, _wrap_rag_results),
                ("lexical_memories", lexical_memories_results, _wrap_memory_results),
                ("skill", skill_results, _wrap_skill_results),
                ("document_fact", document_fact_results, _wrap_document_fact_results),
                ("note", note_results, _wrap_note_results),
            ],
            disabled=off,
            duration_s=time.perf_counter() - _t0,
            federated=federated,
        )

        merged = _rrf_merge(
            [
                rag_matches, kg_matches, memory_matches,
                lexical_chunk_matches, lexical_memory_matches,
                skill_matches, document_fact_matches, note_matches,
            ],
            top_k=top_k,
            k=settings.rag_hybrid_rrf_k,
        )

        # Phase 4: graph expansion (post-RRF). Walk 1-2 hops out from the fused
        # kg_node pivots so the agent/UI sees the neighbourhood, not just the
        # matched nodes. Circle-filtered per hop, leak-safe edges, decay-scored,
        # provenance-marked. Flag-gated (off => merged unchanged). Single seam so
        # the decay survives and it runs once.
        if settings.graph_expansion_enabled:
            from services.graph_expansion import expand_fused
            extra = await expand_fused(
                list(merged), asker_id, self.db,
                max_pivots=settings.graph_expansion_max_pivots,
                max_hops=settings.graph_expansion_max_hops,
                max_expanded=settings.graph_expansion_max_expanded,
                peer=peer,
            )
            if extra:
                have = {m.atom.atom_id for m in merged}
                merged = list(merged) + [m for m in extra if m.atom.atom_id not in have]
                merged.sort(key=lambda m: m.score, reverse=True)
                merged = merged[: top_k + settings.graph_expansion_max_expanded]
        return merged

    async def get_atom(self, atom_id: str, *, asker_id: int) -> Atom | None:
        """Delegates to AtomService — uniform None on not-found AND not-authorized."""
        return await self.atom_service.get_atom(atom_id, asker_id)

    async def upsert_atom(self, atom: Atom) -> str:
        return await self.atom_service.upsert_atom(atom)

    async def update_tier(self, atom_id: str, new_policy: dict[str, Any]) -> None:
        await self.atom_service.update_tier(atom_id, new_policy)

    async def soft_delete(self, atom_id: str) -> None:
        await self.atom_service.soft_delete(atom_id)


def _now() -> datetime:
    return datetime.now()


def _wrap_and_count(
    sources: list[tuple[str, Any, Any]],
    *,
    disabled: set[str],
    duration_s: float,
    federated: bool,
) -> tuple[list[list[AtomMatch]], tuple[list[str], list[str], list[str]]]:
    """Wrap every source and count as we go: delivered / empty / crashed.

    One line per run. 🛑 No content -- source name, counts, exception type. No
    query text, no hits.

    🛑 Emptiness is decided by the WRAPPER, not by `not value`. Every source has
    its own shape, and two of them are truthy when they found nothing (`kg`
    returns a dict with empty lists). The wrapper is the only place that knows
    all eight shapes.

    🛑 Disabled sources do not count towards the denominator. Otherwise an
    instance with `NOTES_ENABLED=false` would permanently read "6/8
    delivered, 2 empty", which looks like a partial outage even though two
    sources were never asked.
    """
    wrapped: list[list[AtomMatch]] = []
    delivered: list[str] = []
    empty: list[str] = []
    crashed: list[str] = []

    for name, value, wrapper in sources:
        if isinstance(value, BaseException):
            crashed.append(name)
            wrapped.append([])
            logger.warning(
                f"🔍 Atom retrieval: source '{name}' crashed "
                f"({type(value).__name__}): {error_text_without_content(value)}"
            )
            continue
        hits = wrapper(value)
        wrapped.append(hits)
        if name in disabled:
            continue
        (delivered if hits else empty).append(name)

    asked = len(sources) - len(disabled)
    line = (
        f"🔍 Atom retrieval{' (federated)' if federated else ''}: "
        f"{len(delivered)}/{asked} sources delivered, "
        f"{len(empty)} empty, {len(crashed)} crashed"
        + (f", {len(disabled)} off" if disabled else "")
        + f" — fan-out {duration_s * 1000:.0f} ms"
    )
    if crashed:
        # A crashed source is not a footnote: the answer then rested on part
        # of the retrieval without anyone seeing it.
        logger.warning(f"{line} — crashed: {', '.join(crashed)}")
    else:
        logger.info(line)
    return wrapped, (delivered, empty, crashed)


def _wrap_rag_results(rag_results: Any) -> list[AtomMatch]:
    """Convert RAGRetrieval.search output -> list[AtomMatch].

    Post-atoms-per-document (pc20260423): emits one AtomMatch per unique
    DOCUMENT, not per chunk. Multiple chunks of the same document that
    matched the query collapse to a single match carrying the best-scoring
    chunk's snippet. This keeps the polymorphic Cross-Source RRF fusion
    fair — documents compete on equal footing with KG entities and
    memories, rather than a long document flooding the top-K with its own
    chunks.
    """
    if isinstance(rag_results, Exception) or not rag_results:
        return []

    # Group chunk results by document atom_id, keeping the highest similarity
    # per document. RAGRetrieval.search returns chunks sorted by similarity
    # desc; we preserve that order when collapsing.
    now = _now()
    seen_atoms: dict[str, AtomMatch] = {}
    next_rank = 1
    for result in rag_results:
        chunk = result.get("chunk", {})
        doc = result.get("document", {})

        doc_atom_id = doc.get("atom_id")
        if doc_atom_id is None:
            logger.warning(
                f"PolymorphicAtomStore: document id={doc.get('id')} has no atom_id "
                f"(post-migration this should not happen — back-fill missed this "
                f"row or writer bypassed AtomService.create_with_source)"
            )
            doc_atom_id = f"kb_document:{doc.get('id', 0)}"
        atom_id = str(doc_atom_id)

        if atom_id in seen_atoms:
            continue  # already have a higher-scoring chunk for this document

        similarity = float(result.get("similarity", 0.0))
        seen_atoms[atom_id] = AtomMatch(
            atom=Atom(
                atom_id=atom_id,
                atom_type=ATOM_TYPE_KB_DOCUMENT,
                owner_user_id=0,
                # Chunks carry the denormalized tier from their document;
                # either side gives the same value, chunk-side is cheap.
                policy={"tier": chunk.get("circle_tier", doc.get("circle_tier", 0))},
                created_at=now,
                updated_at=now,
                payload={
                    "document_id": doc.get("id"),
                    "best_chunk_id": chunk.get("id"),
                    "content": chunk.get("content", ""),
                    "page_number": chunk.get("page_number"),
                    "section_title": chunk.get("section_title"),
                    "document_filename": doc.get("filename", ""),
                    "document_title": doc.get("title"),
                },
            ),
            score=similarity,
            snippet=chunk.get("content", "")[:200],
            rank=next_rank,
        )
        next_rank += 1
    return list(seen_atoms.values())


def _wrap_kg_atoms(kg_atoms: Any) -> list[AtomMatch]:
    """
    Convert KGRetrieval.get_relevant_atoms output -> list[AtomMatch].

    Emits ONE ``kg_node`` atom per matched entity and ONE ``kg_edge`` atom per
    relation, each carrying its source id in the payload (``entity_id`` /
    ``relation_id``) so the unified-search detail drawer can render the entity
    and edit its circle tier via the KG int-id endpoint. Replaces the old single
    ``kg_aggregated`` string blob (which had no id and couldn't be opened).
    """
    if isinstance(kg_atoms, Exception) or not kg_atoms:
        return []
    entities = kg_atoms.get("entities") or []
    relations = kg_atoms.get("relations") or []
    if not entities and not relations:
        return []

    now = _now()
    matches: list[AtomMatch] = []
    rank = 1
    # Entities first (more relevant for a "tell me about X" search), then edges.
    for e in entities:
        eid = e.get("id")
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=f"kg_node:{eid}",
                    atom_type=ATOM_TYPE_KG_NODE,
                    owner_user_id=0,
                    policy={"tier": int(e.get("circle_tier", 0) or 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "entity_id": eid,
                        "name": e.get("name", ""),
                        "entity_type": e.get("entity_type"),
                    },
                ),
                score=float(e.get("similarity", 0.0) or 0.0),
                snippet=str(e.get("name", ""))[:200],
                rank=rank,
            )
        )
        rank += 1
    for r in relations:
        rid = r.get("id")
        subj = r.get("subject_name", "?")
        pred = r.get("predicate", "")
        obj = r.get("object_name", "?")
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=f"kg_edge:{rid}",
                    atom_type=ATOM_TYPE_KG_EDGE,
                    owner_user_id=0,
                    policy={"tier": int(r.get("circle_tier", 0) or 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "relation_id": rid,
                        "subject_id": r.get("subject_id"),
                        "subject_name": subj,
                        "predicate": pred,
                        "object_id": r.get("object_id"),
                        "object_name": obj,
                    },
                ),
                score=0.5,  # relations rank below entities; RRF blends across sources
                snippet=f"{subj} {pred} {obj}"[:200],
                rank=rank,
            )
        )
        rank += 1
    return matches


def _wrap_skill_results(skill_results: Any) -> list[AtomMatch]:
    """Convert SkillService.find_similar output -> list[AtomMatch]."""
    if isinstance(skill_results, Exception) or not skill_results:
        return []
    matches: list[AtomMatch] = []
    now = _now()
    for rank, s in enumerate(skill_results, start=1):
        atom_id = f"procedural_skill:{s.get('id', 0)}"
        body = s.get("body_md") or ""
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=str(atom_id),
                    atom_type="procedural_skill",
                    owner_user_id=0,  # not exposed by find_similar; not needed downstream
                    policy={"tier": 0},  # tier already enforced by find_similar's WHERE
                    created_at=now,
                    updated_at=now,
                    payload={
                        "skill_id": s.get("id"),
                        "title": s.get("title"),
                        "content": body,
                        "trigger_examples": s.get("trigger_examples") or [],
                        "tool_sequence": s.get("tool_sequence") or [],
                        "source": s.get("source"),
                    },
                ),
                score=float(s.get("similarity", 0.0)),
                snippet=body[:200],
                rank=rank,
            )
        )
    return matches


def _wrap_document_fact_results(fact_results: Any) -> list[AtomMatch]:
    """Convert DocumentFactRetrieval.search output -> list[AtomMatch].

    Each fact already carries its real ``atom_id`` (the document_fact atom),
    so duplicates across sources fuse correctly in RRF. The snippet prefers the
    fact ``value`` (the headline — a Steuernummer, issuer, summary) and falls
    back to the excerpt. The payload carries the structured fields the /brain
    UI and any obligation surface need (obligation_date ISO, amount, legal_gate,
    source). Exceptions / empty -> [] (gather resilience).
    """
    if isinstance(fact_results, Exception) or not fact_results:
        return []
    matches: list[AtomMatch] = []
    now = _now()
    for rank, f in enumerate(fact_results, start=1):
        atom_id = f.get("atom_id") or f"document_fact:{f.get('id', 0)}"
        value = f.get("value") or ""
        snippet = value or (f.get("excerpt") or "")
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=str(atom_id),
                    atom_type="document_fact",
                    owner_user_id=0,  # not exposed by search; not needed downstream
                    policy={"tier": f.get("circle_tier", 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "fact_id": f.get("id"),
                        "document_id": f.get("document_id"),
                        "category": f.get("category"),
                        "kind": f.get("kind"),
                        "value": value,
                        "normalized_value": f.get("normalized_value"),
                        "excerpt": f.get("excerpt"),
                        "obligation_date": f.get("obligation_date"),
                        "amount_value": f.get("amount_value"),
                        "amount_currency": f.get("amount_currency"),
                        "legal_gate": f.get("legal_gate", False),
                        "payment_method": f.get("payment_method"),
                        "source": f.get("source"),
                        "tier_overridden": f.get("tier_overridden", False),
                    },
                ),
                score=float(f.get("similarity", 0.0)),
                snippet=snippet[:200],
                rank=rank,
            )
        )
    return matches


def _wrap_note_results(note_results: Any) -> list[AtomMatch]:
    """Convert NoteRetrieval.search output -> list[AtomMatch] (Phase 4B).

    Each note carries its real ``atom_id`` (the note atom), so cross-source
    duplicates fuse correctly in RRF. The payload gives the /brain + /wissen
    drawer the note's title + snippet + project scope. Exceptions / empty -> [].
    """
    if isinstance(note_results, Exception) or not note_results:
        return []
    matches: list[AtomMatch] = []
    now = _now()
    for rank, n in enumerate(note_results, start=1):
        atom_id = n.get("atom_id") or f"note:{n.get('id', 0)}"
        title = n.get("title") or ""
        snippet = n.get("snippet") or title
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=str(atom_id),
                    atom_type="note",
                    owner_user_id=int(n.get("owner_user_id") or 0),
                    policy={"tier": n.get("circle_tier", 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "note_id": n.get("id"),
                        "title": title,
                        "project_id": n.get("project_id"),
                    },
                ),
                score=float(n.get("similarity", 0.0)),
                snippet=snippet[:200],
                rank=rank,
            )
        )
    return matches


def _wrap_memory_results(memory_results: Any) -> list[AtomMatch]:
    """Convert MemoryRetrieval.retrieve output -> list[AtomMatch]."""
    if isinstance(memory_results, Exception) or not memory_results:
        return []
    matches = []
    now = _now()
    for rank, m in enumerate(memory_results, start=1):
        atom_id = m.get("atom_id") or f"memory:{m.get('id', 0)}"
        matches.append(
            AtomMatch(
                atom=Atom(
                    atom_id=str(atom_id),
                    atom_type=ATOM_TYPE_CONVERSATION_MEMORY,
                    owner_user_id=0,
                    policy={"tier": m.get("circle_tier", 0)},
                    created_at=now,
                    updated_at=now,
                    payload={
                        "memory_id": m.get("id"),
                        "content": m.get("content", ""),
                        "category": m.get("category"),
                        "importance": m.get("importance", 0.5),
                    },
                ),
                score=float(m.get("similarity", 0.0)),
                snippet=m.get("content", "")[:200],
                rank=rank,
            )
        )
    return matches


def _rrf_merge(
    source_lists: list[list[AtomMatch]],
    top_k: int,
    k: int = 60,
) -> list[AtomMatch]:
    """
    Reciprocal rank fusion across N source lists.

    score = sum(1 / (k + rank)) for each appearance.
    Equal source weighting (could be made configurable; not in v1 scope).
    """
    scores: dict[str, float] = {}
    matches_by_id: dict[str, AtomMatch] = {}

    for source_list in source_lists:
        for match in source_list:
            atom_id = match.atom.atom_id
            scores[atom_id] = scores.get(atom_id, 0.0) + 1.0 / (k + match.rank)
            # Per PR #402 review SHOULD-FIX #10: keep the highest-ranked source
            # (lowest rank value) for the snippet/score, not first-seen.
            if atom_id not in matches_by_id or match.rank < matches_by_id[atom_id].rank:
                matches_by_id[atom_id] = match

    sorted_ids = sorted(scores.keys(), key=lambda aid: scores[aid], reverse=True)[:top_k]

    result = []
    for new_rank, atom_id in enumerate(sorted_ids, start=1):
        original = matches_by_id[atom_id]
        result.append(
            AtomMatch(
                atom=original.atom,
                score=round(scores[atom_id], 6),
                snippet=original.snippet,
                rank=new_rank,
            )
        )
    return result
