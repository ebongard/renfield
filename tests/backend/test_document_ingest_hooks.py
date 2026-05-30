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
from utils.hooks import _hooks, clear_hooks, register_hook  # noqa: E402

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
def test_kg_gated_off_registers_only_schicht_a(monkeypatch):
    """Symmetric to the case above — guards against an inverted KG flag check."""
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.knowledge_graph_enabled", False
    )
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.schicht_a_extraction_enabled", True
    )
    register_document_ingest_hooks()

    names = _handler_names()
    assert any("schicht_a_post_document_ingest_hook" in n for n in names)
    assert not any("kg_post_document_ingest_hook" in n for n in names)


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
def test_idempotent_against_external_prior_registration(monkeypatch):
    """The real-world case: the API lifecycle already registered the KG hook,
    then the worker startup runs the helper. The KG handler must not be
    double-added — this is what the is_hook_registered guard protects."""
    from services.knowledge_graph_service import kg_post_document_ingest_hook

    register_hook(_EVENT, kg_post_document_ingest_hook)  # pre-seed, as lifecycle would
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.knowledge_graph_enabled", True
    )
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.schicht_a_extraction_enabled", True
    )
    register_document_ingest_hooks()

    # KG present exactly once (not re-added), Schicht A newly added → 2 total.
    assert len(_hooks.get(_EVENT, [])) == 2
    names = _handler_names()
    assert sum("kg_post_document_ingest_hook" in n for n in names) == 1


@pytest.mark.unit
def test_consumer_import_failure_is_fail_open(monkeypatch):
    """A consumer whose import raises must not block the other consumer nor
    raise — a registration crash in the worker would otherwise take down the
    whole ingestion loop, not just extraction."""
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.knowledge_graph_enabled", True
    )
    monkeypatch.setattr(
        "services.document_ingest_hooks.settings.schicht_a_extraction_enabled", True
    )
    # Make the KG import explode; Schicht A must still register.
    import builtins

    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "services.knowledge_graph_service":
            raise ImportError("simulated bad KG import")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    register_document_ingest_hooks()  # must not raise

    names = _handler_names()
    assert any("schicht_a_post_document_ingest_hook" in n for n in names)
    assert not any("kg_post_document_ingest_hook" in n for n in names)


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


@pytest.mark.unit
def test_worker_main_wires_the_helper():
    """Regression guard for the actual bug: the document-worker must call
    register_document_ingest_hooks() in its startup. The helper tests above all
    pass even if main() never calls it — this asserts the wiring so a future
    refactor that drops the call fails loudly instead of silently restoring the
    no-op. Source-level (not a main() run) to avoid booting Redis/Docling."""
    import inspect

    import workers.document_processor_worker as worker

    src = inspect.getsource(worker.main)
    assert "register_document_ingest_hooks()" in src, (
        "document_processor_worker.main() must call "
        "register_document_ingest_hooks() or the worker registers no hooks"
    )
