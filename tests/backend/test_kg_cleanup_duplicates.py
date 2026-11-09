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


def _row(eid, name, mentions=1, etype="thing", user_id=1, tier=0):
    return SimpleNamespace(
        id=eid, name=name, mention_count=mentions, entity_type=etype,
        user_id=user_id, circle_tier=tier,
    )


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
        "entities_skipped": 0,
        "skipped": [],
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


# --- #1499: clusters stay within one owner and one tier; skipped merges are reported


@pytest.mark.unit
async def test_the_same_name_of_two_users_is_no_cluster():
    """merge_entities refuses a cross-user pair; such a pair must not cluster."""
    svc = KGCleanupService(_db([
        _row(1, "Stadtwerke Bonn", 5, user_id=1),
        _row(2, "Stadtwerke Bnn", 1, user_id=2),
    ]))
    assert await svc.find_duplicate_clusters() == []


@pytest.mark.unit
async def test_one_users_entities_on_two_tiers_are_no_cluster():
    """A fold takes the MIN tier: a shared entity would turn private."""
    svc = KGCleanupService(_db([
        _row(1, "Stadtwerke Bonn", 5, tier=2),
        _row(2, "Stadtwerke Bnn", 1, tier=0),
    ]))
    assert await svc.find_duplicate_clusters() == []


@pytest.mark.unit
async def test_partitions_do_not_bridge_through_a_third_entity():
    """Two same-partition pairs cluster separately, never into one cluster."""
    svc = KGCleanupService(_db([
        _row(1, "Stadtwerke Bonn", 5, user_id=1),
        _row(2, "Stadtwerke Bnn", 1, user_id=1),
        _row(3, "Stadtwerke Bonn", 7, user_id=2),
        _row(4, "Stadtwerke Bon", 1, user_id=2),
    ]))
    clusters = await svc.find_duplicate_clusters()
    assert sorted(sorted([c["canonical"]["id"], *(d["id"] for d in c["duplicates"])]) for c in clusters) == [
        [1, 2], [3, 4],
    ]


@pytest.mark.unit
async def test_a_skipped_merge_is_reported_not_counted():
    svc = KGCleanupService(_db([
        _row(10, "Stadtwerke Bonn", 5),
        _row(11, "Stadtwerke Bnn", 1),
        _row(12, "Stadtwerk Bonn", 1),
    ]))
    kg = MagicMock()

    async def merge(loser, winner):
        if loser == 11:
            return None  # e.g. a concurrent pass merged it first
        if loser == 12:
            raise RuntimeError("db gone")
        return SimpleNamespace(id=winner)

    kg.merge_entities = AsyncMock(side_effect=merge)
    with patch("services.kg_cleanup_service.KnowledgeGraphService", return_value=kg):
        result = await svc.merge_duplicate_clusters(dry_run=False)

    assert result["entities_merged"] == 0
    assert result["entities_skipped"] == 2
    assert sorted(result["skipped"], key=lambda p: p["loser_id"]) == [
        {"loser_id": 11, "winner_id": 10, "reason": "skipped"},
        {"loser_id": 12, "winner_id": 10, "reason": "error"},
    ]


@pytest.mark.unit
def test_the_response_schema_carries_the_skipped_pairs():
    from api.routes.knowledge_graph_schemas import MergeDuplicatesResponse

    pair = {"loser_id": 2, "winner_id": 1, "reason": "skipped"}
    resp = MergeDuplicatesResponse(dry_run=False, clusters_found=1, entities_merged=0,
                                   entities_skipped=1, skipped=[pair])
    assert resp.entities_skipped == 1 and resp.skipped == [pair]


def _required_permission(router, path: str, method: str):
    """The permission a route's REAL dependency demands (not its source text)."""
    for route in router.routes:
        if route.path.endswith(path) and method in route.methods:
            for dep in route.dependant.dependencies:
                for sub in [dep, *dep.dependencies]:
                    for cell in (getattr(sub.call, "__closure__", None) or ()):
                        value = getattr(cell.cell_contents, "value", cell.cell_contents)
                        if isinstance(value, str) and value.startswith("kg."):
                            return value
    raise AssertionError(f"{method} {path}: no kg.* permission dependency found")


@pytest.mark.unit
def test_the_duplicate_scan_needs_kg_manage():
    """The scan reads every user's entities at every tier; KG_VIEW is role Familie."""
    from api.routes.knowledge_graph import router
    from models.permissions import Permission

    # Negative control: the detector reads KG_VIEW where a route asks for it.
    assert _required_permission(router, "/stats", "GET") == Permission.KG_VIEW.value
    assert _required_permission(router, "/cleanup/duplicates", "GET") == Permission.KG_MANAGE.value
    assert _required_permission(router, "/cleanup/merge-duplicates", "POST") == Permission.KG_MANAGE.value
