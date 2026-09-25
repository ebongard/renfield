"""Meldet, wenn eine Tabelle die Groesse erreicht, ab der ihr HNSW-Index lohnt.

WARUM ES DAS GIBT
-----------------
Ein HNSW-Index auf `(embedding::halfvec(N)) halfvec_cosine_ops` ist ein
AUSDRUCKSindex: der Planer benutzt ihn nur bei syntaktisch passendem
`ORDER BY`. Der noetige Cast ist aber nur dann gratis, wenn der Planer den Index
daraufhin auch WAEHLT. Tut er es nicht, kostet die halfvec-Umwandlung jede
Zeile eines Seq Scans, ohne etwas einzubringen.

Gemessen im Haushalt am 2026-09-25, echte Daten, nicht synthetisch:

    kg_entities            4 813 Zeilen   mit Cast   4,6 ms (hnsw)   ohne  34 ms
    document_chunks        2 122 Zeilen   mit Cast   101 ms (seq)    ohne  16 ms
    conversation_memories     81 Zeilen   mit Cast   4,1 ms (seq)    ohne 0,9 ms

Die Schwelle liegt also zwischen 2 122 und 4 813 Zeilen — fuer DIESE
Datenverteilung. Ein Bank-Test mit 5 000 ZUFALLSvektoren hatte 0,57 ms gegen
46 ms gezeigt und damit weit zu optimistisch: Zufallsvektoren liegen weit
auseinander, echte Einbettungen dicht beieinander, und HNSW muss dort viel mehr
absuchen. Aus dem synthetischen Gewinn auf den echten zu schliessen war der
Fehler, den diese Aufgabe kuenftig verhindert.

🛑 **Diese Aufgabe behauptet NICHT, dass der Index ab der Schwelle lohnt.** Sie
sagt: hier ist neu zu messen. Ein Befund mit Verfallsdatum braucht einen
Ausloeser, sonst ist er nur ein Kommentar, den niemand wiederfindet.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text

from services.database import AsyncSessionLocal

# Zeilenzahl, ab der neu zu messen ist. Zwischen den beiden gemessenen Punkten
# gewaehlt und bewusst konservativ: lieber einmal zu frueh nachsehen als eine
# langsame Suche monatelang nicht bemerken.
RECHECK_ABOVE_ROWS = 4000

# Tabellen, deren ORDER-BY-Suche HEUTE bewusst NICHT auf halfvec castet, weil
# der Planer den Index bei ihrer Groesse nicht waehlt. Der Index existiert
# trotzdem (er ist billig zu halten und sofort da, wenn er lohnt).
_WATCHED: dict[str, tuple[str, ...]] = {
    "document_chunks": ("services/rag_retrieval.py", "services/rag_service.py"),
    "conversation_memories": (
        "services/memory_retrieval.py", "services/conversation_memory_service.py",
    ),
}


async def check_vector_index_thresholds() -> str | None:
    """Zaehle die beobachteten Tabellen und melde jede ueber der Schwelle.

    Rueckgabe: eine kurze Zusammenfassung fuer das Laufprotokoll, oder None,
    wenn nichts zu melden ist (die Aufgabe schweigt im Normalfall).
    """
    crossed: list[str] = []
    async with AsyncSessionLocal() as db:
        for table, sites in _WATCHED.items():
            # to_regclass: eine Instanz muss nicht jede Tabelle haben.
            exists = (await db.execute(
                text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}
            )).scalar()
            if not exists:
                continue
            # Nur Zeilen MIT Einbettung — nur die landen im Index und nur sie
            # bestimmen, ob sich der Indexscan lohnt.
            n = (await db.execute(
                text(f"SELECT count(*) FROM {table} WHERE embedding IS NOT NULL")  # noqa: S608
            )).scalar_one()
            if n > RECHECK_ABOVE_ROWS:
                crossed.append(f"{table}={n}")
                logger.warning(
                    f"🔎 Vektorindex-Schwelle: {table} hat {n} eingebettete Zeilen "
                    f"(> {RECHECK_ABOVE_ROWS}). Die ORDER-BY-Suche in "
                    f"{', '.join(sites)} castet bewusst NICHT auf halfvec, weil der "
                    f"Planer den Index bei kleinerer Tabelle nicht waehlte. JETZT NEU "
                    f"MESSEN (EXPLAIN ANALYZE mit und ohne Cast) und den Cast "
                    f"gegebenenfalls setzen — nicht annehmen, dass er inzwischen lohnt."
                )
    return f"ueber der Schwelle: {', '.join(crossed)}" if crossed else None
