"""Log an exception without shipping user content along with it.

🛑 The occasion, found in review on 2026-10-29: the atom-store fan-out had a guard against
content in the log, and four of its eight retrieval sources bypassed it by
logging the raw exception (`logger.warning(f"... {e}")`).

SQLAlchemy appends this to its messages:

    [SQL: SELECT ... WHERE ts @@ websearch_to_tsquery(%(or_query)s)]
    [parameters: {'or_query': '<whatever the person asked>'}]

The bound query IS the user's text. In `lexical_retrieval` it is the search
string; in `document_fact_retrieval` and `note_retrieval` the same.

🛑 Postgres has a second place for content, BEFORE that tail: the `DETAIL:`
line of a constraint violation carries the offending row values --

    duplicate key value violates unique constraint "ix_users_username"
    DETAIL:  Key (username)=(<the value>) already exists.

-- likewise `Key (peer_id)=(...) is not present in table ...` of a foreign
key and `Failing row contains (...)`, which lists every column of the row.
`HINT:` and `CONTEXT:` are written by whoever raises (a trigger, a function)
and can carry the same. So the text is cut at the FIRST of `[SQL:`,
`DETAIL:`, `HINT:`, `CONTEXT:`. What stays is the primary message with the
constraint's name -- the diagnostic part. Proven against errors the database
raises itself: `tests/backend/test_log_safety_pg.py`.

🛑 The cut is a floor, not a guarantee. What still survives it, because the
value sits in the PRIMARY message:

* `invalid input syntax for type <t>: "<value>"` (Postgres) and `invalid
  input for query argument $N: <repr(value)>` (asyncpg) -- a type error in a
  binding, not on the search paths here;
* `RAISE EXCEPTION '<text>'` of a trigger or function that formats row data
  into its own message;
* any non-database exception whose message quotes its input.

A watertight guard would log `type(e).__name__` plus the sqlstate and drop
the message entirely -- which costs exactly the diagnosis the line exists
for. The trade-off is written down here so it is not guessed again.
"""
from __future__ import annotations

import logging
import re
import sys
from urllib.parse import unquote_plus

#: Nobody needs more than this to place an error, and the longer the message
#: the likelier it carries something along.
MAX_LEN = 200


#: Where content starts in a database error (module docstring). The labels
#: are matched as Postgres writes them: upper case, colon attached, not as
#: part of a longer word.
_CONTENT_STARTS = re.compile(r"\[SQL:|(?<![A-Za-z])(?:DETAIL|HINT|CONTEXT):")


#: How much of a content value a line shows when LOG_CONTENT is on.
CONTENT_PREVIEW = 100


def logged_content(value: object, limit: int = CONTENT_PREVIEW) -> str:
    """What a log line may say about USER CONTENT -- a chat message, a
    transcription, a title, a query, an entity name (#1565).

    By default its length only (`<87 chars>`): the pod log is read by whoever
    operates the cluster, and a household's messages and names do not belong
    there. With `LOG_CONTENT=true` (a per-instance debugging switch, off by
    default) the text itself, cut to `limit` characters and quoted.
    `tests/backend/test_log_content_structure.py` holds every log call to it.
    """
    if value is None:
        return "<none>"
    text = value if isinstance(value, str) else str(value)
    from utils.config import settings

    if not getattr(settings, "log_content", False):
        return f"<{len(text)} chars>"
    shown = text if len(text) <= limit else text[:limit] + "…"
    return repr(shown)


def error_text_without_content(e: BaseException) -> str:
    """The primary message: up to the first `DETAIL:` / `HINT:` / `CONTEXT:`
    line or the `[SQL: ...]` tail, truncated."""
    return _CONTENT_STARTS.split(str(e), maxsplit=1)[0].strip()[:MAX_LEN]


#: Frames of a stack that are worth a log line; the innermost ones are kept.
MAX_FRAMES = 12


