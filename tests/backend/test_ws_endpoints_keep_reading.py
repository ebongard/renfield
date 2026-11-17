"""Every WebSocket endpoint that works inline between frames reads through ClientFrameReader (#1570).

A handler that awaits long work between two ``receive`` calls stops uvicorn
from reading the socket as soon as the client sends anything during that work;
the server then misses its own keepalive pong and closes with 1011. The fix is
``api.websocket.shared.ClientFrameReader``. This test holds the CLASS: a new
endpoint whose receive loop awaits anything besides the receive itself must
use the reader or be listed below with a reason.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import api.websocket.chat_handler as _chat_handler

pytestmark = [pytest.mark.unit]

_BACKEND = Path(_chat_handler.__file__).resolve().parents[2]

#: Endpoints that call ``websocket.receive*`` directly although their loop
#: awaits something else. Each needs a reason why no long await happens there.
ALLOWED_DIRECT_RECEIVE = {
    # Per-chunk CPU detection (milliseconds) plus a send on a hit; no await
    # that can outlive the keepalive budget.
    ("main.py", "wakeword_websocket"),
}

_RECEIVE_NAMES = {"receive", "receive_json", "receive_text", "receive_bytes"}


def _is_websocket_route(func: ast.AST) -> bool:
    for deco in getattr(func, "decorator_list", []):
        if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "websocket"):
            return True
    return False


def _direct_receive(node: ast.AST) -> bool:
    return (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr in _RECEIVE_NAMES)


def offending_loops(source: str) -> list[str]:
    """Names of websocket endpoints with a loop that awaits a direct
    ``receive*`` AND some other awaitable -- the shape that pauses reading."""
    hits = []
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, ast.AsyncFunctionDef) or not _is_websocket_route(func):
            continue
        for loop in ast.walk(func):
            if not isinstance(loop, (ast.While, ast.For, ast.AsyncFor)):
                continue
            awaits = [n for n in ast.walk(loop) if isinstance(n, ast.Await)]
            if any(_direct_receive(a) for a in awaits) and any(not _direct_receive(a) for a in awaits):
                hits.append(func.name)
                break
    return hits


def _endpoint_sources() -> list[Path]:
    return [
        p for p in sorted(_BACKEND.rglob("*.py"))
        if "satellite" not in p.relative_to(_BACKEND).parts[:1]  # the device-side client package
        and "websocket" in p.read_text(encoding="utf-8", errors="ignore")
    ]


def test_detector_flags_an_inline_turn_and_spares_a_push_only_loop():
    # Negative control: without it a detector that never fires passes.
    inline = '''
@router.websocket("/x")
async def inline(websocket):
    while True:
        data = await websocket.receive_json()
        await run_turn(data)
'''
    push_only = '''
@router.websocket("/y")
async def push_only(websocket):
    while True:
        await websocket.receive_text()
'''
    reader = '''
@router.websocket("/z")
async def reader(websocket):
    frames = ClientFrameReader(websocket.receive_json, label="z")
    while True:
        data = await frames.next()
        await run_turn(data)
'''
    assert offending_loops(inline) == ["inline"]
    assert offending_loops(push_only) == []
    assert offending_loops(reader) == []


def test_the_scan_sees_the_known_endpoints():
    # Scope control: the scan must actually reach the handlers it guards.
    seen = set()
    for path in _endpoint_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        seen |= {f.name for f in ast.walk(tree)
                 if isinstance(f, ast.AsyncFunctionDef) and _is_websocket_route(f)}
    assert {"websocket_endpoint", "device_websocket", "satellite_websocket", "wakeword_websocket"} <= seen, seen


def test_no_websocket_endpoint_works_inline_between_direct_receives():
    offenders = []
    for path in _endpoint_sources():
        rel = str(path.relative_to(_BACKEND))
        for name in offending_loops(path.read_text(encoding="utf-8")):
            if (rel, name) not in ALLOWED_DIRECT_RECEIVE:
                offenders.append(f"{rel}:{name}")
    assert not offenders, (
        "These WebSocket endpoints await work between direct receive calls; a "
        "client frame sent during that work pauses uvicorn's reading and the "
        f"server closes with 1011 (#1570). Use ClientFrameReader: {offenders}"
    )
