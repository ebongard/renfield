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


# (Index, Tabelle, Spalte) — die HNSW-Vektorindizes.
_VECTOR_INDEXES = [
    ("idx_document_chunks_embedding_hnsw", "document_chunks", "embedding"),
    ("idx_kg_entities_embedding_hnsw", "kg_entities", "embedding"),
    ("ix_conversation_memories_embedding_hnsw", "conversation_memories", "embedding"),
    ("ix_episodic_embedding_hnsw", "episodic_memories", "embedding"),
    ("idx_intent_corrections_embedding_hnsw", "intent_corrections", "embedding"),
]

# Die übrigen drei, ohne Dimensionsabhängigkeit.
_PLAIN_INDEXES = [
    ("idx_document_chunks_doc_chunk", "document_chunks",
     "CREATE INDEX IF NOT EXISTS idx_document_chunks_doc_chunk "
     "ON document_chunks (document_id, chunk_index)"),
    # NICHT `idx_document_chunks_search_vector` — das ist der ALTE Name. Der
    # GIN heisst heute `..._gin`: `pc20260529` wirft die alte Spalte weg
    # (Postgres entfernt deren Index automatisch) und benennt den neuen um.
    # `rag_service.py:1207` sucht ihn NAMENTLICH unter `..._gin`. Den alten
    # Namen anzulegen hiesse: im Haushalt ein zweiter, vollstaendig redundanter
    # GIN auf derselben GENERATED-Spalte (doppelte Schreibkosten, null
    # Lesenutzen), auf xidra ein Index, den der Dienst nie findet. Gefunden im
    # adversarialen Review von #1336 — ich hatte die Liste aus den alten
    # Migrationen abgeschrieben, ohne den Rename mitzulesen.
    # Auf xidra fehlt der GIN wirklich; unter DIESEM Namen schliesst das die Luecke.
    ("idx_document_chunks_search_vector_gin", "document_chunks",
     "CREATE INDEX IF NOT EXISTS idx_document_chunks_search_vector_gin "
     "ON document_chunks USING gin(search_vector)"),
    ("ix_conv_memories_cleanup", "conversation_memories",
     "CREATE INDEX IF NOT EXISTS ix_conv_memories_cleanup "
     "ON conversation_memories (category, last_accessed_at) WHERE is_active = true"),
]

# Der Rückbau fasst NICHT alles an, was das `upgrade()` anlegt.
# `idx_document_chunks_search_vector_gin` existiert im Haushalt bereits (aus dem
# Rename in `pc20260529`), dort ist das `upgrade()` wegen `IF NOT EXISTS` ein
# Nichts — ein `DROP` im Rückbau würde also einen FREMDEN, produktiv genutzten
# Index entfernen und der lexikalischen Suche die Grundlage nehmen
# (`rag_service.py:1207` sucht ihn namentlich). Nur auf xidra legt ihn diese
# Migration wirklich an; dort bleibt er nach einem Rückbau stehen. Das ist die
# richtige Richtung des Irrtums: ein Index zu viel kostet Schreibzeit, ein
# fehlender kostet jede Suche.
_KEEP_ON_DOWNGRADE = {"idx_document_chunks_search_vector_gin"}

_ALL_NAMES = [
    n for n, _, _ in _VECTOR_INDEXES + _PLAIN_INDEXES
    if n not in _KEEP_ON_DOWNGRADE
]


def _column_dim(conn, table: str, column: str) -> int | None:
    """Dimension einer `vector(N)`-Spalte, oder None wenn es sie nicht gibt.

    ROH, ohne `- 4`: pgvector legt N direkt im typmod ab. Siehe Kopfkommentar.
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
    """`EMBEDDING_DIMENSION` aus der Konfiguration, oder None.

    Nur für eine Warnung. Eine Migration darf nicht daran scheitern, dass die
    Anwendungskonfiguration in diesem Kontext nicht importierbar ist — dann
    entfällt eben die Warnung, nicht die Reparatur.
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
        # Eine Instanz muss nicht jede Tabelle haben (dunkle Funktionen, andere
        # Ausbaustufe). Ein fehlendes Ziel ist kein Fehler, sondern ein Auslassen.
        if not _table_exists(conn, table):
            continue
        dim = _column_dim(conn, table, column)
        if dim is None:
            continue
        # Der Index wird mit der Dimension der SPALTE gebaut — sie ist die
        # Wahrheit darüber, was dort liegt. Die Abfragen der Dienste casten
        # dagegen auf `EMBEDDING_DIMENSION` aus der Konfiguration. Laufen die
        # beiden auseinander, ist der Index gebaut, gepflegt und wird NIE
        # benutzt: exakt der stumme Fehlermodus, gegen den diese Migration
        # antritt. Der Vorgabewert der Einstellung ist 768, produktiv steht
        # 2560 nur, weil die ConfigMap es setzt — eine Instanz ohne diesen
        # Schlüssel driftet also sofort. Das kann die Migration nicht
        # reparieren, aber sie kann es SAGEN statt es geschehen zu lassen.
        if _configured_dim is not None and _configured_dim != dim:
            logger.warning(
                "%s: Spalte %s.%s ist vector(%d), die Konfiguration sagt "
                "EMBEDDING_DIMENSION=%d. Der Index wird auf %d gebaut und von "
                "Abfragen, die auf %d casten, NIE benutzt werden.",
                name, table, column, dim, _configured_dim, dim, _configured_dim,
            )
        if dim > 2000:
            # halfvec, weil der reguläre vector-Typ beim Indizieren bei 2000 endet.
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
    # Rein additiv, also ist der Rückweg ein reines Entfernen. `IF EXISTS`, damit
    # ein Rückbau auf einer Instanz, die einen dieser Indizes nie hatte, nicht
    # abbricht — genau die Lage, aus der diese Migration hervorging.
    #
    # 🛑 EINE UNSCHÄRFE, die hier steht statt verschwiegen zu werden: auf einer
    # Instanz, die einen dieser Indizes SCHON HATTE, war das `upgrade()` wegen
    # `IF NOT EXISTS` ein Nichts — dieses `downgrade()` entfernt ihn trotzdem.
    # Der Rückbau stellt dort also nicht den Zustand von vorher her, sondern
    # einen schlechteren. Bewusst in Kauf genommen: es sind reine Indizes, kein
    # Datenverlust, in Sekunden neu gebaut (gemessen 762 ms je HNSW bei
    # Produktionsgröße), und ein Buchführen darüber, wer welchen Index angelegt
    # hat, wäre mehr Apparat als der Fehler wert. Wiederherstellung: `upgrade`
    # erneut. Auf BEIDEN Produktivinstanzen fehlen heute alle acht, dort ist der
    # Rundlauf exakt (gemessen: 326 → 334 → 326 → 334).
    for name in _ALL_NAMES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
