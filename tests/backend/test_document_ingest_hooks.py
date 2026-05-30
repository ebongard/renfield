"""``register_document_ingest_hooks`` — flag gating + idempotency.

The load-bearing property: the document-worker (primary ingestion path) must
populate the ``post_document_ingest`` registry itself, because it never runs
the FastAPI lifecycle. This helper is the single source of truth shared by
both. Regression guard for the silent no-op where KG + Schicht A extraction
never fired for knowledge-base uploads.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Stub heavy/native modules unavailable in a bare test env (mirrors siblings).
_missing_stubs = [
    "asyncpg", "whisper", "piper", "piper.voice", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
]
import importlib as _importlib  # noqa: E402

for _mod in _missing_stubs:
    if _mod in sys.modules:
        continue
    try:
        _importlib.import_module(_mod)
    except Exception:  # noqa: BLE001
        sys.modules[_mod] = MagicMock()

from services.document_ingest_hooks import register_document_ingest_hooks  # noqa: E402
from utils.hooks import _hooks, clear_hooks  # noqa: E402

_EVENT = "post_document_ingest"


def _handler_names() -> list[str]:
    return [getattr(f, "__qualname__", repr(f)) for f in _hooks.get(_EVENT, [])]


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_hooks()
    yield
    clear_hooks()


@pytest.mark.unit
def test_registers_both_when_flags_on(monkeypatch):
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.knowledge_graph_enabled", True
    )
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.schicht_a_extraction_enabled", True
    )
    register_document_ingest_hooks()

    names = _handler_names()
    assert any("kg_post_document_ingest_hook" in n for n in names)
    assert any("schicht_a_post_document_ingest_hook" in n for n in names)
    assert len(_hooks.get(_EVENT, [])) == 2


@pytest.mark.unit
def test_schicht_a_gated_off_by_default(monkeypatch):
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.knowledge_graph_enabled", True
    )
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.schicht_a_extraction_enabled", False
    )
    register_document_ingest_hooks()

    names = _handler_names()
    assert any("kg_post_document_ingest_hook" in n for n in names)
    assert not any("schicht_a" in n for n in names)


@pytest.mark.unit
def test_idempotent_no_double_registration(monkeypatch):
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.knowledge_graph_enabled", True
    )
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.schicht_a_extraction_enabled", True
    )
    register_document_ingest_hooks()
    register_document_ingest_hooks()  # second call must not append duplicates

    assert len(_hooks.get(_EVENT, [])) == 2


@pytest.mark.unit
def test_nothing_registered_when_all_flags_off(monkeypatch):
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.knowledge_graph_enabled", False
    )
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.schicht_a_extraction_enabled", False
    )
    register_document_ingest_hooks()
    assert _hooks.get(_EVENT, []) == []
