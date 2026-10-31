"""knowledge_bases: the name is unique per OWNER, not globally.

Folder and email ingest file into an inbox called "Eingang" (or the credential /
mailbox kb name). With one global KB of that name, its owner -- whoever it is --
reaches every document in it: the document filters grant the KB owner access
through ``kb.owner_id`` (``circle_sql`` owner branch). So ingest now keeps ONE
INBOX PER PERSON, looked up by (name, owner_id), and two people need two KBs
of the same name. The global ``UNIQUE (name)`` forbids exactly that.

Replaced by a unique index on (name, owner_id) NULLS NOT DISTINCT (Postgres 15+;
this stack runs 16): without NULLS NOT DISTINCT two OWNERLESS KBs of one name
would be allowed, because NULL <> NULL -- the shared ownerless inbox must stay
single. Existing data satisfies the new index trivially (names were globally
unique), so no data changes.

No data is moved: an existing shared "Eingang" stays as it is; new documents of
a resolved owner go to that owner's inbox.

Fresh installs: the baseline builds the CURRENT model, which already declares the
new index and no name constraint -- both steps below are guarded and no-ops then.

Revision ID: pc20261031_kb_name_per_owner
Revises: pc20261029_audit_basis
Create Date: 2026-10-31
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261031_kb_name_per_owner"
down_revision: str | None = "pc20261029_audit_basis"
branch_labels: None = None
depends_on: None = None

NEW_INDEX = "uq_knowledge_bases_name_owner"
# The name Postgres gives `name ... UNIQUE` (both the original migration and
# create_all produce it); the upgrade does not rely on it -- it drops whatever
# unique constraint covers exactly (name).
OLD_CONSTRAINT = "knowledge_bases_name_key"


def _name_only_unique_constraints(bind) -> list[str]:
    return list(bind.execute(sa.text(
        "SELECT c.conname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
        "WHERE c.conrelid = to_regclass('knowledge_bases') AND c.contype = 'u' "
        "AND array_length(c.conkey, 1) = 1 AND a.attname = 'name'"
    )).scalars())


def _name_only_unique_indexes(bind) -> list[str]:
    """Stand-alone unique indexes on (name) that back no constraint."""
    return list(bind.execute(sa.text(
        "SELECT ic.relname FROM pg_index i "
        "JOIN pg_class ic ON ic.oid = i.indexrelid "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
        "WHERE i.indrelid = to_regclass('knowledge_bases') AND i.indisunique "
        "AND i.indnatts = 1 AND a.attname = 'name' AND i.indpred IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)"
    )).scalars())


def upgrade() -> None:
    bind = op.get_bind()
    for name in _name_only_unique_constraints(bind):
        op.execute(sa.text(f'ALTER TABLE knowledge_bases DROP CONSTRAINT "{name}"'))
    for name in _name_only_unique_indexes(bind):
        op.execute(sa.text(f'DROP INDEX "{name}"'))
    op.execute(sa.text(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {NEW_INDEX} "
        "ON knowledge_bases (name, owner_id) NULLS NOT DISTINCT"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    # The way back re-imposes a GLOBAL name uniqueness that per-owner inboxes
    # have since broken. Which of two same-named KBs should give way -- merge,
    # rename, delete -- is not derivable from the data, so refuse instead of
    # picking one. Counts only: KB names are user data.
    dupes = bind.execute(sa.text(
        "SELECT COUNT(*) FROM (SELECT name FROM knowledge_bases "
        "GROUP BY name HAVING COUNT(*) > 1) d"
    )).scalar()
    if dupes:
        raise RuntimeError(
            f"pc20261031_kb_name_per_owner downgrade: {dupes} KB name(s) are used "
            f"by more than one knowledge base (per-owner inboxes). A global "
            f"UNIQUE(name) cannot be restored without deciding which KB gives way; "
            f"the downgrade refuses to choose. Resolve the duplicates by hand first."
        )
    op.execute(sa.text(f"DROP INDEX IF EXISTS {NEW_INDEX}"))
    if not _name_only_unique_constraints(bind):
        op.execute(sa.text(
            f"ALTER TABLE knowledge_bases ADD CONSTRAINT {OLD_CONSTRAINT} UNIQUE (name)"
        ))
