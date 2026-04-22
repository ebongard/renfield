"""
Circle-aware SQL filter helpers — Lane C consumer rewrite support.

Provides reusable WHERE-clause snippets that retrieval modules
(rag_retrieval, kg_retrieval, memory_retrieval) inject into their
search SQL to filter by circle access.

The access predicate for any source row (where `circle_tier_col` is the
denormalized tier column on the source table and `owner_col` is the user_id
column on the source table) is:

    asker owns row                                       -- own everything
    OR row.circle_tier == public_tier_index              -- public to anyone
    OR EXISTS (atom_explicit_grants for this asker)      -- per-resource grant
    OR EXISTS (circle_memberships where asker.tier <= row.circle_tier)
                                                          -- tier reach

Implementation note (per PR #402 review BLOCKING #5 + the dimension-agnostic
generalization in PR #402): the SQL filter only handles the 'tier' dimension.
For multi-dimension policies (tenant + project), the SQL pre-filter widens
the candidate set; PolicyEvaluator.satisfies in CircleResolver does the
final per-row check Python-side. This keeps the SQL query plan simple at
v1 scale (households + Reva enterprise tier-only deployments) and pushes
the multi-dim complexity to per-result evaluation.

The PUBLIC_TIER_INDEX default of 4 matches the standard home ladder
(self/trusted/household/extended/public). Enterprise deployments with
different ladder shapes can pass an explicit value via the helpers.
"""
from __future__ import annotations

from typing import Any

from models.database import TIER_PUBLIC


def circles_filter_clause(
    *,
    table_alias: str,
    owner_col: str = "user_id",
    tier_col: str = "circle_tier",
    asker_param: str = "asker_id",
    public_tier_index: int = TIER_PUBLIC,
    source_table_value: str = "",
    owner_table_alias: str | None = None,
    source_id_expr: str | None = None,
) -> str:
    """
    Build a parameterized WHERE-clause snippet that enforces circle access.

    Returns a SQL fragment (without leading AND/WHERE) that callers append
    to their existing WHERE clause. The fragment uses two named parameters:
      :{asker_param}        the authenticated user's id (e.g., :asker_id)
      :{asker_param}_pub    the public tier index (auto-derived; bind via params)

    Example usage in a retrieval module:
        clause = circles_filter_clause(
            table_alias="e",
            source_table_value="kg_entities",
        )
        sql = f"SELECT ... FROM kg_entities e WHERE e.is_active AND ({clause})"
        params = {"asker_id": user_id, "asker_id_pub": TIER_PUBLIC, ...}

    Args:
        table_alias:       SQL alias of the source table in the outer query
                           (e.g., "e" for "kg_entities e", "dc" for chunks).
        owner_col:         column name that holds the atom owner's user id.
                           Default "user_id". Resolved against `owner_table_alias`
                           if provided, otherwise `table_alias`.
        tier_col:          denormalized circle_tier column on `table_alias`.
                           Default "circle_tier".
        asker_param:       SQL parameter name carrying the authenticated user's
                           id. Default "asker_id".
        public_tier_index: highest index in the ladder (atoms at this tier are
                           visible to anyone, paired or not). Default TIER_PUBLIC=4.
        source_table_value: literal source-table name used in the
                           atom_explicit_grants join (e.g., 'kg_entities').
                           Required when grants need to be checked; pass ""
                           to skip the explicit-grant subquery.
        owner_table_alias: SQL alias whose row carries the owner column when
                           that lives on a JOINed table (e.g., "kb" when
                           filtering document_chunks but ownership is on
                           knowledge_bases). Default: same as `table_alias`.
        source_id_expr:    SQL expression for the source-table row id used in
                           the explicit-grants join. Default: "{table_alias}.id".
                           Override when filtering through a JOIN where the
                           atom row's source_id matches a non-default column.
    """
    owner_alias = owner_table_alias or table_alias
    sid_expr = source_id_expr or f"{table_alias}.id"

    parts = [
        # Owner sees all their own atoms (regardless of tier).
        f"{owner_alias}.{owner_col} = :{asker_param}",
        # Public-tier atoms accessible to anyone.
        f"{table_alias}.{tier_col} = :{asker_param}_pub",
    ]

    if source_table_value:
        # Per-resource explicit grant — MAX-permissive with tier check.
        # `source_table_value` flows through a bind param (`{asker_param}_src`)
        # so even if a future caller forwards user-supplied input, there's no
        # SQL injection sink. `owner_col`, `tier_col`, `sid_expr` remain
        # structural (identifier interpolation) — NEVER pass user input there.
        parts.append(
            f"EXISTS ("
            f"  SELECT 1 FROM atom_explicit_grants g "
            f"  JOIN atoms a ON a.atom_id = g.atom_id "
            f"  WHERE a.source_table = :{asker_param}_src "
            f"  AND a.source_id = ({sid_expr})::text "
            f"  AND g.granted_to_user_id = :{asker_param}"
            f")"
        )

    # Tier-membership check: asker is in owner's circles AND their tier
    # value is at-or-below the atom's tier (deeper-placed members can
    # reach atoms at their depth or wider).
    parts.append(
        f"EXISTS ("
        f"  SELECT 1 FROM circle_memberships m "
        f"  WHERE m.circle_owner_id = {owner_alias}.{owner_col} "
        f"  AND m.member_user_id = :{asker_param} "
        f"  AND m.dimension = 'tier' "
        f"  AND (m.value)::int <= {table_alias}.{tier_col}"
        f")"
    )

    return "(" + " OR ".join(parts) + ")"


