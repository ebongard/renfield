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
    owner_atom_id_expr: str | None = None,
    peer_scoped: bool = False,
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
        owner_atom_id_expr: SQL expression yielding the atom_id whose
                           ``atoms.owner_user_id`` is an ALTERNATE owner source.
                           When set, the owner branch also matches if the asker
                           owns that atom. Needed for documents whose ownership
                           is normally read from ``kb.owner_id`` but whose KB is
                           NULL (global-RAG / null-KB rows) — there ``kb.owner_id``
                           is NULL so the owner can never reach their own content
                           via the KB-owner branch. The atom-owner fallback keeps
                           ownership intact for KB-less documents. (CM-1 fix.)
        peer_scoped:       when True, DROP the owner-equality and explicit-grant
                           branches entirely, leaving only ``public-tier OR
                           tier-membership EXISTS``. Used for FEDERATION queries,
                           where ``:{asker_param}`` originates from
                           ``PeerUser.remote_user_id`` — a REMOTE-controlled
                           integer written into the FK-constrained
                           ``circle_memberships.member_user_id`` column at pairing
                           time (see ``pairing_service``). In a single-user
                           household that FK forces the value to equal the local
                           owner's own ``users.id``, so the raw equality checks
                           ``owner_col = :asker`` and ``granted_to_user_id = :asker``
                           would authorize a peer as if it were the owner — a
                           full-brain leak. The tier-membership EXISTS is safe to
                           keep: it matches ONE specific ``(circle_owner_id,
                           member_user_id, dimension='tier')`` row the local owner
                           deliberately created at pairing with a chosen tier, not
                           an arbitrary owned/granted row. Removing (not merely
                           neutralizing) the two equality branches makes the peer
                           clause provably ``(tier = public) OR (tier-membership)``
                           regardless of ``auth_enabled``.
    """
    owner_alias = owner_table_alias or table_alias
    sid_expr = source_id_expr or f"{table_alias}.id"

    # Owner sees all their own atoms (regardless of tier).
    owner_branch = f"{owner_alias}.{owner_col} = :{asker_param}"
    if owner_atom_id_expr:
        # Fall back to the atom owner when the structural owner column can be
        # NULL (null-KB documents). `da` is local to this subquery — no clash
        # with the `a` alias used by the explicit-grant EXISTS below.
        owner_branch = (
            f"({owner_branch} "
            f"OR EXISTS ("
            f"  SELECT 1 FROM atoms da "
            f"  WHERE da.atom_id = {owner_atom_id_expr} "
            f"  AND da.owner_user_id = :{asker_param}"
            f"))"
        )

    # Public-tier atoms accessible to anyone (paired or not). Always present.
    parts = [
        f"{table_alias}.{tier_col} = :{asker_param}_pub",
    ]
    # Owner-equality branch — SUPPRESSED for peer_scoped (federation) queries:
    # the asker_id is a remote-controlled value that can equal the local owner id
    # (see peer_scoped in the docstring). A federated peer is never the local owner.
    if not peer_scoped:
        parts.insert(0, owner_branch)

    if source_table_value and not peer_scoped:
        # Per-resource explicit grant — MAX-permissive with tier check.
        # SUPPRESSED for peer_scoped: `granted_to_user_id = :asker` is the same
        # raw-equality collision class as the owner branch.
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
    #
    # ``circle_memberships.value`` is JSON (int for dimension='tier', str for
    # dimension='tenant'/'project' — see CircleMembership model). PostgreSQL
    # cannot cast `json` directly to `integer` (raises CannotCoerceError);
    # the canonical idiom is to go through `text`. Filter on dimension='tier'
    # already guarantees the JSON value is a number, so `::text::int` always
    # parses cleanly here. SQLite is permissive about the direct cast which
    # is why the string-shape tests (run on SQLite) didn't catch this — the
    # production bug surfaced as 500 on /knowledge-graph against asyncpg.
    #
    # Alias for circle_memberships is "cm", NOT "m". A previous version used
    # "m", which silently shadowed the OUTER alias when callers pass
    # table_alias="m" (the default for conversation_memories_circles_filter).
    # In that case {owner_alias}.{owner_col} expands to "m.user_id" — and
    # inside this EXISTS scope, "m" resolves to circle_memberships, which
    # has no user_id column. The query fails with
    # "column m.user_id does not exist". Caught in prod 2026-05-12 in
    # the chat_handler memory-retrieval path.
    parts.append(
        f"EXISTS ("
        f"  SELECT 1 FROM circle_memberships cm "
        f"  WHERE cm.circle_owner_id = {owner_alias}.{owner_col} "
        f"  AND cm.member_user_id = :{asker_param} "
        f"  AND cm.dimension = 'tier' "
        f"  AND (cm.value::text)::int <= {table_alias}.{tier_col}"
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


def kg_entities_circles_filter(
    asker_id: int, alias: str = "e", *, peer_scoped: bool = False
) -> tuple[str, dict[str, Any]]:
    """Returns (clause, params) for circle-filtering kg_entities.

    ``peer_scoped`` (federation): drop the owner + explicit-grant branches —
    see ``circles_filter_clause``.
    """
    src = "kg_entities"
    clause = circles_filter_clause(table_alias=alias, source_table_value=src, peer_scoped=peer_scoped)
    # peer_scoped drops the explicit-grant branch (the only consumer of the
    # `_src` bind), so omit it — otherwise a strict `text(clause).bindparams()`
    # caller (kg_retrieval._resolve_entity_names) raises on the unreferenced name.
    return clause, circles_filter_params(
        asker_id, source_table_value=("" if peer_scoped else src)
    )


def kg_relations_circles_filter(
    asker_id: int, alias: str = "r", *, peer_scoped: bool = False
) -> tuple[str, dict[str, Any]]:
    """Returns (clause, params) for circle-filtering kg_relations.

    ``peer_scoped`` (federation): drop the owner + explicit-grant branches.
    """
    src = "kg_relations"
    clause = circles_filter_clause(table_alias=alias, source_table_value=src, peer_scoped=peer_scoped)
    # peer_scoped drops the explicit-grant branch (the only consumer of the
    # `_src` bind), so omit it — otherwise a strict `text(clause).bindparams()`
    # caller (kg_retrieval._resolve_entity_names) raises on the unreferenced name.
    return clause, circles_filter_params(
        asker_id, source_table_value=("" if peer_scoped else src)
    )


def conversation_memories_circles_filter(
    asker_id: int, alias: str = "m", *, peer_scoped: bool = False
) -> tuple[str, dict[str, Any]]:
    """Returns (clause, params) for circle-filtering conversation_memories.

    ``peer_scoped`` (federation): drop the owner + explicit-grant branches.
    """
    src = "conversation_memories"
    clause = circles_filter_clause(table_alias=alias, source_table_value=src, peer_scoped=peer_scoped)
    # peer_scoped drops the explicit-grant branch (the only consumer of the
    # `_src` bind), so omit it — otherwise a strict `text(clause).bindparams()`
    # caller (kg_retrieval._resolve_entity_names) raises on the unreferenced name.
    return clause, circles_filter_params(
        asker_id, source_table_value=("" if peer_scoped else src)
    )


def notes_circles_filter(
    asker_id: int, alias: str = "n", *, peer_scoped: bool = False
) -> tuple[str, dict[str, Any]]:
    """Returns (clause, params) for circle-filtering the ``notes`` table (Phase 4B).

    Directly owned (``owner_user_id``, unlike the default ``user_id``); a note is
    a first-class atom, so the standard 4-branch filter applies. ``peer_scoped``
    (federation): drop the owner + explicit-grant branches.
    """
    src = "notes"
    clause = circles_filter_clause(
        table_alias=alias, owner_col="owner_user_id",
        source_table_value=src, peer_scoped=peer_scoped,
    )
    return clause, circles_filter_params(
        asker_id, source_table_value=("" if peer_scoped else src)
    )


def document_chunks_circles_filter(
    asker_id: int,
    *,
    chunk_alias: str = "dc",
    doc_alias: str = "d",
    kb_alias: str = "kb",
    peer_scoped: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Returns (clause, params) for circle-filtering document_chunks.

    ``peer_scoped`` (federation): drop the owner + explicit-grant branches —
    see ``circles_filter_clause``.

    Post-atoms-per-document (pc20260423): the access-control unit is the
    parent Document. ``atom_explicit_grants`` hang on ``atoms`` rows with
    ``source_table='documents'``, so the explicit-grant EXISTS check must
    match against ``d.id`` (not ``dc.id``). Tier stays on ``dc.circle_tier``
    (denormalized mirror of ``d.circle_tier``) for the hot-path similarity
    filter. Ownership comes from ``kb.owner_id`` (documents inherit from KB
    owner) WITH a fallback to the document's atom owner: null-KB / global-RAG
    documents have no ``knowledge_bases`` row, so ``kb.owner_id`` is NULL and
    the owner could otherwise never reach their own content via ownership
    (CM-1 fix — uses ``d.atom_id``). Callers MUST join knowledge_bases under
    ``kb_alias`` (LEFT JOIN, so null-KB docs survive) AND documents under
    ``doc_alias``.

    Example:
        clause, params = document_chunks_circles_filter(asker_id=42)
        sql = '''
            SELECT ... FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
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
        owner_atom_id_expr=f"{doc_alias}.atom_id",
        peer_scoped=peer_scoped,
    )
    # peer_scoped drops the explicit-grant branch (the only consumer of the
    # `_src` bind), so omit it — otherwise a strict `text(clause).bindparams()`
    # caller (kg_retrieval._resolve_entity_names) raises on the unreferenced name.
    return clause, circles_filter_params(
        asker_id, source_table_value=("" if peer_scoped else src)
    )


def document_facts_circles_filter(
    asker_id: int,
    *,
    fact_alias: str = "df",
    doc_alias: str = "d",
    kb_alias: str = "kb",
    peer_scoped: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Returns (clause, params) for circle-filtering ``document_facts``.

    ``peer_scoped`` (federation): drop the owner + explicit-grant branches —
    see ``circles_filter_clause``.

    A Schicht A fact inherits the access policy of its parent Document — the
    same access unit as ``document_chunks``. ``atom_explicit_grants`` hang on
    the ``atoms`` row with ``source_table='documents'`` and ``source_id=d.id``;
    ownership comes from ``kb.owner_id`` with the null-KB atom-owner fallback
    (``d.atom_id`` → ``atoms.owner_user_id``, CM-1). Tier comes from the
    denormalized ``df.circle_tier`` (mirrored from the parent doc). Callers
    MUST join documents under ``doc_alias`` and LEFT JOIN knowledge_bases
    under ``kb_alias`` (LEFT so null-KB facts survive the join).

    Example:
        clause, params = document_facts_circles_filter(asker_id=42)
        sql = '''
            SELECT ... FROM document_facts df
            JOIN documents d ON df.document_id = d.id
            LEFT JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id
            WHERE ... AND ({clause})
        '''
    """
    src = "documents"
    clause = circles_filter_clause(
        table_alias=fact_alias,
        owner_col="owner_id",
        tier_col="circle_tier",
        source_table_value=src,
        owner_table_alias=kb_alias,
        source_id_expr=f"{doc_alias}.id",
        owner_atom_id_expr=f"{doc_alias}.atom_id",
        peer_scoped=peer_scoped,
    )
    # peer_scoped drops the explicit-grant branch (the only consumer of the
    # `_src` bind), so omit it — otherwise a strict `text(clause).bindparams()`
    # caller (kg_retrieval._resolve_entity_names) raises on the unreferenced name.
    return clause, circles_filter_params(
        asker_id, source_table_value=("" if peer_scoped else src)
    )
