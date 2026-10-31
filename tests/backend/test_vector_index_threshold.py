"""The trigger asks the PLANNER, not the row count.

The first version reported from 4,000 rows up. The measurement on the second
instance disproved that threshold on the day it was introduced:

    xidra  document_chunks  3,165 rows  -> index NOT chosen (145 ms / 22 ms)
    xidra  kg_entities      1,953 rows  -> index CHOSEN     (4.1 ms / 14.7 ms)

The row count does not predict the tipping point -- heap pages and index shape
do (`document_chunks`: 416 pages at 2,847 rows, `kg_entities`: 150 at 4,813,
estimated HNSW entry cost a factor of eight apart, at FEWER rows).

So the task asks directly: would the planner take the index if the query cast?
A plain `EXPLAIN` answers that. Demonstrated on both production instances on
2026-09-25: both watched tables stay silent, `kg_entities` (which casts) fires
-- on BOTH, although the row counts there run in opposite directions.

This test covers the plan reading: the index name sits at any depth in the JSON
structure, and a trigger that misses it stays silent forever -- quietly,
without anything breaking.
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
        # The real shape: Limit -> Index Scan, with an InitPlan next to it.
        # That is exactly what the probe's plan looks like; a search only at
        # the top level would miss it and the trigger would stay mute
        # forever.
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
        # The primary key ALWAYS turns up in the probe's plan (the InitPlan
        # fetches one row). Counting it as a hit would mean: every table
        # reports immediately, and the trigger would be constant noise.
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
        # Postgres delivers "Plan" as an object, "Plans" as a list. Both
        # forms have to be walked, otherwise it depends on chance.
        assert _plan_uses_hnsw(
            {"Plan": {"Index Name": "ix_episodic_embedding_hnsw"}}
        ) == "ix_episodic_embedding_hnsw"


class TestWatchedSet:
    def test_every_watched_table_names_the_files_to_change(self):
        # A warning without an instruction is half useless: whoever reads it
        # has to know WHERE the cast belongs.
        assert _WATCHED, "nothing watched — the trigger would have no effect"
        for table, sites in _WATCHED.items():
            assert sites, f"{table} names no file"
            for s in sites:
                assert s.startswith("services/") and s.endswith(".py"), s
