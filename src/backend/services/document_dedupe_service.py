"""KB near-duplicate DOCUMENT detection (#1170).

Byte-hash ingest dedup (``folder_ingest.classify_existing``, unique
``(file_hash, knowledge_base_id)``) only catches BYTE-identical re-uploads. Two
different-bytes files of the *same* document — a re-scan, a re-export, the same
invoice arriving via email and via a portal — get distinct ``file_hash`` and both
land in the KB. ``internal.paperless_dedupe`` can't see them (it only dedupes
Paperless, and one of the two often never becomes a second Paperless doc).

This detector finds such pairs by CONTENT evidence and — like the KG reconciler,
PDF-split and Simba-ingest queues — **only proposes** them for owner review. It
never deletes.

Phase 1 signal: a **shared document-unique identifier**. Two documents carrying a
``document_facts`` row with ``category='identifier'`` and the SAME
``(kind, normalized_value)`` (e.g. both ``invoice_number = "1SOGUR2D-0011"``) are
almost certainly the same document. Precision is protected by a
**recurring-identifier frequency gate**: an identifier value present on more than
``document_dedupe_recurring_identifier_max_docs`` documents is a recurring id
(Steuernummer, IBAN, Kundennummer) and is skipped — otherwise it would emit N²
pairs across an issuer's whole correspondence.

Later phases add a normalized-text-similarity signal (P3) for pairs that share no
extracted identifier.

Scope + safety:
  * **Owner-scoped** — pairs only documents the caller owns (``atoms.owner_user_id``);
    auth-off single-user mode drops the owner filter (one user, sees everything).
  * **Idempotent** — a ``NOT EXISTS`` against pending proposals + the partial-unique
    ``uq_document_duplicate_proposals_pending_pair`` index; a concurrent insert loser
    is swallowed.
  * **Serialized per-user** by a non-blocking advisory lock (ns ``0x4444`` "DD") on a
    DEDICATED connection (mirrors the KG reconciler), so two overlapping runs (a chat
    call + the scheduled scan) don't double-propose.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import and_, exists, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from models.database import (
    DOC_DUP_PROPOSAL_APPROVED,
    DOC_DUP_PROPOSAL_PENDING,
    DOC_DUP_PROPOSAL_REJECTED,
    DOC_DUP_RESOLUTION_DELETE,
    DOC_DUP_RESOLUTION_SUPERSEDE,
    DOC_DUP_SIGNAL_SHARED_IDENTIFIER,
    DOC_DUP_SIGNAL_TEXT_SIMILARITY,
    EMBEDDING_DIMENSION,
    Atom,
    Document,
    DocumentDuplicateProposal,
    DocumentFact,
)
from utils.config import settings

# Fixed namespace key (classid) for the per-user detector advisory lock; objid is
# the user_id. int4 — "DD" for Document-Dedupe. Fresh: not one of the taken
# 0x4B47/0x4F42/0x4F43/0x4F44/0x5341/0x5354 (see services/database.py).
_DEDUPE_LOCK_NS = 0x4444

_COMPLETED = "completed"
_IDENTIFIER = "identifier"


@dataclass
class _Pair:
    doc_a_id: int          # always < doc_b_id
    doc_b_id: int
    signal: str            # DOC_DUP_SIGNAL_SHARED_IDENTIFIER | DOC_DUP_SIGNAL_TEXT_SIMILARITY
    shared_key: str | None # "<kind>=<normalized_value>" for identifier; a "~NN%" note for text
    similarity: float = 1.0


@dataclass
class DedupeReport:
    user_id: int
    candidates: int = 0
    proposed: int = 0
    skipped_existing: int = 0
    notes: list[str] = field(default_factory=list)


def _resolve_lock_engine(bind) -> AsyncEngine | None:
    """The AsyncEngine to open the dedicated advisory-lock connection on.

    Mirrors ``kg_reconciler_service._resolve_lock_engine``: prod binds an
    ``AsyncEngine`` (use directly — ``.engine`` proxies to the sync Engine and
    explodes under ``async with``); tests bind an ``AsyncConnection`` (``.engine``
    IS the AsyncEngine); sqlite/unknown → None → caller runs unlocked (safe: the
    single scheduler won't collide per-user).
    """
    if isinstance(bind, AsyncEngine):
        return bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    return None


class DocumentDedupeService:
    """Per-user KB near-duplicate document detector (propose-only)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def build_pairs_query(user_id: int | None, auth_enabled: bool):
        """The shared-identifier self-join select (extracted so it is compile-testable).

        Returns a Select of (a, b, kind, nv) ordered pairs (a<b). Static so a unit
        test can compile it against the Postgres dialect without a live DB or a
        session bind.
        """
        fa = aliased(DocumentFact)
        fb = aliased(DocumentFact)
        da = aliased(Document)
        db_ = aliased(Document)

        min_len = settings.document_dedupe_min_identifier_length
        recurring_max = settings.document_dedupe_recurring_identifier_max_docs

        # Correlated count of DISTINCT documents carrying this exact identifier —
        # the recurring-identifier gate. > recurring_max ⇒ a recurring id, skip.
        fc = aliased(DocumentFact)
        doc_count = (
            select(func.count(func.distinct(fc.document_id)))
            .where(
                fc.category == _IDENTIFIER,
                fc.kind == fa.kind,
                fc.normalized_value == fa.normalized_value,
            )
            .scalar_subquery()
        )

        conds = [
            fa.category == _IDENTIFIER,
            fb.category == _IDENTIFIER,
            fa.kind == fb.kind,
            fa.normalized_value == fb.normalized_value,
            fa.normalized_value.isnot(None),
            func.length(func.trim(fa.normalized_value)) >= min_len,
            fa.document_id < fb.document_id,
            da.status == _COMPLETED,
            db_.status == _COMPLETED,
            da.superseded_by_document_id.is_(None),
            db_.superseded_by_document_id.is_(None),
            doc_count <= recurring_max,
            # idempotency + DURABLE RESOLUTION: skip any pair that has EVER been
            # proposed — pending (awaiting review), approved (already resolved), OR
            # rejected. Excluding rejected is the durable-reject contract (mirrors
            # PDF-split "reject = permanent, consulted by detection"): a pair the
            # owner dismissed as not-a-duplicate must never be re-proposed on the
            # next scan. Deliberately NOT filtered by status.
            ~exists().where(
                DocumentDuplicateProposal.document_a_id == fa.document_id,
                DocumentDuplicateProposal.document_b_id == fb.document_id,
            ),
        ]

        q = (
            select(
                fa.document_id.label("a"),
                fb.document_id.label("b"),
                fa.kind.label("kind"),
                fa.normalized_value.label("nv"),
            )
            .join(fb, and_(fa.kind == fb.kind, fa.normalized_value == fb.normalized_value))
            .join(da, da.id == fa.document_id)
            .join(db_, db_.id == fb.document_id)
        )

        # Owner scope: with auth on, both documents must be owned by the caller.
        # atoms.owner_user_id is NOT NULL, so the join also excludes atom-less docs
        # (which have no owner to scope by) when auth is on — deliberately
        # conservative. Auth-off single-user mode drops the filter.
        if auth_enabled:
            aa = aliased(Atom)
            ab = aliased(Atom)
            q = (
                q.join(aa, aa.atom_id == da.atom_id)
                .join(ab, ab.atom_id == db_.atom_id)
            )
            conds.append(aa.owner_user_id == user_id)
            conds.append(ab.owner_user_id == user_id)

        return q.where(*conds).order_by(fa.document_id, fb.document_id)

    async def find_duplicate_pairs(self, user_id: int | None) -> list[_Pair]:
        """Candidate near-duplicate pairs via a shared document-unique identifier.

        Postgres-only self-join (the sqlite harness has the ORM tables but the
        correlated/self-join SQL is Postgres-shaped; a sqlite bind short-circuits
        to []). Returns at most ``document_dedupe_max_per_run`` ordered (a<b) pairs,
        each deduped to a single representative identifier.
        """
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return []

        q = self.build_pairs_query(user_id, settings.auth_enabled)
        rows = (await self.db.execute(q)).all()

        # A pair can share multiple identifier facts → multiple rows; keep the
        # first representative per (a, b) and cap.
        seen: set[tuple[int, int]] = set()
        pairs: list[_Pair] = []
        cap = settings.document_dedupe_max_per_run
        for a, b, kind, nv in rows:
            key = (int(a), int(b))
            if key in seen:
                continue
            seen.add(key)
            pairs.append(_Pair(
                doc_a_id=int(a), doc_b_id=int(b),
                signal=DOC_DUP_SIGNAL_SHARED_IDENTIFIER,
                shared_key=f"{kind}={nv}",
                similarity=1.0,
            ))
            if len(pairs) >= cap:
                break
        return pairs

    @staticmethod
    def build_text_similar_sql(auth_enabled: bool) -> str:
        """The content-embedding halfvec cosine self-join SQL (extracted so a unit
        test can assert its shape without a live DB). Owner-scoped when auth is on."""
        dim = EMBEDDING_DIMENSION
        cos = (
            f"1 - (a.content_embedding::halfvec({dim}) "
            f"<=> b.content_embedding::halfvec({dim}))"
        )
        owner_join = ""
        if auth_enabled:
            owner_join = (
                "JOIN atoms aa ON aa.atom_id = a.atom_id AND aa.owner_user_id = :uid "
                "JOIN atoms ab ON ab.atom_id = b.atom_id AND ab.owner_user_id = :uid "
            )
        return f"""
            SELECT a.id AS id_a, b.id AS id_b, {cos} AS similarity
            FROM documents a
            JOIN documents b ON a.id < b.id
            {owner_join}
            WHERE a.content_embedding IS NOT NULL AND b.content_embedding IS NOT NULL
              AND a.status = 'completed' AND b.status = 'completed'
              AND a.superseded_by_document_id IS NULL
              AND b.superseded_by_document_id IS NULL
              AND ({cos}) >= :thr
              AND NOT EXISTS (
                  SELECT 1 FROM document_duplicate_proposals p
                  WHERE (p.document_a_id = a.id AND p.document_b_id = b.id)
                     OR (p.document_a_id = b.id AND p.document_b_id = a.id))
            ORDER BY similarity DESC
            LIMIT :cap
        """

    async def find_text_similar_pairs(self, user_id: int | None) -> list[_Pair]:
        """P3 fuzzy signal: near-duplicate pairs by document CONTENT-EMBEDDING
        cosine (mean of chunk embeddings). Catches re-scans/re-exports that share
        NO extracted identifier. Postgres-only halfvec self-join, mirroring the KG
        reconciler; high threshold + propose-only keeps distinct-but-similar docs
        (e.g. two invoices from one vendor) from being merged.

        Skips pairs that already have a proposal of ANY status (so the identifier
        pass — run first — isn't re-proposed, and rejected pairs stay durable).
        """
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return []

        sql_str = self.build_text_similar_sql(settings.auth_enabled)
        params: dict = {
            "thr": settings.document_dedupe_text_threshold,
            "cap": settings.document_dedupe_text_max_pairs,
        }
        if settings.auth_enabled:
            params["uid"] = user_id
        rows = (await self.db.execute(text(sql_str), params)).all()
        pairs: list[_Pair] = []
        for id_a, id_b, sim in rows:
            a, b = (int(id_a), int(id_b))
            lo, hi = (a, b) if a < b else (b, a)
            pairs.append(_Pair(
                doc_a_id=lo, doc_b_id=hi,
                signal=DOC_DUP_SIGNAL_TEXT_SIMILARITY,
                shared_key=f"~{round(float(sim) * 100)}% Textähnlichkeit",
                similarity=float(sim),
            ))
        return pairs

    async def _pick_survivor(self, a_id: int, b_id: int) -> int:
        """Which document to keep: Paperless-linked > more facts > lower id.

        A Paperless-linked copy is the canonical filed one; failing that keep the
        richer (more extracted facts) row; final tie-break the lower (older) id.
        """
        rows = (
            await self.db.execute(
                select(
                    Document.id,
                    Document.paperless_document_id,
                    select(func.count())
                    .select_from(DocumentFact)
                    .where(DocumentFact.document_id == Document.id)
                    .scalar_subquery()
                    .label("nfacts"),
                ).where(Document.id.in_([a_id, b_id]))
            )
        ).all()
        info = {int(r.id): (r.paperless_document_id is not None, int(r.nfacts or 0)) for r in rows}
        a_pl, a_nf = info.get(a_id, (False, 0))
        b_pl, b_nf = info.get(b_id, (False, 0))
        # rank tuple: (paperless_linked, nfacts) — higher wins; lower id breaks ties
        if (a_pl, a_nf) != (b_pl, b_nf):
            return a_id if (a_pl, a_nf) > (b_pl, b_nf) else b_id
        return min(a_id, b_id)

    async def _propose(self, user_id: int | None, pair: _Pair) -> bool:
        """Persist one PENDING proposal (a<b). Idempotent — swallows the
        partial-unique / concurrent-insert loser. Returns True if a row landed.

        The insert runs inside a SAVEPOINT (``begin_nested``) so a partial-unique
        collision rolls back ONLY this row, not the whole batch's prior successful
        proposals (a plain ``session.rollback()`` would discard them and the outer
        commit would then over-report). The collision is already unreachable in
        practice — the per-user advisory lock serializes writers and the query's
        NOT EXISTS excludes proposed pairs — but the savepoint makes the swallow
        genuinely local, matching the docstring's intent."""
        survivor = await self._pick_survivor(pair.doc_a_id, pair.doc_b_id)
        try:
            async with self.db.begin_nested():
                self.db.add(
                    DocumentDuplicateProposal(
                        user_id=user_id,
                        document_a_id=pair.doc_a_id,
                        document_b_id=pair.doc_b_id,
                        signal=pair.signal,
                        shared_key=pair.shared_key,
                        similarity=pair.similarity,
                        suggested_survivor_id=survivor,
                        status=DOC_DUP_PROPOSAL_PENDING,
                    )
                )
                await self.db.flush()
            return True
        except IntegrityError:
            return False

    async def list_owner_ids(self) -> list[int | None]:
        """Users to scan in the autonomous pass (#1170 P3 scheduled task).

        Auth-off single-user → ``[None]`` (one pass over all docs). Auth-on →
        distinct owners of ``kb_document`` atoms (a non-doc owner would find no
        pairs anyway). Mirrors ``KgReconcilerService.list_active_user_ids``."""
        if not settings.auth_enabled:
            return [None]
        rows = (
            await self.db.execute(
                select(Atom.owner_user_id)
                .where(Atom.atom_type == "kb_document")
                .distinct()
            )
        ).scalars().all()
        return [int(r) for r in rows if r is not None]

    async def _detect_pass(self, user_id: int | None, report: DedupeReport) -> DedupeReport:
        pairs = await self.find_duplicate_pairs(user_id)
        report.candidates = len(pairs)
        for pair in pairs:
            if await self._propose(user_id, pair):
                report.proposed += 1
            else:
                report.skipped_existing += 1
        # P3: the text-similarity pass (fuzzy — catches re-scans that share no
        # identifier), after the identifier pass so it skips already-proposed pairs.
        if settings.document_dedupe_text_similarity_enabled:
            text_pairs = await self.find_text_similar_pairs(user_id)
            report.candidates += len(text_pairs)
            for pair in text_pairs:
                if await self._propose(user_id, pair):
                    report.proposed += 1
                else:
                    report.skipped_existing += 1
        if report.proposed:
            await self.db.commit()
        return report

    async def run_for_user(self, user_id: int | None) -> DedupeReport:
        """One detection pass for a user, serialized per-user (idempotent).

        Non-blocking per-user advisory lock on a dedicated connection (ns
        ``0x4444``): a second overlapping run finds the lock held and returns a
        no-op report. sqlite/unknown bind → run unlocked (safe fallback).
        """
        report = DedupeReport(user_id=user_id if user_id is not None else 0)
        dialect = self.db.bind.dialect.name if self.db.bind is not None else ""
        if dialect != "postgresql":
            return await self._detect_pass(user_id, report)

        lock_engine = _resolve_lock_engine(self.db.bind)
        if lock_engine is None:
            return await self._detect_pass(user_id, report)

        lock_objid = user_id if user_id is not None else 0
        async with lock_engine.connect() as lock_conn:
            got = (
                await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:ns, :uid)"),
                    {"ns": _DEDUPE_LOCK_NS, "uid": lock_objid},
                )
            ).scalar()
            if not got:
                report.notes.append("skipped: another detection run holds this user's lock")
                return report
            try:
                return await self._detect_pass(user_id, report)
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:ns, :uid)"),
                    {"ns": _DEDUPE_LOCK_NS, "uid": lock_objid},
                )

    # ---- Phase 2: owner resolution (approve / reject) ---------------------

    async def resolve_proposal(
        self,
        proposal: DocumentDuplicateProposal,
        *,
        user_id: int | None,
        resolution: str,
        survivor_id: int | None = None,
    ) -> dict:
        """Approve a pending pair: keep the survivor, and either SUPERSEDE
        (recoverable — mark the loser ``superseded_by_document_id`` so it drops out
        of retrieval) or DELETE the loser via the standard document-delete path.

        The ``proposal`` is already ownership-gated by the route. Returns
        ``{"status": "approved"|"superseded"|"invalid", ...}``. ``superseded``
        (the return status, distinct from the resolution) means a concurrent
        resolve already handled it → no-op.

        Survivor selection: the caller may override (``survivor_id`` must be one of
        the pair); else the detector's ``suggested_survivor_id``; else the lower id.
        """
        pair = {proposal.document_a_id, proposal.document_b_id}
        survivor = survivor_id if survivor_id in pair else proposal.suggested_survivor_id
        if survivor not in pair:
            survivor = min(pair)
        loser = (pair - {survivor}).pop()

        if resolution == DOC_DUP_RESOLUTION_DELETE:
            # Deleting the loser CASCADE-deletes this proposal row (FK ON DELETE
            # CASCADE) — intended: with the loser gone the pair can never re-form,
            # so no lasting proposal record is needed. delete_document commits.
            from services.rag_service import RAGService

            # Guard double-resolve: claim the proposal (pending→approved) first;
            # if 0 rows, another resolve already took it → no-op.
            claimed = await self.db.execute(
                update(DocumentDuplicateProposal)
                .where(
                    DocumentDuplicateProposal.id == proposal.id,
                    DocumentDuplicateProposal.status == DOC_DUP_PROPOSAL_PENDING,
                )
                .values(
                    status=DOC_DUP_PROPOSAL_APPROVED,
                    resolution=DOC_DUP_RESOLUTION_DELETE,
                    resolved_at=datetime.now(UTC).replace(tzinfo=None),
                    resolved_by_user_id=user_id,
                    suggested_survivor_id=survivor,
                )
            )
            if (claimed.rowcount or 0) == 0:
                await self.db.rollback()
                return {"status": "superseded"}
            await self.db.flush()
            deleted = await RAGService(self.db).delete_document(loser)  # commits
            return {
                "status": "approved",
                "resolution": DOC_DUP_RESOLUTION_DELETE,
                "survivor_id": survivor,
                "loser_id": loser,
                "deleted": bool(deleted),
            }

        # supersede (default): mark the loser superseded + resolve the proposal.
        await self.db.execute(
            update(Document)
            .where(Document.id == loser)
            .values(superseded_by_document_id=survivor)
        )
        claimed = await self.db.execute(
            update(DocumentDuplicateProposal)
            .where(
                DocumentDuplicateProposal.id == proposal.id,
                DocumentDuplicateProposal.status == DOC_DUP_PROPOSAL_PENDING,
            )
            .values(
                status=DOC_DUP_PROPOSAL_APPROVED,
                resolution=DOC_DUP_RESOLUTION_SUPERSEDE,
                resolved_at=datetime.now(UTC).replace(tzinfo=None),
                resolved_by_user_id=user_id,
                suggested_survivor_id=survivor,
            )
        )
        if (claimed.rowcount or 0) == 0:
            await self.db.rollback()
            return {"status": "superseded"}
        await self.db.commit()
        return {
            "status": "approved",
            "resolution": DOC_DUP_RESOLUTION_SUPERSEDE,
            "survivor_id": survivor,
            "loser_id": loser,
        }

    async def reject_proposal(
        self, proposal: DocumentDuplicateProposal, *, user_id: int | None
    ) -> dict:
        """Reject a pending pair (not a duplicate). Durable — the detector's
        idempotency guard skips any pair with a proposal of ANY status, so a
        rejected pair is never re-proposed."""
        claimed = await self.db.execute(
            update(DocumentDuplicateProposal)
            .where(
                DocumentDuplicateProposal.id == proposal.id,
                DocumentDuplicateProposal.status == DOC_DUP_PROPOSAL_PENDING,
            )
            .values(
                status=DOC_DUP_PROPOSAL_REJECTED,
                resolved_at=datetime.now(UTC).replace(tzinfo=None),
                resolved_by_user_id=user_id,
            )
        )
        if (claimed.rowcount or 0) == 0:
            await self.db.rollback()
            return {"status": "superseded"}
        await self.db.commit()
        return {"status": "rejected"}
