"""No credential from a URL query string in the voice-server's uvicorn log lines.

The voice socket opens `/ws/voice?token=<JWT>`; uvicorn logs that URL in its
access line and in its `WebSocket ... [accepted]` line (the latter even with
`--no-access-log`).

`voice_server/log_safety.py` is a copy of the backend's redaction (the image has
no backend package). The parity tests at the bottom run the SAME vectors through
the backend module, loaded from the repository, so the two cannot drift apart.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import logging
import logging.config
import socket
from pathlib import Path

import pytest
import uvicorn
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter
from voice_server import log_safety
from voice_server.log_safety import (
    REDACTED,
    UrlSecretRedactingFilter,
    install_uvicorn_url_redaction,
    redact_url_secrets,
)

SECRET = "eyJSECRETSECRET.payload.sig"
VS_ROOT = Path(__file__).resolve().parents[1]
BACKEND_LOG_SAFETY = VS_ROOT.parent / "src/backend/utils/log_safety.py"

# Shared with the backend's tests/backend/test_log_url_redaction.py.
CREDENTIAL_URLS = [
    f"/ws?token={SECRET}", f"/ws?a=1&TOKEN={SECRET}", f"/x?ticket={SECRET}#f",
    f"/x?refresh_token={SECRET}&b=2",
    f"/ws/voice?token={SECRET}", f"/ws/voice?lang=de&token={SECRET}",
]
BYPASS_URLS = [
    # Starlette percent-decodes query NAMES, so each of these authenticates as
    # `token` and must not slip past a literal name match.
    f"/ws?%74oken={SECRET}", f"/ws?to%6Ben={SECRET}", f"/ws?%54%4F%4B%45%4E={SECRET}",
    f"/ws?a=1&%74oken={SECRET}",
    # Duplicate parameter: every occurrence, not just the first.
    f"/ws?token=decoy&token={SECRET}", f"/ws?token={SECRET}&token=decoy",
]
HARMLESS_URLS = ["/api/x?tokens_used=5&page=2", "/api/x?monkey=1&keyboard=de", "/x?%74okens=3"]


def _strip_redaction() -> None:
    for name in log_safety.UVICORN_URL_LOGGERS:
        lg = logging.getLogger(name)
        for f in [f for f in lg.filters if isinstance(f, UrlSecretRedactingFilter)]:
            lg.removeFilter(f)


@pytest.fixture(autouse=True)
def _restore_redaction():
    """Tests that strip the filter (negative controls) must not leak that state."""
    yield
    install_uvicorn_url_redaction()


def _capture(buf: io.StringIO) -> list[tuple[str, logging.Handler]]:
    fmts = LOGGING_CONFIG["formatters"]
    handlers = []
    for name, fmt in (
        ("uvicorn.access", AccessFormatter(fmts["access"]["fmt"], use_colors=False)),
        ("uvicorn.error", DefaultFormatter(fmts["default"]["fmt"], use_colors=False)),
    ):
        h = logging.StreamHandler(buf)
        h.setFormatter(fmt)
        logging.getLogger(name).addHandler(h)
        handlers.append((name, h))
    return handlers


def _release(handlers: list[tuple[str, logging.Handler]]) -> None:
    for name, h in handlers:
        logging.getLogger(name).removeHandler(h)


@pytest.fixture
def uvicorn_logs():
    """uvicorn's own logging config, plus the app's redaction, into a buffer."""
    logging.config.dictConfig(LOGGING_CONFIG)
    install_uvicorn_url_redaction()
    buf = io.StringIO()
    handlers = _capture(buf)
    yield buf
    _release(handlers)


def _log_ws_accepted(path: str) -> None:
    # Exactly uvicorn's call in protocols/websockets/*_impl.py (uvicorn.error).
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" [accepted]', "10.0.0.5:51234", path,
    )


# --- uvicorn's record shapes -------------------------------------------------


def test_access_line_has_no_token(uvicorn_logs):
    # Exactly uvicorn's call in protocols/http/*_impl.py.
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "10.0.0.5:51234", "GET", f"/ws/voice?token={SECRET}&lang=de", "1.1", 101,
    )
    line = uvicorn_logs.getvalue()
    assert SECRET not in line
    assert f'10.0.0.5:51234 - "GET /ws/voice?token={REDACTED}&lang=de HTTP/1.1" 101' in line


def test_websocket_accepted_line_has_no_token(uvicorn_logs):
    _log_ws_accepted(f"/ws/voice?token={SECRET}")
    line = uvicorn_logs.getvalue()
    assert SECRET not in line
    assert f'10.0.0.5:51234 - "WebSocket /ws/voice?token={REDACTED}" [accepted]' in line


def test_without_the_filter_the_token_is_logged(uvicorn_logs):
    """Negative control: the fixture captures the real line, so the tests above
    pass because of the filter and not because nothing was written."""
    _strip_redaction()
    _log_ws_accepted(f"/ws/voice?token={SECRET}")
    assert SECRET in uvicorn_logs.getvalue()


# --- the redaction itself ----------------------------------------------------


@pytest.mark.parametrize("url", CREDENTIAL_URLS)
def test_redacts_every_credential_param(url):
    assert SECRET not in redact_url_secrets(url)


@pytest.mark.parametrize("url", BYPASS_URLS)
def test_encoded_or_repeated_names_cannot_bypass(url):
    assert SECRET not in redact_url_secrets(url)


def test_starlette_reads_the_encoded_name_as_token():
    """Negative control for the test above: the bypass is real on the server side."""
    from starlette.datastructures import QueryParams

    assert QueryParams(f"%74oken={SECRET}").get("token") == SECRET


@pytest.mark.parametrize("url", HARMLESS_URLS)
def test_leaves_other_params_alone(url):
    assert redact_url_secrets(url) == url


# --- installation ------------------------------------------------------------


def test_install_is_idempotent():
    install_uvicorn_url_redaction()
    install_uvicorn_url_redaction()
    for name in log_safety.UVICORN_URL_LOGGERS:
        lg = logging.getLogger(name)
        assert sum(isinstance(f, UrlSecretRedactingFilter) for f in lg.filters) == 1


def test_main_installs_redaction_at_module_level():
    """Every launch path imports `voice_server.main`; the install must be a
    top-level call there, not inside the lifespan (which the anon listener and
    `lifespan="off"` launches skip)."""
    tree = ast.parse((VS_ROOT / "voice_server/main.py").read_text())
    calls = [
        n.value.func.id for n in tree.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
    ]
    assert "install_uvicorn_url_redaction" in calls


def test_importing_main_leaves_the_filter_on_both_loggers():
    """In a fresh interpreter, so no earlier test's install can stand in for it."""
    import subprocess
    import sys

    probe = (
        "import logging, voice_server.main\n"
        "from voice_server.log_safety import UrlSecretRedactingFilter as F\n"
        "for n in ('uvicorn.access', 'uvicorn.error'):\n"
        "    print(n, sum(isinstance(f, F) for f in logging.getLogger(n).filters))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=VS_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert out.split("\n")[:2] == ["uvicorn.access 1", "uvicorn.error 1"]


def test_filter_survives_a_second_uvicorn_config():
    """The anonymous listener builds a second `uvicorn.Config` inside the lifespan,
    which re-runs uvicorn's dictConfig on the same loggers."""
    install_uvicorn_url_redaction()

    async def _noop(scope, receive, send):  # pragma: no cover - never served
        pass

    uvicorn.Config(_noop, log_level="info", lifespan="off")
    for name in log_safety.UVICORN_URL_LOGGERS:
        assert any(
            isinstance(f, UrlSecretRedactingFilter) for f in logging.getLogger(name).filters
        ), name


# --- a real uvicorn server ---------------------------------------------------


async def _ws_echo_app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.close", "code": 1000})
    elif scope["type"] == "http":
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve_and_hit(*, redact: bool) -> str:
    """Start a real uvicorn, open `/ws/voice?token=...` and GET the same URL;
    return everything uvicorn logged."""
    import httpx
    import websockets

    port = _free_port()
    # Config() runs uvicorn's dictConfig, exactly as the image CMD does.
    config = uvicorn.Config(_ws_echo_app, host="127.0.0.1", port=port, log_level="info")
    if redact:
        install_uvicorn_url_redaction()
    else:
        _strip_redaction()
    buf = io.StringIO()
    handlers = _capture(buf)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/voice?token={SECRET}") as ws:
            with pytest.raises(websockets.ConnectionClosed):
                await ws.recv()
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{port}/ws/voice?%74oken={SECRET}")
    finally:
        server.should_exit = True
        await task
        _release(handlers)
    return buf.getvalue()


async def test_real_uvicorn_logs_no_token():
    out = await _serve_and_hit(redact=True)
    assert f'"WebSocket /ws/voice?token={REDACTED}" [accepted]' in out
    assert f"GET /ws/voice?%74oken={REDACTED} HTTP/1.1" in out
    assert SECRET not in out


async def test_real_uvicorn_logs_the_token_without_the_filter():
    """Negative control: the real server does write the token into both lines."""
    out = await _serve_and_hit(redact=False)
    assert f'"WebSocket /ws/voice?token={SECRET}" [accepted]' in out
    assert f"GET /ws/voice?%74oken={SECRET} HTTP/1.1" in out


# --- parity with the backend's module ----------------------------------------


@pytest.fixture(scope="module")
def backend_log_safety():
    if not BACKEND_LOG_SAFETY.exists():
        pytest.skip(f"backend module not in this checkout: {BACKEND_LOG_SAFETY}")
    spec = importlib.util.spec_from_file_location("_backend_log_safety", BACKEND_LOG_SAFETY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # A rename on the backend side must FAIL here, never skip: a skip would let
    # the two copies drift apart unnoticed.
    assert hasattr(mod, "redact_url_secrets"), "backend URL redaction renamed or removed"
    return mod


def test_same_secret_names_as_backend(backend_log_safety):
    assert log_safety.SECRET_QUERY_PARAMS == backend_log_safety.SECRET_QUERY_PARAMS
    assert log_safety.REDACTED == backend_log_safety.REDACTED
    assert log_safety.UVICORN_URL_LOGGERS == backend_log_safety.UVICORN_URL_LOGGERS
    assert log_safety._QUERY_PAIR_RE.pattern == backend_log_safety._QUERY_PAIR_RE.pattern


@pytest.mark.parametrize(
    "text",
    CREDENTIAL_URLS + BYPASS_URLS + HARMLESS_URLS + [
        f'10.0.0.5:51234 - "WebSocket /ws/voice?token={SECRET}" [accepted]',
        f"/x?password={SECRET}&apikey={SECRET}&q=hello+world",
        f"/x?+token+={SECRET}", f"/x?key=&token={SECRET}",
    ],
)
def test_same_output_as_backend(backend_log_safety, text):
    assert redact_url_secrets(text) == backend_log_safety.redact_url_secrets(text)