def circles_filter_params(
    asker_id: int,
    *,
    asker_param: str = "asker_id",
    public_tier_index: int = TIER_PUBLIC,
    source_table_value: str = "",
) -> dict[str, Any]:
    """
    Build the parameter dict to bind alongside the clause from circles_filter_clause.

    Caller merges this with their other query parameters:
        params = {**other_params, **circles_filter_params(asker_id=user_id)}

    When `source_table_value` is set, also emits `{asker_param}_src` — the
    bind that the explicit-grants EXISTS subquery reads. Safe to pass
    user-supplied strings here (bind param, not interpolated).
    """
    params = {
        asker_param: asker_id,
        f"{asker_param}_pub": public_tier_index,
    }
    if source_table_value:
        params[f"{asker_param}_src"] = source_table_value
    return params


# =============================================================================
# Convenience wrappers per source table (most callers want these)
# =============================================================================


def kg_entities_circles_filter(asker_id: int, alias: str = "e") -> tuple[str, dict[str, Any]]:
    """Returns (clause, params) for circle-filtering kg_entities."""
    src = "kg_entities"
    clause = circles_filter_clause(table_alias=alias, source_table_value=src)
    return clause, circles_filter_params(asker_id, source_table_value=src)


def kg_relations_circles_filter(asker_id: int, alias: str = "r") -> tuple[str, dict[str, Any]]:
    """Returns (clause, params) for circle-filtering kg_relations."""
    src = "kg_relations"
    clause = circles_filter_clause(table_alias=alias, source_table_value=src)
    return clause, circles_filter_params(asker_id, source_table_value=src)


def conversation_memories_circles_filter(asker_id: int, alias: str = "m") -> tuple[str, dict[str, Any]]:
    """Returns (clause, params) for circle-filtering conversation_memories."""
    src = "conversation_memories"
    clause = circles_filter_clause(table_alias=alias, source_table_value=src)
    return clause, circles_filter_params(asker_id, source_table_value=src)


def document_chunks_circles_filter(
    asker_id: int,
    *,
    chunk_alias: str = "dc",
    doc_alias: str = "d",
    kb_alias: str = "kb",
) -> tuple[str, dict[str, Any]]:
    """
    Returns (clause, params) for circle-filtering document_chunks.

    Post-atoms-per-document (pc20260423): the access-control unit is the
    parent Document. ``atom_explicit_grants`` hang on ``atoms`` rows with
    ``source_table='documents'``, so the explicit-grant EXISTS check must
    match against ``d.id`` (not ``dc.id``). Tier stays on ``dc.circle_tier``
    (denormalized mirror of ``d.circle_tier``) for the hot-path similarity
    filter. Ownership still comes from ``kb.owner_id`` (documents inherit
    from KB owner). Callers MUST join knowledge_bases under ``kb_alias``
    AND documents under ``doc_alias``.

    Example:
        clause, params = document_chunks_circles_filter(asker_id=42)
        sql = '''
            SELECT ... FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
            WHERE ... AND ({clause})
        '''
    """
    src = "documents"
    clause = circles_filter_clause(
        table_alias=chunk_alias,
        owner_col="owner_id",
        tier_col="circle_tier",
        source_table_value=src,
        owner_table_alias=kb_alias,
        source_id_expr=f"{doc_alias}.id",
    )
    return clause, circles_filter_params(asker_id, source_table_value=src)
