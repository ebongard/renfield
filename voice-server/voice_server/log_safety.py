"""No credential from a URL query string in uvicorn's log lines.

🛑 The voice socket opens `/ws/voice?token=<voice-scoped JWT>`. uvicorn writes
that full URL in its access line (`uvicorn.access`) AND in its
`"WebSocket /ws/voice?token=... [accepted]"` line (`uvicorn.error`), which
`--no-access-log` does NOT suppress. The redaction is installed by the app
itself (`voice_server.main`, at import) so every launch path gets it -- the
image CMD, the in-process anonymous listener, a manual `uvicorn` call.

🛑 This is a COPY of the URL part of the backend's `src/backend/utils/log_safety.py`
(renfield #1441). The voice-server image is built from `voice-server/` alone and
does not contain the backend package, so it cannot import it. The copy is pinned
to the backend's behaviour by `tests/test_log_url_redaction.py`, which loads the
backend module from the repository and runs the same vectors through both.
Change one, change the other.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote_plus

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
