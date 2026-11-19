"""Delete rules for three provenance foreign keys: ``ON DELETE SET NULL``.

#1585. ``NotificationService.cleanup_expired`` deletes expired notifications in
ONE statement every hour. Two columns point at ``notifications.id`` with no
delete rule:

- ``reminders.notification_id`` -- the notification a reminder produced when it
  fired;
- ``notification_suppressions.source_notification_id`` -- the notification a
  suppression rule was learned from.

A single expired notification still referenced by either made the whole DELETE
fail, so from then on NO expired notification was removed (xidra, 2026-11-19:
every hourly run failed, 45 expired rows stuck). Both columns are nullable and
only record where a row came from; nothing reads them back. The reminder and
the learned suppression must outlive the notification -- ``CASCADE`` would
delete a user's suppression rule along with an expired notification.

The third key, ``procedural_skills.learned_from_conversation_id``, has carried
``ondelete="SET NULL"`` on the model all along and has it in the household
database, but not in xidra's: there, deleting a conversation that a skill was
learned from is refused. This revision brings it to the model's rule wherever
it differs.

Each key is looked up by its COLUMN, not by a constraint name, and rebuilt
under the name it had (a fresh install builds the keys from the model, which
already carries the rules -- then nothing is changed). Re-adding a key
validates the existing rows; they already satisfy it, the key itself is
unchanged.

The way back restores NO ACTION on the two notification keys, which is what
they had before. It leaves ``procedural_skills`` alone: the model says SET NULL
on both sides of this revision, and whether a given database had the rule
before is not something the current state records (the household had it).

Revision ID: pc20261119_fk_delete_rules
Revises: pc20261114_fed_log_user_null
Create Date: 2026-11-19
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261119_fk_delete_rules"
down_revision: str | None = "pc20261114_fed_log_user_null"
branch_labels: None = None
depends_on: None = None

#: (table, column, referenced table, revert on downgrade)
KEYS: tuple[tuple[str, str, str, bool], ...] = (
    ("reminders", "notification_id", "notifications", True),
    ("notification_suppressions", "source_notification_id", "notifications", True),
    ("procedural_skills", "learned_from_conversation_id", "conversations", False),
)

#: pg_constraint.confdeltype -> the clause that re-creates it.
_RULE_SQL = {"a": "", "r": " ON DELETE RESTRICT", "c": " ON DELETE CASCADE", "n": " ON DELETE SET NULL"}


def _key(table: str, column: str, ref: str) -> tuple[str, str] | None:
    """(constraint name, confdeltype) of the single-column foreign key from
    ``table.column`` onto ``ref`` in THIS schema, or ``None`` without one.
    Qualified with ``current_schema()``: the unqualified DDL below follows the
    search path, and the lookup has to see the same table."""
    row = op.get_bind().execute(sa.text(
        "SELECT c.conname, c.confdeltype::text FROM pg_constraint c"
        " JOIN pg_class t ON t.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = t.relnamespace"
        " JOIN pg_class r ON r.oid = c.confrelid"
        " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]"
        " WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 1"
        "   AND n.nspname = current_schema() AND t.relname = :t"
        "   AND a.attname = :c AND r.relname = :r"
    ), {"t": table, "c": column, "r": ref}).first()
    return None if row is None else (row[0], row[1])


def _set_rule(table: str, column: str, ref: str, rule: str) -> None:
    found = _key(table, column, ref)
    if found is None:
        # Every database at `down_revision` has these keys. Without one in this
        # schema there is nothing of ours to change -- and inventing a key the
        # model's table might not even hold is not this revision's job.
        raise RuntimeError(
            f"{revision}: no foreign key {table}.{column} -> {ref} in the current schema; "
            "refusing to guess one"
        )
    name, current = found
    if current == rule:
        return
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
    op.execute(
        f'ALTER TABLE {table} ADD CONSTRAINT "{name}" FOREIGN KEY ({column})'
        f" REFERENCES {ref}(id){_RULE_SQL[rule]}"
    )


def upgrade() -> None:
    for table, column, ref, _revert in KEYS:
        _set_rule(table, column, ref, "n")


def downgrade() -> None:
    for table, column, ref, revert in KEYS:
        if revert and _key(table, column, ref) is not None:
            _set_rule(table, column, ref, "a")
