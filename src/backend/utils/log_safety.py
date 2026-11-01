"""Log an exception without shipping user content along with it.

🛑 The occasion, found in review on 2026-10-29: the atom-store fan-out had a guard against
content in the log, and four of its eight retrieval sources bypassed it by
logging the raw exception (`logger.warning(f"... {e}")`).

SQLAlchemy appends this to its messages:

    [SQL: SELECT ... WHERE ts @@ websearch_to_tsquery(%(or_query)s)]
    [parameters: {'or_query': '<whatever the person asked>'}]

The bound query IS the user's text. In `lexical_retrieval` it is the search
string; in `document_fact_retrieval` and `note_retrieval` the same.

🛑 The cut at `[SQL:` is a floor, not a guarantee. Two formats carry the value in the
PRIMARY message and therefore survive it: `invalid input syntax for type <t>:
"<value>"` (Postgres) and `invalid input for query argument $N: <repr(value)>`
(asyncpg). Both only arise from a type error in a binding, not on the search
paths here. A watertight guard would log `type(e).__name__` plus the sqlstate
and drop the message entirely -- which costs exactly the diagnosis the line
exists for. The trade-off is written down here so it is not guessed again.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote_plus

#: Nobody needs more than this to place an error, and the longer the message
#: the likelier it carries something along.
MAX_LEN = 200


def error_text_without_content(e: BaseException) -> str:
    """The message up to the `[SQL: ...]` tail, truncated."""
    return str(e).split("[SQL:")[0].strip()[:MAX_LEN]


# --- Credentials in logged URLs ----------------------------------------------
#
# 🛑 Found on 2026-10-31: in Bearer mode (no HttpOnly cookie) the browser opens
# `/ws?token=<JWT>`. uvicorn writes that full URL in its access line
# (`uvicorn.access`) AND in its `"WebSocket /ws?token=... [accepted]"` line
# (`uvicorn.error`), which `--no-access-log` does NOT suppress. A command-line
# flag would also break on the next manifest edit, so the redaction lives here
# and is installed by the app itself (`main.py`) for every launch path.

#: Query parameters whose value is a credential. Matched case-insensitively.
SECRET_QUERY_PARAMS = (
    "token", "access_token", "refresh_token", "id_token", "ws_token",
    "ticket", "api_key", "apikey", "key", "secret", "password", "auth",
)

REDACTED = "[REDACTED]"

_SECRET_NAMES = frozenset(SECRET_QUERY_PARAMS)

# Every `?name=value` / `&name=value` pair. The NAME is matched loosely and
# decoded before the lookup: Starlette's query parser percent-decodes names, so
# `?%74oken=<JWT>` authenticates exactly like `?token=` and must be redacted too.
_QUERY_PAIR_RE = re.compile(r"([?&])([^=&\s\"'#?]+)=([^&\s\"'#]*)")

#: The loggers uvicorn writes request URLs to.
UVICORN_URL_LOGGERS = ("uvicorn.access", "uvicorn.error")


def redact_url_secrets(text: str) -> str:
    """Replace the value of every credential query parameter in `text`."""
    return _QUERY_PAIR_RE.sub(_redact_pair, text)


def _redact_pair(m: re.Match[str]) -> str:
    sep, name, _value = m.groups()
    if unquote_plus(name).strip().lower() in _SECRET_NAMES:
        return f"{sep}{name}={REDACTED}"
    return m.group(0)


def _redact_arg(arg: object) -> object:
    return redact_url_secrets(arg) if isinstance(arg, str) else arg


class UrlSecretRedactingFilter(logging.Filter):
    """Redacts credential query parameters in a record's message and arguments.

    uvicorn passes the path as a %-argument, so both `msg` and `args` are cleaned;
    the record is never dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_url_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        return True


def install_uvicorn_url_redaction() -> None:
    """Attach the filter to uvicorn's URL-bearing loggers. Idempotent."""
    for name in UVICORN_URL_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, UrlSecretRedactingFilter) for f in lg.filters):
            lg.addFilter(UrlSecretRedactingFilter())
