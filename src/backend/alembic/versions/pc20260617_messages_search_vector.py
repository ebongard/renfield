"""Multilingual FTS column on messages — chat message search (roadmap item 3).

Revision ID: pc20260617_msg_search
Revises: pc20260616_sat_id
Create Date: 2026-06-17 00:00:00.000000

Adds ``messages.search_vector`` so chat message search
(``GET /api/chat/messages/search`` → ``ConversationService.search_messages``)
can rank with Postgres FTS (``ts_rank``) over message ``content`` instead of a
token-OR'd ILIKE. ts_rank applies IDF-style term weighting so a rare proper
noun outranks a frequent function word — exactly what natural-language message
search needs.

Pattern — identical to ``pc20260528`` (conversation_memories): a STORED
GENERATED column unioning ``to_tsvector`` across all
``services/fts_languages.FTS_LANGUAGES`` (DE / EN / FR / IT / ES / NL), plus a
GIN index built CONCURRENTLY. NOT the atomic-swap variant (pc20260529) — the
``messages`` table has no pre-existing ``search_vector`` to keep serving reads,
and the swap's forward-compat argument doesn't apply: this is a brand-new
column. The simpler DROP-then-ADD is correct here.

Why GENERATED STORED (not trigger + backfill):

  1. The column auto-populates for EVERY existing row the instant it's added
     — Postgres computes the generated expression from ``content`` (already
     populated) at ALTER time. No separate backfill UPDATE, no backfill window
     where old messages are unsearchable.
  2. Every future INSERT recomputes it DB-side. App write paths
     (``ConversationService.save_message``, the ``Message(...)`` sites in
     ``api/routes/chat.py``) do NOT set it — UPDATEs/INSERTs that supply a value
     for a GENERATED column raise, so the ORM column is declared
     ``FetchedValue()`` (mirrors DocumentChunk / ConversationMemory) to keep it
     out of ORM INSERTs.

Adding a 7th language later requires a follow-up migration that DROPs + re-ADDs
the GENERATED column with the new expression (Postgres does not allow ALTER on
a generated column's body) — same rule as the other FTS columns.

Transaction model: ``env.py`` runs ``transaction_per_migration=True``. The
GIN index is built inside ``op.get_context().autocommit_block()`` (CONCURRENTLY
cannot run inside a transaction). A defensive ``DROP INDEX CONCURRENTLY IF
EXISTS`` precedes the CREATE so a prior partial run that left an INVALID index
is cleaned up rather than silently skipped — recoverable per the pc20260528
precedent.

Scoping note: ``messages`` is NOT an atom (no ``circle_tier`` / ``atom_id``),
so message search is scoped by conversation ownership in the query layer, NOT
through ``services/circle_sql.py``. This migration adds only the lexical
infrastructure; the ownership join lives in ``ConversationService``.
"""
from alembic import op

from services.fts_languages import build_generated_tsvector_expression


revision = "pc20260617_msg_search"
down_revision = "pc20260616_sat_id"
branch_labels = None
depends_on = None


_INDEX = "idx_messages_search_vector_gin"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        # Sqlite test harness has no tsvector. Skip silently — the
        # message-search service has a sqlite ILIKE fallback branch.
        return

    # Defensive DROP before ADD: ADD COLUMN IF NOT EXISTS is satisfied by name
    # alone, so a pre-existing column with the WRONG shape (e.g. a dev DB
    # bootstrapped via Base.metadata.create_all, which gets a plain nullable
    # TSVECTOR — see Message.search_vector in models/database.py) would survive
    # and never populate. Unconditional DROP-then-ADD is cheap because
    # search_vector is fully derived from content — Postgres repopulates it for
    # every existing row the instant the GENERATED column is re-added.
    tsvector_expr = build_generated_tsvector_expression("content")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS search_vector")
    op.execute(
        "ALTER TABLE messages "
        f"ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({tsvector_expr}) STORED"
    )

    with op.get_context().autocommit_block():
        # Defensive DROP before CREATE: a prior failed CREATE INDEX
        # CONCURRENTLY can leave the index INVALID. `CREATE ... IF NOT EXISTS`
        # checks name+namespace only, NOT validity, so it would match the
        # INVALID index and skip — leaving a perpetual seq-scan with no error.
        # CONCURRENTLY on the DROP avoids the ACCESS EXCLUSIVE table lock a
        # plain DROP INDEX would briefly take.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_INDEX} "
            f"ON messages USING gin (search_vector)"
        )

    # Refresh planner stats: the new GENERATED column has zero rows in
    # pg_statistic until the next autovacuum cycle, so the GIN index may be
    # ignored for seq-scan in the first ~minutes after deploy. ANALYZE
    # eliminates the cold-stats window.
    op.execute("ANALYZE messages")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS search_vector")
