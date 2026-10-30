"""Every ORDER-BY vector search on an indexed table casts to halfvec.

An HNSW index on `(embedding::halfvec(N)) halfvec_cosine_ops` is an EXPRESSION
index: the planner uses it only when the query expression matches
SYNTACTICALLY. `ORDER BY embedding <=> CAST(:embedding AS vector)` does not --
measured on 2026-09-24 over 5 000 rows: without the cast a Seq Scan (46 ms),
with the cast an Index Scan (0.57 ms).

🛑 Why this is a test of its own and not a comment: the defect breaks nothing.
The query returns the same rows, only slowly, and the index is maintained on
every write without ever being read. No functional test notices that. That is
exactly how it happened: five HNSW indexes were created and no production query
could use four of them -- discovered only in the adversarial review of PR
#1336, not by a red suite.

As a TABLE across all services, not as nine individual checks: the same branch
has shown three times that one check per named case leaves the siblings
standing. A new query on an indexed table without the cast turns this test red
without anybody having to remember it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]


def _services_dir() -> Path:
    import services.database as _db

    return Path(_db.__file__).resolve().parent


# Tables that carry an HNSW vector index (present live, or created by
# `pc20260924_restore_idx`). The cast pays off only for these -- on a table
# WITHOUT an index it would be pure compute time.
INDEXED_TABLES = {
    "document_chunks", "kg_entities", "conversation_memories",
    "episodic_memories", "intent_corrections", "procedural_skills",
    "documents", "notes", "paperless_extraction_examples",
    "meeting_speaker_fingerprints",
}

# Deliberate exemptions. They stand here BY NAME, so that "exemption" stays a
# decision and does not turn into a gap -- and each one carries its reason.
#
# (a) Weighted ranking: an HNSW search orders by pure distance only; a formula
#     with importance/confidence is not indexable in principle.
# (b) Tables where the planner does not choose the index AT THEIR SIZE. The
#     cast is only free when the index is used as a result; otherwise the
#     halfvec conversion costs every row of a Seq Scan for nothing.
#     Measured in the household on 2026-09-25 against real data:
#       kg_entities            4 813 rows     4.6 ms with cast (hnsw) / 34 ms without
#       document_chunks        2 122 rows     101 ms with cast (seq)  / 16 ms without
#       conversation_memories     81 rows     4.1 ms with cast (seq)  / 0.9 ms without
#     TRIGGER against forgetting: `services/vector_index_threshold.py` asks
#     the PLANNER daily whether it would take the index in the cast form --
#     not the row count. The row count does not predict it: xidra uses the
#     index on kg_entities already at 1 953 rows, and not on document_chunks
#     at 3 165.
SCALE_EXEMPT_FILES = {
    "rag_retrieval.py", "rag_service.py",
    "memory_retrieval.py", "conversation_memory_service.py",
}

WEIGHTED_EXEMPT = {
    ("memory_retrieval.py", "* importance * confidence"),
}


def _order_by_vector_lines() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for path in sorted(_services_dir().glob("*.py")):
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        for i, line in enumerate(lines, 1):
            if "ORDER BY" not in line or "<=>" not in line:
                continue
            # Determine the table from the surrounding SQL block.
            ctx = "\n".join(lines[max(0, i - 30):i + 5])
            tables = set(re.findall(r"FROM\s+([a-z_]+)", ctx)) | set(
                re.findall(r"JOIN\s+([a-z_]+)", ctx))
            if not (tables & INDEXED_TABLES):
                continue
            out.append((path.name, i, line.strip()))
    return out


def test_there_are_vector_order_by_sites_at_all():
    """Without this assurance a broken detector would be an empty loop.

    Green and worthless -- the same mistake that had already hidden a gap in
    that day's refusal-code test.
    """
    assert len(_order_by_vector_lines()) >= 8


def test_every_indexed_vector_order_by_casts_to_halfvec():
    offenders: list[str] = []
    for name, lineno, line in _order_by_vector_lines():
        if any(name == f and marker in line for f, marker in WEIGHTED_EXEMPT):
            continue
        if name in SCALE_EXEMPT_FILES:
            continue
        if "halfvec" not in line:
            offenders.append(f"{name}:{lineno}  {line[:100]}")
    assert offenders == [], (
        "ORDER-BY-Vektorsuche auf einer indizierten Tabelle OHNE halfvec-Cast — "
        "der HNSW-Index wird dort nicht benutzt (Seq Scan), gepflegt wird er "
        "trotzdem:\n  " + "\n  ".join(offenders)
    )


def test_the_cast_appears_on_both_sides():
    """A cast on one side is not enough -- both operands must be halfvec."""
    offenders: list[str] = []
    for name, lineno, line in _order_by_vector_lines():
        if any(name == f and marker in line for f, marker in WEIGHTED_EXEMPT):
            continue
        if name in SCALE_EXEMPT_FILES:
            continue
        if line.count("halfvec") < 2:
            offenders.append(f"{name}:{lineno}  {line[:100]}")
    assert offenders == [], (
        "halfvec steht nur auf EINER Seite des <=>:\n  " + "\n  ".join(offenders))


def test_the_scale_exemption_is_documented_and_watched():
    """Every size exemption must appear in the trigger -- or it expires mutely.

    An exemption without monitoring is a finding with an expiry date: it is
    true today and goes silently wrong at some point, without anything
    breaking -- it merely gets slow. So every file exempted here must also be
    watched in `services/vector_index_threshold.py`.
    """
    watched = (_services_dir() / "vector_index_threshold.py").read_text(encoding="utf-8")
    missing = [f for f in SCALE_EXEMPT_FILES if f not in watched]
    assert missing == [], (
        "Diese Dateien sind vom halfvec-Cast ausgenommen, werden aber von der "
        "Schwellen-Aufgabe NICHT beobachtet — die Ausnahme koennte still "
        "veralten:\n  " + "\n  ".join(missing))
