"""Misst, ob der halfvec-Cast sich inzwischen lohnen wuerde — statt es zu raten.

WARUM ES DAS GIBT
-----------------
Ein HNSW-Index auf `(embedding::halfvec(N)) halfvec_cosine_ops` ist ein
AUSDRUCKSindex: der Planer benutzt ihn nur bei syntaktisch passendem
`ORDER BY`. Der noetige Cast ist aber nur dann gratis, wenn der Planer den Index
daraufhin auch WAEHLT. Tut er es nicht, kostet die halfvec-Umwandlung jede
Zeile eines Seq Scans, ohne etwas einzubringen.

Deshalb casten einige Abfragen bewusst NICHT (siehe `_WATCHED`). Diese Ausnahme
stimmt heute und wird irgendwann still falsch: es geht nichts kaputt, es wird
nur langsam. Sie braucht also einen Ausloeser.

🛑 **WARUM DIESER AUSLOESER NICHT ZAEHLT.** Die erste Fassung meldete ab 4 000
Zeilen. Die Messung auf der zweiten Instanz hat diese Schwelle am Tag ihrer
Einfuehrung widerlegt:

    xidra  document_chunks  3 165 Zeilen  -> Index NICHT gewaehlt (145 ms / 22 ms)
    xidra  kg_entities      1 953 Zeilen  -> Index GEWAEHLT       (4,1 ms / 14,7 ms)

Die Zeilenzahl sagt es nicht vorher. Der Grund sind Heap-Seiten und Indexform:
`document_chunks` hat 416 Seiten bei 2 847 Zeilen, `kg_entities` nur 150 bei
4 813 — und der geschaetzte HNSW-Einstieg unterscheidet sich um Faktor acht, bei
WENIGER Zeilen. Eine Zahl, die das Falsche misst, ist kein Ausloeser, sondern
eine zweite Falle.

Also fragt diese Aufgabe direkt nach der Kippbedingung: **wuerde der Planer den
Index nehmen, wenn die Abfrage casten wuerde?** Das beantwortet ein blosses
`EXPLAIN` — ohne die Abfrage auszufuehren, ohne Last, auf echten Daten und
echten Statistiken. Sagt er ja, ist der Cast fuer diese Tabelle faellig.
"""
from __future__ import annotations

import json

from loguru import logger
from sqlalchemy import text

from services.database import AsyncSessionLocal

# Tabellen, deren ORDER-BY-Suche HEUTE bewusst NICHT auf halfvec castet, mit den
# Dateien, die dann anzupassen waeren. Der Index existiert trotzdem — er ist
# billig zu halten und sofort da, sobald er lohnt.
_WATCHED: dict[str, tuple[str, ...]] = {
    "document_chunks": ("services/rag_retrieval.py", "services/rag_service.py"),
    "conversation_memories": (
        "services/memory_retrieval.py", "services/conversation_memory_service.py",
    ),
}


def _plan_uses_hnsw(plan: dict) -> str | None:
    """Name des HNSW-Index im Plan, oder None. Rekursiv ueber alle Teilplaene."""
    name = plan.get("Index Name")
    if isinstance(name, str) and "hnsw" in name.lower():
        return name
    for key in ("Plans", "Plan"):
        sub = plan.get(key)
        if isinstance(sub, dict):
            sub = [sub]
        for child in sub or []:
            found = _plan_uses_hnsw(child)
            if found:
                return found
    return None


async def check_vector_index_thresholds() -> str | None:
    """Frage je beobachteter Tabelle: naehme der Planer den Index MIT Cast?

    Rueckgabe: kurze Zusammenfassung fuers Laufprotokoll, oder None, wenn nichts
    zu melden ist — die Aufgabe schweigt im Normalfall.
    """
    due: list[str] = []
    async with AsyncSessionLocal() as db:
        for table, sites in _WATCHED.items():
            exists = (await db.execute(
                text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}
            )).scalar()
            if not exists:
                continue

            # Dimension aus der SPALTE. ROH, ohne `- 4`: pgvector legt N direkt
            # im typmod ab (der +4-Versatz ist die varlena-Konvention). Eine
            # frühere Migration rechnete `- 4` und haette einen Index auf
            # halfvec(2556) gebaut, den keine Abfrage je haette nutzen koennen.
            dim = (await db.execute(text("""
                SELECT a.atttypmod FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = :t AND a.attname = 'embedding' AND a.attnum > 0
            """), {"t": table})).scalar()
            if not dim or dim <= 0:
                continue

            # Ohne Zeilen gibt es nichts zu planen — und eine leere Tabelle
            # sagt ueber die Kippbedingung nichts aus.
            n = (await db.execute(
                text(f"SELECT count(*) FROM {table} WHERE embedding IS NOT NULL")
            )).scalar_one()
            if n == 0:
                continue

            # NUR EXPLAIN, kein ANALYZE: die Frage ist, was der Planer WAEHLT,
            # nicht wie lange die Abfrage laeuft. Das kostet keine Ausfuehrung
            # und belastet die Produktivinstanz nicht.
            probe = (
                f"SELECT id FROM {table} WHERE embedding IS NOT NULL "
                f"ORDER BY embedding::halfvec({dim}) <=> "
                f"CAST((SELECT embedding FROM {table} WHERE embedding IS NOT NULL "
                f"LIMIT 1) AS halfvec({dim})) LIMIT 10"
            )
            try:
                raw = (await db.execute(
                    text(f"EXPLAIN (FORMAT JSON) {probe}")
                )).scalar_one()
            except Exception as exc:  # pragma: no cover - Planerfehler ist kein Ausfall
                logger.debug(f"Vektorindex-Probe fuer {table} fehlgeschlagen: {exc}")
                continue

            plans = json.loads(raw) if isinstance(raw, str) else raw
            index_name = _plan_uses_hnsw(plans[0]["Plan"]) if plans else None
            if not index_name:
                continue

            due.append(f"{table}={n}")
            logger.warning(
                f"🔎 Vektorindex faellig: der Planer wuerde fuer {table} jetzt "
                f"{index_name} nehmen, wenn die Abfrage auf halfvec({dim}) casten "
                f"wuerde ({n} eingebettete Zeilen). Heute castet sie bewusst nicht, "
                f"weil der Index bei kleinerer Tabelle NICHT gewaehlt wurde und die "
                f"Umwandlung dann jede Zeile umsonst kostet. Jetzt gegenmessen "
                f"(EXPLAIN ANALYZE mit und ohne Cast) und den Cast in "
                f"{', '.join(sites)} setzen."
            )
    return f"Cast faellig fuer: {', '.join(due)}" if due else None
