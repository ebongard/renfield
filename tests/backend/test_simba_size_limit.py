"""A file over the Simba portal's size limit is named before the confirm and
refused without an upload (#1609).

2026-11-22: a 16.2 MB PDF was offered for the irreversible upload, failed in
the MCP's own validation, and answered 502 -- the UI showed axios's generic
"Request failed" instead of the reason.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import SIMBA_PROPOSAL_PENDING, Document, SimbaIngestProposal
from services import simba_portal_limits

pytestmark = pytest.mark.backend

#: A tiny portal limit so a test file of a few KB is "too large".
_LIMIT_MB = 0.001  # 1048 bytes
_BIG = 4096
_SMALL = 100


@pytest.fixture(autouse=True)
def _fresh_limits():
    simba_portal_limits.reset_for_tests()
    yield
    simba_portal_limits.reset_for_tests()


def _mcp(upload_result=None):
    """An MCP manager whose portal states `_LIMIT_MB`; records every call."""
    def _exec(name, args, **_kw):
        if name == "mcp.simba.check_connection":
            return {"success": True, "message": json.dumps({"status": "ok", "maxDateigroesseMB": _LIMIT_MB})}
        if name == "mcp.simba.list_transfers":
            return {"success": True, "message": json.dumps({"anzahl": 0, "zeilen": []})}
        return upload_result or {"success": True, "message": json.dumps({"uebertragen": 1, "fehlgeschlagen": 0})}

    m = AsyncMock()
    m.execute_tool = AsyncMock(side_effect=_exec)
    return m


def _called(mgr, name):
    return [c for c in mgr.execute_tool.await_args_list if c.args[0] == name]


async def _doc_with_file(db, size: int, filename: str = "scan.pdf") -> tuple[Document, str]:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4 " + b"x" * (size - 9))
        path = f.name
    doc = Document(filename=filename, file_path=path, status="completed", generated_title="Beleg")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc, path


async def _pending(db, doc) -> SimbaIngestProposal:
    p = SimbaIngestProposal(
        document_id=doc.id, user_id=None, filename=doc.filename,
        suggested_category="Belege", suggested_type="Ausgangsrechnung", status=SIMBA_PROPOSAL_PENDING,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


class _App:
    """Installs an MCP manager on the app for one test."""

    def __init__(self, mgr):
        from main import app

        self.app, self.mgr = app, mgr

    def __enter__(self):
        self.app.state.mcp_manager = self.mgr
        return self.mgr

    def __exit__(self, *_exc):
        self.app.state.mcp_manager = None


class TestTheListNamesIt:
    async def test_an_oversized_proposal_carries_the_finding_a_fitting_one_none(
        self, async_client: AsyncClient, db_session: AsyncSession,
    ):
        big, big_path = await _doc_with_file(db_session, _BIG)
        small, small_path = await _doc_with_file(db_session, _SMALL)
        try:
            pb, ps = await _pending(db_session, big), await _pending(db_session, small)
            with _App(_mcp()):
                resp = await async_client.get("/api/simba-ingest")
            assert resp.status_code == 200
            rows = {r["id"]: r for r in resp.json()["proposals"]}
            assert rows[pb.id]["upload_blocked"] == {"code": "too_large", "size_mb": 0.0, "max_mb": _LIMIT_MB}
            assert rows[ps.id]["upload_blocked"] is None
        finally:
            Path(big_path).unlink(missing_ok=True)
            Path(small_path).unlink(missing_ok=True)

    async def test_an_empty_queue_never_asks_the_portal(self, async_client: AsyncClient):
        """The list is not xidra-only: on an instance without Simba, opening the
        review page must not try a portal login."""
        with _App(_mcp()) as mgr:
            resp = await async_client.get("/api/simba-ingest")
        assert resp.status_code == 200 and resp.json()["proposals"] == []
        mgr.execute_tool.assert_not_awaited()

    async def test_unknown_limits_block_nothing(self, async_client: AsyncClient, db_session: AsyncSession):
        big, path = await _doc_with_file(db_session, _BIG)
        try:
            p = await _pending(db_session, big)
            mgr = AsyncMock()
            mgr.execute_tool = AsyncMock(return_value={"success": False, "message": "not connected"})
            with _App(mgr):
                resp = await async_client.get("/api/simba-ingest")
            row = next(r for r in resp.json()["proposals"] if r["id"] == p.id)
            assert row["upload_blocked"] is None
        finally:
            Path(path).unlink(missing_ok=True)


class TestTheConfirmRefusesIt:
    async def test_422_with_the_reason_and_no_upload(self, async_client: AsyncClient, db_session: AsyncSession):
        doc, path = await _doc_with_file(db_session, _BIG)
        try:
            p = await _pending(db_session, doc)
            with _App(_mcp()) as mgr:
                resp = await async_client.post(
                    f"/api/simba-ingest/{p.id}/confirm", json={"category": "Belege", "type": "Ausgangsrechnung"},
                )
            assert resp.status_code == 422
            assert "Simba-Portal nimmt höchstens" in resp.json()["detail"]
            # Refused before the duplicate check and before the claim.
            assert not _called(mgr, "mcp.simba.upload_documents")
            assert not _called(mgr, "mcp.simba.list_transfers")
            await db_session.refresh(p)
            assert p.status == SIMBA_PROPOSAL_PENDING
        finally:
            Path(path).unlink(missing_ok=True)

    async def test_a_fitting_file_still_uploads(self, async_client: AsyncClient, db_session: AsyncSession):
        """Negative control: the pre-check passes what fits."""
        doc, path = await _doc_with_file(db_session, _SMALL)
        try:
            p = await _pending(db_session, doc)
            with _App(_mcp()) as mgr:
                resp = await async_client.post(
                    f"/api/simba-ingest/{p.id}/confirm", json={"category": "Belege", "type": "Ausgangsrechnung"},
                )
            assert resp.status_code == 200 and resp.json()["success"] is True
            assert len(_called(mgr, "mcp.simba.upload_documents")) == 1
        finally:
            Path(path).unlink(missing_ok=True)


class TestTheSendOverlayNamesIt:
    async def test_no_new_proposal_for_an_oversized_document(
        self, async_client: AsyncClient, db_session: AsyncSession, monkeypatch,
    ):
        from sqlalchemy import select

        from utils.config import settings as cfg

        monkeypatch.setattr(cfg, "folder_ingest_simba_enabled", True)
        doc, path = await _doc_with_file(db_session, _BIG)
        try:
            with _App(_mcp()):
                resp = await async_client.post(f"/api/simba-ingest/from-document/{doc.id}")
            assert resp.status_code == 422
            assert "Simba-Portal nimmt höchstens" in resp.json()["detail"]
            rows = (await db_session.execute(
                select(SimbaIngestProposal).where(SimbaIngestProposal.document_id == doc.id)
            )).scalars().all()
            assert rows == []
        finally:
            Path(path).unlink(missing_ok=True)

    async def test_an_existing_pending_proposal_comes_back_with_the_finding(
        self, async_client: AsyncClient, db_session: AsyncSession, monkeypatch,
    ):
        from utils.config import settings as cfg

        monkeypatch.setattr(cfg, "folder_ingest_simba_enabled", True)
        doc, path = await _doc_with_file(db_session, _BIG)
        try:
            p = await _pending(db_session, doc)
            with _App(_mcp()):
                resp = await async_client.post(f"/api/simba-ingest/from-document/{doc.id}")
            assert resp.status_code == 200
            body = resp.json()
            assert body["proposal_id"] == p.id
            assert body["upload_blocked"]["code"] == "too_large"
        finally:
            Path(path).unlink(missing_ok=True)


class TestTheFailureLogCarriesNoContent:
    async def test_a_not_landed_upload_logs_counts_not_the_envelope(
        self, async_client: AsyncClient, db_session: AsyncSession,
    ):
        """The envelope names the file, the target and the portal's error text
        (#1565); the log line keeps only its shape."""
        doc, path = await _doc_with_file(db_session, _SMALL, filename="Steuerbescheid-Muster.pdf")
        envelope = {"success": True, "message": json.dumps({
            "modus": "echter Upload", "ziel": "Belege / Ausgangsrechnung", "zeitraum": "07/2025",
            "uebertragen": 0, "fehlgeschlagen": 1, "ergebnisse": [{"ok": False, "status": 413}],
            "fehler": [{"file": "Steuerbescheid-Muster.pdf", "error": "Portal lehnte Steuerbescheid-Muster ab"}],
        })}
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="ERROR")
        try:
            p = await _pending(db_session, doc)
            with _App(_mcp(upload_result=envelope)):
                resp = await async_client.post(
                    f"/api/simba-ingest/{p.id}/confirm", json={"category": "Belege", "type": "Ausgangsrechnung"},
                )
        finally:
            logger.remove(sink)
            Path(path).unlink(missing_ok=True)
        assert resp.status_code == 502
        failure = [x for x in lines if "not landed" in x]
        assert len(failure) == 1
        assert "uebertragen=0" in failure[0] and "statuses=[413]" in failure[0] and "errors=1" in failure[0]
        for content in ("Steuerbescheid", "Belege / Ausgangsrechnung", "Portal lehnte"):
            assert content not in failure[0]
