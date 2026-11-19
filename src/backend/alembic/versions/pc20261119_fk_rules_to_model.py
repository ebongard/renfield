"""Three foreign keys get the model's delete rule on every instance (#1588).

A set comparison of every foreign-key delete rule in the model against both
live databases (2026-11-19) left three keys where an instance differs from the
model, and the two instances from each other:

==========================================  ========  =========  ========
key                                         model     household  xidra
==========================================  ========  =========  ========
``documents.atom_id`` -> atoms              CASCADE   SET NULL   CASCADE
``ingest_credentials.created_by_user_id``   none      SET NULL   SET NULL
``procedural_skills.user_id`` -> users      none      SET NULL   none
==========================================  ========  =========  ========

What each key gets, and why:

- ``documents.atom_id``: **CASCADE**, the model's rule and the rule of every
  other atom-bearing table. A document is visible through its atom; with SET
  NULL, deleting the atom left the document behind without an owner or a
  policy. One behaviour change in the household: a document committed while
  its atom still holds the ``__pending__`` placeholder (a crash between the two
  commits in ``RAGService`` document creation) now goes with that atom when the
  reaper removes it, as it already did in xidra; the document was stuck in
  ``pending`` either way.
- ``ingest_credentials.created_by_user_id``: **SET NULL**, both instances'
  rule; the model follows. The column only records who minted the credential;
  the credential belongs to the instance and must outlive that account.
- ``procedural_skills.user_id``: **NO ACTION**, the model's rule. Deleting a
  user who still holds data is refused on purpose and answered with a 409
  (``api/routes/users.py::delete_user``). SET NULL made a personal skill
  ownerless instead, and an ownerless skill is what retrieval treats as a
  system seed -- only its ``source`` column kept it from every other user.

Each key is looked up by its COLUMN and rebuilt under the name it had, and only
where its rule differs (a fresh install builds the keys from the model, which
already carries these rules). Two keys on one column are refused. Re-adding a
key validates the existing rows; they already satisfy it.

The way back changes nothing. Before this revision the rule depended on the
instance, and which one a database had is not recorded anywhere; restoring a
guess would put a key on a rule it may never have had.

Revision ID: pc20261119_fk_rules_to_model
Revises: pc20261119_fk_delete_rules
Create Date: 2026-11-19
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261119_fk_rules_to_model"
down_revision: str | None = "pc20261119_fk_delete_rules"
branch_labels: None = None
depends_on: None = None

#: (table, column, referenced table, referenced column, confdeltype to set)
KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    ("documents", "atom_id", "atoms", "atom_id", "c"),
    ("ingest_credentials", "created_by_user_id", "users", "id", "n"),
    ("procedural_skills", "user_id", "users", "id", "a"),
)

#: pg_constraint.confdeltype -> the clause that re-creates it.
_RULE_SQL = {"a": "", "r": " ON DELETE RESTRICT", "c": " ON DELETE CASCADE", "n": " ON DELETE SET NULL"}


def _key(table: str, column: str, ref: str) -> tuple[str, str]:
    """(constraint name, confdeltype) of THE single-column foreign key from
    ``table.column`` onto ``ref`` in this schema. Qualified with
    ``current_schema()``: the unqualified DDL below follows the search path,
    and the lookup has to see the same table. Refuses none (nothing of ours to
    change, and inventing a key is not this revision's job) and more than one
    (fixing one would leave the other in force, and report success)."""
    rows = op.get_bind().execute(sa.text(
        "SELECT c.conname, c.confdeltype::text FROM pg_constraint c"
        " JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " JOIN pg_class r ON r.oid = c.confrelid"
        " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]"
        " WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 1"
        "   AND n.nspname = current_schema() AND t.relname = :t"
        "   AND a.attname = :c AND r.relname = :r"
    ), {"t": table, "c": column, "r": ref}).all()
    if len(rows) != 1:
        found = ", ".join(r[0] for r in rows) or "none"
        raise RuntimeError(
            f"{revision}: expected one foreign key {table}.{column} -> {ref}, "
            f"found {len(rows)} ({found}); refusing to guess"
        )
    return rows[0][0], rows[0][1]


def upgrade() -> None:
    for table, column, ref, ref_column, rule in KEYS:
        name, current = _key(table, column, ref)
        if current == rule:
            continue
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT "{name}" FOREIGN KEY ({column})'
            f" REFERENCES {ref}({ref_column}){_RULE_SQL[rule]}"
        )


def downgrade() -> None:
    # Deliberately nothing: the prior rule depended on the instance and is not
    # recorded (module docstring).
    pass
