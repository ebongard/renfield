"""Log an exception without shipping user content along with it.

Found in review on 2026-10-29: the atom-store fan-out had a guard against
content in the log, and four of its eight retrieval sources bypassed it by
logging the raw exception (`logger.warning(f"... {e}")`).

SQLAlchemy appends this to its messages:

    [SQL: SELECT ... WHERE ts @@ websearch_to_tsquery(%(or_query)s)]
    [parameters: {'or_query': '<whatever the person asked>'}]

The bound query IS the user's text. In `lexical_retrieval` it is the search
string; in `document_fact_retrieval` and `note_retrieval` the same.

This cut is a floor, not a guarantee. Two formats carry the value in the
PRIMARY message and therefore survive it: `invalid input syntax for type <t>:
"<value>"` (Postgres) and `invalid input for query argument $N: <repr(value)>`
(asyncpg). Both only arise from a type error in a binding, not on the search
paths here. A watertight guard would log `type(e).__name__` plus the sqlstate
and drop the message entirely -- which costs exactly the diagnosis the line
exists for. The trade-off is written down here so it is not guessed again.
"""
from __future__ import annotations

#: Nobody needs more than this to place an error, and the longer the message
#: the likelier it carries something along.
MAX_LEN = 200


def error_text_without_content(e: BaseException) -> str:
    """The message up to the `[SQL: ...]` tail, truncated."""
    return str(e).split("[SQL:")[0].strip()[:MAX_LEN]
