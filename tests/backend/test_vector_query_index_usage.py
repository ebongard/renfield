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

# Bewusste Ausnahmen. Sie stehen NAMENTLICH hier, damit „Ausnahme" eine
# Entscheidung bleibt und nicht zur Lücke wird — und jede trägt ihren Grund.
#
# (a) Gewichtete Rangfolge: eine HNSW-Suche ordnet nur nach reiner Distanz,
#     eine Formel mit importance/confidence ist prinzipiell nicht indexfähig.
# (b) Tabellen, bei denen der Planer den Index BEI IHRER GRÖSSE nicht wählt.
#     Der Cast ist nur gratis, wenn der Index daraufhin benutzt wird; sonst
#     kostet die halfvec-Umwandlung jede Zeile eines Seq Scans umsonst.
#     Gemessen im Haushalt 2026-09-25 an echten Daten:
#       kg_entities            4 813 Zeilen   4,6 ms mit Cast (hnsw) / 34 ms ohne
#       document_chunks        2 122 Zeilen   101 ms mit Cast (seq)  / 16 ms ohne
#       conversation_memories     81 Zeilen   4,1 ms mit Cast (seq)  / 0,9 ms ohne
#     AUSLÖSER gegen das Vergessen: `services/vector_index_threshold.py` fragt
#     täglich den PLANER, ob er den Index bei gecasteter Form nähme — nicht die
#     Zeilenzahl. Die sagt es nicht vorher: xidra nutzt den Index auf
#     kg_entities schon bei 1 953 Zeilen, auf document_chunks bei 3 165 nicht.
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
    """Ein Cast nur auf einer Seite reicht nicht — beide Operanden müssen halfvec sein."""
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
    """Jede Groessen-Ausnahme muss im Ausloeser stehen — sonst verfaellt sie stumm.

    Eine Ausnahme ohne Ueberwachung ist ein Befund mit Verfallsdatum: sie
    stimmt heute und wird irgendwann still falsch, ohne dass etwas kaputtgeht
    — es wird nur langsam. Deshalb muss jede hier ausgenommene Datei auch in
    `services/vector_index_threshold.py` beobachtet werden.
    """
    watched = (_services_dir() / "vector_index_threshold.py").read_text(encoding="utf-8")
    missing = [f for f in SCALE_EXEMPT_FILES if f not in watched]
    assert missing == [], (
        "Diese Dateien sind vom halfvec-Cast ausgenommen, werden aber von der "
        "Schwellen-Aufgabe NICHT beobachtet — die Ausnahme koennte still "
        "veralten:\n  " + "\n  ".join(missing))
