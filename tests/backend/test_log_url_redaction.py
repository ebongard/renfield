"""No credential from a URL query string in uvicorn's log lines.

Bearer mode opens `/ws?token=<JWT>`; uvicorn logs that URL in its access line and
in its `WebSocket ... [accepted]` line (the latter even with `--no-access-log`).
"""
import ast
import io
import logging
import logging.config
from pathlib import Path

import pytest
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter

from utils.log_safety import (
    REDACTED,
    install_uvicorn_url_redaction,
    redact_url_secrets,
)

SECRET = "eyJSECRETSECRET.payload.sig"


@pytest.fixture
def uvicorn_logs():
    """uvicorn's own logging config, plus the app's redaction, into a buffer."""
    logging.config.dictConfig(LOGGING_CONFIG)
    install_uvicorn_url_redaction()
    buf = io.StringIO()
    handlers = []
    fmts = LOGGING_CONFIG["formatters"]
    for name, fmt in (
        ("uvicorn.access", AccessFormatter(fmts["access"]["fmt"], use_colors=False)),
        ("uvicorn.error", DefaultFormatter(fmts["default"]["fmt"], use_colors=False)),
    ):
        h = logging.StreamHandler(buf)
        h.setFormatter(fmt)
        logging.getLogger(name).addHandler(h)
        handlers.append((name, h))
    yield buf
    for name, h in handlers:
        logging.getLogger(name).removeHandler(h)


@pytest.mark.unit
def test_access_line_has_no_token(uvicorn_logs):
    # Exactly uvicorn's call in protocols/http/*_impl.py.
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.0.5:51234", "GET", f"/ws?token={SECRET}&lang=de", "1.1", 101,
    )
    line = uvicorn_logs.getvalue()
    assert SECRET not in line
    assert f'10.0.0.5:51234 - "GET /ws?token={REDACTED}&lang=de HTTP/1.1" 101' in line


@pytest.mark.unit
def test_websocket_accepted_line_has_no_token(uvicorn_logs):
    # Exactly uvicorn's call in protocols/websockets/*_impl.py (uvicorn.error).
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" [accepted]', "10.0.0.5:51234", f"/ws/user?access_token={SECRET}",
    )
    line = uvicorn_logs.getvalue()
    assert SECRET not in line
    assert f'10.0.0.5:51234 - "WebSocket /ws/user?access_token={REDACTED}" [accepted]' in line


@pytest.mark.unit
@pytest.mark.parametrize("url", [
    f"/ws?token={SECRET}", f"/ws?a=1&TOKEN={SECRET}", f"/x?ticket={SECRET}#f",
    f"/x?refresh_token={SECRET}&b=2",
])
def test_redacts_every_credential_param(url):
    assert SECRET not in redact_url_secrets(url)


@pytest.mark.unit
def test_leaves_other_params_alone():
    assert redact_url_secrets("/api/x?tokens_used=5&page=2") == "/api/x?tokens_used=5&page=2"


@pytest.mark.unit
def test_install_is_idempotent():
    install_uvicorn_url_redaction()
    install_uvicorn_url_redaction()
    lg = logging.getLogger("uvicorn.error")
    assert sum(type(f).__name__ == "UrlSecretRedactingFilter" for f in lg.filters) == 1


@pytest.mark.unit
def test_main_installs_redaction_at_module_level():
    """Every launch path imports `main`; the install must be a top-level call there."""
    tree = ast.parse((Path(__file__).parents[2] / "src/backend/main.py").read_text())
    calls = [
        n.value.func.id for n in tree.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert "install_uvicorn_url_redaction" in calls
