"""
KB-level shares — one ``kb_shares`` row per (kb_id, user_id).

A share is evaluated at QUERY time (``services/circle_sql.py`` document filters,
``CircleResolver.can_access_atom``), so it covers every document in the KB,
including documents added or moved in after the share was made. Ownership is
untouched: a document's atom owner stays the KB owner.

🛑 KB level comes ONLY from ``kb_shares`` (#1446, owner decision (a)). A grant in
``atom_explicit_grants`` on a document opens THAT document (and its facts) through
the circle filters, never the KB: it no longer puts the KB into the grantee's
listing, gives no upload right and no level in ``check_kb_access``.

Legacy per-document grants. Before ``kb_shares`` existed (pc20261101) a share was
materialised as one grant per document that was in the KB at share time. Those
rows are KEPT: they still open their documents, and ``revoke_kb_share`` deletes
them with the share (they cannot be told apart from a single-document grant, and
left behind they would keep the old documents readable after the revoke).
Their KB level is carried over into ``kb_shares`` by ``bin/backfill_kb_shares.py``,
which MUST run before this code is deployed (``uncovered_legacy_pairs`` = 0 clean
pairs). Pairs the backfill cannot trust (the flags in ``UNCOVERED_PAIRS_SQL``: a
grant that may have moved with its document into another KB before #1464) are
never converted on their own; the owner adopts them one by one
(``adopt_kb_share_pairs``). Until then such a grantee still reads those documents,
so ``list_kb_shares`` lists them (``kind="document_grants"``) for the owner to see
and revoke.

Public surface:
    share_kb(db, kb_id, target_user_id, permission_level, granted_by, *, can_delete=None)
        ``can_delete`` = the owner's delete switch (None keeps it).
    user_may_delete_in_kb(db, kb_id, user_id) -> bool
    get_user_kb_delete_switches(db, user_id, kb_ids) -> set[int]
    revoke_kb_share(db, kb_id, target_user_id)
    list_kb_shares(db, kb_id) -> list[dict]
    get_user_kb_permission_level(db, kb_id, user_id) -> str | None
    get_user_kb_permission_levels(db, user_id, kb_ids) -> dict[int, str]
    list_user_shared_kb_ids(db, user_id) -> set[int]
    uncovered_legacy_pairs(db) -> LegacyPairs
    backfill_kb_shares(db, *, dry_run) -> KBShareBackfillResult
    adopt_kb_share_pairs(db, pairs) -> KBShareBackfillResult
    revert_kb_share_backfill(db, inserted) -> int
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

# One row per (kb_id, user_id) that holds a legacy grant on a document of the KB
# and has NO kb_shares row: the pairs whose KB level the code no longer derives
# (#1446). Columns: kb_id, user_id, grants, rank (MAX level), granted_by and
# granted_at of the most recent grant, and the suspicion flags.
#
# A grant may have moved with its document into ANOTHER KB before #1464 (the
# move did not delete grants then). The data model has no move trace, so the
# flags are indirect -- false alarms are preferred over false shares. The owner
# moving between her own KBs never sets foreign_granter, and these moves leave
# no trace in the source KB:
#   (a) the source KB was shared again after the move (the old share_kb gave
#       the remaining documents a new granted_at, so split_batch is false);
#   (b) the sibling documents or the source KB were deleted since;
#   (c) every document of the source KB moved together into the destination.
# The destination is then typically a newer KB ("Privat": one document moved
# in, private uploads after it, which partial does not see), hence the last two
# flags. All timestamps are naive UTC (models._utcnow); a NULL created_at sets
# no flag.
#   foreign_granter  a grant was given by someone other than the CURRENT KB
#                    owner (also flags shares an admin made: owner decides);
#   partial          a document of the KB that existed when the grant was given
#                    (created_at <= granted_at, or no created_at) carries no
#                    grant for the user -- the old share_kb granted EVERY
#                    document of the KB in one statement;
#   split_batch      the same share statement (same user, granter and
#                    granted_at) also produced grants on documents that are now
#                    in a DIFFERENT KB, so a document left its share's KB;
#   kb_younger_than_grant  the KB was created after one of the grants: the
#                    grant cannot stem from a share of THIS KB (cases a-c);
#   single_grant     one grant in a KB that now holds more documents -- the
#                    shape of one moved document (also flags a legacy share of a
#                    one-document KB that grew since: a false alarm).
# Not detectable: a single moved document, by its own owner, into a KB older
# than the grant that still holds only that document.
UNCOVERED_PAIRS_SQL = (
    "WITH legacy AS ("
    "  SELECT d.knowledge_base_id AS kb_id, g.granted_to_user_id AS user_id, "
    "         g.permission_level, g.granted_by, g.granted_at "
    f"  {_LEGACY_GRANTS_FROM}"
    "), "
    "uncovered AS ("
    "  SELECT l.* FROM legacy l "
    "  WHERE NOT EXISTS (SELECT 1 FROM kb_shares ks "
    "                    WHERE ks.kb_id = l.kb_id AND ks.user_id = l.user_id)"
    ") "
    "SELECT u.kb_id, u.user_id, COUNT(*) AS grants, "
    f"       MAX({_rank_sql('u.permission_level')}) AS rank, "
    "       (ARRAY_AGG(u.granted_by ORDER BY u.granted_at DESC NULLS LAST))[1] AS granted_by, "
    "       MAX(u.granted_at) AS granted_at, "
    "       BOOL_OR(u.granted_by IS DISTINCT FROM kb.owner_id) AS foreign_granter, "
    "       BOOL_OR(EXISTS ("
    "         SELECT 1 FROM documents d2 "
    "         JOIN atoms a2 ON a2.source_table = 'documents' AND a2.source_id = d2.id::text "
    "         WHERE d2.knowledge_base_id = u.kb_id "
    "           AND (d2.created_at IS NULL OR d2.created_at <= u.granted_at) "
    "           AND NOT EXISTS (SELECT 1 FROM atom_explicit_grants g2 "
    "                           WHERE g2.atom_id = a2.atom_id "
    "                             AND g2.granted_to_user_id = u.user_id)"
    "       )) AS partial, "
    "       BOOL_OR(EXISTS ("
    "         SELECT 1 FROM legacy l2 "
    "         WHERE l2.user_id = u.user_id AND l2.kb_id <> u.kb_id "
    "           AND l2.granted_at = u.granted_at "
    "           AND l2.granted_by IS NOT DISTINCT FROM u.granted_by"
    "       )) AS split_batch, "
    "       BOOL_OR(kb.created_at > u.granted_at) AS kb_younger_than_grant, "
    "       (COUNT(*) = 1 AND (SELECT COUNT(*) FROM documents d3 "
    "                          WHERE d3.knowledge_base_id = u.kb_id) > 1) AS single_grant "
    "FROM uncovered u JOIN knowledge_bases kb ON kb.id = u.kb_id "
    "GROUP BY u.kb_id, u.user_id "
    "ORDER BY u.kb_id, u.user_id"
)


async def share_kb(
    db: AsyncSession,
    kb_id: int,
    target_user_id: int,
    permission_level: str,
    granted_by: int | None,
    *,
    can_delete: bool | None = None,
) -> None:
    """
    Idempotently share `kb_id` with `target_user_id` at `permission_level`.

    ``can_delete`` is the owner's separate delete switch (pc20261102): ``None``
    keeps the stored value (FALSE for a new share), ``True``/``False`` sets it.
    WHO may set it is the route's decision (KB owner or kb.all only); this
    function only writes.

    Upserts the ``kb_shares`` row — which covers every current AND future
    document of the KB, an empty KB included. No per-document grants are
    written or changed: the KB level comes from this row alone (#1446).
    """
    if permission_level not in _PERM_RANK:
        raise ValueError(
            f"Invalid permission_level {permission_level!r} "
            f"(expected one of {sorted(_PERM_RANK)})"
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    await db.execute(
        text(
            "INSERT INTO kb_shares "
            "  (kb_id, user_id, permission_level, granted_by, granted_at, can_delete) "
            "VALUES (:kb_id, :target, :perm, :granter, :now, "
            "        COALESCE(CAST(:can_delete AS BOOLEAN), FALSE)) "
            "ON CONFLICT (kb_id, user_id) DO UPDATE "
            "  SET permission_level = EXCLUDED.permission_level, "
            "      granted_by = EXCLUDED.granted_by, "
            "      granted_at = EXCLUDED.granted_at, "
            "      can_delete = COALESCE(CAST(:can_delete AS BOOLEAN), kb_shares.can_delete)"
        ),
        {
            "target": target_user_id,
            "perm": permission_level,
            "granter": granted_by,
            "now": now,
            "kb_id": kb_id,
            "can_delete": can_delete,
        },
    )
    await db.commit()


async def revoke_kb_share(
    db: AsyncSession,
    kb_id: int,
    target_user_id: int,
) -> int:
    """
    Remove `target_user_id`'s share of `kb_id`: the ``kb_shares`` row AND every
    grant of that user on a document of the KB. The grants are the legacy
    materialisation of the same share (no code writes a single-document grant);
    left behind, they would keep the old documents readable after the revoke.

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


SHARE_KIND_KB = "kb_share"
SHARE_KIND_DOCUMENT_GRANTS = "document_grants"


async def list_kb_shares(db: AsyncSession, kb_id: int) -> list[dict[str, Any]]:
    """Everyone the KB's documents are shared with, for the owner's share list.

    ``kind="kb_share"``: one row per ``kb_shares`` grantee (permission,
    can_delete, granted_by, granted_at; ``documents`` None).
    ``kind="document_grants"``: a grantee WITHOUT a kb_shares row who still holds
    grants on documents of the KB (a legacy share the backfill did not convert,
    #1446). No KB level (``permission`` None), never ``can_delete``;
    ``documents`` = how many documents the grants open, granted_by / granted_at
    of the most recent one. Listed so the owner sees who still reads those
    documents and can revoke it (``revoke_kb_share`` removes the grants).
    Sorted by user_id; a user appears once.
    """
    shares = (await db.execute(
        text(
            "SELECT ks.user_id, ks.permission_level, ks.granted_by, ks.granted_at, "
            "       ks.can_delete "
            "FROM kb_shares ks WHERE ks.kb_id = :kb_id"
        ),
        {"kb_id": kb_id},
    )).fetchall()
    residual = (await db.execute(
        text(
            "SELECT g.granted_to_user_id AS user_id, COUNT(DISTINCT d.id) AS documents, "
            "       (ARRAY_AGG(g.granted_by ORDER BY g.granted_at DESC NULLS LAST))[1] "
            "         AS granted_by, "
            "       MAX(g.granted_at) AS granted_at "
            f"{_LEGACY_GRANTS_FROM}"
            "  AND d.knowledge_base_id = :kb_id "
            "  AND NOT EXISTS (SELECT 1 FROM kb_shares ks "
            "                  WHERE ks.kb_id = :kb_id AND ks.user_id = g.granted_to_user_id) "
            "GROUP BY g.granted_to_user_id"
        ),
        {"kb_id": kb_id},
    )).fetchall()
    rows = [
        {
            "user_id": row.user_id,
            "kind": SHARE_KIND_KB,
            # An unknown stored level grants nothing (get_user_kb_permission_levels
            # drops it): listed without a level, still revocable.
            "permission": row.permission_level if row.permission_level in _PERM_RANK else None,
            "documents": None,
            "granted_by": row.granted_by,
            "granted_at": row.granted_at,
            "can_delete": bool(row.can_delete),
        }
        for row in shares
    ] + [
        {
            "user_id": row.user_id,
            "kind": SHARE_KIND_DOCUMENT_GRANTS,
            "permission": None,
            "documents": int(row.documents),
            "granted_by": row.granted_by,
            "granted_at": row.granted_at,
            "can_delete": False,
        }
        for row in residual
    ]
    return sorted(rows, key=lambda r: r["user_id"])


async def get_user_kb_permission_level(
    db: AsyncSession,
    kb_id: int,
    user_id: int,
) -> str | None:
    """The user's ``kb_shares`` level on `kb_id`, or None."""
    levels = await get_user_kb_permission_levels(db, user_id, [kb_id])
    return levels.get(kb_id)


async def user_may_delete_in_kb(db: AsyncSession, kb_id: int, user_id: int) -> bool:
    """Whether `user_id`'s share of `kb_id` carries the owner's delete switch.

    Independent of the level (an admin share without the switch may not delete);
    a legacy per-document grant never carries it. The KB owner and kb.all are the
    caller's business (``check_kb_access``)."""
    return bool((await db.execute(
        text("SELECT can_delete FROM kb_shares WHERE kb_id = :kb_id AND user_id = :u"),
        {"kb_id": kb_id, "u": user_id},
    )).scalar())


async def get_user_kb_delete_switches(
    db: AsyncSession, user_id: int, kb_ids: list[int] | set[int],
) -> set[int]:
    """Batch form of :func:`user_may_delete_in_kb` (one query for the KB list)."""
    if not kb_ids:
        return set()
    rows = await db.execute(
        text("SELECT kb_id FROM kb_shares WHERE user_id = :u AND can_delete "
             "AND kb_id = ANY(:kb_ids)"),
        {"u": user_id, "kb_ids": list(kb_ids)},
    )
    return {int(r.kb_id) for r in rows}


async def list_user_shared_kb_ids(db: AsyncSession, user_id: int) -> set[int]:
    """KB IDs shared with the user through a ``kb_shares`` row."""
    return set(await get_user_kb_permission_levels(db, user_id))


async def get_user_kb_permission_levels(
    db: AsyncSession,
    user_id: int,
    kb_ids: list[int] | set[int] | None = None,
) -> dict[int, str]:
    """One query for many KBs (audit K2): {kb_id: "read"|"write"|"admin"} for every
    KB the user holds a ``kb_shares`` row on. A grant on one of a KB's documents
    does NOT count (#1446): it opens that document, not the KB.

    If `kb_ids` is provided, the result is restricted to that subset (still one
    query); `None` returns the full map for the user.
    """
    params: dict[str, Any] = {"user_id": user_id}
    kb_filter = ""
    if kb_ids is not None:
        if not kb_ids:
            return {}
        kb_filter = " AND ks.kb_id = ANY(:kb_ids)"
        params["kb_ids"] = list(kb_ids)
    result = await db.execute(
        text(
            "SELECT ks.kb_id, ks.permission_level FROM kb_shares ks "
            f"WHERE ks.user_id = :user_id{kb_filter}"
        ),
        params,
    )
    return {
        int(row.kb_id): row.permission_level
        for row in result.fetchall()
        if row.permission_level in _PERM_RANK
    }


# =============================================================================
# Backfill: derive kb_shares rows from legacy per-document grants
# =============================================================================


_SUSPICION_FLAGS = (
    "foreign_granter", "partial", "split_batch", "kb_younger_than_grant", "single_grant",
)


@dataclass
class LegacyPair:
    """A (kb, user) pair with legacy grants and no kb_shares row. Ids, counts and
    level only -- no KB names, no document content."""

    kb_id: int
    user_id: int
    grants: int
    permission: str
    granted_by: int | None
    granted_at: datetime | None
    reasons: list[str]

    def as_log(self) -> dict[str, Any]:
        return {"kb_id": self.kb_id, "user_id": self.user_id, "grants": self.grants,
                "permission": self.permission, "reasons": self.reasons}


@dataclass
class LegacyPairs:
    clean: list[LegacyPair] = field(default_factory=list)
    suspicious: list[LegacyPair] = field(default_factory=list)


async def uncovered_legacy_pairs(db: AsyncSession) -> LegacyPairs:
    """Every (kb, user) pair with legacy grants and no kb_shares row, split into
    the ones the backfill converts (``clean``) and the ones only the owner may
    adopt (``suspicious``). Read-only. The deploy precondition of #1446 is
    ``clean == []``; suspicious pairs are reported, never counted against it."""
    out = LegacyPairs()
    for row in (await db.execute(text(UNCOVERED_PAIRS_SQL))).fetchall():
        reasons = [name for name in _SUSPICION_FLAGS if getattr(row, name)]
        pair = LegacyPair(
            kb_id=int(row.kb_id), user_id=int(row.user_id), grants=int(row.grants),
            permission=_RANK_TO_PERM[int(row.rank)], granted_by=row.granted_by,
            granted_at=row.granted_at, reasons=reasons,
        )
        (out.suspicious if reasons else out.clean).append(pair)
    return out


@dataclass
class KBShareBackfillResult:
    """Counts only (no KB names, no document content) plus the ids a revert needs."""

    pairs_with_grants: int = 0     # distinct (kb, user) pairs holding a legacy grant
    already_shared: int = 0        # of those, already a kb_shares row
    clean_missing: int = 0         # uncovered pairs the backfill converts
    clean: list[dict[str, Any]] = field(default_factory=list)
    # those clean pairs, listed for the owner before --commit (same shape as suspicious)
    suspicious: list[dict[str, Any]] = field(default_factory=list)
    # uncovered pairs only the owner may adopt:
    # {"kb_id", "user_id", "grants", "permission", "reasons"}
    inserted: list[dict[str, Any]] = field(default_factory=list)
    # inserted rows: {"kb_id", "user_id", "granted_at"} — what --revert deletes
    skipped: list[dict[str, Any]] = field(default_factory=list)
    # adopt only: requested pairs that are no uncovered legacy pair

    @property
    def missing(self) -> int:
        """Uncovered pairs in total (clean + suspicious)."""
        return self.pairs_with_grants - self.already_shared


async def _insert_pairs(db: AsyncSession, pairs: list[LegacyPair]) -> list[dict[str, Any]]:
    """One kb_shares row per pair (ON CONFLICT DO NOTHING); returns what a revert needs."""
    inserted: list[dict[str, Any]] = []
    for p in pairs:
        row = (await db.execute(
            text(
                "INSERT INTO kb_shares "
                "  (kb_id, user_id, permission_level, granted_by, granted_at, can_delete) "
                "VALUES (:kb_id, :user_id, :perm, :by, "
                "        COALESCE(CAST(:at AS TIMESTAMP), now() AT TIME ZONE 'UTC'), "
                # A legacy grant never meant "may delete": the switch is the owner's.
                "        FALSE) "
                "ON CONFLICT (kb_id, user_id) DO NOTHING "
                "RETURNING kb_id, user_id, granted_at"
            ),
            {"kb_id": p.kb_id, "user_id": p.user_id, "perm": p.permission,
             "by": p.granted_by, "at": p.granted_at},
        )).first()
        if row is not None:
            inserted.append({"kb_id": int(row.kb_id), "user_id": int(row.user_id),
                             "granted_at": row.granted_at.isoformat()})
    return inserted


async def backfill_kb_shares(db: AsyncSession, *, dry_run: bool) -> KBShareBackfillResult:
    """Insert one ``kb_shares`` row per CLEAN uncovered legacy pair.

    The row carries the MAX level over the user's grants on the KB, and
    granted_by / granted_at of the most recent grant. Suspicious pairs
    (``UNCOVERED_PAIRS_SQL``) are listed, never inserted: converting a grant that
    moved with its document would share the whole destination KB. Idempotent: an
    existing kb_shares row is never touched. Legacy grants are left as they are.
    ``dry_run`` counts and writes nothing.
    """
    total = (await db.execute(text(
        "SELECT COUNT(*) FROM (SELECT DISTINCT d.knowledge_base_id, g.granted_to_user_id "
        f"{_LEGACY_GRANTS_FROM}) p"
    ))).scalar_one()
    pairs = await uncovered_legacy_pairs(db)
    uncovered = len(pairs.clean) + len(pairs.suspicious)
    result = KBShareBackfillResult(
        pairs_with_grants=int(total), already_shared=int(total) - uncovered,
        clean_missing=len(pairs.clean),
        clean=[p.as_log() for p in pairs.clean],
        suspicious=[p.as_log() for p in pairs.suspicious],
    )
    if dry_run:
        return result
    result.inserted = await _insert_pairs(db, pairs.clean)
    await db.commit()
    return result


async def adopt_kb_share_pairs(
    db: AsyncSession, requested: list[tuple[int, int]], *, dry_run: bool,
) -> KBShareBackfillResult:
    """The owner's explicit decision for SUSPICIOUS pairs: turn exactly the named
    (kb_id, user_id) pairs into kb_shares rows, at the MAX level of their grants.
    A requested pair that is no uncovered legacy pair (unknown, or already
    shared) is skipped and reported. Revert with ``revert_kb_share_backfill``."""
    pairs = await uncovered_legacy_pairs(db)
    by_key = {(p.kb_id, p.user_id): p for p in pairs.clean + pairs.suspicious}
    wanted = list(dict.fromkeys(requested))
    chosen = [by_key[k] for k in wanted if k in by_key]
    result = KBShareBackfillResult(
        skipped=[{"kb_id": k[0], "user_id": k[1]} for k in wanted if k not in by_key],
        suspicious=[p.as_log() for p in chosen],
    )
    if dry_run:
        return result
    result.inserted = await _insert_pairs(db, chosen)
    await db.commit()
    return result


async def revert_kb_share_backfill(db: AsyncSession, inserted: list[dict[str, Any]]) -> int:
    """Delete exactly the rows a backfill run inserted (from its log).

    A row is deleted only while its ``granted_at`` still equals the logged value:
    a share re-made or changed through the UI since then is somebody's decision,
    not backfill output, and stays -- as does one the owner has given the delete
    switch since (the backfill writes FALSE). Legacy grants were never touched: after a
    revert they open their documents again, but not the KB (#1446). Also reverts
    an ``adopt_kb_share_pairs`` run. Returns the number of deleted rows.

    Known overlap: a backfill that runs again after the share was revoked
    (or the row deleted otherwise) writes the SAME granted_at (the MAX of the
    legacy grants) -- so reverting the OLD log also deletes that new row. That
    is harmless: the new row is backfill output of the same grants too, and a
    further backfill run recreates it.
    """
    deleted = 0
    for row in inserted:
        res = await db.execute(
            text(
                "DELETE FROM kb_shares WHERE kb_id = :kb_id AND user_id = :user_id "
                "AND granted_at = :granted_at AND NOT can_delete"
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
