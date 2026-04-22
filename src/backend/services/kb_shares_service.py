"""
KB-level share helpers — Lane C consumer rewrite for KBPermission.

Backs the legacy `KBPermissionCreate`/`KBPermissionResponse` API surface
with circles v1's atom_explicit_grants. A "KB share" is logically one
permission per (kb_id, user_id) pair, but physically one
atom_explicit_grants row per chunk of the KB (mirrors the explosion done
by pc20260420_circles_v1_schema.py).

Why this shape (vs a `kb_explicit_grants` table):
  - Single grants table = single SQL filter pushdown for retrieval. The
    document_chunks circle filter only has to consider one EXISTS subquery
    per row.
  - Per-chunk granularity is the long-term shape. v2 will allow sharing
    individual chunks (e.g. one paragraph of a document) without altering
    the schema.
  - Aggregation back to KB-level for the share-management UI is a single
    GROUP BY at read time.

Public surface:
    share_kb(db, kb_id, target_user_id, permission_level, granted_by)
        Idempotent: re-sharing with a different level updates rows.
    revoke_kb_share(db, kb_id, target_user_id)
        Deletes every grant for chunks of this KB granted to this user.
    list_kb_shares(db, kb_id) -> list[dict]
        Returns one aggregated row per (granted_to_user_id) with
        permission_level (MAX), granted_by, granted_at.
    get_user_kb_permission_level(db, kb_id, user_id) -> str | None
        Returns "read"/"write"/"admin" or None. MAX-permissive across chunks.
    list_user_shared_kb_ids(db, user_id) -> set[int]
        Returns KB IDs the user has any grant on.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_PERM_RANK = {"read": 1, "write": 2, "admin": 3}


async def share_kb(
    db: AsyncSession,
    kb_id: int,
    target_user_id: int,
    permission_level: str,
    granted_by: int | None,
) -> None:
    """
    Idempotently grant `target_user_id` `permission_level` access to all
    chunks in `kb_id`. If a grant already exists for a chunk it is updated
    in place; missing grants are inserted.
    """
    if permission_level not in _PERM_RANK:
        raise ValueError(
            f"Invalid permission_level {permission_level!r} "
            f"(expected one of {sorted(_PERM_RANK)})"
        )

    # Post-atoms-per-document (pc20260423): one grant per DOCUMENT, not per
    # chunk. The KB-share explosion size drops from O(chunks) to O(documents);
    # for a typical book-sized KB that's 2-3 orders of magnitude smaller.
    now = datetime.now(UTC).replace(tzinfo=None)
    await db.execute(
        text(
            "INSERT INTO atom_explicit_grants "
            "  (atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
            "SELECT a.atom_id, :target, :perm, :granter, :now "
            "FROM atoms a "
            "JOIN documents d ON a.source_id = d.id::text "
            "WHERE a.source_table = 'documents' "
            "  AND d.knowledge_base_id = :kb_id "
            "ON CONFLICT (atom_id, granted_to_user_id) DO UPDATE "
            "  SET permission_level = EXCLUDED.permission_level, "
            "      granted_by = EXCLUDED.granted_by, "
            "      granted_at = EXCLUDED.granted_at"
        ),
        {
            "target": target_user_id,
            "perm": permission_level,
            "granter": granted_by,
            "now": now,
            "kb_id": kb_id,
        },
    )
    await db.commit()


async def revoke_kb_share(
    db: AsyncSession,
    kb_id: int,
    target_user_id: int,
) -> int:
    """
    Delete every atom_explicit_grants row on chunks of `kb_id` granted to
    `target_user_id`.

    Returns the number of DELETED ROWS (one per chunk, NOT one per logical
    share). Callers that want "did we revoke something" should compare
    `> 0`; callers that need the logical share count should call
    `list_kb_shares` before and after.
    """
    result = await db.execute(
        text(
            "DELETE FROM atom_explicit_grants g "
            "USING atoms a, documents d "
            "WHERE g.atom_id = a.atom_id "
            "  AND a.source_table = 'documents' "
            "  AND a.source_id = d.id::text "
            "  AND d.knowledge_base_id = :kb_id "
            "  AND g.granted_to_user_id = :target"
        ),
        {"kb_id": kb_id, "target": target_user_id},
    )
    await db.commit()
    return result.rowcount or 0


async def list_kb_shares(db: AsyncSession, kb_id: int) -> list[dict[str, Any]]:
    """
    One aggregated row per granted user.

    Returns:
        permission   MAX-permissive level (admin > write > read) across
                     all this user's chunk-grants on the KB.
        granted_at   timestamp of the MOST RECENT grant for this user.
        granted_by   the user_id that issued THAT most-recent grant
                     (paired correctly via DISTINCT ON, not the arbitrary
                     MAX(granted_by) the legacy aggregation produced).

    DISTINCT ON in the inner CTE picks one representative row per
    (user, atom) pair (the most-recent grant per chunk); the outer
    aggregation then collapses across chunks.
    """
    result = await db.execute(
        text(
            "WITH latest_per_doc AS ("
            "  SELECT DISTINCT ON (g.granted_to_user_id, g.atom_id) "
            "    g.granted_to_user_id, g.atom_id, g.permission_level, "
            "    g.granted_by, g.granted_at "
            "  FROM atom_explicit_grants g "
            "  JOIN atoms a ON g.atom_id = a.atom_id "
            "  JOIN documents d ON a.source_id = d.id::text "
            "  WHERE a.source_table = 'documents' "
            "    AND d.knowledge_base_id = :kb_id "
            "  ORDER BY g.granted_to_user_id, g.atom_id, g.granted_at DESC "
            "), "
            "ranked AS ("
            "  SELECT granted_to_user_id, "
            "         MAX(CASE permission_level "
            "             WHEN 'admin' THEN 3 WHEN 'write' THEN 2 ELSE 1 END) AS rank, "
            "         MAX(granted_at) AS latest_at "
            "  FROM latest_per_doc "
            "  GROUP BY granted_to_user_id "
            ") "
            "SELECT DISTINCT ON (r.granted_to_user_id) "
            "       r.granted_to_user_id AS user_id, "
            "       r.rank, "
            "       r.latest_at AS granted_at, "
            "       lpd.granted_by "
            "FROM ranked r "
            "JOIN latest_per_doc lpd "
            "  ON lpd.granted_to_user_id = r.granted_to_user_id "
            "  AND lpd.granted_at = r.latest_at "
            "ORDER BY r.granted_to_user_id, lpd.granted_by NULLS LAST"
        ),
        {"kb_id": kb_id},
    )
    rank_to_perm = {1: "read", 2: "write", 3: "admin"}
    return [
        {
            "user_id": row.user_id,
            "permission": rank_to_perm.get(int(row.rank), "read"),
            "granted_by": row.granted_by,
            "granted_at": row.granted_at,
        }
        for row in result.fetchall()
    ]


async def get_user_kb_permission_level(
    db: AsyncSession,
    kb_id: int,
    user_id: int,
) -> str | None:
    """MAX-permissive level across this user's grants on chunks of `kb_id`, or None."""
    result = await db.execute(
        text(
            "SELECT MAX(CASE g.permission_level "
            "             WHEN 'admin' THEN 3 WHEN 'write' THEN 2 ELSE 1 END) AS rank "
            "FROM atom_explicit_grants g "
            "JOIN atoms a ON g.atom_id = a.atom_id "
            "JOIN documents d ON a.source_id = d.id::text "
            "WHERE a.source_table = 'documents' "
            "  AND d.knowledge_base_id = :kb_id "
            "  AND g.granted_to_user_id = :user_id"
        ),
        {"kb_id": kb_id, "user_id": user_id},
    )
    rank = result.scalar()
    if rank is None:
        return None
    return {1: "read", 2: "write", 3: "admin"}.get(int(rank))


async def list_user_shared_kb_ids(db: AsyncSession, user_id: int) -> set[int]:
    """KB IDs the user has any document-level grant on."""
    result = await db.execute(
        text(
            "SELECT DISTINCT d.knowledge_base_id "
            "FROM atom_explicit_grants g "
            "JOIN atoms a ON g.atom_id = a.atom_id "
            "JOIN documents d ON a.source_id = d.id::text "
            "WHERE a.source_table = 'documents' "
            "  AND g.granted_to_user_id = :user_id "
            "  AND d.knowledge_base_id IS NOT NULL"
        ),
        {"user_id": user_id},
    )
    return {int(r[0]) for r in result.fetchall()}
