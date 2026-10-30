"""restore indexes lost to the fresh-install bootstrap

Revision ID: pc20260924_restore_idx
Revises: pc20260922_conv_meeting_atoms
Create Date: 2026-09-24

WARUM ES DIESE MIGRATION GIBT
-----------------------------
`services/database.py:_ensure_alembic_baseline()` erzeugt das Schema einer
Neuinstallation aus den SQLAlchemy-Modellen (`create_all`) und STEMPELT dann den
Alembic-Kopf. Der Docstring dort sagt es selbst: „the 41-migration history is
skipped". Folge: alles, was eine Migration per `op.execute("CREATE INDEX …")`
anlegt, entsteht auf einer Neuinstallation NIE — und wird nie nachgeholt, weil
der Stempel behauptet, die Migration sei angewandt.

Nachgewiesen am 2026-09-24, 37 Datenpunkte ohne eine Ausnahme: jeder per
Roh-SQL angelegte Index aus einer Migration bis 2026-04-02 fehlt auf BEIDEN
Instanzen, jeder ab 2026-04-25 ist vorhanden. Die Haushaltsdatenbank wurde am
2026-04-18 aufgesetzt. Der Schnitt liegt exakt dort.

Die Ironie: `models/database.py:748` schreibt ausdrücklich „Vector-search index
is created at migration time, NOT by SQLAlchemy `create_all`" und notiert das
exakte DDL darunter. Der Code weiß es — der Bootstrap-Pfad hebelt es aus.

Diese Migration REPARIERT die bestehenden Instanzen. Sie behebt NICHT die
Ursache: die nächste Neuinstallation verliert dasselbe erneut. Dafür braucht es
eine Basis-Migration, die das Grundschema selbst erzeugt — eigenes Vorhaben,
denn die Kette läuft heute nicht aus dem Leeren (die Wurzelmigration
`9a0d8ccea5b0` ist ein leerer Rumpf, `rooms` legt keine Migration je an).

ZWEI FALLEN, DIE BEIM ABSCHREIBEN DES ALTEN DDL ZUGESCHLAGEN HÄTTEN
-------------------------------------------------------------------
1. **`atttypmod - 4` ist falsch.** pgvector legt die Dimension DIREKT im typmod
   ab; der `+4`-Versatz ist die varlena-Konvention (varchar), nicht die von
   `vector`. Live gemessen: `vector(2560)` hat `atttypmod = 2560`.
   `pc20260402_add_episodic_hnsw_index.py:60` rechnet `atttypmod - 4` und hätte
   einen Index auf `halfvec(2556)` gebaut — Abfragen casten auf `halfvec(2560)`,
   der Index wäre also NIE benutzbar gewesen: gebaut, bei jedem Schreibvorgang
   gepflegt, nutzlos. `y8z9a0b1c2d3` macht es richtig und liest roh.
2. **`vector_cosine_ops` geht bei 2560 Dimensionen nicht.** pgvector begrenzt
   den regulären `vector`-Typ beim Indizieren auf 2000. Das alte DDL für
   `idx_intent_corrections_embedding_hnsw` nutzt `vector_cosine_ops` — das
   stammt aus der Zeit von 768-dim-Embeddings und ist heute schlicht nicht
   ausführbar. Deshalb entscheidet hier die GEMESSENE Dimension über die
   Operatorklasse, statt sie festzuschreiben.

BEWUSST NICHT WIEDERHERGESTELLT
-------------------------------
`ix_kg_entities_embedding` (aus `g7h8i9j0k1l3`) ist der VORGÄNGER von
`idx_kg_entities_embedding_hnsw`: `vector_cosine_ops` auf einer 2560-dim-Spalte,
heute nicht anlegbar und durch die halfvec-Fassung ersetzt. Ihn „der
Vollständigkeit halber" mitzunehmen hieße, einen toten Index zu bauen.

SPERRDAUER
----------
Gemessen auf der Baubox an 5 000 Zeilen × 2560 Dimensionen (Produktionsgröße:
2 847–4 813 Zeilen): **762 ms** je HNSW-Index. Alle acht zusammen bleiben im
Sekundenbereich, deshalb gewöhnliches `CREATE INDEX` in der Transaktion und
kein `CONCURRENTLY` (das ginge in einer Alembic-Transaktion ohnehin nicht).
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

logger = logging.getLogger("alembic.runtime.migration")

revision: str = 'pc20260924_restore_idx'
down_revision: Union[str, None] = 'pc20260922_conv_meeting_atoms'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (index, table, column) -- the HNSW vector indexes.
_VECTOR_INDEXES = [
    ("idx_document_chunks_embedding_hnsw", "document_chunks", "embedding"),
    ("idx_kg_entities_embedding_hnsw", "kg_entities", "embedding"),
    ("ix_conversation_memories_embedding_hnsw", "conversation_memories", "embedding"),
    ("ix_episodic_embedding_hnsw", "episodic_memories", "embedding"),
    ("idx_intent_corrections_embedding_hnsw", "intent_corrections", "embedding"),
]

# The other three, with no dependency on the dimension.
_PLAIN_INDEXES = [
    ("idx_document_chunks_doc_chunk", "document_chunks",
     "CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_chunk "
     "ON document_chunks (document_id, chunk_index)"),
    # NOT `idx_document_chunks_search_vector` -- that is the OLD name. Today
    # the GIN is called `..._gin`: `pc20260529` drops the old column (Postgres
    # removes its index automatically) and renames the new one.
    # `rag_service.py:1207` looks it up BY NAME as `..._gin`. Creating the old
    # name would mean: in the household a second, fully redundant GIN on the
    # same GENERATED column (double write cost, zero read benefit), on xidra an
    # index the service never finds. Found in the adversarial review of #1336
    # -- I had copied the list from the old migrations without reading the
    # rename along with it.
    # On xidra the GIN really is missing; under THIS name that closes the gap.
    ("idx_document_chunks_search_vector_gin", "document_chunks",
     "CREATE INDEX IF NOT EXISTS idx_document_chunks_search_vector_gin "
     "ON document_chunks USING gin(search_vector)"),
    ("ix_conv_memories_cleanup", "conversation_memories",
     "CREATE INDEX IF NOT EXISTS ix_conv_memories_cleanup "
     "ON conversation_memories (category, last_accessed_at) WHERE is_active = true"),
]

# The downgrade does NOT touch everything `upgrade()` creates.
# `idx_document_chunks_search_vector_gin` already exists in the household (from
# the rename in `pc20260529`); there `upgrade()` is a no-op because of
# `IF NOT EXISTS` -- so a `DROP` in the downgrade would remove a FOREIGN index
# that is in production use and pull the ground out from under the lexical
# search (`rag_service.py:1207` looks it up by name). Only on xidra does this
# migration really create it; there it stays standing after a downgrade. That
# is the right direction for the error to point: one index too many costs write
# time, a missing one costs every search.
_KEEP_ON_DOWNGRADE = {"idx_document_chunks_search_vector_gin"}

_ALL_NAMES = [
    n for n, _, _ in _VECTOR_INDEXES + _PLAIN_INDEXES
    if n not in _KEEP_ON_DOWNGRADE
]


def _column_dim(conn, table: str, column: str) -> int | None:
    """Dimension of a `vector(N)` column, or None when there is none.

    RAW, without `- 4`: pgvector stores N directly in the typmod. See the
    header comment.
    """
    row = conn.execute(text("""
        SELECT a.atttypmod
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relname = :t AND a.attname = :col AND a.attnum > 0
          AND NOT a.attisdropped
    """), {"t": table, "col": column}).first()
    if row is None or row[0] is None or row[0] <= 0:
        return None
    return int(row[0])


def _table_exists(conn, table: str) -> bool:
    return conn.execute(text(
        "SELECT to_regclass(current_schema() || '.' || :t) IS NOT NULL"
    ), {"t": table}).scalar() is True


def _configured_embedding_dim() -> int | None:
    """`EMBEDDING_DIMENSION` from the configuration, or None.

    For a warning only. A migration must not fail because the application
    configuration is not importable in this context -- then the warning is what
    falls away, not the repair.
    """
    try:
        from utils.config import settings

        return int(settings.embedding_dimension)
    except Exception:
        return None


def upgrade() -> None:
    conn = op.get_bind()
    _configured_dim = _configured_embedding_dim()

    for name, table, column in _VECTOR_INDEXES:
        # An instance need not have every table (dark features, a different
        # stage of build-out). A missing target is not an error, it is a skip.
        if not _table_exists(conn, table):
            continue
        dim = _column_dim(conn, table, column)
        if dim is None:
            continue
        # The index is built with the dimension of the COLUMN -- that is the
        # truth about what lies there. The services' queries, by contrast, cast
        # to `EMBEDDING_DIMENSION` from the configuration. If the two drift
        # apart, the index is built, maintained and NEVER used: exactly the
        # silent failure mode this migration exists to fix. The setting's
        # default is 768; production runs 2560 only because the ConfigMap sets
        # it -- so an instance without that key drifts immediately. The
        # migration cannot repair that, but it can SAY it instead of letting it
        # happen.
        if _configured_dim is not None and _configured_dim != dim:
            logger.warning(
                "%s: Spalte %s.%s ist vector(%d), die Konfiguration sagt "
                "EMBEDDING_DIMENSION=%d. Der Index wird auf %d gebaut und von "
                "Abfragen, die auf %d casten, NIE benutzt werden.",
                name, table, column, dim, _configured_dim, dim, _configured_dim,
            )
        if dim > 2000:
            # halfvec, because the regular vector type stops at 2000 when indexing.
            expr = f"(({column}::halfvec({dim})) halfvec_cosine_ops)"
        else:
            expr = f"({column} vector_cosine_ops)"
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
            f"USING hnsw {expr} WITH (m = 16, ef_construction = 64)"
        )

    for name, table, ddl in _PLAIN_INDEXES:
        if not _table_exists(conn, table):
            continue
        op.execute(ddl)


def downgrade() -> None:
    # Purely additive, so the way back is a pure removal. `IF EXISTS`, so a
    # downgrade on an instance that never had one of these indexes does not
    # abort -- exactly the situation this migration came out of.
    #
    # 🛑 ONE IMPRECISION, stated here instead of passed over in silence: on an
    # instance that ALREADY HAD one of these indexes, `upgrade()` was a no-op
    # because of `IF NOT EXISTS` -- this `downgrade()` removes it regardless.
    # There the downgrade does not restore the state from before but a worse
    # one. Deliberately accepted: these are pure indexes, no data loss, rebuilt
    # in seconds (measured 762 ms per HNSW at production size), and keeping
    # books on who created which index would be more apparatus than the error
    # is worth. Recovery: run `upgrade` again. On BOTH production instances all
    # eight are missing today, so there the round trip is exact (measured:
    # 326 -> 334 -> 326 -> 334).
    for name in _ALL_NAMES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
