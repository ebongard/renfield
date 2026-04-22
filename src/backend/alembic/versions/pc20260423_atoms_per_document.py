"""Atoms per document — collapse per-chunk atoms to per-document atoms

Revision ID: pc20260423_atoms_per_document
Revises: pc20260422_federation_audit
Create Date: 2026-04-23

Design rationale: docs/design/atoms-granularity.md

The circles v1 migration (pc20260420_circles_v1_schema) placed atoms at the
chunk level — one atom per document_chunks row, with ``atom_id NOT NULL + FK``.
That granularity allows per-chunk tier overrides, but splits the semantic
information-carrier (a document) across different access levels — a reader
in tier N sees SOME chunks of a document, gets a distorted picture.

This migration moves the access-control unit up to the document:

  BEFORE                              AFTER
  documents          (no atom)        documents          atom_id + circle_tier
  document_chunks    atom_id NOT NULL document_chunks    circle_tier only
  atoms (kb_chunk)   1-per-chunk      atoms (kb_document) 1-per-document

Chunks keep ``circle_tier`` as a denormalized mirror of the parent document's
tier so the hot retrieval path (pgvector + tier-filter) doesn't need the join.
AtomService.update_tier on a kb_document atom cascades into document_chunks
in the same transaction.

Verified against prod-DB (2026-04-22):
  - 0 atom_explicit_grants for kb_chunk atoms (feature not in use)
  - 0 documents with heterogeneous chunk tiers (collapse is lossless)
  - 123 kb_chunk atoms / 123 document_chunks rows (1:1, no drift)

Pre-migration gate below re-verifies the heterogeneity invariant at
migration time — a defensive assertion in case the dataset evolved between
design and deployment.
"""
import sqlalchemy as sa
from alembic import op


