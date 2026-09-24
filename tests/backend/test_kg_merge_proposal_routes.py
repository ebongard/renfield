"""Schema + route-wiring tests for the KG merge-proposal endpoints (T5/D3).

The endpoint *behaviour* (list/approve/reject/run) is thin glue over
KgReconcilerService, which is covered against real Postgres in
test_kg_reconciler_pg.py. Here we validate the response schemas and that the
routes are actually registered on the router (catches decorator/import wiring
regressions without standing up a full HTTP client).
"""
import pytest
from pydantic import ValidationError

from api.routes.knowledge_graph import router
from api.routes.knowledge_graph_schemas import (
    ClusterResolveRequest,
    ClusterResolveResponse,
    MergeProposalEntityBrief,
    MergeProposalResponse,
    MergeProposalsListResponse,
    ReconcilerRunResponse,
)

pytestmark = pytest.mark.unit


class TestMergeProposalSchemas:
    def test_entity_brief_defaults(self):
        b = MergeProposalEntityBrief(id=1, name="Alice", entity_type="person")
        assert b.circle_tier == 0
        assert b.mention_count == 1
        assert b.surface_forms == []
        # Evidence fields default to "nothing known", never to a wrong number:
        # the cluster view shows them and the owner decides on them.
        assert b.description is None
        assert b.relation_count == 0
        assert b.first_seen_at is None and b.last_seen_at is None

    def test_entity_brief_carries_the_evidence(self):
        b = MergeProposalEntityBrief(
            id=1, name="Anna", entity_type="person", description="Nachbarin",
            relation_count=12, first_seen_at="2026-06-01T00:00:00",
            last_seen_at="2026-09-01T00:00:00",
        )
        assert (b.description, b.relation_count) == ("Nachbarin", 12)

    def test_cluster_request_needs_two_entities(self):
        with pytest.raises(ValidationError):
            ClusterResolveRequest(entity_ids=[1], decision="merge", survivor_id=1)

    def test_cluster_request_rejects_an_unknown_decision(self):
        with pytest.raises(ValidationError):
            ClusterResolveRequest(entity_ids=[1, 2], decision="obliterate")  # type: ignore[arg-type]

    def test_cluster_response_defaults(self):
        r = ClusterResolveResponse()
        assert (r.merged, r.approved, r.rejected, r.skipped_cross_tier) == (0, 0, 0, 0)
        assert r.notes == []

    def test_proposal_response_roundtrip(self):
        r = MergeProposalResponse(
            id=7, similarity=0.91, reason="cross_tier", status="pending",
            created_at="2026-06-04T00:00:00",
            loser=MergeProposalEntityBrief(id=1, name="Alice", entity_type="person",
                                           circle_tier=0, mention_count=2,
                                           surface_forms=["J."]),
            winner=MergeProposalEntityBrief(id=2, name="Alice Brown", entity_type="person",
                                            circle_tier=2, mention_count=9),
        )
        assert r.reason == "cross_tier"
        assert r.loser.surface_forms == ["J."]
        assert r.winner.circle_tier == 2

    def test_list_response(self):
        lst = MergeProposalsListResponse(proposals=[], total=0)
        assert lst.total == 0 and lst.proposals == []

    def test_reconciler_run_response(self):
        rr = ReconcilerRunResponse(candidates=5, auto_merged=2, proposed=3)
        assert rr.candidates == 5 and rr.auto_merged == 2 and rr.proposed == 3
        assert rr.notes == []


class TestRouteRegistration:
    def _paths(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for r in router.routes:
            path = getattr(r, "path", None)
            methods = getattr(r, "methods", None)
            if path is not None:
                out.setdefault(path, set()).update(methods or set())
        return out

    def test_merge_proposal_routes_registered(self):
        paths = self._paths()
        assert "/merge-proposals" in paths
        assert "GET" in paths["/merge-proposals"]
        assert "/merge-proposals/{proposal_id}/approve" in paths
        assert "POST" in paths["/merge-proposals/{proposal_id}/approve"]
        assert "/merge-proposals/{proposal_id}/reject" in paths
        assert "/reconciler/run" in paths
        assert "POST" in paths["/reconciler/run"]
        # The cluster route must not collide with /{proposal_id}/... — a
        # literal segment that a path parameter could swallow.
        assert "/merge-proposals/cluster" in paths
        assert "POST" in paths["/merge-proposals/cluster"]
