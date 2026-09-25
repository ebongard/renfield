"""Basis: eine Neuinstallation bekommt dasselbe Schema wie die Produktion

Revision ID: pc20260926_baseline
Revises: pc20260924_restore_idx
Create Date: 2026-09-26

WARUM ES DIESE MIGRATION GIBT
-----------------------------
`services/database.py:_ensure_alembic_baseline()` erzeugte das Schema einer
Neuinstallation aus den ORM-Modellen (`create_all`) und STEMPELTE dann den
Alembic-Kopf. Der Docstring dort sagte es selbst: „the 41-migration history is
skipped". Folge: alles, was eine Migration per `op.execute("CREATE INDEX …")`
anlegt, entstand auf einer Neuinstallation NIE — und wurde nie nachgeholt, weil
der Stempel behauptete, die Migration sei angewandt. #1336 hat den BESTAND
repariert; diese Migration behebt die URSACHE.

Gemessen 2026-09-25: `create_all` und das Produktionsschema hatten beide exakt
333 Indizes — und ein Drittel davon war VERSCHIEDEN (45 nur hier, 45 nur dort).
Gleiche Zahl ist nicht gleiche Menge.

WARUM AM KETTENENDE UND NICHT AN DER WURZEL
--------------------------------------------
Die Kette hat genau EINE Wurzel (`9a0d8ccea5b0`), und die ist ein leerer
`pass`-Rumpf — sie war nie eine vollständige Beschreibung des Schemas und läuft
aus dem Leeren nicht durch (`rooms` legt keine Migration an). Eine zweite Wurzel
würde die Kette gabeln, und `alembic heads` MUSS eine einzige Revision liefern.
Eine gestauchte Kette wiederum bräche jede Datenbank, die auf einer älteren
Revision steht — reva, ein zurückgespieltes Backup, ein älterer Abzug.

Deshalb hier, hinter dem heutigen Kopf, MIT WÄCHTER:

* **Bestehende Instanz** (Tabellen vorhanden): tut nichts. Nachweisbar daran,
  dass die Sentinel-Tabelle existiert.
* **Frische Datenbank**: der Bootstrap stempelt auf `pc20260924_restore_idx`
  und läuft dann `alembic upgrade head` — diese Migration ist das Einzige, was
  läuft, und sie bringt beides mit: die Tabellen aus den Modellen UND das
  Roh-SQL, das heute verlorengeht.

🛑 **Was das für KÜNFTIGE Migrationen heißt:** nichts Besonderes. Sie stehen
hinter dieser Basis und laufen auf einer Neuinstallation ganz normal mit. Nur
was VOR ihr liegt, deckt sie ab. Diese Basis muss also nie wieder nachgeführt
werden — ein neuer Roh-SQL-Index gehört in eine neue Migration, nicht hierher.

🛑 **Die Vektorindizes werden NICHT festgeschrieben.** Ihre Dimension wird zur
Laufzeit aus der Spalte gelesen (ROH, ohne `- 4`: pgvector legt N direkt im
typmod ab). `pc20260402` rechnete `atttypmod - 4` und hätte einen Index auf
halfvec(2556) gebaut, den keine Abfrage je hätte nutzen können — gebaut, bei
jedem Schreibvorgang gepflegt, nutzlos.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "pc20260926_baseline"
down_revision: Union[str, None] = "pc20260924_restore_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Existiert diese Tabelle, ist das Schema da und die Migration tut nichts.
#: `users` ist die aelteste und auf jeder Instanz vorhanden.
SENTINEL_TABLE = "users"

#: Vektorindizes: (Name, Tabelle, Spalte). Die Dimension kommt zur Laufzeit.
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

#: Alles uebrige woertlich aus der Produktion (2026-09-25), mit IF NOT EXISTS.
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
    """Dimension einer `vector(N)`-Spalte. ROH, ohne `- 4` — siehe Kopf."""
    row = conn.execute(text("""
        SELECT a.atttypmod FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = current_schema()
        WHERE c.relname = :t AND a.attname = :col AND a.attnum > 0 AND NOT a.attisdropped
    """), {"t": table, "col": column}).first()
    return int(row[0]) if row and row[0] and row[0] > 0 else None


def upgrade() -> None:
    conn = op.get_bind()

    # WAECHTER. Eine bestehende Instanz hat das Schema laengst; diese Migration
    # darf dort nichts tun, sonst liefe sie auf jeder Produktivdatenbank an.
    if _table_exists(conn, SENTINEL_TABLE):
        return

    # Ab hier: eine LEERE Datenbank.
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

    # Die Tabellen aus den Modellen. `ha_glue.models.database` MUSS importiert
    # werden: `Base.metadata` kennt nur Tabellen, deren Modul geladen wurde —
    # ohne diesen Import fehlten 12 Tabellen (rooms, satellites, ha_entities …).
    from models.database import Base
    import ha_glue.models.database  # noqa: F401 — fuellt Base.metadata

    Base.metadata.create_all(bind=conn)

    for ddl in _RAW_DDL:
        op.execute(ddl)

    for name, table, column in _VECTOR_INDEXES:
        if not _table_exists(conn, table):
            continue
        dim = _column_dim(conn, table, column)
        if dim is None:
            continue
        # halfvec ueber 2000 Dimensionen: der regulaere vector-Typ ist darueber
        # nicht indizierbar.
        expr = (f"(({column}::halfvec({dim})) halfvec_cosine_ops)" if dim > 2000
                else f"({column} vector_cosine_ops)")
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
                   f"USING hnsw {expr} WITH (m = 16, ef_construction = 64)")


def downgrade() -> None:
    """Bewusst ein Nichts — und das ist eine Aussage, keine Luecke.

    Der Rueckweg einer Basis waere „das ganze Schema loeschen". Das ist kein
    Rueckbau, das ist Datenverlust. Wer hinter diese Migration zurueck will,
    nimmt die Datenbank, die es vor ihr gab.

    DURCHLAUFEN, nicht behauptet (2026-09-26, beide Faelle):

        frische DB      333 Indizes -> downgrade -> 333 -> upgrade -> 333
        Bestandsschema  326 Indizes -> downgrade -> 326 -> upgrade -> 326

    🛑 Daraus folgt eine Eigenschaft, die man kennen muss: nach einem Rueckbau
    BLEIBT das Schema stehen, waehrend `alembic_version` behauptet, es gaebe es
    noch nicht. „Vorher" ist aus „nachher" hier nicht rekonstruierbar. Das ist
    in sich stimmig, weil der WAECHTER im `upgrade()` an der Sentinel-Tabelle
    entscheidet und nicht an der Revision — ein erneutes `upgrade` tut deshalb
    korrekt nichts, statt ein vorhandenes Schema neu bauen zu wollen.
    """