# revision identifiers
revision = "pc20260423_atoms_per_document"
down_revision = "pc20260422_federation_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ---------------------------------------------------------------------
    # Pre-migration gate — fail loud if any document has chunks across
    # different circle_tiers. MIN-based collapse below would otherwise
    # silently promote some chunks (tier-up leak) when merging to a single
    # document atom.
    # ---------------------------------------------------------------------
    if dialect == "postgresql":
        result = bind.exec_driver_sql(
            "SELECT document_id, COUNT(DISTINCT circle_tier) AS tiers "
            "FROM document_chunks "
            "GROUP BY document_id "
            "HAVING COUNT(DISTINCT circle_tier) > 1"
        ).fetchall()
        if result:
            details = ", ".join(f"doc {r[0]}: {r[1]} tiers" for r in result[:5])
            raise RuntimeError(
                f"Migration blocked: {len(result)} documents have chunks across "
                f"heterogeneous circle_tiers ({details}). "
                "Either consolidate tiers in the app first (set each document's "
                "chunks to the most-restrictive tier across the set) or extend "
                "this migration with an aggregation policy. See "
                "docs/design/atoms-granularity.md § Pre-Migration-Gate."
            )

    # ---------------------------------------------------------------------
    # 1. Add Document.atom_id + Document.circle_tier columns
    # ---------------------------------------------------------------------
    op.add_column(
        "documents",
        sa.Column("atom_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column(
            "circle_tier",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_documents_atom_id",
        "documents",
        ["atom_id"],
    )
    # ondelete=SET NULL (not CASCADE): atoms are metadata descriptors for
    # documents, not their parents. Deleting an atom (admin cleanup, /api/atoms
    # DELETE) must NOT nuke the Document + every chunk. The reverse direction
    # — deleting a Document — is handled by the app's delete_document which
    # explicitly cleans up the atom beforehand.
    op.create_foreign_key(
        "fk_documents_atom",
        "documents",
        "atoms",
        ["atom_id"],
        ["atom_id"],
        ondelete="SET NULL",
    )

    # ---------------------------------------------------------------------
    # 2. Back-fill atoms (one per document, MIN-based tier collapse)
    # ---------------------------------------------------------------------
    if dialect == "postgresql":
        bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")

        # Determine fallback owner (first user id) for documents whose parent
        # KB has NULL owner_id. Matches pc20260420_circles_v1 pattern.
        fallback_user_id = bind.exec_driver_sql(
            "SELECT id FROM users ORDER BY id ASC LIMIT 1"
        ).scalar()

        if fallback_user_id is None:
            # No users yet (fresh DB pre-bootstrap). Skip back-fill; the
            # bootstrap path goes through RAGService which will register
            # atoms itself. Schema changes above are still applied.
            import logging
            logging.getLogger("alembic").warning(
                "pc20260423_atoms_per_document: no users in DB — skipping "
                "back-fill. documents.atom_id stays NULL on pre-existing rows."
            )
        else:
            fb = int(fallback_user_id)

            # One atoms row per existing document. Tier = MIN(chunks.tier).
            # Documents with no chunks fall back to kb.default_circle_tier.
            #
            # ON CONFLICT ON CONSTRAINT uq_atoms_source DO NOTHING makes the
            # back-fill idempotent: re-running after a partial-rollback that
            # left some kb_document atoms behind won't create duplicates. The
            # unique constraint exists on atoms(atom_type, source_table,
            # source_id) from pc20260420_circles_v1_schema.
            bind.exec_driver_sql(
                "WITH new_atoms AS ("
                "  INSERT INTO atoms (atom_id, atom_type, source_table, source_id, "
                "                     owner_user_id, policy, created_at, updated_at) "
                "  SELECT "
                "    gen_random_uuid()::text, "
                "    'kb_document', "
                "    'documents', "
                "    d.id::text, "
                f"    COALESCE(kb.owner_id, {fb}), "
                "    json_build_object('tier', COALESCE( "
                "      (SELECT MIN(c.circle_tier) FROM document_chunks c "
                "       WHERE c.document_id = d.id), "
                "      kb.default_circle_tier, "
                "      0 "
                "    )), "
                "    d.created_at, "
                "    NOW() "
                "  FROM documents d "
                "  JOIN knowledge_bases kb ON kb.id = d.knowledge_base_id "
                "  ON CONFLICT ON CONSTRAINT uq_atoms_source DO NOTHING "
                "  RETURNING atom_id, source_id"
                ") "
                "UPDATE documents d "
                "SET atom_id = new_atoms.atom_id "
                "FROM new_atoms "
                "WHERE d.id::text = new_atoms.source_id"
            )

            # Defensive second UPDATE: catches any document whose atom already
            # existed (ON CONFLICT skipped) — rare but possible on a re-run
            # after a partial ROLLBACK that left atoms behind. Pairs docs →
            # existing atoms via the unique constraint's lookup key.
            bind.exec_driver_sql(
                "UPDATE documents d "
                "SET atom_id = a.atom_id "
                "FROM atoms a "
                "WHERE a.atom_type = 'kb_document' "
                "  AND a.source_table = 'documents' "
                "  AND a.source_id = d.id::text "
                "  AND d.atom_id IS NULL"
            )

            # Denormalize the tier onto documents.circle_tier
            bind.exec_driver_sql(
                "UPDATE documents d "
                "SET circle_tier = CAST(a.policy->>'tier' AS INTEGER) "
                "FROM atoms a "
                "WHERE d.atom_id = a.atom_id"
            )

            # Leave documents.atom_id nullable to match the ORM and the
            # empty-users bootstrap path (create_all on fresh SQLite + first-
            # run Postgres before any user exists). Application invariant:
            # RAGService.create_document_record always populates atom_id when
            # at least one user is in the DB — see services/rag_service.py.

    else:  # sqlite (tests)
        # On SQLite tests we don't run back-fill; Base.metadata.create_all
        # builds the schema at the post-migration state, and individual tests
        # populate their own atoms as needed. Leave atom_id nullable on
        # SQLite to avoid tests that never inserted an atoms row failing.
        pass

    # ---------------------------------------------------------------------
    # 3. Drop document_chunks.atom_id (FK, index, column)
    # ---------------------------------------------------------------------
    if dialect == "postgresql":
        # FK name from pc20260420_circles_v1_schema — hard-coded because
        # autogen would need a reflection call in the migration body.
        op.drop_constraint(
            "fk_document_chunks_atom",
            "document_chunks",
            type_="foreignkey",
        )
        # Old kb_chunk atoms will cascade-delete on FK drop anyway, but we
        # delete them explicitly for audit clarity.
        bind.exec_driver_sql(
            "DELETE FROM atoms WHERE atom_type = 'kb_chunk'"
        )
        # IF EXISTS needed because the b-tree index was only created on dev
        # DBs built via Base.metadata.create_all (the ORM had index=True).
        # Prod was built via pc20260420_circles_v1_schema which only added
        # the FK — Postgres does not auto-create an index for simple FKs,
        # so there's nothing to drop. Using raw DDL because `batch_alter_table`
        # defers execution until __exit__, where try/except around the
        # op-building call cannot catch the deferred DDL error.
        bind.exec_driver_sql("DROP INDEX IF EXISTS ix_document_chunks_atom_id")
        bind.exec_driver_sql(
            "ALTER TABLE document_chunks DROP COLUMN IF EXISTS atom_id"
        )
    else:
        # SQLite (tests) — ALTER TABLE DROP COLUMN requires table rebuild via
        # batch mode; try/except works here because batch materializes the
        # CREATE TABLE … INSERT … DROP TABLE synchronously.
        with op.batch_alter_table("document_chunks") as batch:
            try:
                batch.drop_index("ix_document_chunks_atom_id")
            except Exception:
                pass
            try:
                batch.drop_column("atom_id")
            except Exception:
                pass


def downgrade() -> None:
    """Rollback: re-add per-chunk atoms from per-document atoms.

    NOTE: this is a LOSSY downgrade. Any app-level tier changes applied via
    AtomService.update_tier(kb_document) are propagated to all chunks in
    the current state; we reconstruct per-chunk atoms using the document's
    current tier. Any historical per-chunk tier diversity that existed
    before the upgrade back-fill is not recoverable — this is the same
    trade-off the upgrade's MIN-based collapse accepted.
    """
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Re-add document_chunks.atom_id (nullable first)
    op.add_column(
        "document_chunks",
        sa.Column("atom_id", sa.String(36), nullable=True),
    )
    op.create_index(
        "ix_document_chunks_atom_id",
        "document_chunks",
        ["atom_id"],
    )

    if dialect == "postgresql":
        # 2. For each document with an atom, create per-chunk atoms copying
        #    the document's owner + tier. New UUIDs per chunk.
        bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        bind.exec_driver_sql(
            "WITH new_atoms AS ("
            "  INSERT INTO atoms (atom_id, atom_type, source_table, source_id, "
            "                     owner_user_id, policy, created_at, updated_at) "
            "  SELECT "
            "    gen_random_uuid()::text, "
            "    'kb_chunk', "
            "    'document_chunks', "
            "    c.id::text, "
            "    d_atoms.owner_user_id, "
            "    d_atoms.policy, "
            "    c.created_at, "
            "    NOW() "
            "  FROM document_chunks c "
            "  JOIN documents d ON d.id = c.document_id "
            "  JOIN atoms d_atoms ON d_atoms.atom_id = d.atom_id "
            "  WHERE d.atom_id IS NOT NULL "
            "  RETURNING atom_id, source_id"
            ") "
            "UPDATE document_chunks c "
            "SET atom_id = new_atoms.atom_id "
            "FROM new_atoms "
            "WHERE c.id::text = new_atoms.source_id"
        )

        # 3. Delete kb_document atoms
        bind.exec_driver_sql(
            "DELETE FROM atoms WHERE atom_type = 'kb_document'"
        )

        # 4. Re-create FK on document_chunks
        op.create_foreign_key(
            "fk_document_chunks_atom",
            "document_chunks",
            "atoms",
            ["atom_id"],
            ["atom_id"],
            ondelete="CASCADE",
        )

    # 5. Drop Document columns
    op.drop_constraint(
        "fk_documents_atom",
        "documents",
        type_="foreignkey",
    )
    op.drop_index("ix_documents_atom_id", table_name="documents")
    op.drop_column("documents", "atom_id")
    op.drop_column("documents", "circle_tier")