def frames_without_content(e: BaseException) -> str:
    """WHERE an exception was raised -- the stack as `file:line in function`,
    outermost first -- and nothing else.

    🛑 For the handlers that used to log a full traceback
    (`logger.opt(exception=True)`). A traceback ends in `str(e)`, i.e. in the
    `[SQL: ...] [parameters: ...]` tail this module exists to cut. (Until
    2026-11-16 the sink also ran with loguru's default `diagnose=True` and
    printed the VALUES of the variables of every frame; `install_log_sink`
    ended that, the last line of a traceback it cannot change.) The stack
    itself is the useful part and carries no content.

    What is lost against the traceback: the source lines, the variable
    values (the point), and the frames of a chained exception (`__cause__` /
    `__context__`) -- only the outermost exception's own stack is listed.
    """
    import traceback

    frames = traceback.extract_tb(e.__traceback__)[-MAX_FRAMES:]
    return " > ".join(
        f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} in {frame.name}"
        for frame in frames
    ) or "no stack"


# --- The sink ------------------------------------------------------------------


def install_log_sink(level: str) -> None:
    """Replace every loguru sink of this process by ONE to stderr that prints
    no variable values.

    🛑 `diagnose=False` is the point. loguru's default is True: under every
    source line of a traceback it prints the VALUES of the names on that line
    -- the session, the statement, the bound parameters, the row. Measured on
    2026-11-16 with the app's sink as it was: a failing query came out with
    its full `[SQL: ...] [parameters: ...]` text a second time, from the
    frames, whatever the handler had cut from the message.

    `backtrace=True` stays, written out so it is a decision: it extends the
    stack ABOVE the frame that caught the exception -- more `file:line in
    function` plus source lines, the same kind of text as the stack below it
    and no values. Turning it off would cost the callers' frames and remove
    no content.

    🛑 What this does NOT change: a traceback still ends in `str(e)`, i.e. in
    the raw message with its tail. That is what `error_text_without_content`
    and `frames_without_content` are for, at the handler.

    Every process calls this once at start: `main.py` at import, each worker
    in its `__main__` block (a worker never imports `main.py`; without the
    call it logs through loguru's built-in sink, which has the default).
    `tests/backend/test_log_safety_structure.py` holds both ends: no sink
    without `diagnose=False`, no entry point without this call.
    """
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level=level, backtrace=True, diagnose=False)
    _route_stdlib_logging_into_loguru()


class _StdlibToLoguru(logging.Handler):
    """Hands a stdlib `logging` record to loguru, i.e. to the one sink above.

    19 backend modules (and every library) log through stdlib `logging`. With
    no handler on the root logger, their WARNING lines went to stderr through
    `logging.lastResort` -- unformatted, and past the sink whose options this
    module exists for (#1565). The record's message is rendered by stdlib
    (`getMessage`), so a `{` in it is never read as a loguru placeholder.
    """

    def emit(self, record: logging.LogRecord) -> None:
        from loguru import logger

        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        try:
            message = record.getMessage()
        except Exception:
            self.handleError(record)
            return
        # Attribute the line to the stdlib caller, not to this handler: walk up
        # past this frame and every frame of the `logging` package (loguru's
        # documented recipe).
        import inspect

        frame, depth = inspect.currentframe(), 0
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, message)


def _route_stdlib_logging_into_loguru() -> None:
    """One `_StdlibToLoguru` on the root logger. Idempotent.

    🛑 The root LEVEL is not touched: it stays WARNING, so exactly the lines
    that reached stderr before still do, now through the sink -- no library
    starts printing its INFO chatter (httpx logs every request at INFO).
    Loggers with handlers of their own (uvicorn's) and `propagate=False`
    never reach the root and are unchanged.
    """
    root = logging.getLogger()
    if not any(isinstance(h, _StdlibToLoguru) for h in root.handlers):
        root.addHandler(_StdlibToLoguru())


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
    # The OAuth authorization code on /api/auth/sso/callback/{provider} (#1344):
    # PKCE-bound and single-use, but still a bearer of a login until redeemed.
    "code",
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
