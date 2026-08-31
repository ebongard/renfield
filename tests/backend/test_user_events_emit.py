"""Emit-point wiring tests (review M1): the seams that PUBLISH user events must
fire with the right reason on the right state transition, and never on a
non-terminal one. Covers the Paperless settle seam (which funnels all 5 filing
terminal states through the shared `_emit_paperless_changed` helper) + the helper
itself. The rag_service ingest/delete and Simba seams are integration-heavy
(full pipeline / MCP fixtures) and are verified by the post-deploy browser E2E;
the shared emit core (`emit_documents_changed`) is unit-tested in
test_user_events.py.
"""
from __future__ import annotations

import pytest

import services.folder_ingest_paperless as fip

pytestmark = [pytest.mark.backend, pytest.mark.asyncio]


class FakeDoc:
    def __init__(self):
        self.paperless_state = None
        self.paperless_task_id = "task-1"
        self.paperless_document_id = None
        self.atom_id = "atom-1"
        self.id = 5
        self.file_hash = "hash-1"


class FakeDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _patch_emit(monkeypatch):
    calls: list = []

    async def _emit(db, doc):
        calls.append(doc)

    monkeypatch.setattr(fip, "_emit_paperless_changed", _emit)
    return calls


async def test_settle_done_emits_paperless(monkeypatch):
    calls = _patch_emit(monkeypatch)

    async def _no_checksum(_h):
        return None

    monkeypatch.setattr(fip, "_resolve_paperless_id_by_checksum", _no_checksum)
    doc = FakeDoc()

    settled = await fip._settle_from_outcome(FakeDB(), doc, {"status": "success"})

    assert settled is True
    assert doc.paperless_state == fip.PAPERLESS_STATE_DONE
    assert doc.paperless_task_id is None
    assert len(calls) == 1  # emitted exactly once on the DONE transition


async def test_settle_failure_emits_paperless(monkeypatch):
    calls = _patch_emit(monkeypatch)
    doc = FakeDoc()

    settled = await fip._settle_from_outcome(FakeDB(), doc, {"status": "failure"})

    assert settled is True
    assert doc.paperless_state == fip.PAPERLESS_STATE_FAILED
    assert len(calls) == 1


async def test_settle_pending_does_not_emit(monkeypatch):
    calls = _patch_emit(monkeypatch)
    doc = FakeDoc()

    settled = await fip._settle_from_outcome(FakeDB(), doc, {"status": "pending"})

    assert settled is False  # non-terminal → no state change, no event
    assert calls == []


async def test_emit_paperless_changed_uses_correct_reason(monkeypatch):
    captured: dict = {}

    async def _emit_dc(_redis, *, reason, db=None, document=None):
        captured["reason"] = reason
        captured["document"] = document

    monkeypatch.setattr("services.user_events.emit_documents_changed", _emit_dc)
    monkeypatch.setattr("services.redis_client.get_redis", lambda: object())

    doc = FakeDoc()
    await fip._emit_paperless_changed(FakeDB(), doc)

    assert captured["reason"] == "paperless"
    assert captured["document"] is doc


async def test_emit_paperless_changed_is_best_effort(monkeypatch):
    # A failure in the emit path must never propagate out of the filing code.
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr("services.redis_client.get_redis", _boom)
    # Must not raise.
    await fip._emit_paperless_changed(FakeDB(), FakeDoc())
