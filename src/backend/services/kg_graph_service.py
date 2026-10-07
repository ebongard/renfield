"""
Knowledge-Graph view service — backs the 3D Wissensgraph (GraphView.tsx).

The frontend GraphView component speaks three shapes that originated in the
Reva ``/api/wissensbasis/*`` surface:

  - corpus  → connected-component clusters with hub entities
  - focus   → an entity's 1-hop + 2-hop neighborhood
  - search  → name-prefix entity suggestions

Reva ships a richer implementation (reasoning trace, role-mix, observed
fields). This module is Renfield's *native* implementation of just the three
shapes the Wissensgraph tab needs, computed over the local ``kg_entities`` /
``kg_relations`` tables. It deliberately omits Reva-only extras (``/trace``,
``/me/mix``) — those stay 404 in standalone Renfield, which is what
``useWissensbasisAvailable`` keys off to hide the Reva-only side panels.

Circle access is enforced identically to ``KnowledgeGraphService.list_entities``:
``AUTH_ENABLED=false`` sees everything; an authenticated asker sees own + public
+ explicit-grant + tier-reach; ``asker_id=None`` in auth-enabled mode reduces to
public-tier only. Edges are kept only when *both* endpoints are accessible, so
relation visibility never leaks an entity the asker cannot already see.

Scale note: production holds ~200 entities / ~50 relations, so we load the
accessible slice into memory and compute components/BFS in Python rather than
leaning on recursive SQL. The corpus load is capped (``CORPUS_ENTITY_CAP``) and
reports ``truncated`` when the cap or the cluster cap bites.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import TIER_PUBLIC, KGEntity, KGRelation
from services.circle_sql import kg_entities_circles_filter
from services.kg_validity_sql import live_conditions
from utils.config import settings

# Render budgets. Tuned for the current ~200-entity prod graph; generous
# enough that the 3D scene stays legible without a config knob per the
# "measure first, don't hardcode caps blindly" rule — these are presentation
# limits, not data limits, and the response flags when they bite.
CORPUS_ENTITY_CAP = 300          # most-mentioned N entities loaded for corpus mode
MAX_NAMED_CLUSTERS = 16          # connected components rendered as named clusters
MAX_HUBS_PER_CLUSTER = 6         # orbiting hub spheres per cluster
DEFAULT_MAX_PER_HOP = 30         # hop1 / hop2 node cap in focus mode
SEARCH_LIMIT_DEFAULT = 12
SEARCH_LIMIT_MAX = 25


@dataclass
class _Entity:
    """Lightweight projection of a KGEntity row for in-memory graph work."""

    id: int
    name: str
    entity_type: str
    mention_count: int
    circle_tier: int = 0


@dataclass
class _Component:
    member_ids: list[int] = field(default_factory=list)


def _escape_like(s: str) -> str:
    """Escape LIKE/ILIKE metacharacters so a user query matches literally.

    Without this, a search for ``50%`` or ``a_b`` treats ``%``/``_`` as
    wildcards. The backslash escape pairs with ``ilike(..., escape="\\")``.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _union_find(entity_ids: list[int], edges: list[tuple[int, int]]) -> list[list[int]]:
    """Group entity ids into connected components via union-find.

    Returns a list of components, each a list of member ids. Membership order
    within a component follows ``entity_ids`` input order (callers pass it
    pre-sorted by importance, so the first member is the most-mentioned).
    """
    parent = {eid: eid for eid in entity_ids}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        # Path compression.
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    present = set(entity_ids)
    for a, b in edges:
        if a in present and b in present:
            union(a, b)

    groups: dict[int, list[int]] = defaultdict(list)
    for eid in entity_ids:  # preserves importance order within each component
        groups[find(eid)].append(eid)
    return list(groups.values())


