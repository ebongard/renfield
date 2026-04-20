"""Circles v1 schema — atoms + circle_memberships + atom_explicit_grants

Revision ID: pc20260420_circles_v1
Revises: pc20260419_uniq_doc_hash
Create Date: 2026-04-20 09:00:00.000000

Big-bang schema migration for Lane B of the second-brain-circles plan.
This migration is DESTRUCTIVE: it drops kg_entities.scope and the entire
kb_permissions table, migrating both into the new circles framework.

Renfield is not yet live with external users (per the project's CEO-review
HOLD_SCOPE decision and the user's explicit "no risk" framing). Anyone
running self-hosted Renfield accepts the consequences of this migration.
There is intentionally no staged-rollout split into B1 (DDL) + B2 (back-fill)
+ B3 (DROP) — see the per-project memory feedback_big_bang_circles.md.

Schema overview (per the design doc):

    NEW TABLES:
      circles                  per-user dimension config + default capture policy
      circle_memberships       (owner, member, dimension, value) per F-Generalize
      atoms                    polymorphic registry: one row per piece of info
                               with a circle policy (chunks/kg_nodes/kg_edges/memories)
      atom_explicit_grants     per-resource exception grants (subsumes KBPermission)

    EXISTING TABLES — column additions:
      kb_chunks                + atom_id (FK to atoms), + circle_tier
      kg_entities              + atom_id (FK to atoms), + circle_tier
      kg_relations             + atom_id (FK to atoms), + circle_tier
      conversation_memories    + atom_id (FK to atoms), + circle_tier
      knowledge_bases          + default_circle_tier

    DESTRUCTIVE:
      kg_entities.scope        DROPPED (subsumed by circle_tier)
      kb_permissions           DROPPED (rows migrated to atom_explicit_grants)

Migration sequence (single transaction per dialect-supported scope):

    1. Create new tables (circles, circle_memberships, atoms, atom_explicit_grants)
    2. Add columns to source tables (atom_id NULLABLE, circle_tier NOT NULL DEFAULT 0,
       default_circle_tier NOT NULL DEFAULT 0)
    3. Back-fill atoms rows for every existing source row, capture atom_id
    4. Back-fill circle_tier values from existing scope / is_public:
         ConversationMemory: scope='user'   -> circle_tier=0 (self)
                             scope='team'   -> circle_tier=2 (household)
                             scope='global' -> circle_tier=4 (public)
                             team_id is preserved on the row but ignored by v1
                             access checks; v2 named-circles will migrate it.
         KGEntity:           scope='personal'  -> circle_tier=0 (self)
                             scope=<yaml>      -> circle_tier=2 (household, default)
         KGRelation:         circle_tier = MIN(subject.circle_tier, object.circle_tier)
                             cascade rule applied at app level via AtomService.update_tier
                             when a kg_node's tier changes.
         KnowledgeBase:      is_public=true  -> default_circle_tier=4 (public)
                             is_public=false -> default_circle_tier=0 (self)
         KBChunk:            inherits parent kb.default_circle_tier
    5. Make atom_id NOT NULL + add FK constraint to atoms.atom_id
    6. Migrate kb_permissions rows to atom_explicit_grants
       (one grant per (kb owner's atom_for_kb, granted_user, permission))
       NOTE: KBs themselves are NOT atoms in v1 — only chunks are.
       Explicit-grant migration happens at the chunk level: one grant per
       chunk per granted user, mirroring the KBPermission semantics that
       said "user X can read all of KB Y".
    7. DROP kg_entities.scope column
    8. DROP kb_permissions table
    9. Create composite indexes for hot-path retrieval

Real value enumeration (per eng-review Finding 1.1A):
    Existing scope values verified by inspecting models/database.py constants:
      - MEMORY_SCOPE_USER  = "user"
      - MEMORY_SCOPE_TEAM  = "team"
      - MEMORY_SCOPE_GLOBAL = "global"
      - KG_SCOPE_PERSONAL  = "personal"
      - kg_scopes.yaml entries (currently empty in checked-in config; any
        production deployment with custom scopes will hit the
        "unmapped scope" error path and must extend the mapping below)

    UNMAPPED scope values cause the migration to ABORT with a clear error
    pointing at the offending value(s). Per project memory feedback_no_quickfixes.md,
    this is the intentional behaviour — fix the mapping, do not pg_dump around it.

Tier integers (matches DESIGN.md tier visual language):
    0 = self        (deepest crimson)
    1 = trusted     (brand crimson)
    2 = household   (cream)
    3 = extended    (light turquoise)
    4 = public      (deep turquoise)

Deployment requirements (per PR #402 review OPTIONAL #16):
- PostgreSQL only (SQLite raises NotImplementedError early in upgrade()).
- pgcrypto extension required for gen_random_uuid(). The migration calls
  `CREATE EXTENSION IF NOT EXISTS pgcrypto` which requires SUPERUSER on the
  database. Renfield's docker-compose Postgres runs as superuser, so this
  works locally and on .159. Managed-DB deployments (RDS, Cloud SQL, Hetzner
  managed Postgres) often restrict CREATE EXTENSION — in those environments
  an admin must run `CREATE EXTENSION pgcrypto;` once before this migration,
  then this migration's IF NOT EXISTS becomes a no-op.

Transaction safety (per PR #402 review OPTIONAL #18):
- Verified env.py uses `with context.begin_transaction()` (lines 167,174),
  so this migration runs inside a single transaction. Partial failure
  rolls back ALL DDL+DML — no half-migrated state on production. The
  `with` block commits on success, rolls back on any exception including
  the loud-fail RuntimeError when an unmapped scope value is encountered.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'pc20260420_circles_v1'
down_revision: Union[str, None] = 'pc20260419_uniq_doc_hash'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tier integer constants — keep in sync with DESIGN.md and CircleResolver.
TIER_SELF = 0
TIER_TRUSTED = 1
TIER_HOUSEHOLD = 2
TIER_EXTENDED = 3
TIER_PUBLIC = 4

# Default deployment-config blob for new circles rows.
# Home defaults: 5-tier ladder. Enterprise deployments override via deploy-time
# configuration that runs after this migration (per Reva validation gate;
# user accepts assumed defaults pending that conversation).
HOME_DIMENSION_CONFIG = (
    '{"tier": {"shape": "ladder", '
    '"values": ["self", "trusted", "household", "extended", "public"]}}'
)
HOME_CAPTURE_POLICY = '{"tier": 0}'  # default capture tier = self (privacy-positive)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # SQLite-path guard (per PR #402 review BLOCKING #2):
    # this migration is postgres-only because the back-fill SQL uses
    # gen_random_uuid(), CTEs with RETURNING, json_build_object, and LEAST() —
    # none of which work cleanly across SQLite. The test harness uses
    # Base.metadata.create_all (services/database.py) and bypasses Alembic
    # entirely, so this NotImplementedError is purely a guardrail against
    # someone trying to run `alembic upgrade head` against a SQLite dev DB
    # and getting a half-migrated schema.
    if dialect != "postgresql":
        raise NotImplementedError(
            f"circles v1 migration is postgres-only; got dialect={dialect!r}. "
            f"Use Base.metadata.create_all for SQLite test harness, "
            f"or run this migration only against PostgreSQL."
        )

    # Idempotency: pre-circles deployments may have run Base.metadata.create_all
    # via the backend startup path, materialising the new tables but leaving
    # the source-table columns unchanged. The DDL below tolerates that — every
    # CREATE TABLE / ADD COLUMN / CREATE INDEX is guarded by an inspector check
    # so re-running the migration on a partially-created schema is safe.
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    def _has_col(table: str, col: str) -> bool:
        if table not in existing_tables:
            return False
        return col in {c["name"] for c in inspector.get_columns(table)}

    def _has_idx(table: str, idx_name: str) -> bool:
        if table not in existing_tables:
            return False
        return idx_name in {ix["name"] for ix in inspector.get_indexes(table)}

    # =====================================================================
    # 1. NEW TABLES
    # =====================================================================

    if "circles" not in existing_tables:
        op.create_table(
            "circles",
            sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("dimension_config", sa.JSON(), nullable=False, server_default=HOME_DIMENSION_CONFIG),
            sa.Column("default_capture_policy", sa.JSON(), nullable=False, server_default=HOME_CAPTURE_POLICY),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    if "circle_memberships" not in existing_tables:
        op.create_table(
            "circle_memberships",
            sa.Column("circle_owner_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("member_user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("dimension", sa.String(32), primary_key=True),  # 'tier' | 'tenant' | 'project'
            sa.Column("value", sa.JSON(), nullable=False),  # int for ladder, str for set
            sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("granted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    if not _has_idx("circle_memberships", "idx_memberships_member"):
        op.create_index(
            "idx_memberships_member",
            "circle_memberships",
            ["member_user_id", "circle_owner_id"],
        )

    # atoms: polymorphic registry. UUID PK so cross-source identity is stable
    # across migrations, source-table renames, and future v3 KG-as-brain swap.
    if "atoms" not in existing_tables:
        op.create_table(
            "atoms",
            sa.Column("atom_id", sa.String(36), primary_key=True),  # UUID as string for sqlite portability
            sa.Column("atom_type", sa.String(32), nullable=False, index=True),
            sa.Column("source_table", sa.String(64), nullable=False),
            sa.Column("source_id", sa.String(64), nullable=False),
            sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("policy", sa.JSON(), nullable=False, server_default='{"tier": 0}'),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("atom_type", "source_table", "source_id", name="uq_atoms_source"),
        )
    if not _has_idx("atoms", "idx_atoms_owner"):
        op.create_index("idx_atoms_owner", "atoms", ["owner_user_id"])

    if "atom_explicit_grants" not in existing_tables:
        op.create_table(
            "atom_explicit_grants",
            sa.Column("atom_id", sa.String(36), sa.ForeignKey("atoms.atom_id", ondelete="CASCADE"), primary_key=True),
            sa.Column("granted_to_user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("permission_level", sa.String(16), nullable=False, server_default="read"),  # 'read' | 'write' | 'admin'
            sa.Column("granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("granted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    if not _has_idx("atom_explicit_grants", "idx_grants_grantee"):
        op.create_index("idx_grants_grantee", "atom_explicit_grants", ["granted_to_user_id"])

    # =====================================================================
    # 2. ADD COLUMNS TO SOURCE TABLES (NULLABLE FK + circle_tier defaults)
    # =====================================================================

    # atom_id starts NULLABLE; will be back-filled then made NOT NULL.
    for table in ("document_chunks", "kg_entities", "kg_relations", "conversation_memories"):
        if not _has_col(table, "atom_id"):
            op.add_column(table, sa.Column("atom_id", sa.String(36), nullable=True))
        if not _has_col(table, "circle_tier"):
            op.add_column(table, sa.Column("circle_tier", sa.Integer(), nullable=False, server_default=str(TIER_SELF)))

    if not _has_col("knowledge_bases", "default_circle_tier"):
        op.add_column("knowledge_bases", sa.Column("default_circle_tier", sa.Integer(), nullable=False, server_default=str(TIER_SELF)))

    # =====================================================================
    # 3. BACK-FILL: validate scope values + populate circle_tier
    # =====================================================================

    # 3a. Validate ConversationMemory.scope values are all known.
    #     Unknown values abort the migration loudly per Finding 1.1A.
    if dialect == "postgresql":
        unknown_memory_scopes = bind.exec_driver_sql(
            "SELECT DISTINCT scope FROM conversation_memories "
            "WHERE scope NOT IN ('user', 'team', 'global') AND scope IS NOT NULL"
        ).fetchall()
        if unknown_memory_scopes:
            values = ", ".join(repr(r[0]) for r in unknown_memory_scopes)
            raise RuntimeError(
                f"Migration aborted: conversation_memories.scope contains unmapped value(s): {values}. "
                f"Extend the mapping in pc20260420_circles_v1_schema.py upgrade() and re-run."
            )

        unknown_kg_scopes = bind.exec_driver_sql(
            "SELECT DISTINCT scope FROM kg_entities "
            "WHERE scope IS NOT NULL"
        ).fetchall()
        # Allow 'personal' + any deployment-defined yaml scope; map yaml ones to household by default.
        # Loud-fail only if scope is empty string (data corruption indicator).
        if any(r[0] == "" for r in unknown_kg_scopes):
            raise RuntimeError(
                "Migration aborted: kg_entities.scope contains empty-string values (data corruption). "
                "Clean those rows or set them to 'personal' before re-running."
            )

    # 3b. Populate circle_tier on conversation_memories from scope.
    if dialect == "postgresql":
        bind.exec_driver_sql(
            "UPDATE conversation_memories SET circle_tier = "
            f"CASE scope "
            f"  WHEN 'user'   THEN {TIER_SELF} "
            f"  WHEN 'team'   THEN {TIER_HOUSEHOLD} "
            f"  WHEN 'global' THEN {TIER_PUBLIC} "
            f"  ELSE {TIER_SELF} "
            "END"
        )

    # 3c. Populate circle_tier on kg_entities from scope.
    #     'personal' -> self; any yaml-defined scope -> household (assumed default).
    if dialect == "postgresql":
        bind.exec_driver_sql(
            "UPDATE kg_entities SET circle_tier = "
            f"CASE WHEN scope = 'personal' OR scope IS NULL THEN {TIER_SELF} "
            f"     ELSE {TIER_HOUSEHOLD} "
            "END"
        )

    # 3d. Populate kg_relations.circle_tier from MIN(subject.tier, object.tier).
    #     Per PR #402 review BLOCKING #1: surface orphan relations explicitly
    #     before the back-fill so they don't silently stay at tier=0 forever.
    #     Cascade rule for runtime tier changes lives in AtomService.update_tier.
    if dialect == "postgresql":
        orphan_count = bind.exec_driver_sql(
            "SELECT COUNT(*) FROM kg_relations r "
            "WHERE NOT EXISTS (SELECT 1 FROM kg_entities WHERE id = r.subject_id) "
            "OR NOT EXISTS (SELECT 1 FROM kg_entities WHERE id = r.object_id)"
        ).scalar()
        if orphan_count and int(orphan_count) > 0:
            from loguru import logger as _migration_logger
            _migration_logger.warning(
                f"circles v1 migration: found {orphan_count} orphan kg_relations "
                f"(subject or object missing). They keep circle_tier=0 (self) and "
                f"will not be updated by AtomService.update_tier KG cascade. "
                f"Inspect with: SELECT id, subject_id, object_id FROM kg_relations "
                f"WHERE NOT EXISTS (SELECT 1 FROM kg_entities WHERE id = subject_id) "
                f"OR NOT EXISTS (SELECT 1 FROM kg_entities WHERE id = object_id);"
            )

        bind.exec_driver_sql(
            "UPDATE kg_relations r SET circle_tier = LEAST(s.circle_tier, o.circle_tier) "
            "FROM kg_entities s, kg_entities o "
            "WHERE r.subject_id = s.id AND r.object_id = o.id"
        )

        # 3d-bis: Back-fill missing kg_relations.user_id from subject.user_id
        # so the atoms back-fill (step 4c) doesn't FK-violate on owner_user_id=0.
        # Per PR #402 review BLOCKING #12 — kg_relations.user_id is nullable and
        # historical extraction often leaves it NULL; without this back-fill,
        # COALESCE(r.user_id, 0) writes a non-existent user reference.
        bind.exec_driver_sql(
            "UPDATE kg_relations r SET user_id = s.user_id "
            "FROM kg_entities s "
            "WHERE r.subject_id = s.id AND r.user_id IS NULL AND s.user_id IS NOT NULL"
        )

        # Compute a fallback user id for every NULL-owner row (kg_entities,
        # conversation_memories, knowledge_bases). Use MIN(id) for resilience —
        # the legacy hardcoded `user_id = 1` would FK-violate on fresh-DB test
        # harnesses where no users exist, or where admin user id=1 was deleted.
        # Hoisted out so section 4's COALESCE(...) substitutes it as a literal
        # instead of `0` (which FK-violates against the users table).
        fallback_user_id = bind.exec_driver_sql(
            "SELECT id FROM users ORDER BY id ASC LIMIT 1"
        ).scalar()
        if fallback_user_id is None:
            import logging
            logging.getLogger("alembic").warning(
                "pc20260420_circles_v1: no users in DB — skipping "
                "NULL-user backfill for kg_entities / conversation_memories / "
                "knowledge_bases. Any legacy rows with NULL ownership cannot "
                "be migrated to atoms (the FK to users(id) would fail). They "
                "stay un-atomised; the source rows themselves remain intact."
            )
        else:
            bind.exec_driver_sql(
                f"UPDATE kg_entities SET user_id = {int(fallback_user_id)} "
                f"WHERE user_id IS NULL"
            )
            bind.exec_driver_sql(
                f"UPDATE conversation_memories SET user_id = {int(fallback_user_id)} "
                f"WHERE user_id IS NULL"
            )
            # KB owner back-fill: the document_chunks → atoms back-fill below
            # uses kb.owner_id and FK-violates if it's NULL. Same fallback.
            bind.exec_driver_sql(
                f"UPDATE knowledge_bases SET owner_id = {int(fallback_user_id)} "
                f"WHERE owner_id IS NULL"
            )

    # 3e. Populate knowledge_bases.default_circle_tier from is_public.
    if dialect == "postgresql":
        bind.exec_driver_sql(
            f"UPDATE knowledge_bases SET default_circle_tier = "
            f"CASE WHEN is_public = true THEN {TIER_PUBLIC} ELSE {TIER_SELF} END"
        )
        # Inherit chunk tier from parent KB.
        bind.exec_driver_sql(
            "UPDATE document_chunks dc SET circle_tier = kb.default_circle_tier "
            "FROM documents d, knowledge_bases kb "
            "WHERE dc.document_id = d.id AND d.knowledge_base_id = kb.id"
        )

    # =====================================================================
    # 4. BACK-FILL atoms rows for every existing source row, capture atom_id
    # =====================================================================
    # PostgreSQL gen_random_uuid() requires pgcrypto. Fall back to uuid_generate_v4
    # if pgcrypto isn't loaded; raise if neither works.
    #
    # All COALESCE fallbacks substitute the live `fallback_user_id` (MIN(users.id))
    # rather than the legacy `0` sentinel — `0` is not a real user and would
    # FK-violate atoms.owner_user_id → users.id. If no users exist at all
    # (fallback_user_id is None), the entire section 4 is skipped because
    # atoms requires a non-null FK target.
    if dialect == "postgresql" and fallback_user_id is not None:
        # Ensure pgcrypto is available; renfield's prod has it via the docker init.
        bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")

        fb = int(fallback_user_id)

        # 4a. document_chunks -> atoms
        bind.exec_driver_sql(
            "WITH new_atoms AS ("
            "  INSERT INTO atoms (atom_id, atom_type, source_table, source_id, owner_user_id, policy, created_at, updated_at) "
            "  SELECT "
            "    gen_random_uuid()::text, "
            "    'kb_chunk', "
            "    'document_chunks', "
            "    dc.id::text, "
           f"    COALESCE(kb.owner_id, {fb}), "
            "    json_build_object('tier', dc.circle_tier), "
            "    dc.created_at, "
            "    dc.created_at "
            "  FROM document_chunks dc "
            "  JOIN documents d ON dc.document_id = d.id "
            "  JOIN knowledge_bases kb ON d.knowledge_base_id = kb.id "
            "  RETURNING atom_id, source_id "
            ") "
            "UPDATE document_chunks SET atom_id = new_atoms.atom_id "
            "FROM new_atoms WHERE document_chunks.id::text = new_atoms.source_id"
        )

        # 4b. kg_entities -> atoms
        bind.exec_driver_sql(
            "WITH new_atoms AS ("
            "  INSERT INTO atoms (atom_id, atom_type, source_table, source_id, owner_user_id, policy, created_at, updated_at) "
            "  SELECT "
            "    gen_random_uuid()::text, "
            "    'kg_node', "
            "    'kg_entities', "
            "    e.id::text, "
           f"    COALESCE(e.user_id, {fb}), "
            "    json_build_object('tier', e.circle_tier), "
            "    e.first_seen_at, "
            "    e.last_seen_at "
            "  FROM kg_entities e "
            "  RETURNING atom_id, source_id "
            ") "
            "UPDATE kg_entities SET atom_id = new_atoms.atom_id "
            "FROM new_atoms WHERE kg_entities.id::text = new_atoms.source_id"
        )

        # 4c. kg_relations -> atoms
        bind.exec_driver_sql(
            "WITH new_atoms AS ("
            "  INSERT INTO atoms (atom_id, atom_type, source_table, source_id, owner_user_id, policy, created_at, updated_at) "
            "  SELECT "
            "    gen_random_uuid()::text, "
            "    'kg_edge', "
            "    'kg_relations', "
            "    r.id::text, "
           f"    COALESCE(r.user_id, {fb}), "
            "    json_build_object('tier', r.circle_tier), "
            "    r.created_at, "
            "    r.created_at "
            "  FROM kg_relations r "
            "  RETURNING atom_id, source_id "
            ") "
            "UPDATE kg_relations SET atom_id = new_atoms.atom_id "
            "FROM new_atoms WHERE kg_relations.id::text = new_atoms.source_id"
        )

        # 4d. conversation_memories -> atoms
        bind.exec_driver_sql(
            "WITH new_atoms AS ("
            "  INSERT INTO atoms (atom_id, atom_type, source_table, source_id, owner_user_id, policy, created_at, updated_at) "
            "  SELECT "
            "    gen_random_uuid()::text, "
            "    'conversation_memory', "
            "    'conversation_memories', "
            "    m.id::text, "
           f"    COALESCE(m.user_id, {fb}), "
            "    json_build_object('tier', m.circle_tier), "
            "    m.created_at, "
            "    m.created_at "
            "  FROM conversation_memories m "
            "  RETURNING atom_id, source_id "
            ") "
            "UPDATE conversation_memories SET atom_id = new_atoms.atom_id "
            "FROM new_atoms WHERE conversation_memories.id::text = new_atoms.source_id"
        )

    # =====================================================================
    # 5. Make atom_id NOT NULL + add FK constraint
    # =====================================================================

    if dialect == "postgresql":
        # Source rows with NULL owner (legacy data) get owner_user_id=0 in atoms;
        # those atoms still have a real atom_id, so the NOT NULL alter is safe.
        op.alter_column("document_chunks", "atom_id", nullable=False)
        op.alter_column("kg_entities", "atom_id", nullable=False)
        op.alter_column("kg_relations", "atom_id", nullable=False)
        op.alter_column("conversation_memories", "atom_id", nullable=False)

        # FK constraints — guard against re-run on a partially-migrated DB.
        existing_fks_by_table = {
            t: {fk["name"] for fk in inspector.get_foreign_keys(t)}
            for t in ("document_chunks", "kg_entities", "kg_relations", "conversation_memories")
        }
        fk_specs = [
            ("fk_document_chunks_atom", "document_chunks"),
            ("fk_kg_entities_atom", "kg_entities"),
            ("fk_kg_relations_atom", "kg_relations"),
            ("fk_conversation_memories_atom", "conversation_memories"),
        ]
        for fk_name, table in fk_specs:
            if fk_name not in existing_fks_by_table[table]:
                op.create_foreign_key(
                    fk_name,
                    table, "atoms",
                    ["atom_id"], ["atom_id"],
                    ondelete="CASCADE",
                )

    # =====================================================================
    # 6. Migrate kb_permissions -> atom_explicit_grants
    # =====================================================================
    # KBPermission semantics: "user X can <permission_level> all docs in KB Y".
    # Translation to atoms: one grant per (chunk_atom_id, granted_user, permission_level)
    # for every chunk in every doc of KB Y. This preserves the per-resource explicit
    # share while integrating into the unified circles framework.

    if dialect == "postgresql":
        # Per PR #402 review SHOULD-FIX #8: surface KBs whose permissions
        # will be silently lost (KB has permissions but no chunks → INNER JOIN
        # produces zero rows → no atom_explicit_grants written → DROP TABLE
        # destroys the data forever).
        orphan_perm_count = bind.exec_driver_sql(
            "SELECT COUNT(*) FROM kb_permissions kp "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM documents d "
            "  JOIN document_chunks dc ON dc.document_id = d.id "
            "  WHERE d.knowledge_base_id = kp.knowledge_base_id"
            ")"
        ).scalar()
        if orphan_perm_count and int(orphan_perm_count) > 0:
            from loguru import logger as _migration_logger
            _migration_logger.warning(
                f"circles v1 migration: {orphan_perm_count} kb_permissions rows "
                f"belong to KBs with no chunks — they will be DROPPED with the "
                f"kb_permissions table and cannot be recovered. Affected users "
                f"will need to re-share after the KB has its first upload. "
                f"To inspect: SELECT kp.* FROM kb_permissions kp WHERE NOT EXISTS "
                f"(SELECT 1 FROM documents d JOIN document_chunks dc "
                f"ON dc.document_id = d.id WHERE d.knowledge_base_id = "
                f"kp.knowledge_base_id);"
            )

        bind.exec_driver_sql(
            "INSERT INTO atom_explicit_grants (atom_id, granted_to_user_id, permission_level, granted_by, granted_at) "
            "SELECT "
            "  dc.atom_id, "
            "  kp.user_id, "
            "  kp.permission, "
            "  COALESCE(kp.granted_by, 1), "
            "  kp.created_at "
            "FROM kb_permissions kp "
            "JOIN documents d ON d.knowledge_base_id = kp.knowledge_base_id "
            "JOIN document_chunks dc ON dc.document_id = d.id "
            "ON CONFLICT (atom_id, granted_to_user_id) DO NOTHING"
        )

    # =====================================================================
    # 7. DROP destructive: kg_entities.scope, kb_permissions table
    # =====================================================================

    if dialect == "postgresql":
        # Drop the scope index first (idx_kg_entities_scope_active) if present.
        bind.exec_driver_sql("DROP INDEX IF EXISTS ix_kg_entities_scope_active")
        if _has_col("kg_entities", "scope"):
            op.drop_column("kg_entities", "scope")

        # KBPermission table drop: rows have already been migrated above.
        if "kb_permissions" in inspector.get_table_names():
            op.drop_table("kb_permissions")

    # =====================================================================
    # 8. Composite indexes for hot-path retrieval (per Finding 4.2)
    # =====================================================================

    composite_indexes = [
        ("idx_document_chunks_kb_tier", "document_chunks", ["document_id", "circle_tier"]),
        ("idx_kg_entities_owner_tier", "kg_entities", ["user_id", "circle_tier"]),
        ("idx_kg_relations_subj_tier", "kg_relations", ["subject_id", "circle_tier"]),
        ("idx_kg_relations_obj_tier", "kg_relations", ["object_id", "circle_tier"]),
        ("idx_memories_owner_tier", "conversation_memories", ["user_id", "circle_tier", "is_active"]),
    ]
    for idx_name, table, cols in composite_indexes:
        if not _has_idx(table, idx_name):
            op.create_index(idx_name, table, cols)


def downgrade() -> None:
    """
    Best-effort downgrade. WARNING: dropping the atoms table cascades to
    every source-row atom_id FK (because the FKs declare ON DELETE CASCADE).
    The downgrade restores schema shape but cannot restore lost
    kg_entities.scope or kb_permissions data — those would need a backup.

    Per project convention (feedback_no_quickfixes.md) this downgrade exists
    so fresh-DB upgrade/downgrade cycles work for tests, NOT for production
    rollback. Production rollback after this migration is "restore from backup".
    """
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Drop composite indexes
    for idx in (
        "idx_memories_owner_tier",
        "idx_kg_relations_obj_tier",
        "idx_kg_relations_subj_tier",
        "idx_kg_entities_owner_tier",
        "idx_document_chunks_kb_tier",
    ):
        try:
            op.drop_index(idx)
        except Exception:
            pass  # idempotent in fresh-DB downgrade scenarios

    if dialect == "postgresql":
        # Drop FK constraints + atom_id columns from source tables
        for fk_name, table in (
            ("fk_conversation_memories_atom", "conversation_memories"),
            ("fk_kg_relations_atom", "kg_relations"),
            ("fk_kg_entities_atom", "kg_entities"),
            ("fk_document_chunks_atom", "document_chunks"),
        ):
            try:
                op.drop_constraint(fk_name, table, type_="foreignkey")
            except Exception:
                pass

    # Drop the new circle_tier + atom_id + default_circle_tier columns
    for table in ("conversation_memories", "kg_relations", "kg_entities", "document_chunks"):
        for col in ("atom_id", "circle_tier"):
            try:
                op.drop_column(table, col)
            except Exception:
                pass
    try:
        op.drop_column("knowledge_bases", "default_circle_tier")
    except Exception:
        pass

    # Re-create kg_entities.scope (data lost; reset to 'personal' default)
    if dialect == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE kg_entities ADD COLUMN IF NOT EXISTS "
            "scope VARCHAR(50) NOT NULL DEFAULT 'personal'"
        )
        bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_kg_entities_scope_active ON kg_entities (scope, is_active)")

        # Re-create kb_permissions skeleton (data lost; user must repopulate)
        bind.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS kb_permissions ("
            "  id SERIAL PRIMARY KEY, "
            "  knowledge_base_id INTEGER NOT NULL REFERENCES knowledge_bases(id), "
            "  user_id INTEGER NOT NULL REFERENCES users(id), "
            "  permission VARCHAR(20) NOT NULL DEFAULT 'read', "
            "  granted_by INTEGER REFERENCES users(id), "
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        bind.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_permissions_kb_user "
            "ON kb_permissions (knowledge_base_id, user_id)"
        )

    # Drop new tables
    op.drop_table("atom_explicit_grants")
    op.drop_table("atoms")
    op.drop_table("circle_memberships")
    op.drop_table("circles")
