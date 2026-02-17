"""
Knowledge Graph Cleanup Service — Bulk operations for data quality.

Provides admin-only operations to scan and clean up invalid entities,
find duplicate clusters via string similarity, and auto-merge them.
All destructive operations support dry_run mode.
"""
import difflib
from collections import defaultdict

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KGEntity, KGRelation
from services.knowledge_graph_service import KnowledgeGraphService

# Prefixes stripped during name normalization (lowercase)
_STRIP_PREFIXES = ("herr ", "frau ", "dr. ", "dr ", "prof. ", "prof ")
# Suffixes stripped for organizations (lowercase)
_STRIP_ORG_SUFFIXES = (" gmbh", " ag", " e.v.", " mbh", " ohg", " kg", " ug", " gbr")


def _normalize_name(name: str, entity_type: str = "") -> str:
    """Normalize entity name for string comparison."""
    n = name.strip().lower()
    # Strip common person titles
    for prefix in _STRIP_PREFIXES:
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    # Strip common org suffixes
    if entity_type == "organization":
        for suffix in _STRIP_ORG_SUFFIXES:
            if n.endswith(suffix):
                n = n[: -len(suffix)]
                break
    return n.strip()


class KGCleanupService:
    """Bulk cleanup operations for the knowledge graph."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def cleanup_invalid_entities(
        self,
        dry_run: bool = True,
    ) -> dict:
        """
        Scan all active entities and soft-delete those failing validation.

        Cascade-deactivates orphaned relations for deleted entities.
        Returns summary with counts and sample rejected names.
        """
        # Fetch all active entities (name + type + id)
        result = await self.db.execute(
            select(KGEntity.id, KGEntity.name, KGEntity.entity_type)
            .where(KGEntity.is_active == True)  # noqa: E712
        )
        rows = result.fetchall()

        invalid_ids = []
        invalid_samples = []

        for row in rows:
            if not KnowledgeGraphService._is_valid_entity(row.name, row.entity_type):
                invalid_ids.append(row.id)
                if len(invalid_samples) < 50:
                    invalid_samples.append({
                        "id": row.id,
                        "name": row.name,
                        "entity_type": row.entity_type,
                    })

        if not dry_run and invalid_ids:
            # Soft-delete invalid entities
            await self.db.execute(
                update(KGEntity)
                .where(KGEntity.id.in_(invalid_ids))
                .values(is_active=False)
            )

            # Cascade-deactivate orphaned relations
            orphaned_result = await self.db.execute(
                update(KGRelation)
                .where(
                    KGRelation.is_active == True,  # noqa: E712
                    (KGRelation.subject_id.in_(invalid_ids)) | (KGRelation.object_id.in_(invalid_ids)),
                )
                .values(is_active=False)
                .returning(KGRelation.id)
            )
            orphaned_count = len(orphaned_result.fetchall())

            await self.db.commit()
            logger.info(
                f"KG cleanup: Deleted {len(invalid_ids)} invalid entities, "
                f"{orphaned_count} orphaned relations"
            )
        else:
            orphaned_count = 0
            if invalid_ids:
                # Count relations that would be orphaned
                count_result = await self.db.execute(
                    select(func.count(KGRelation.id))
                    .where(
                        KGRelation.is_active == True,  # noqa: E712
                        (KGRelation.subject_id.in_(invalid_ids)) | (KGRelation.object_id.in_(invalid_ids)),
                    )
                )
                orphaned_count = count_result.scalar() or 0

        return {
            "dry_run": dry_run,
            "total_scanned": len(rows),
            "invalid_count": len(invalid_ids),
            "orphaned_relations": orphaned_count,
            "samples": invalid_samples,
        }

    async def find_duplicate_clusters(
        self,
        entity_type: str | None = None,
        threshold: float | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Find clusters of likely-duplicate entities via string similarity.

        Uses normalized Levenshtein ratio (difflib.SequenceMatcher) to detect
        OCR variants and typos. Returns clusters sorted by size, each with
        a canonical entity (highest mention_count) and its duplicates.
        """
        if threshold is None:
            threshold = 0.82

        # Fetch all active entities
        query = (
            select(KGEntity.id, KGEntity.name, KGEntity.mention_count, KGEntity.entity_type)
            .where(KGEntity.is_active == True)  # noqa: E712
        )
        if entity_type:
            query = query.where(KGEntity.entity_type == entity_type)

        result = await self.db.execute(query)
        rows = result.fetchall()

        # Group by entity_type (only compare within same type)
        by_type: dict[str, list] = defaultdict(list)
        for row in rows:
            by_type[row.entity_type].append(row)

        # Find similar pairs via string comparison
        entity_info: dict[int, dict] = {}
        # Store best similarity per pair for output
        pair_similarity: dict[tuple[int, int], float] = {}

        parent: dict[int, int] = {}

        def find(x: int) -> int:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        for etype, entities in by_type.items():
            # Pre-compute normalized names
            normed = [(e, _normalize_name(e.name, etype)) for e in entities]
            n = len(normed)

            for i in range(n):
                a_entity, a_name = normed[i]
                if not a_name:
                    continue

                for j in range(i + 1, n):
                    b_entity, b_name = normed[j]
                    if not b_name:
                        continue

                    # Quick length filter — OCR variants have similar length
                    len_a, len_b = len(a_name), len(b_name)
                    if min(len_a, len_b) / max(len_a, len_b) < 0.5:
                        continue

                    # SequenceMatcher with quick_ratio pre-filter
                    sm = difflib.SequenceMatcher(None, a_name, b_name)
                    if sm.quick_ratio() < threshold:
                        continue
                    if sm.real_quick_ratio() < threshold:
                        continue

                    ratio = sm.ratio()
                    if ratio < threshold:
                        continue

                    # Record match
                    for e in (a_entity, b_entity):
                        entity_info[e.id] = {
                            "id": e.id,
                            "name": e.name,
                            "mention_count": e.mention_count or 1,
                            "entity_type": e.entity_type,
                        }

                    parent.setdefault(a_entity.id, a_entity.id)
                    parent.setdefault(b_entity.id, b_entity.id)
                    union(a_entity.id, b_entity.id)

                    key = (min(a_entity.id, b_entity.id), max(a_entity.id, b_entity.id))
                    pair_similarity[key] = round(ratio, 3)

        # Group by cluster root
        clusters_map: dict[int, list[int]] = defaultdict(list)
        for eid in entity_info:
            root = find(eid)
            clusters_map[root].append(eid)

        # Build output clusters (only multi-entity clusters)
        clusters = []
        for member_ids in clusters_map.values():
            if len(member_ids) < 2:
                continue

            members = [entity_info[eid] for eid in member_ids]
            # Canonical = highest mention_count
            members.sort(key=lambda m: m["mention_count"], reverse=True)
            canonical = members[0]
            canonical_id = canonical["id"]

            # Add similarity score for each duplicate (relative to canonical)
            duplicates = []
            for m in members[1:]:
                key = (min(canonical_id, m["id"]), max(canonical_id, m["id"]))
                sim = pair_similarity.get(key)
                duplicates.append({**m, "similarity": sim})

            clusters.append({
                "canonical": canonical,
                "duplicates": duplicates,
                "cluster_size": len(members),
                "entity_type": canonical["entity_type"],
            })

        # Sort by cluster size descending
        clusters.sort(key=lambda c: c["cluster_size"], reverse=True)

        logger.info(
            f"KG duplicates: Found {len(clusters)} clusters from "
            f"{len(rows)} entities (threshold={threshold})"
        )
        return clusters[:limit]

    async def merge_duplicate_clusters(
        self,
        entity_type: str | None = None,
        threshold: float | None = None,
        dry_run: bool = True,
    ) -> dict:
        """
        Auto-merge duplicate clusters found by find_duplicate_clusters().

        Picks highest mention_count as canonical, merges others into it.
        """
        clusters = await self.find_duplicate_clusters(
            entity_type=entity_type,
            threshold=threshold,
            limit=200,
        )

        if not clusters:
            return {
                "dry_run": dry_run,
                "clusters_found": 0,
                "entities_merged": 0,
                "clusters": [],
            }

        kg_service = KnowledgeGraphService(self.db)
        merged_count = 0
        cluster_summaries = []

        for cluster in clusters:
            canonical_id = cluster["canonical"]["id"]
            dup_ids = [d["id"] for d in cluster["duplicates"]]

            summary = {
                "canonical": cluster["canonical"],
                "merged": cluster["duplicates"],
            }
            cluster_summaries.append(summary)

            if not dry_run:
                for dup_id in dup_ids:
                    try:
                        await kg_service.merge_entities(dup_id, canonical_id)
                        merged_count += 1
                    except Exception as e:
                        logger.warning(f"KG cleanup: Failed to merge {dup_id} → {canonical_id}: {e}")
            else:
                merged_count += len(dup_ids)

        if not dry_run:
            logger.info(
                f"KG cleanup: Merged {merged_count} duplicate entities "
                f"across {len(clusters)} clusters"
            )

        return {
            "dry_run": dry_run,
            "clusters_found": len(clusters),
            "entities_merged": merged_count,
            "clusters": cluster_summaries[:50],  # Limit response size
        }
