"""
Knowledge Graph Cleanup Service — Bulk operations for data quality.

Provides admin-only operations to scan and clean up invalid entities,
find duplicate clusters via embedding similarity, and auto-merge them.
All destructive operations support dry_run mode.
"""
from loguru import logger
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KGEntity, KGRelation
from services.knowledge_graph_service import KnowledgeGraphService
from utils.config import settings


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
        Find clusters of likely-duplicate entities via embedding similarity.

        Returns clusters sorted by size, each with a canonical entity
        (highest mention_count) and its duplicates.
        """
        if threshold is None:
            threshold = settings.kg_similarity_threshold

        # Build entity type filter
        type_filter = ""
        params: dict = {"threshold": threshold}
        if entity_type:
            type_filter = "AND a.entity_type = :entity_type AND b.entity_type = :entity_type"
            params["entity_type"] = entity_type

        # Find pairs of similar entities using pgvector
        sql = text(f"""
            SELECT a.id as id_a, a.name as name_a, a.mention_count as mc_a,
                   a.entity_type as type_a,
                   b.id as id_b, b.name as name_b, b.mention_count as mc_b,
                   b.entity_type as type_b,
                   1 - (a.embedding <=> b.embedding) as similarity
            FROM kg_entities a
            JOIN kg_entities b ON a.id < b.id
            WHERE a.is_active = true AND b.is_active = true
              AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
              AND a.entity_type = b.entity_type
              AND 1 - (a.embedding <=> b.embedding) >= :threshold
              {type_filter}
            ORDER BY similarity DESC
            LIMIT 500
        """)

        result = await self.db.execute(sql, params)
        pairs = result.fetchall()

        # Build clusters via union-find
        parent: dict[int, int] = {}
        entity_info: dict[int, dict] = {}

        def find(x: int) -> int:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        for row in pairs:
            entity_info[row.id_a] = {
                "id": row.id_a,
                "name": row.name_a,
                "mention_count": row.mc_a or 1,
                "entity_type": row.type_a,
            }
            entity_info[row.id_b] = {
                "id": row.id_b,
                "name": row.name_b,
                "mention_count": row.mc_b or 1,
                "entity_type": row.type_b,
            }
            parent.setdefault(row.id_a, row.id_a)
            parent.setdefault(row.id_b, row.id_b)
            union(row.id_a, row.id_b)

        # Group by cluster root
        clusters_map: dict[int, list[int]] = {}
        for eid in entity_info:
            root = find(eid)
            clusters_map.setdefault(root, []).append(eid)

        # Build output clusters (only multi-entity clusters)
        clusters = []
        for member_ids in clusters_map.values():
            if len(member_ids) < 2:
                continue

            members = [entity_info[eid] for eid in member_ids]
            # Canonical = highest mention_count
            members.sort(key=lambda m: m["mention_count"], reverse=True)
            canonical = members[0]
            duplicates = members[1:]

            clusters.append({
                "canonical": canonical,
                "duplicates": duplicates,
                "cluster_size": len(members),
                "entity_type": canonical["entity_type"],
            })

        # Sort by cluster size descending
        clusters.sort(key=lambda c: c["cluster_size"], reverse=True)
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
