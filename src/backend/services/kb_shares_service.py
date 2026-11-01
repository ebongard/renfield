"""
KB-level shares — one ``kb_shares`` row per (kb_id, user_id).

A share is evaluated at QUERY TIME (``services/circle_sql.py`` document filters,
``CircleResolver.can_access_atom``), so it covers every document in the KB,
including documents added or moved in after the share was made. Ownership is
untouched: a document's atom owner stays the KB owner.

Legacy per-document grants. Before ``kb_shares`` existed (pc20261101) a share was
materialised as one ``atom_explicit_grants`` row per document that was in the KB
at share time. Those rows are KEPT and still count everywhere in this module:

    access / ACL level = kb_shares row  OR  any grant on a document of the KB
    level              = MAX over both (admin > write > read)

so nothing is lost before ``bin/backfill_kb_shares.py`` has derived the missing
``kb_shares`` rows. Because the legacy grants cannot be told apart from a grant
given on a single document (there is no marker, and no code writes such a grant
today), ``share_kb`` keeps their level in step and ``revoke_kb_share`` deletes
them, exactly as before.

Public surface:
    share_kb(db, kb_id, target_user_id, permission_level, granted_by)
        Idempotent upsert of the kb_shares row; aligns legacy grants' level.
    revoke_kb_share(db, kb_id, target_user_id)
        Deletes the kb_shares row AND the user's grants on the KB's documents.
    list_kb_shares(db, kb_id) -> list[dict]
        One row per grantee (kb_shares ∪ legacy grants), MAX level.
    get_user_kb_permission_level(db, kb_id, user_id) -> str | None
    get_user_kb_permission_levels(db, user_id, kb_ids) -> dict[int, str]
    list_user_shared_kb_ids(db, user_id) -> set[int]
    backfill_kb_shares(db, *, dry_run) -> KBShareBackfillResult
        Derives missing kb_shares rows from legacy grants (owner-run script).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PERM_RANK = {"read": 1, "write": 2, "admin": 3}
_RANK_TO_PERM = {1: "read", 2: "write", 3: "admin"}


def _rank_sql(col: str) -> str:
    return f"CASE {col} WHEN 'admin' THEN 3 WHEN 'write' THEN 2 ELSE 1 END"


# Legacy per-document grants on documents of a KB: (kb_id, user_id, level, by, at).
_LEGACY_GRANTS_FROM = (
    "FROM atom_explicit_grants g "
    "JOIN atoms a ON g.atom_id = a.atom_id "
    "JOIN documents d ON a.source_id = d.id::text "
    "WHERE a.source_table = 'documents' "
    "  AND d.knowledge_base_id IS NOT NULL "
)


async def share_kb(
    db: AsyncSession,
    kb_id: int,
    target_user_id: int,
    permission_level: str,
    granted_by: int | None,
) -> None:
    """
    Idempotently share `kb_id` with `target_user_id` at `permission_level`.

    Upserts the ``kb_shares`` row — which covers every current AND future
    document of the KB, an empty KB included. Legacy per-document grants of the
    same user on this KB are set to the same level, so a level change (a
    downgrade in particular) takes effect: the ACL level is the MAX over both.
    No new per-document grants are written.
    """
    if permission_level not in _PERM_RANK:
        raise ValueError(
            f"Invalid permission_level {permission_level!r} "
            f"(expected one of {sorted(_PERM_RANK)})"
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    params = {
        "target": target_user_id,
        "perm": permission_level,
        "granter": granted_by,
        "now": now,
        "kb_id": kb_id,
    }
    await db.execute(
        text(
            "INSERT INTO kb_shares "
            "  (kb_id, user_id, permission_level, granted_by, granted_at) "
            "VALUES (:kb_id, :target, :perm, :granter, :now) "
            "ON CONFLICT (kb_id, user_id) DO UPDATE "
            "  SET permission_level = EXCLUDED.permission_level, "
            "      granted_by = EXCLUDED.granted_by, "
            "      granted_at = EXCLUDED.granted_at"
        ),
        params,
    )
    await db.execute(
        text(
            "UPDATE atom_explicit_grants g "
            "SET permission_level = :perm "
            "FROM atoms a, documents d "
            "WHERE g.atom_id = a.atom_id "
            "  AND a.source_table = 'documents' "
            "  AND a.source_id = d.id::text "
            "  AND d.knowledge_base_id = :kb_id "
            "  AND g.granted_to_user_id = :target "
            "  AND g.permission_level <> :perm"
        ),
        {"perm": permission_level, "kb_id": kb_id, "target": target_user_id},
    )
    await db.commit()


async def revoke_kb_share(
    db: AsyncSession,
    kb_id: int,
    target_user_id: int,
) -> int:
    """
    Remove `target_user_id`'s share of `kb_id`: the ``kb_shares`` row AND every
    legacy grant of that user on a document of the KB (they cannot be told apart
    from a single-document grant, so this is the pre-kb_shares behaviour).

    Returns the number of deleted rows (share row + grants). ``0`` means there
    was nothing to revoke.
    """
    share = await db.execute(
        text("DELETE FROM kb_shares WHERE kb_id = :kb_id AND user_id = :target"),
        {"kb_id": kb_id, "target": target_user_id},
    )
    grants = await db.execute(
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
    from services.circle_resolver import CircleResolver
    CircleResolver.invalidate_for_grantee(target_user_id)
    return (share.rowcount or 0) + (grants.rowcount or 0)


async def list_kb_shares(db: AsyncSession, kb_id: int) -> list[dict[str, Any]]:
    """
    One aggregated row per grantee — from ``kb_shares`` and legacy grants.

    Returns:
        permission   MAX-permissive level (admin > write > read) across the
                     kb_shares row and all legacy grants on the KB.
        granted_at   timestamp of the MOST RECENT of those rows.
        granted_by   the user_id that issued THAT most-recent row (paired via
                     DISTINCT ON, not an arbitrary MAX(granted_by)).
    """
    result = await db.execute(
        text(
            "WITH per_source AS ("
            "  (SELECT DISTINCT ON (g.granted_to_user_id, g.atom_id) "
            "     g.granted_to_user_id AS user_id, g.permission_level, "
            "     g.granted_by, g.granted_at "
            f"   {_LEGACY_GRANTS_FROM}"
            "     AND d.knowledge_base_id = :kb_id "
            "   ORDER BY g.granted_to_user_id, g.atom_id, g.granted_at DESC) "
            "  UNION ALL "
            "  SELECT ks.user_id, ks.permission_level, ks.granted_by, ks.granted_at "
            "  FROM kb_shares ks WHERE ks.kb_id = :kb_id "
            "), "
            "ranked AS ("
            f"  SELECT user_id, MAX({_rank_sql('permission_level')}) AS rank, "
            "         MAX(granted_at) AS latest_at "
            "  FROM per_source GROUP BY user_id "
            ") "
            "SELECT DISTINCT ON (r.user_id) "
            "       r.user_id, r.rank, r.latest_at AS granted_at, ps.granted_by "
            "FROM ranked r "
            "JOIN per_source ps "
            "  ON ps.user_id = r.user_id AND ps.granted_at = r.latest_at "
            "ORDER BY r.user_id, ps.granted_by NULLS LAST"
        ),
        {"kb_id": kb_id},
    )
    return [
        {
            "user_id": row.user_id,
            "permission": _RANK_TO_PERM.get(int(row.rank), "read"),
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
    """MAX level over the kb_shares row and legacy grants of `user_id` on `kb_id`, or None."""
    levels = await get_user_kb_permission_levels(db, user_id, [kb_id])
    return levels.get(kb_id)


async def list_user_shared_kb_ids(db: AsyncSession, user_id: int) -> set[int]:
    """KB IDs shared with the user — via kb_shares or a legacy document grant."""
    return set(await get_user_kb_permission_levels(db, user_id))


async def get_user_kb_permission_levels(
    db: AsyncSession,
    user_id: int,
    kb_ids: list[int] | set[int] | None = None,
) -> dict[int, str]:
    """One query for many KBs (audit K2): {kb_id: "read"|"write"|"admin"} for every
    KB the user holds a share on — a ``kb_shares`` row or a legacy grant on one of
    its documents, MAX over both. KBs without either are absent.

    If `kb_ids` is provided, the result is restricted to that subset (still one
    query); `None` returns the full map for the user.
    """
    params: dict[str, Any] = {"user_id": user_id}
    kb_filter_g = kb_filter_s = ""
    if kb_ids is not None:
        if not kb_ids:
            return {}
        kb_filter_g = " AND d.knowledge_base_id = ANY(:kb_ids)"
        kb_filter_s = " AND ks.kb_id = ANY(:kb_ids)"
        params["kb_ids"] = list(kb_ids)
    sql = (
        "SELECT kb_id, MAX(rank) AS rank FROM ("
        f"  SELECT d.knowledge_base_id AS kb_id, {_rank_sql('g.permission_level')} AS rank "
        f"  {_LEGACY_GRANTS_FROM}"
        f"    AND g.granted_to_user_id = :user_id{kb_filter_g} "
        "  UNION ALL "
        f"  SELECT ks.kb_id, {_rank_sql('ks.permission_level')} AS rank "
        f"  FROM kb_shares ks WHERE ks.user_id = :user_id{kb_filter_s} "
        ") s GROUP BY kb_id"
    )

    result = await db.execute(text(sql), params)
    return {
        int(row.kb_id): _RANK_TO_PERM[int(row.rank)]
        for row in result.fetchall()
    }


# =============================================================================
# Backfill: derive kb_shares rows from legacy per-document grants
# =============================================================================


@dataclass
class KBShareBackfillResult:
    """Counts only (no KB names, no document content) plus the ids a revert needs."""

    pairs_with_grants: int = 0     # distinct (kb, user) pairs holding a legacy grant
    already_shared: int = 0        # of those, already a kb_shares row
    inserted: list[dict[str, Any]] = field(default_factory=list)
    # inserted rows: {"kb_id", "user_id", "granted_at"} — what --revert deletes

    @property
    def missing(self) -> int:
        return self.pairs_with_grants - self.already_shared


async def backfill_kb_shares(db: AsyncSession, *, dry_run: bool) -> KBShareBackfillResult:
    """Insert one ``kb_shares`` row per (kb, user) that holds a legacy grant on a
    document of the KB but has no kb_shares row yet.

    The new row carries the MAX level over that user's grants on the KB, and
    granted_by / granted_at of the most recent grant. Idempotent: an existing
    kb_shares row (e.g. a share made through the UI after the upgrade) is never
    touched (``ON CONFLICT DO NOTHING``). Legacy grants are left as they are.
    ``dry_run`` counts and writes nothing.
    """
    derived_cte = (
        "WITH derived AS ("
        "  SELECT DISTINCT ON (d.knowledge_base_id, g.granted_to_user_id) "
        "    d.knowledge_base_id AS kb_id, g.granted_to_user_id AS user_id, "
        "    g.granted_by, g.granted_at, "
        f"    MAX({_rank_sql('g.permission_level')}) OVER ("
        "      PARTITION BY d.knowledge_base_id, g.granted_to_user_id) AS rank "
        f"  {_LEGACY_GRANTS_FROM}"
        "  ORDER BY d.knowledge_base_id, g.granted_to_user_id, g.granted_at DESC "
        ") "
    )
    counts = (await db.execute(text(
        derived_cte
        + "SELECT COUNT(*) AS pairs, "
        "       COUNT(ks.kb_id) AS already "
        "FROM derived dv "
        "LEFT JOIN kb_shares ks ON ks.kb_id = dv.kb_id AND ks.user_id = dv.user_id"
    ))).one()
    result = KBShareBackfillResult(
        pairs_with_grants=int(counts.pairs), already_shared=int(counts.already)
    )
    if dry_run:
        return result

    rows = (await db.execute(text(
        derived_cte
        + "INSERT INTO kb_shares (kb_id, user_id, permission_level, granted_by, granted_at) "
        "SELECT dv.kb_id, dv.user_id, "
        "       CASE dv.rank WHEN 3 THEN 'admin' WHEN 2 THEN 'write' ELSE 'read' END, "
        "       dv.granted_by, COALESCE(dv.granted_at, now() AT TIME ZONE 'UTC') "
        "FROM derived dv "
        "ON CONFLICT (kb_id, user_id) DO NOTHING "
        "RETURNING kb_id, user_id, granted_at"
    ))).fetchall()
    await db.commit()
    result.inserted = [
        {"kb_id": int(r.kb_id), "user_id": int(r.user_id), "granted_at": r.granted_at.isoformat()}
        for r in rows
    ]
    return result


async def revert_kb_share_backfill(db: AsyncSession, inserted: list[dict[str, Any]]) -> int:
    """Delete exactly the rows a backfill run inserted (from its log).

    A row is deleted only while its ``granted_at`` still equals the logged value:
    a share re-made or changed through the UI since then is somebody's decision,
    not backfill output, and stays. Legacy grants were never touched, so access
    falls back to them. Returns the number of deleted rows.
    """
    deleted = 0
    for row in inserted:
        res = await db.execute(
            text(
                "DELETE FROM kb_shares WHERE kb_id = :kb_id AND user_id = :user_id "
                "AND granted_at = :granted_at"
            ),
            {
                "kb_id": int(row["kb_id"]),
                "user_id": int(row["user_id"]),
                "granted_at": datetime.fromisoformat(row["granted_at"]),
            },
        )
        deleted += res.rowcount or 0
    await db.commit()
    return deleted
