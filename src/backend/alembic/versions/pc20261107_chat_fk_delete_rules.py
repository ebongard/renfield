"""Delete rules on the foreign keys into conversations and messages.

``messages.conversation_id`` carried no ON DELETE rule: only the ORM cascade
(``Conversation.messages``) removed the messages, so the atom purge -- a Core
``DELETE FROM atoms`` whose CASCADE reaches ``conversations`` -- failed with a
foreign key violation on every conversation that has messages. An Art. 17
erasure of a chat was refused.

The two provenance pointers onto a message (``conversation_memories`` and
``kg_relations.source_message_id``) had no rule either, so deleting a chat whose
messages produced a memory or a relation failed on every path. The memory and
the relation are atoms of their own and outlive the message; the pointer goes.

``procedural_skills.learned_from_conversation_id`` already has SET NULL from its
migration; only the model lacked it (fresh installs are built from the model).

Each constraint is found by its column, not by its name: the names come from
``create_all`` on one install and from a migration on another.

Revision ID: pc20261107_chat_fk_rules
Revises: pc20261106_conv_tombstones
Create Date: 2026-11-07
"""
import sqlalchemy as sa
from alembic import op

revision: str = "pc20261107_chat_fk_rules"
down_revision: str | None = "pc20261106_conv_tombstones"
branch_labels: None = None
depends_on: None = None

# (table, column, referenced table, rule after upgrade)
_RULES = (
    ("messages", "conversation_id", "conversations", "CASCADE"),
    ("conversation_memories", "source_message_id", "messages", "SET NULL"),
    ("kg_relations", "source_message_id", "messages", "SET NULL"),
)


def _fk_names(conn, table: str, column: str, ref: str) -> list[str]:
    return list(conn.execute(sa.text("""
        SELECT c.conname
          FROM pg_constraint c
          JOIN pg_attribute a
            ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
         WHERE c.contype = 'f'
           AND c.conrelid = to_regclass(quote_ident(current_schema()) || '.' || :table)
           AND c.confrelid = to_regclass(quote_ident(current_schema()) || '.' || :ref)
           AND a.attname = :column
           AND array_length(c.conkey, 1) = 1
    """), {"table": table, "column": column, "ref": ref}).scalars())


def _set_rule(table: str, column: str, ref: str, rule: str | None) -> None:
    conn = op.get_bind()
    if conn.execute(
        sa.text("SELECT to_regclass(quote_ident(current_schema()) || '.' || :t)"), {"t": table}
    ).scalar() is None:
        return
    for name in _fk_names(conn, table, column, ref):
        op.drop_constraint(name, table, type_="foreignkey")
    op.create_foreign_key(
        f"{table}_{column}_fkey", table, ref, [column], ["id"], ondelete=rule,
    )


def upgrade() -> None:
    for table, column, ref, rule in _RULES:
        _set_rule(table, column, ref, rule)


def downgrade() -> None:
    for table, column, ref, _rule in reversed(_RULES):
        _set_rule(table, column, ref, None)
