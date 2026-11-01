"""A fresh install gets GENERATED `search_vector` columns, as production has

Revision ID: pc20261031_sv_generated
Revises: pc20261029_audit_basis
Create Date: 2026-10-31

THE DEFECT (measured 2026-10-31 on a brand-new instance)
--------------------------------------------------------
`init_db()` stamps a fresh database to the baseline's parent and runs
`upgrade head`; the baseline (`pc20260926_baseline`) builds the schema with
`create_all` plus a list of raw-SQL indexes. `create_all` renders the six
`search_vector` columns as PLAIN `tsvector` -- the models only carry a
`FetchedValue()` marker -- and the GENERATED shape lives solely in the
migrations BEFORE the baseline (pc20260528, pc20260529, pc20260602,
pc20260617, pc20260720, pc20260829), which a fresh install never runs.
Result: `information_schema.columns.is_generated = 'NEVER'` on all six, every
chunk `search_vector IS NULL`, BM25 returns 0 hits on every fresh install.
The baseline's "same as production" proof compared tables and indexes; the GIN
indexes it brought sit on columns that are never filled, so both sets matched.

This is not a regression of the baseline: before it, `create_all` + a stamp to
HEAD produced the same plain columns. The live instances work only because
those six migrations ran on them AFTER they were set up.

WHAT THIS DOES
--------------
`services.fts_generated_columns.ensure_generated_search_vectors`, by the STATE
of each column (`pg_attribute.attgenerated`), not by revision or name:

* already GENERATED -> nothing. **Every production database is a no-op**:
  catalog reads only, no DDL, no table lock, no rewrite. Safe through the
  normal deploy gate (`--migrate`).
* plain or missing -> DROP + ADD as GENERATED with the exact production
  expression (Postgres fills every existing row), then the GIN index under its
  production name if no valid one exists, then ANALYZE. On a fresh install the
  tables are empty; on a populated plain-column instance this is a table
  rewrite under ACCESS EXCLUSIVE, which is the price of the repair.

Fully transactional (no `CONCURRENTLY`): with `transaction_per_migration=True`
a failure rolls the whole migration back and leaves `alembic_version` on the
previous revision.

A future GENERATED column is added by a migration after this one and runs on a
fresh install like any other. `services/database.py` warns at startup when a
`FetchedValue()` column is not GENERATED, so this class no longer hides.
"""
from typing import Sequence, Union

from alembic import op
from loguru import logger

from services.fts_generated_columns import ensure_generated_search_vectors

revision: str = "pc20261031_sv_generated"
down_revision: Union[str, None] = "pc20261029_audit_basis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    changed = ensure_generated_search_vectors(conn)
    if changed:
        logger.info("search_vector rebuilt as GENERATED on: {}", ", ".join(changed))
    else:
        logger.info("search_vector already GENERATED on every table -- no-op")


def downgrade() -> None:
    """Deliberately nothing -- the reverse is the identity, not a gap.

    The state before `upgrade()` was either GENERATED (every production
    database: upgrade did nothing, so there is nothing to undo) or a plain
    column that is never filled -- the defect itself. The previous revision's
    correct schema IS the GENERATED one, so leaving it standing is the exact
    inverse for production and the only sensible one for a repaired install.
    Recreating the dead column would restore a bug and throw away the data
    Postgres just computed. Running `upgrade()` again afterwards is a no-op,
    because it decides by column state.

    Exercised, not asserted: `test_pc20261031_search_vector_generated.py`
    runs upgrade -> downgrade -> upgrade on real Postgres and checks
    `attgenerated` and the table's `relfilenode` at each step.
    """
