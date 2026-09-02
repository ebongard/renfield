"""Graph-expansion retrieval (HANDOVER_graph_expansion.md §4, Phase 4 — post-RRF).

Runs AFTER RRF fusion in PolymorphicAtomStore.query: takes the fused
``AtomMatch`` list, finds the top ``kg_node`` pivots, walks ``kg_relations``
outward 1-``max_hops`` hops, and returns NEW neighbour ``kg_node`` atoms (plus
the connecting ``kg_edge`` atoms) so the agent/UI sees the graph *neighbourhood*
of what the query matched. Single insertion point (not per-module) so the decay
score survives and the work runs once. Gated by ``GRAPH_EXPANSION_ENABLED``
(off => caller is byte-identical).

Design decisions baked in from the /plan-eng-review + outside voice:
  * LEVEL-SYNCHRONOUS BFS over ALL pivots at once → a node's hop distance is the
    true shortest distance from any pivot (recorded on discovery, before the
    frontier cap), so the frontier cap can never mislabel a shorter path.
  * Circle filter at EVERY hop (kg_entities_circles_filter): a neighbour is
    surfaced only if the asker may see it. The relation existing is not enough.
  * LEAK-SAFE edges: a kg_edge is emitted only when BOTH endpoints are in the
    accessible set (pivots ∪ surfaced neighbours). A relation can be visible
    while an endpoint is not (tier = MIN(subj,obj)); we never name an
    inaccessible endpoint. (This is why expansion does NOT reuse
    get_relevant_atoms' unfiltered name_map.)
  * Per-hop frontier cap: only the top-N (by origin pivot score) discovered
    neighbours are carried into the next hop, so a hub can't explode the hop-2
    query. Discovered nodes are still recorded at their true hop first.
  * Decay: score = pivot_rrf_score / (1 + hop). Output capped at max_expanded.
  * Provenance: every emitted atom carries payload["expanded"]=True + ["hop"],
    so downstream can mark 2-hop context as weaker than a direct match.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ATOM_TYPE_KG_EDGE, ATOM_TYPE_KG_NODE, TIER_PUBLIC
from services.atom_types import Atom, AtomMatch
from utils.config import settings


def _entity_filter(
    asker_id: int | None, enforce_circles: bool = False
) -> tuple[str, dict[str, Any]]:
    """Circle WHERE-fragment for kg_entities alias ``e`` (mirrors kg_retrieval).

    ``enforce_circles`` (federation): keep the peer-scoped circle filter active
    even with auth off (drops the owner + explicit-grant branches).
    """
    if not settings.auth_enabled and not enforce_circles:
        return ("", {})
    if asker_id is None:
        return ("AND e.circle_tier = :pub_tier", {"pub_tier": TIER_PUBLIC})
    from services.circle_sql import kg_entities_circles_filter
    clause, params = kg_entities_circles_filter(asker_id, alias="e", peer_scoped=enforce_circles)
    return (f"AND {clause}", params)


async def _accessible(
    db: AsyncSession, ids: list[int], asker_id: int | None, enforce_circles: bool = False
) -> dict[int, dict[str, Any]]:
    """Of ``ids``, the live canonical entities the asker may see (kg_node shape)."""
    if not ids:
        return {}
    efilter, eparams = _entity_filter(asker_id, enforce_circles)
    rows = (await db.execute(text(f"""
        SELECT e.id, e.name, e.entity_type, e.circle_tier
        FROM kg_entities e
        WHERE e.is_active = true AND e.canonical_id IS NULL AND e.id = ANY(:ids) {efilter}
    """), {**eparams, "ids": ids})).fetchall()
    return {
        int(r.id): {"id": int(r.id), "name": r.name, "entity_type": r.entity_type,
                    "circle_tier": int(r.circle_tier or 0)}
        for r in rows
    }


def _relation_filter(
    asker_id: int | None, enforce_circles: bool = False
) -> tuple[str, dict[str, Any]]:
    """Peer-scoped circle WHERE-fragment for kg_relations alias ``r``.

    A relation carries its OWN ``circle_tier`` (can be stricter than either
    endpoint), so accessible endpoints are NOT sufficient to disclose the
    predicate — a private relationship between two public entities would
    otherwise leak.

    Mirrors ``_entity_filter``'s gating (#1116): auth-off with no federation
    scope → no filter (byte-identical legacy); otherwise the relation's own tier
    is enforced on BOTH the federation (peer-scoped) AND the auth-on household
    path. Previously only the federation path was filtered, so a household member
    who could see two entities also saw a stricter-tier relation between them —
    entities were tier-filtered but relations were not.
    """
    if not settings.auth_enabled and not enforce_circles:
        return ("", {})
    if asker_id is None:
        return ("AND r.circle_tier = :rel_pub_tier", {"rel_pub_tier": TIER_PUBLIC})
    from services.circle_sql import kg_relations_circles_filter
    clause, params = kg_relations_circles_filter(asker_id, alias="r", peer_scoped=enforce_circles)
    return (f"AND {clause}", params)


async def _edges_within(
    db: AsyncSession, ids: list[int], asker_id: int | None = None, enforce_circles: bool = False
) -> list[dict[str, Any]]:
    """Active relations whose BOTH endpoints are in ``ids`` (all already accessible)
    AND whose own ``circle_tier`` the asker may reach (relation-tier gate)."""
    if len(ids) < 2:
        return []
    rfilter, rparams = _relation_filter(asker_id, enforce_circles)
    rows = (await db.execute(text(f"""
        SELECT r.id, r.subject_id, r.predicate, r.object_id, r.circle_tier
        FROM kg_relations r
        WHERE r.is_active = true AND r.subject_id <> r.object_id
          AND r.subject_id = ANY(:ids) AND r.object_id = ANY(:ids) {rfilter}
    """), {"ids": ids, **rparams})).fetchall()
    return [{"id": int(r.id), "subject_id": int(r.subject_id), "predicate": r.predicate,
             "object_id": int(r.object_id), "circle_tier": int(r.circle_tier or 0)} for r in rows]


def _node_atom(e: dict[str, Any], score: float, hop: int) -> AtomMatch:
    from datetime import datetime
    now = datetime.now()
    return AtomMatch(
        atom=Atom(
            atom_id=f"kg_node:{e['id']}", atom_type=ATOM_TYPE_KG_NODE, owner_user_id=0,
            policy={"tier": e["circle_tier"]}, created_at=now, updated_at=now,
            payload={"entity_id": e["id"], "name": e.get("name", ""),
                     "entity_type": e.get("entity_type"), "expanded": True, "hop": hop},
        ),
        score=round(score, 6), snippet=str(e.get("name", ""))[:200], rank=0,
    )


def _edge_atom(r: dict[str, Any], names: dict[int, str], score: float, hop: int) -> AtomMatch:
    from datetime import datetime
    now = datetime.now()
    subj, obj = names.get(r["subject_id"], "?"), names.get(r["object_id"], "?")
    return AtomMatch(
        atom=Atom(
            atom_id=f"kg_edge:{r['id']}", atom_type=ATOM_TYPE_KG_EDGE, owner_user_id=0,
            policy={"tier": r["circle_tier"]}, created_at=now, updated_at=now,
            payload={"relation_id": r["id"], "subject_id": r["subject_id"], "subject_name": subj,
                     "predicate": r["predicate"], "object_id": r["object_id"], "object_name": obj,
                     "expanded": True, "hop": hop},
        ),
        score=round(score, 6), snippet=f"{subj} {r['predicate']} {obj}"[:200], rank=0,
    )


async def expand_fused(
    merged: list[AtomMatch],
    asker_id: int | None,
    db: AsyncSession,
    *,
    max_pivots: int = 8,
    max_hops: int = 2,
    max_expanded: int = 15,
    enforce_circles: bool = False,
) -> list[AtomMatch]:
    """Post-RRF expansion. Returns NEW kg_node/kg_edge atoms (decay-scored,
    provenance-marked) to append to ``merged``. [] if flag off / nothing to do.

    ``enforce_circles`` (federation): every hop's circle filter stays peer-scoped
    even with auth off — otherwise expansion re-opens the leak the fused clause
    closed."""
    if not settings.graph_expansion_enabled:
        return []

    # Pivots = the top kg_node atoms already in the fused result.
    pivots: list[tuple[int, float]] = []
    present: set[int] = set()
    for m in merged:
        if m.atom.atom_type == "kg_node":
            eid = m.atom.payload.get("entity_id")
            if eid is not None:
                present.add(int(eid))
                if len(pivots) < max_pivots:
                    pivots.append((int(eid), float(m.score or 0.0)))
    if not pivots:
        return []

    seen: set[int] = {eid for eid, _ in pivots}
    origin: dict[int, float] = {eid: sc for eid, sc in pivots}      # pivot score a node descends from
    accessible: dict[int, dict[str, Any]] = {}                      # all surfaced neighbours
    frontier: list[int] = [eid for eid, _ in pivots]

    for hop in range(1, max(1, max_hops) + 1):
        if not frontier:
            break
        # Peer path: don't TRAVERSE through relations the peer can't see, else a
        # private edge discloses graph structure (that two nodes are linked) even
        # after _edges_within hides the predicate. Non-federation path: rfilter=""
        # → byte-identical.
        rfilter, rparams = _relation_filter(asker_id, enforce_circles)
        rows = (await db.execute(text(f"""
            SELECT r.subject_id, r.object_id FROM kg_relations r
            WHERE r.is_active = true AND r.subject_id <> r.object_id
              AND (r.subject_id = ANY(:f) OR r.object_id = ANY(:f)) {rfilter}
        """), {"f": frontier, **rparams})).fetchall()
        best: dict[int, float] = {}
        for s, o in rows:
            s, o = int(s), int(o)
            for near, far in ((s, o), (o, s)):
                if near in origin and far not in seen:
                    best[far] = max(best.get(far, 0.0), origin[near])
        if not best:
            break
        acc = await _accessible(db, list(best), asker_id, enforce_circles)   # circle filter at THIS hop
        for eid, ent in acc.items():
            seen.add(eid)
            ent["_score"] = best[eid] / (1 + hop)
            ent["_hop"] = hop
            origin[eid] = best[eid]
            accessible[eid] = ent
        # frontier cap: carry only the top-N newly-accessible nodes by origin score
        ranked = sorted(acc.keys(), key=lambda i: origin[i], reverse=True)
        frontier = ranked[:max_expanded]

    if not accessible:
        return []

    # Cap neighbours, then emit nodes + leak-safe edges among (pivots ∪ neighbours).
    top = sorted(accessible.values(), key=lambda e: e["_score"], reverse=True)[:max_expanded]
    out: list[AtomMatch] = [_node_atom(e, e["_score"], e["_hop"]) for e in top]

    # Score map keeps EDGE scores at RRF-scale: an edge ranks no higher than its
    # lower-scored endpoint (pivot RRF score, or neighbour decayed score). A fixed
    # base here (e.g. 0.5) would dwarf real RRF scores (~1/(60+rank)) and flood the
    # fused list — the synthetic-score distortion the review flagged.
    score_map: dict[int, float] = {eid: sc for eid, sc in pivots}
    for e in top:
        score_map[e["id"]] = e["_score"]
    node_ids = set(score_map)
    names = {e["id"]: e.get("name", "") for e in top}
    pivot_only = [eid for eid, _ in pivots if eid not in names]  # pivot names not in `accessible`
    if pivot_only:
        for eid, ent in (await _accessible(db, pivot_only, asker_id, enforce_circles)).items():
            names[eid] = ent.get("name", "")
    for r in await _edges_within(db, list(node_ids), asker_id, enforce_circles):
        if r["subject_id"] in names and r["object_id"] in names:
            hop = max(accessible.get(r["subject_id"], {}).get("_hop", 0),
                      accessible.get(r["object_id"], {}).get("_hop", 0)) or 1
            edge_score = min(score_map.get(r["subject_id"], 0.0), score_map.get(r["object_id"], 0.0))
            out.append(_edge_atom(r, names, score=edge_score, hop=hop))
    return out
