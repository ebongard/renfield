"""Der Ausloeser fragt den PLANER, nicht die Zeilenzahl.

Die erste Fassung meldete ab 4 000 Zeilen. Die Messung auf der zweiten Instanz
hat diese Schwelle am Tag ihrer Einfuehrung widerlegt:

    xidra  document_chunks  3 165 Zeilen  -> Index NICHT gewaehlt (145 ms / 22 ms)
    xidra  kg_entities      1 953 Zeilen  -> Index GEWAEHLT       (4,1 ms / 14,7 ms)

Die Zeilenzahl sagt die Kippbedingung nicht vorher — Heap-Seiten und Indexform
tun es (`document_chunks`: 416 Seiten bei 2 847 Zeilen, `kg_entities`: 150 bei
4 813, geschaetzter HNSW-Einstieg Faktor acht auseinander, bei WENIGER Zeilen).

Also fragt die Aufgabe direkt: naehme der Planer den Index, wenn die Abfrage
casten wuerde? Das beantwortet ein blosses `EXPLAIN`. Nachgewiesen auf beiden
Produktivinstanzen am 2026-09-25: beide beobachteten Tabellen schweigen,
`kg_entities` (das castet) schlaegt an — auf BEIDEN, obwohl die Zeilenzahlen
dort gegenlaeufig sind.

Dieser Test deckt das Plan-Lesen ab: der Indexname steckt beliebig tief in der
JSON-Struktur, und ein Ausloeser, der ihn uebersieht, schweigt fuer immer —
lautlos, ohne dass etwas kaputtgeht.
"""
from __future__ import annotations

import pytest

from services.vector_index_threshold import _WATCHED, _plan_uses_hnsw

pytestmark = [pytest.mark.unit]


class TestPlanWalking:
    def test_finds_the_index_at_the_top_level(self):
        plan = {"Node Type": "Index Scan", "Index Name": "idx_kg_entities_embedding_hnsw"}
        assert _plan_uses_hnsw(plan) == "idx_kg_entities_embedding_hnsw"

    def test_finds_it_nested_under_limit_and_subplans(self):
        # Die echte Form: Limit -> Index Scan, mit einem InitPlan daneben. Genau
        # so sieht der Plan der Sonde aus; eine Suche nur auf oberster Ebene
        # wuerde ihn uebersehen und der Ausloeser bliebe fuer immer stumm.
        plan = {
            "Node Type": "Limit",
            "Plans": [
                {"Node Type": "Result", "Plans": [
                    {"Node Type": "Seq Scan", "Relation Name": "document_chunks"},
                ]},
                {"Node Type": "Index Scan",
                 "Index Name": "idx_document_chunks_embedding_hnsw"},
            ],
        }
        assert _plan_uses_hnsw(plan) == "idx_document_chunks_embedding_hnsw"

    def test_a_plain_btree_index_is_not_a_hit(self):
        # Der Primaerschluessel taucht im Plan der Sonde IMMER auf (der
        # InitPlan holt sich eine Zeile). Ihn als Treffer zu werten hiesse:
        # jede Tabelle meldet sofort, der Ausloeser waere Dauerlaerm.
        plan = {"Node Type": "Limit", "Plans": [
            {"Node Type": "Index Scan", "Index Name": "document_chunks_pkey"},
        ]}
        assert _plan_uses_hnsw(plan) is None

    def test_a_seq_scan_plan_is_silent(self):
        plan = {"Node Type": "Limit", "Plans": [
            {"Node Type": "Sort", "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "document_chunks"},
            ]},
        ]}
        assert _plan_uses_hnsw(plan) is None

    def test_a_single_dict_subplan_is_walked_too(self):
        # Postgres liefert "Plan" als Objekt, "Plans" als Liste. Beide Formen
        # muessen durchlaufen werden, sonst haengt es vom Zufall ab.
        assert _plan_uses_hnsw(
            {"Plan": {"Index Name": "ix_episodic_embedding_hnsw"}}
        ) == "ix_episodic_embedding_hnsw"


class TestWatchedSet:
    def test_every_watched_table_names_the_files_to_change(self):
        # Eine Warnung ohne Handlungsanweisung ist halb nutzlos: wer sie liest,
        # muss wissen, WO der Cast hingehoert.
        assert _WATCHED, "nichts beobachtet — der Ausloeser waere wirkungslos"
        for table, sites in _WATCHED.items():
            assert sites, f"{table} nennt keine Datei"
            for s in sites:
                assert s.startswith("services/") and s.endswith(".py"), s
