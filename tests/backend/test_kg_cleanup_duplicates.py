"""Admin string-similarity cleanup must not fold a release series (#1329).

``KGCleanupService.find_duplicate_clusters`` unions entities whose normalized
names reach a SequenceMatcher ratio, and ``merge_duplicate_clusters`` folds every
cluster into its most-mentioned member. "Product A - 1.2.4" vs "... 1.2.3" is
~0.94 — the same field shape the reconciler auto-merged in production. A name
that differs only in a number names a different thing, here as there.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.kg_cleanup_service import KGCleanupService


def _row(eid, name, mentions=1, etype="thing"):
    return SimpleNamespace(id=eid, name=name, mention_count=mentions, entity_type=etype)


def _db(rows):
    db = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


def _names(cluster):
    return {cluster["canonical"]["name"], *(d["name"] for d in cluster["duplicates"])}


@pytest.mark.unit
@pytest.mark.parametrize("older,newer", [
    ("Product A - 1.2.3", "Product A - 1.2.4"),
    ("Product B - 2.2.2", "Product B - 2.2.3"),
    ("Konto 01", "Konto 02"),
    ("Jahresbericht Stadtwerke", "Jahresbericht Stadtwerke 2025"),
])
async def test_release_versions_never_form_a_cluster(older, newer):
    svc = KGCleanupService(_db([_row(1, older, 469), _row(2, newer, 232)]))
    assert await svc.find_duplicate_clusters() == []


@pytest.mark.unit
async def test_digit_free_typo_still_clusters_without_conflict():
    svc = KGCleanupService(_db([
        _row(1, "Stadtwerke Bonn", 5),
        _row(2, "Stadtwerke Bnn", 1),
    ]))
    clusters = await svc.find_duplicate_clusters()
    assert len(clusters) == 1
    assert _names(clusters[0]) == {"Stadtwerke Bonn", "Stadtwerke Bnn"}
    assert clusters[0]["identifier_conflict"] is False
    assert clusters[0]["canonical"]["id"] == 1


@pytest.mark.unit
async def test_typo_bridge_marks_cluster_as_identifier_conflict():
    # "Prodct A - 1.2.3" is a typo of 1.2.3 AND close enough to 1.2.4 (it differs
    # there in a word, not only in the number), so both releases reach one
    # cluster through the third spelling.
    svc = KGCleanupService(_db([
        _row(1, "Product A - 1.2.3", 469),
        _row(2, "Product A - 1.2.4", 232),
        _row(3, "Prodct A - 1.2.3", 2),
    ]))
    clusters = await svc.find_duplicate_clusters()
    assert len(clusters) == 1
    assert _names(clusters[0]) == {"Product A - 1.2.3", "Product A - 1.2.4", "Prodct A - 1.2.3"}
    assert clusters[0]["identifier_conflict"] is True


@pytest.mark.unit
async def test_merge_refuses_conflicting_cluster_and_merges_clean_one():
    svc = KGCleanupService(_db([
        _row(1, "Product A - 1.2.3", 469),
        _row(2, "Product A - 1.2.4", 232),
        _row(3, "Prodct A - 1.2.3", 2),
        _row(10, "Stadtwerke Bonn", 5),
        _row(11, "Stadtwerke Bnn", 1),
    ]))
    kg = MagicMock()
    kg.merge_entities = AsyncMock()
    with patch("services.kg_cleanup_service.KnowledgeGraphService", return_value=kg):
        result = await svc.merge_duplicate_clusters(dry_run=False)

    kg.merge_entities.assert_awaited_once_with(11, 10)
    assert result["clusters_found"] == 2
    assert result["clusters_refused"] == 1
    assert result["entities_merged"] == 1
    assert [c["canonical"]["id"] for c in result["clusters"]] == [10]


@pytest.mark.unit
async def test_merge_dry_run_counts_refusal_without_merging():
    svc = KGCleanupService(_db([
        _row(1, "Product A - 1.2.3", 469),
        _row(2, "Product A - 1.2.4", 232),
        _row(3, "Prodct A - 1.2.3", 2),
    ]))
    kg = MagicMock()
    kg.merge_entities = AsyncMock()
    with patch("services.kg_cleanup_service.KnowledgeGraphService", return_value=kg):
        result = await svc.merge_duplicate_clusters(dry_run=True)

    kg.merge_entities.assert_not_awaited()
    assert result == {
        "dry_run": True,
        "clusters_found": 1,
        "entities_merged": 0,
        "clusters_refused": 1,
        "clusters": [],
    }


@pytest.mark.unit
async def test_merge_with_no_clusters_reports_zero_refused():
    svc = KGCleanupService(_db([_row(1, "Product A - 1.2.3"), _row(2, "Product A - 1.2.4")]))
    result = await svc.merge_duplicate_clusters(dry_run=False)
    assert result["clusters_found"] == 0
    assert result["clusters_refused"] == 0


@pytest.mark.unit
def test_response_schemas_carry_the_refusal():
    from api.routes.knowledge_graph_schemas import DuplicateCluster, MergeDuplicatesResponse

    info = {"id": 1, "name": "x", "mention_count": 1, "entity_type": "thing"}
    assert DuplicateCluster(canonical=info, duplicates=[], cluster_size=1,
                            entity_type="thing").identifier_conflict is False
    assert MergeDuplicatesResponse(dry_run=True, clusters_found=1, entities_merged=0,
                                   clusters_refused=1).clusters_refused == 1
