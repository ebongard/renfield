"""Guard against the asyncio function-local-shadow bug in the chat WebSocket handler.

A function-local `import asyncio` inside `websocket_endpoint` makes `asyncio` a
LOCAL for the entire function, so any `asyncio.*` use on a code path that skips
the import raises `UnboundLocalError` at runtime (prod WebSocket chat crash,
chat_handler:~1980). asyncio is imported at module scope; nobody should re-import
it inside the function. This static check fails fast if the shadow is reintroduced.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import api.websocket.chat_handler as _chat_handler  # noqa: E402

pytestmark = [pytest.mark.unit]

# Locate the source via the imported module so the path resolves both locally
# (repo layout) and in the .159 container (src/backend mounted at /app).
_HANDLER = Path(_chat_handler.__file__)


def _function_local_imports(func: ast.AsyncFunctionDef | ast.FunctionDef, module: str) -> list[int]:
    """Line numbers of any `import <module>` / `from <module> import ...` nested in func."""
    hits: list[int] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Import):
            if any(alias.name == module for alias in node.names):
                hits.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module == module:
                hits.append(node.lineno)
    return hits


def test_websocket_endpoint_does_not_shadow_asyncio():
    tree = ast.parse(_HANDLER.read_text())
    target = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
         and n.name == "websocket_endpoint"),
        None,
    )
    assert target is not None, "websocket_endpoint not found in chat_handler.py"
    shadows = _function_local_imports(target, "asyncio")
    assert not shadows, (
        f"function-local 'import asyncio' in websocket_endpoint at line(s) {shadows} "
        "shadows the module import → UnboundLocalError on paths that skip it. "
        "asyncio is imported at module scope; remove the local import."
    )


def test_asyncio_imported_at_module_scope():
    tree = ast.parse(_HANDLER.read_text())
    module_level = [
        n for n in tree.body
        if isinstance(n, ast.Import) and any(a.name == "asyncio" for a in n.names)
    ]
    assert module_level, "asyncio must be imported at module scope in chat_handler.py"
