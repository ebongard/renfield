"""Baseline: a fresh install gets the same schema as production

Revision ID: pc20260926_baseline
Revises: pc20260924_restore_idx
Create Date: 2026-09-26

WHY THIS MIGRATION EXISTS
-------------------------
`services/database.py:_ensure_alembic_baseline()` built a fresh install's
schema from the ORM models (`create_all`) and then STAMPED the Alembic head.
The docstring there said it itself: "the 41-migration history is skipped".
Consequence: everything a migration creates via `op.execute("CREATE INDEX ...")`
NEVER came into being on a fresh install -- and was never caught up, because
the stamp claimed the migration had been applied. #1336 repaired the EXISTING
STOCK; this migration fixes the CAUSE.

Measured 2026-09-25: `create_all` and the production schema both had exactly
333 indexes -- and a third of them were DIFFERENT (45 only here, 45 only
there). The same number is not the same set.

WHY AT THE END OF THE CHAIN AND NOT AT ITS ROOT
-----------------------------------------------
The chain has exactly ONE root (`9a0d8ccea5b0`), and that one is an empty
`pass` body -- it was never a complete description of the schema and does not
run through from empty (`rooms` is created by no migration). A second root
would fork the chain, and `alembic heads` MUST yield a single revision. A
squashed chain in turn would break every database standing on an older
revision -- reva, a restored backup, an older dump.

Hence here, behind today's head, WITH A GUARD:

* **Existing instance** (tables present): does nothing. Provable by the
  sentinel table existing.
* **Fresh database**: the bootstrap stamps `pc20260924_restore_idx` and then
  runs `alembic upgrade head` -- this migration is the only thing that runs,
  and it brings both: the tables from the models AND the raw SQL that is lost
  today.

🛑 **What that means for FUTURE migrations:** nothing special. They stand
behind this baseline and run normally on a fresh install. It covers only what
lies BEFORE it. So this baseline never has to be updated again -- a new raw-SQL
index belongs in a new migration, not here.

🛑 **The vector indexes are NOT pinned here.** Their dimension is read from the
column at runtime (RAW, without `- 4`: pgvector stores N directly in the
typmod). `pc20260402` computed `atttypmod - 4` and would have built an index on
halfvec(2556) that no query could ever have used -- built, maintained on every
write, useless.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "pc20260926_baseline"
down_revision: Union[str, None] = "pc20260924_restore_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: If this table exists the schema is there and the migration does nothing.
#: `users` is the oldest one and present on every instance.
SENTINEL_TABLE = "users"

#: Vector indexes: (name, table, column). The dimension comes at runtime.
_VECTOR_INDEXES = [
    ('idx_document_chunks_embedding_hnsw', 'document_chunks', 'embedding'),
    ('idx_documents_content_embedding_hnsw', 'documents', 'content_embedding'),
    ('idx_intent_corrections_embedding_hnsw', 'intent_corrections', 'embedding'),
    ('idx_kg_entities_embedding_hnsw', 'kg_entities', 'embedding'),
    ('idx_msf_centroid_hnsw', 'meeting_speaker_fingerprints', 'centroid'),
    ('idx_notes_embedding_hnsw', 'notes', 'embedding'),
    ('idx_paperless_examples_embedding_hnsw', 'paperless_extraction_examples', 'doc_text_embedding'),
    ('idx_procedural_skills_embedding', 'procedural_skills', 'embedding'),
    ('ix_conversation_memories_embedding_hnsw', 'conversation_memories', 'embedding'),
    ('ix_episodic_embedding_hnsw', 'episodic_memories', 'embedding'),
]

#: Everything else verbatim from production (2026-09-25), with IF NOT EXISTS.
_RAW_DDL = [
    'CREATE INDEX IF NOT EXISTS idx_conversation_memories_search_vector_gin ON conversation_memories USING gin (search_vector)',
    'CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_chunk ON document_chunks USING btree (document_id, chunk_index)',
    'CREATE INDEX IF NOT EXISTS idx_document_chunks_kb_tier ON document_chunks USING btree (document_id, circle_tier)',
    'CREATE INDEX IF NOT EXISTS idx_document_chunks_search_vector_gin ON document_chunks USING gin (search_vector)',
    "CREATE INDEX IF NOT EXISTS idx_document_facts_obligation_due ON document_facts USING btree (obligation_date) WHERE (((category)::text = 'obligation'::text) AND (obligation_date IS NOT NULL))",
    'CREATE INDEX IF NOT EXISTS idx_document_facts_search_vector_gin ON document_facts USING gin (search_vector)',
    'CREATE INDEX IF NOT EXISTS idx_documents_search_vector_gin ON documents USING gin (search_vector)',
    'CREATE INDEX IF NOT EXISTS idx_dph_document_id ON document_processing_history USING btree (document_id)',
    'CREATE INDEX IF NOT EXISTS idx_kg_entities_surface_forms_gin ON kg_entities USING gin (surface_forms jsonb_path_ops)',
    'CREATE INDEX IF NOT EXISTS idx_memories_owner_tier ON conversation_memories USING btree (user_id, circle_tier, is_active)',
    'CREATE INDEX IF NOT EXISTS idx_memv2sl_created_at ON memory_v2_shadow_log USING btree (created_at)',
    'CREATE INDEX IF NOT EXISTS idx_memv2sl_session_id ON memory_v2_shadow_log USING btree (session_id)',
    'CREATE INDEX IF NOT EXISTS idx_memv2sl_user_created ON memory_v2_shadow_log USING btree (user_id, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_messages_search_vector_gin ON messages USING gin (search_vector)',
    'CREATE INDEX IF NOT EXISTS idx_notes_search_vector_gin ON notes USING gin (search_vector)',
    'CREATE INDEX IF NOT EXISTS idx_procedural_skills_atom_id ON procedural_skills USING btree (atom_id)',
    'CREATE INDEX IF NOT EXISTS idx_procedural_skills_last_used ON procedural_skills USING btree (last_used_at)',
    'CREATE INDEX IF NOT EXISTS idx_procedural_skills_merged_into ON procedural_skills USING btree (merged_into_id)',
    'CREATE INDEX IF NOT EXISTS idx_procedural_skills_status_user ON procedural_skills USING btree (status, user_id)',
    'CREATE INDEX IF NOT EXISTS idx_procedural_skills_tier_status ON procedural_skills USING btree (circle_tier, status)',
    'CREATE INDEX IF NOT EXISTS idx_skill_curator_runs_started ON skill_curator_runs USING btree (started_at)',
    'CREATE INDEX IF NOT EXISTS idx_skill_would_have_created ON skill_would_have_injected_log USING btree (created_at)',
    'CREATE INDEX IF NOT EXISTS idx_skill_would_have_skill ON skill_would_have_injected_log USING btree (skill_id)',
    'CREATE INDEX IF NOT EXISTS idx_trajectories_created_at ON agent_trajectories USING btree (created_at)',
    'CREATE INDEX IF NOT EXISTS idx_trajectories_flagged ON agent_trajectories USING btree (flagged_for_retention)',
    'CREATE INDEX IF NOT EXISTS idx_wb_fp_recent ON wb_field_provenance USING btree (fetched_at)',
    'CREATE INDEX IF NOT EXISTS ix_conv_memories_cleanup ON conversation_memories USING btree (category, last_accessed_at) WHERE (is_active = true)',
    'CREATE INDEX IF NOT EXISTS ix_paperless_pending_confirms_session_created ON paperless_pending_confirms USING btree (session_id, created_at)',
    'CREATE INDEX IF NOT EXISTS ix_paperless_upload_tracking_chat_upload_id ON paperless_upload_tracking USING btree (chat_upload_id)',
    'CREATE INDEX IF NOT EXISTS ix_paperless_upload_tracking_sweep_candidates ON paperless_upload_tracking USING btree (swept_at, uploaded_at)',
    'CREATE INDEX IF NOT EXISTS ix_pf_finalize_unfinalized ON paperless_pending_finalize USING btree (created_at) WHERE (finalized_at IS NULL)',
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_kg_merge_proposals_pending_pair ON kg_merge_proposals USING btree (loser_entity_id, winner_entity_id) WHERE ((status)::text = 'pending'::text)",
    'CREATE UNIQUE INDEX IF NOT EXISTS uq_msf_owner_label ON meeting_speaker_fingerprints USING btree (owner_user_id, label)',
    'CREATE UNIQUE INDEX IF NOT EXISTS uq_notes_owner_lower_title ON notes USING btree (owner_user_id, lower((title)::text))',
]


def _table_exists(conn, table: str) -> bool:
    return conn.execute(text(
        "SELECT to_regclass(current_schema() || '.' || :t) IS NOT NULL"
    ), {"t": table}).scalar() is True


def _column_dim(conn, table: str, column: str) -> int | None:
    """Dimension of a `vector(N)` column. RAW, without `- 4` -- see the header."""
    row = conn.execute(text("""
        SELECT a.atttypmod FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = current_schema()
        WHERE c.relname = :t AND a.attname = :col AND a.attnum > 0 AND NOT a.attisdropped
    """), {"t": table, "col": column}).first()
    return int(row[0]) if row and row[0] and row[0] > 0 else None


def upgrade() -> None:
    conn = op.get_bind()

    # GUARD. An existing instance has had the schema for ages; this migration
    # must do nothing there, or it would run against every production database.
    if _table_exists(conn, SENTINEL_TABLE):
        return

    # From here on: an EMPTY database.
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    # The tables from the models. `ha_glue.models.database` MUST be imported:
    # `Base.metadata` only knows tables whose module was loaded -- without this
    # import 12 tables were missing (rooms, satellites, ha_entities ...).
    from models.database import Base
    import ha_glue.models.database  # noqa: F401 -- fills Base.metadata

    Base.metadata.create_all(bind=conn)

    for ddl in _RAW_DDL:
        op.execute(ddl)

    for name, table, column in _VECTOR_INDEXES:
        if not _table_exists(conn, table):
            continue
        dim = _column_dim(conn, table, column)
        if dim is None:
            continue
        # halfvec above 2000 dimensions: the regular vector type is not
        # indexable beyond that.
        expr = (f"(({column}::halfvec({dim})) halfvec_cosine_ops)" if dim > 2000
                else f"({column} vector_cosine_ops)")
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
                   f"USING hnsw {expr} WITH (m = 16, ef_construction = 64)")


def downgrade() -> None:
    """Deliberately nothing -- and that is a statement, not a gap.

    The way back from a baseline would be "drop the whole schema". That is not
    a downgrade, that is data loss. Whoever wants to get behind this migration
    takes the database that existed before it.

    EXERCISED, not asserted (2026-09-26, both cases):

        fresh DB         333 indexes -> downgrade -> 333 -> upgrade -> 333
        existing schema  326 indexes -> downgrade -> 326 -> upgrade -> 326

    🛑 From that follows a property one has to know: after a downgrade the
    schema STAYS standing while `alembic_version` claims it does not exist yet.
    "Before" cannot be reconstructed from "after" here. That is coherent in
    itself, because the GUARD in `upgrade()` decides on the sentinel table and
    not on the revision -- so running `upgrade` again correctly does nothing
    instead of trying to rebuild a schema that is already there.
    """