class KGGraphService:
    """Read-only graph projections for the Wissensgraph 3D view."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Accessible-slice loaders (circle-filtered)
    # ------------------------------------------------------------------
    async def _load_entities(
        self, asker_id: int | None, limit: int
    ) -> tuple[list[_Entity], int]:
        """Load up to ``limit`` accessible active entities, most-mentioned first.

        Returns ``(entities, total_accessible)`` where ``total_accessible`` is
        the unbounded count (so corpus mode can flag truncation).
        """
        base = select(KGEntity).where(KGEntity.is_active == True)  # noqa: E712
        count_q = select(func.count(KGEntity.id)).where(KGEntity.is_active == True)  # noqa: E712

        if not settings.auth_enabled:
            pass  # single-user bypass — no circle filter
        elif asker_id is None:
            base = base.where(KGEntity.circle_tier == TIER_PUBLIC)
            count_q = count_q.where(KGEntity.circle_tier == TIER_PUBLIC)
        else:
            clause, params = kg_entities_circles_filter(asker_id, alias="kg_entities")
            base = base.where(sa_text(clause).bindparams(**params))
            count_q = count_q.where(sa_text(clause).bindparams(**params))

        total = (await self.db.execute(count_q)).scalar() or 0

        base = base.order_by(
            KGEntity.mention_count.desc().nullslast(), KGEntity.id
        ).limit(limit)
        rows = (await self.db.execute(base)).scalars().all()
        entities = [
            _Entity(
                id=e.id,
                name=e.name,
                entity_type=e.entity_type,
                mention_count=int(e.mention_count or 1),
                circle_tier=int(e.circle_tier if e.circle_tier is not None else 0),
            )
            for e in rows
        ]
        return entities, total

    async def _load_relations(
        self, entity_ids: list[int]
    ) -> list[tuple[int, int, str]]:
        """Active relations with *both* endpoints inside ``entity_ids``.

        Endpoint-membership is the circle gate: since ``entity_ids`` is already
        the accessible set, an edge survives only when both ends are visible to
        the asker.
        """
        if not entity_ids:
            return []
        q = select(
            KGRelation.subject_id, KGRelation.object_id, KGRelation.predicate
        ).where(
            KGRelation.is_active == True,  # noqa: E712
            *live_conditions(KGRelation),  # #875; [] when the validity filter is off
            KGRelation.subject_id.in_(entity_ids),
            KGRelation.object_id.in_(entity_ids),
        )
        rows = (await self.db.execute(q)).all()
        return [(int(s), int(o), p) for s, o, p in rows]

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------
    async def search(
        self, query: str, asker_id: int | None, limit: int = SEARCH_LIMIT_DEFAULT
    ) -> list[dict]:
        """Name-substring entity suggestions, most-mentioned first.

        Empty/blank query → no results (the direct-entry premise is "I know
        what I'm looking for", not "show me everything").
        """
        q = query.strip()
        if not q:
            return []
        limit = max(1, min(limit, SEARCH_LIMIT_MAX))

        stmt = select(KGEntity).where(
            KGEntity.is_active == True,  # noqa: E712
            KGEntity.name.ilike(f"%{_escape_like(q)}%", escape="\\"),
        )
        if not settings.auth_enabled:
            pass
        elif asker_id is None:
            stmt = stmt.where(KGEntity.circle_tier == TIER_PUBLIC)
        else:
            clause, params = kg_entities_circles_filter(asker_id, alias="kg_entities")
            stmt = stmt.where(sa_text(clause).bindparams(**params))

        stmt = stmt.order_by(
            KGEntity.mention_count.desc().nullslast(), KGEntity.id
        ).limit(limit)
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            {
                "entity_id": str(e.id),
                "display_name": e.name,
                "entity_type": e.entity_type,
                "mention_count": int(e.mention_count or 1),
            }
            for e in rows
        ]

    # ------------------------------------------------------------------
    # focus
    # ------------------------------------------------------------------
    async def _accessible_entities_by_ids(
        self, ids, asker_id: int | None
    ) -> dict[int, _Entity]:
        """Fetch the given entity ids the asker may see, keyed by id.

        Same 3-branch circle gate as the other loaders, restricted to an
        explicit id set. This keeps focus mode independent of any global load
        cap — an accessible entity is reachable no matter how large the graph
        grows (each node's visibility is decided individually).
        """
        ids = list(ids)
        if not ids:
            return {}
        base = select(KGEntity).where(
            KGEntity.is_active == True,  # noqa: E712
            KGEntity.id.in_(ids),
        )
        if not settings.auth_enabled:
            pass
        elif asker_id is None:
            base = base.where(KGEntity.circle_tier == TIER_PUBLIC)
        else:
            clause, params = kg_entities_circles_filter(asker_id, alias="kg_entities")
            base = base.where(sa_text(clause).bindparams(**params))
        rows = (await self.db.execute(base)).scalars().all()
        return {
            e.id: _Entity(
                id=e.id, name=e.name, entity_type=e.entity_type,
                mention_count=int(e.mention_count or 1),
                circle_tier=int(e.circle_tier if e.circle_tier is not None else 0),
            )
            for e in rows
        }

    async def _relations_touching(
        self, frontier_ids
    ) -> list[tuple[int, int, str]]:
        """Active relations with at least one endpoint in ``frontier_ids``.

        Self-loops (subject == object) are dropped so a node never lands in its
        own neighborhood. Far-endpoint accessibility is enforced by the caller,
        which fetches those endpoints through the circle gate.
        """
        frontier_ids = list(frontier_ids)
        if not frontier_ids:
            return []
        q = select(
            KGRelation.subject_id, KGRelation.object_id, KGRelation.predicate
        ).where(
            KGRelation.is_active == True,  # noqa: E712
            *live_conditions(KGRelation),  # #875
            KGRelation.subject_id != KGRelation.object_id,
            or_(
                KGRelation.subject_id.in_(frontier_ids),
                KGRelation.object_id.in_(frontier_ids),
            ),
        )
        rows = (await self.db.execute(q)).all()
        return [(int(s), int(o), p) for s, o, p in rows]

    async def focus(
        self,
        entity_id: int,
        asker_id: int | None,
        hops: int = 2,
        max_per_hop: int = DEFAULT_MAX_PER_HOP,
    ) -> dict | None:
        """1-hop (+2-hop) neighborhood for an entity.

        Authorizes the focus entity directly, then walks its relations one
        frontier at a time, fetching each frontier of neighbors through the
        circle gate. No global entity cap: visibility is decided per node, so
        an accessible entity is always reachable regardless of graph size.
        Neighbors and edges the asker cannot see are dropped.

        Returns ``None`` when the entity does not exist or is not accessible to
        the asker (the route maps that to 404 — identical responses so an
        inaccessible entity's existence does not leak).
        """
        max_per_hop = max(1, max_per_hop)

        focus_map = await self._accessible_entities_by_ids([entity_id], asker_id)
        focus_entity = focus_map.get(entity_id)
        if focus_entity is None:
            return None

        by_id: dict[int, _Entity] = dict(focus_map)

        def sort_ids(ids) -> list[int]:
            return sorted(ids, key=lambda i: (by_id[i].mention_count, i), reverse=True)

        # hop1: accessible neighbors of the focus node.
        rel1 = await self._relations_touching([entity_id])
        cand1 = {(o if s == entity_id else s) for s, o, _ in rel1}
        cand1.discard(entity_id)
        hop1_map = await self._accessible_entities_by_ids(cand1, asker_id)
        by_id.update(hop1_map)
        hop1_all = sort_ids(hop1_map.keys())

        # hop2: accessible neighbors of hop1, minus the focus and hop1 sets.
        rel2: list[tuple[int, int, str]] = []
        hop2_all: list[int] = []
        if hops >= 2 and hop1_all:
            rel2 = await self._relations_touching(hop1_all)
            hop1_set = set(hop1_all)
            cand2: set[int] = set()
            for s, o, _ in rel2:
                if s in hop1_set:
                    cand2.add(o)
                if o in hop1_set:
                    cand2.add(s)
            cand2 -= hop1_set
            cand2.discard(entity_id)
            hop2_map = await self._accessible_entities_by_ids(cand2, asker_id)
            by_id.update(hop2_map)
            hop2_all = sort_ids(hop2_map.keys())

        overflow_hop1 = max(0, len(hop1_all) - max_per_hop)
        overflow_hop2 = max(0, len(hop2_all) - max_per_hop)
        hop1 = hop1_all[:max_per_hop]
        hop2 = hop2_all[:max_per_hop]

        included = {entity_id, *hop1, *hop2}
        seen_edges: set[tuple[int, int, str]] = set()
        edges = []
        for s, o, pred in (*rel1, *rel2):
            if s in included and o in included and (s, o, pred) not in seen_edges:
                seen_edges.add((s, o, pred))
                edges.append(
                    {"from_entity": str(s), "to_entity": str(o), "relation": pred}
                )

        def to_focus_entity(e: _Entity) -> dict:
            return {
                "entity_id": str(e.id),
                "display_name": e.name,
                "entity_type": e.entity_type,
                "importance": float(e.mention_count),
                "circle_tier": e.circle_tier,
            }

        return {
            "focus": to_focus_entity(focus_entity),
            "hop1": [to_focus_entity(by_id[i]) for i in hop1],
            "hop2": [to_focus_entity(by_id[i]) for i in hop2],
            "edges": edges,
            "overflow_hop1": overflow_hop1,
            "overflow_hop2": overflow_hop2,
        }

    # ------------------------------------------------------------------
    # corpus / graph
    # ------------------------------------------------------------------
    async def corpus(self, asker_id: int | None, lang: str = "de") -> dict:
        """Connected-component clusters over the most-mentioned entities."""
        loose_label = "Lose Enden" if lang != "en" else "Loose ends"
        entity_word = "Entitäten" if lang != "en" else "entities"

        entities, total_entities = await self._load_entities(
            asker_id, CORPUS_ENTITY_CAP
        )
        by_id = {e.id: e for e in entities}
        ordered_ids = [e.id for e in entities]  # already most-mentioned first
        relations = await self._load_relations(ordered_ids)

        components = _union_find(ordered_ids, [(s, o) for s, o, _ in relations])

        # Connected components (size >= 2) become named clusters; everything
        # left over (singletons + clusters beyond the cap) folds into one
        # "loose ends" bucket so the scene stays legible.
        named = [c for c in components if len(c) >= 2]
        # Deterministic order: size, then namesake importance, then id — so
        # equal-size clusters keep a stable order (and stable color_seed)
        # across requests, independent of DB row iteration order.
        named.sort(
            key=lambda c: (len(c), by_id[c[0]].mention_count, c[0]), reverse=True
        )
        rendered = named[:MAX_NAMED_CLUSTERS]
        overflow_named = named[MAX_NAMED_CLUSTERS:]

        loose_ids: list[int] = [c[0] for c in components if len(c) == 1]
        for c in overflow_named:
            loose_ids.extend(c)
        # Keep loose-ends importance-ordered for stable hub selection.
        loose_ids.sort(key=lambda i: (by_id[i].mention_count, i), reverse=True)

        clusters: list[dict] = []

        def hubs_for(member_ids: list[int]) -> list[dict]:
            top = sorted(
                member_ids,
                key=lambda i: (by_id[i].mention_count, i),
                reverse=True,
            )[:MAX_HUBS_PER_CLUSTER]
            return [
                {
                    "entity_id": str(by_id[i].id),
                    "name": by_id[i].name,
                    "entity_type": by_id[i].entity_type,
                    "mention_count": by_id[i].mention_count,
                    "circle_tier": by_id[i].circle_tier,
                }
                for i in top
            ]

        def hub_edges_for(hubs: list[dict]) -> list[dict]:
            # Relations whose BOTH endpoints are rendered hubs of this
            # cluster — lets the scene draw real structure inside the
            # cluster sphere instead of free-floating dots.
            hub_ids = {int(h["entity_id"]) for h in hubs}
            out: list[dict] = []
            seen: set[tuple[int, int]] = set()
            for s_id, o_id, pred in relations:
                if s_id in hub_ids and o_id in hub_ids and (s_id, o_id) not in seen:
                    seen.add((s_id, o_id))
                    out.append(
                        {
                            "from_entity": str(s_id),
                            "to_entity": str(o_id),
                            "relation": pred,
                        }
                    )
            return out

        for seed, member_ids in enumerate(rendered):
            # member_ids preserves importance order (union-find kept input
            # order), so the first member is the namesake.
            namesake = by_id[member_ids[0]]
            hubs = hubs_for(member_ids)
            clusters.append(
                {
                    "id": f"c{namesake.id}",
                    "label": namesake.name,
                    "sub_label": f"{len(member_ids)} {entity_word}",
                    "entity_count": len(member_ids),
                    "hubs": hubs,
                    "hub_edges": hub_edges_for(hubs),
                    "color_seed": seed,
                    "namesake_entity_id": str(namesake.id),
                }
            )

        if loose_ids:
            loose_hubs = hubs_for(loose_ids)
            clusters.append(
                {
                    "id": "loose",
                    "label": loose_label,
                    "sub_label": f"{len(loose_ids)} {entity_word}",
                    "entity_count": len(loose_ids),
                    "hubs": loose_hubs,
                    "hub_edges": hub_edges_for(loose_hubs),
                    "color_seed": len(rendered),
                    "namesake_entity_id": None,
                }
            )

        truncated = total_entities > len(entities) or len(overflow_named) > 0
        return {
            "clusters": clusters,
            "total_entities": total_entities,
            # Edges among the loaded (capped) entity slice — the rendered edge
            # count, not an unbounded corpus-wide total. total_entities IS the
            # unbounded count, so the two differ when the slice is truncated.
            "total_relations": len(relations),
            "truncated": truncated,
        }
