"""Verify the document-worker module is isolated from the FastAPI app (#388).

The worker pod's memory budget (6 GiB) assumes it only loads Docling +
EasyOCR + embedding clients. If ``import workers.document_processor_worker``
transitively pulls ``main.app``, the worker boots the full lifecycle
(MCP-connect to 10 servers, Whisper download, Speechbrain, …) and the
budget is fiction.

This test guards against that regression. Run it in a fresh subprocess so
the global module cache is clean.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.unit
def test_worker_import_does_not_load_fastapi_app():
    """Importing the worker must not load ``main`` (where FastAPI's ``app``
    is instantiated) or any MCP client module."""
    script = textwrap.dedent(
        """
        import sys
        import workers.document_processor_worker as w  # noqa: F401

        loaded_main = 'main' in sys.modules
        loaded_mcp = [m for m in sys.modules if m.startswith('services.mcp')]
        loaded_lifecycle = 'api.lifecycle' in sys.modules
        loaded_chat_handler = 'api.websocket.chat_handler' in sys.modules

        print(f'main={loaded_main}')
        print(f'mcp_modules={loaded_mcp}')
        print(f'lifecycle={loaded_lifecycle}')
        print(f'chat_handler={loaded_chat_handler}')

        assert not loaded_main, 'worker transitively loaded main (FastAPI app)'
        assert not loaded_lifecycle, 'worker transitively loaded api.lifecycle'
        assert not loaded_chat_handler, 'worker transitively loaded chat_handler'
        # mcp_client is imported by rag_service only through the agent path;
        # if it sneaks in here we've broken the worker budget.
        assert not loaded_mcp, f'worker loaded MCP modules: {loaded_mcp}'
        """
    )

    # The subprocess gets a fresh Python and does NOT inherit sys.path
    # modifications made by conftest.py. Inject PYTHONPATH explicitly so
    # ``import workers.document_processor_worker`` resolves both in the
    # container (where PYTHONPATH is unset but CWD=/app works) and on CI
    # runners (where CWD is the repo root and only PYTHONPATH helps).
    backend_root = Path(__file__).resolve().parents[2] / "src" / "backend"
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(backend_root) + (os.pathsep + existing if existing else "")
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"worker import test failed.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
