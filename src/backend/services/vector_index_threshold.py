"""Measures whether the halfvec cast would pay off by now -- instead of guessing.

WHY IT EXISTS
-------------
An HNSW index on `(embedding::halfvec(N)) halfvec_cosine_ops` is an EXPRESSION
index: the planner only uses it on a syntactically matching `ORDER BY`. But the
required cast is only free when the planner then also CHOOSES the index. If it
does not, the halfvec conversion costs every row of a Seq Scan without
delivering anything.

That is why some queries deliberately do NOT cast (see `_WATCHED`). This
exception holds today and will at some point quietly become wrong: nothing
breaks, it only gets slow. So it needs a trigger.

🛑 **WHY ROW COUNT IS NOT THAT TRIGGER.** The first version reported from 4 000
rows upwards. The measurement on the second instance disproved that threshold on
the day it was introduced:

    xidra  document_chunks  3 165 rows  -> index NOT chosen (145 ms / 22 ms)
    xidra  kg_entities      1 953 rows  -> index CHOSEN     (4,1 ms / 14,7 ms)

The row count does not predict it. The reason is heap pages and index shape:
`document_chunks` has 416 pages at 2 847 rows, `kg_entities` only 150 at
4 813 -- and the estimated HNSW entry differs by a factor of eight, at FEWER
rows. A number that measures the wrong thing is not a trigger but a second trap.

So this task asks the tipping condition directly: **would the planner take the
index if the query cast?** A bare `EXPLAIN` answers that -- without running the
query, without load, on real data and real statistics. If it says yes, the cast
is due for that table.
"""
from __future__ import annotations

import json

from loguru import logger
from sqlalchemy import text

from services.database import AsyncSessionLocal

# Tables whose ORDER BY search deliberately does NOT cast to halfvec TODAY, with
# the files that would then have to be adjusted. The index exists regardless --
# it is cheap to keep and there immediately once it pays off.
_WATCHED: dict[str, tuple[str, ...]] = {
    "document_chunks": ("services/rag_retrieval.py", "services/rag_service.py"),
    "conversation_memories": (
        "services/memory_retrieval.py", "services/conversation_memory_service.py",
    ),
}


def _plan_uses_hnsw(plan: dict) -> str | None:
    """Name of the HNSW index in the plan, or None. Recursive over all subplans."""
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
    """Ask per watched table: would the planner take the index WITH a cast?

    Returns a short summary for the run log, or None when there is nothing to
    report -- the task stays silent in the normal case.
    """
    due: list[str] = []
    async with AsyncSessionLocal() as db:
        for table, sites in _WATCHED.items():
            exists = (await db.execute(
                text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{table}"}
            )).scalar()
            if not exists:
                continue

            # Dimension from the COLUMN. RAW, without `- 4`: pgvector stores N
            # directly in the typmod (the +4 offset is the varlena convention).
            # An earlier migration computed `- 4` and would have built an index
            # on halfvec(2556) that no query could ever have used.
            dim = (await db.execute(text("""
                SELECT a.atttypmod FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = :t AND a.attname = 'embedding' AND a.attnum > 0
            """), {"t": table})).scalar()
            if not dim or dim <= 0:
                continue

            # Without rows there is nothing to plan -- and an empty table says
            # nothing about the tipping condition.
            n = (await db.execute(
                text(f"SELECT count(*) FROM {table} WHERE embedding IS NOT NULL")
            )).scalar_one()
            if n == 0:
                continue

            # EXPLAIN ONLY, no ANALYZE: the question is what the planner
            # CHOOSES, not how long the query runs. That costs no execution and
            # puts no load on the production instance.
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
