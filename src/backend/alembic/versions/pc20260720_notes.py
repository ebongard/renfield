"""notes — Phase 4B.1 (Notes as a 5th atom_type, FTS-only slice)

Revision ID: pc20260720_notes
Revises: pc20260719_project_links
Create Date: 2026-07-20

Adds ``notes`` — hand-authored atomic notes as a first-class atom
(``atom_type='note'``, see models.database.Note + AtomService). Directly owned +
circle-tiered; each row is wrapped by an :class:`Atom` at write time
(NoteService → AtomService.create_with_source). 4B.1 retrieval is FTS-only (a
GENERATED multilingual ``search_vector`` over title+body, like document_facts); a
dense embedding column is a documented follow-up.

Idempotent: ``Base.metadata.create_all`` (backend boot) creates this table for a
fresh install, so every DDL op is inspector/IF-EXISTS-guarded (same pattern as
pc20260713_projects + pc20260602 facts-FTS). The GENERATED ``search_vector`` +
GIN + unique-title index are Postgres-only (the sqlite test harness has no
tsvector; NoteRetrieval's sqlite branch uses a LIKE fallback). Fully
transactional except the CONCURRENTLY index builds (autocommit_block, allowed
under transaction_per_migration).
"""
import sqlalchemy as sa
from alembic import op

from services.fts_languages import build_generated_tsvector_expression

revision = "pc20260720_notes"
down_revision = "pc20260719_project_links"
branch_labels = None
depends_on = None

_GIN_INDEX = "idx_notes_search_vector_gin"
_UNIQUE_TITLE = "uq_notes_owner_lower_title"
# title is NOT NULL, body defaults ''; both IMMUTABLE-safe for GENERATED ALWAYS.
_CONTENT_EXPR = "title || ' ' || coalesce(body, '')"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name if bind is not None else "postgresql"
    tables = set(inspector.get_table_names())

    if "notes" not in tables:
        # search_vector is added below as a GENERATED column (Postgres) — not here.
        op.create_table(
            "notes",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column(
                "owner_user_id", sa.Integer,
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column(
                "project_id", sa.Integer,
                sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("body", sa.Text, nullable=False, server_default=""),
            sa.Column(
                "atom_id", sa.String(36),
                sa.ForeignKey("atoms.atom_id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("circle_tier", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        )

    inspector = sa.inspect(bind)  # re-read after a possible create
    idx = {ix["name"] for ix in inspector.get_indexes("notes")}
    for name, cols in (
        ("ix_notes_atom_id", ["atom_id"]),
        ("ix_notes_owner_user_id", ["owner_user_id"]),
        ("ix_notes_project_id", ["project_id"]),
    ):
        if name not in idx:
            op.create_index(name, "notes", cols)

    if dialect != "postgresql":
        return  # sqlite: no tsvector / functional unique index; skip FTS + uniq

    # GENERATED multilingual FTS over title+body (DROP-IF-EXISTS makes re-run safe;
    # a fresh-install create_all made a PLAIN search_vector column — replace it).
    tsvector_expr = build_generated_tsvector_expression(_CONTENT_EXPR)
    op.execute("ALTER TABLE notes DROP COLUMN IF EXISTS search_vector")
    op.execute(
        f"ALTER TABLE notes ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({tsvector_expr}) STORED"
    )

    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_GIN_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {_GIN_INDEX} ON notes USING gin (search_vector)"
        )
        # A [[link]] title must resolve to ONE note per owner (4B.2 depends on it).
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_UNIQUE_TITLE}")
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY {_UNIQUE_TITLE} "
            f"ON notes (owner_user_id, lower(title))"
        )
    op.execute("ANALYZE notes")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "postgresql"
    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_UNIQUE_TITLE}")
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_GIN_INDEX}")
    op.drop_index("ix_notes_project_id", table_name="notes")
    op.drop_index("ix_notes_owner_user_id", table_name="notes")
    op.drop_index("ix_notes_atom_id", table_name="notes")
    op.drop_table("notes")
