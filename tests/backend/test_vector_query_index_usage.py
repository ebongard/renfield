"""Jede ORDER-BY-Vektorsuche auf einer indizierten Tabelle castet auf halfvec.

Ein HNSW-Index auf `(embedding::halfvec(N)) halfvec_cosine_ops` ist ein
AUSDRUCKSindex: der Planer benutzt ihn nur, wenn der Abfrageausdruck
SYNTAKTISCH passt. `ORDER BY embedding <=> CAST(:embedding AS vector)` passt
nicht — gemessen am 2026-09-24 auf 5 000 Zeilen: ohne Cast Seq Scan (46 ms),
mit Cast Index Scan (0,57 ms).

🛑 Warum das ein eigener Test ist und kein Kommentar: der Fehler bricht nichts.
Die Abfrage liefert dieselben Zeilen, nur langsam, und der Index wird bei jedem
Schreibvorgang gepflegt, ohne je gelesen zu werden. Das fällt in keinem
Funktionstest auf. Genau so ist es passiert: fünf HNSW-Indizes wurden angelegt
und vier davon konnte keine Produktionsabfrage benutzen — entdeckt erst im
adversarialen Review von PR #1336, nicht von einer roten Suite.

Als TABELLE über alle Dienste, nicht als neun Einzelprüfungen: derselbe Zweig
hat dreimal gezeigt, dass eine Prüfung pro benanntem Fall die Geschwister
stehen lässt. Eine neue Abfrage auf einer indizierten Tabelle ohne Cast färbt
diesen Test rot, ohne dass jemand daran denken muss.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]


def _services_dir() -> Path:
    import services.database as _db

    return Path(_db.__file__).resolve().parent


# Tabellen, auf denen ein HNSW-Vektorindex liegt (live vorhanden oder von
# `pc20260924_restore_idx` angelegt). Nur für sie zahlt sich der Cast aus —
# auf einer Tabelle OHNE Index wäre er reine Rechenzeit.
INDEXED_TABLES = {
    "document_chunks", "kg_entities", "conversation_memories",
    "episodic_memories", "intent_corrections", "procedural_skills",
    "documents", "notes", "paperless_extraction_examples",
    "meeting_speaker_fingerprints",
}

# Eine gewichtete Rangfolge kann eine HNSW-Suche prinzipiell nicht bedienen
# (sie ordnet nur nach reiner Distanz). Solche Stellen tragen bewusst keinen
# Cast; sie stehen hier namentlich, damit „Ausnahme" eine Entscheidung bleibt
# und nicht zur Lücke wird.
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
            # Tabelle aus dem umgebenden SQL-Block bestimmen.
            ctx = "\n".join(lines[max(0, i - 30):i + 5])
            tables = set(re.findall(r"FROM\s+([a-z_]+)", ctx)) | set(
                re.findall(r"JOIN\s+([a-z_]+)", ctx))
            if not (tables & INDEXED_TABLES):
                continue
            out.append((path.name, i, line.strip()))
    return out


def test_there_are_vector_order_by_sites_at_all():
    """Ohne diese Zusicherung wäre eine kaputte Erkennung eine leere Schleife.

    Grün und wertlos — derselbe Fehler, der im Refus-Code-Test dieses Tages
    schon einmal eine Lücke verdeckt hat.
    """
    assert len(_order_by_vector_lines()) >= 8


def test_every_indexed_vector_order_by_casts_to_halfvec():
    offenders: list[str] = []
    for name, lineno, line in _order_by_vector_lines():
        if any(name == f and marker in line for f, marker in WEIGHTED_EXEMPT):
            continue
        if "halfvec" not in line:
            offenders.append(f"{name}:{lineno}  {line[:100]}")
    assert offenders == [], (
        "ORDER-BY-Vektorsuche auf einer indizierten Tabelle OHNE halfvec-Cast — "
        "der HNSW-Index wird dort nicht benutzt (Seq Scan), gepflegt wird er "
        "trotzdem:\n  " + "\n  ".join(offenders)
    )


def test_the_cast_appears_on_both_sides():
    """Ein Cast nur auf einer Seite reicht nicht — beide Operanden müssen halfvec sein."""
    offenders: list[str] = []
    for name, lineno, line in _order_by_vector_lines():
        if any(name == f and marker in line for f, marker in WEIGHTED_EXEMPT):
            continue
        if line.count("halfvec") < 2:
            offenders.append(f"{name}:{lineno}  {line[:100]}")
    assert offenders == [], (
        "halfvec steht nur auf EINER Seite des <=>:\n  " + "\n  ".join(offenders))
