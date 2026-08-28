"""
Tests for the watch-folder PDF → Simba review flow (hook gating + review actions).
"""
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    FOLDER_INGEST_SOURCE,
    SIMBA_PROPOSAL_PENDING,
    SIMBA_PROPOSAL_REJECTED,
    SIMBA_PROPOSAL_UPLOADED,
    SimbaIngestProposal,
)
from services import simba_ingest_review as review


# --------------------------------------------------------------------------
# Hook gating (mocked DB)
# --------------------------------------------------------------------------

def _doc(source=FOLDER_INGEST_SOURCE, filename="scan_2026.pdf", uid=7):
    d = MagicMock()
    d.id = 1
    d.source = source
    d.filename = filename
    d.user_id = uid
    return d


async def _run_hook(doc, existing=None):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=MagicMock(return_value=doc)),
        MagicMock(scalar_one_or_none=MagicMock(return_value=existing)),
    ])
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    @asynccontextmanager
    async def _session(*_a, **_k):
        yield db

    with patch("services.simba_ingest_review.AsyncSessionLocal", lambda *a, **k: _session()), patch(
        "services.simba_ingest_review.settings"
    ) as ms, patch(
        "services.simba_classify.classify_simba", AsyncMock(return_value=("Belege", "Ausgangsrechnung"))
    ):
        ms.folder_ingest_simba_enabled = True
        await review.simba_ingest_post_hook(document_id=1, field_text="Rechnung", lang="de")
    return db


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hook_creates_proposal_for_folder_pdf():
    db = await _run_hook(_doc())
    db.add.assert_called_once()
    prop = db.add.call_args.args[0]
    assert isinstance(prop, SimbaIngestProposal)
    assert prop.suggested_category == "Belege" and prop.suggested_type == "Ausgangsrechnung"
    assert prop.user_id == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hook_skips_non_folder_source():
    db = await _run_hook(_doc(source="meeting_transcript"))
    db.add.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hook_skips_non_pdf():
    db = await _run_hook(_doc(filename="photo.jpg"))
    db.add.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hook_skips_when_proposal_exists():
    db = await _run_hook(_doc(), existing=99)
    db.add.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hook_noop_when_flag_off():
    with patch("services.simba_ingest_review.settings") as ms:
        ms.folder_ingest_simba_enabled = False
        # Should return before touching the DB.
        await review.simba_ingest_post_hook(document_id=1, field_text="x")


# --------------------------------------------------------------------------
# Review routes (real DB via fixtures)
# --------------------------------------------------------------------------

class TestSimbaIngestRoutes:
    @pytest.mark.backend
    async def test_list_and_reject(self, async_client: AsyncClient, db_session: AsyncSession):
        p = SimbaIngestProposal(
            document_id=123, user_id=None, filename="a.pdf",
            suggested_category="Belege", suggested_type="Ausgangsrechnung",
            status=SIMBA_PROPOSAL_PENDING,
        )
        db_session.add(p)
        await db_session.commit()
        await db_session.refresh(p)

        resp = await async_client.get("/api/simba-ingest")
        assert resp.status_code == 200
        ids = [x["id"] for x in resp.json()["proposals"]]
        assert p.id in ids

        rej = await async_client.post(f"/api/simba-ingest/{p.id}/reject")
        assert rej.status_code == 200
        await db_session.refresh(p)
        assert p.status == SIMBA_PROPOSAL_REJECTED

        # Now it's gone from the pending list.
        resp2 = await async_client.get("/api/simba-ingest")
        assert p.id not in [x["id"] for x in resp2.json()["proposals"]]

    @pytest.mark.backend
    async def test_confirm_uploads_and_marks(self, async_client: AsyncClient, db_session: AsyncSession):
        import json as _json

        from models.database import Document

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 x")
            tmp = f.name
        try:
            doc = Document(filename="b.pdf", file_path=tmp, status="completed")
            db_session.add(doc)
            await db_session.commit()
            await db_session.refresh(doc)
            p = SimbaIngestProposal(
                document_id=doc.id, user_id=None, filename="b.pdf",
                suggested_category="Belege", suggested_type="Ausgangsrechnung",
                status=SIMBA_PROPOSAL_PENDING,
            )
            db_session.add(p)
            await db_session.commit()
            await db_session.refresh(p)

            from main import app
            mock = AsyncMock()
            mock.execute_tool = AsyncMock(return_value={
                "success": True, "message": _json.dumps({"uebertragen": 1, "fehlgeschlagen": 0}),
            })
            app.state.mcp_manager = mock
            try:
                resp = await async_client.post(
                    f"/api/simba-ingest/{p.id}/confirm",
                    json={"category": "Posteingang", "type": "Schriftverkehr"},
                )
            finally:
                app.state.mcp_manager = None
            assert resp.status_code == 200
            args = mock.execute_tool.await_args.args[1]
            assert args["dry_run"] is False and args["confirm"] is True
            assert args["category"] == "Posteingang"
            await db_session.refresh(p)
            assert p.status == SIMBA_PROPOSAL_UPLOADED
            assert p.suggested_category == "Posteingang"  # edited value persisted
        finally:
            Path(tmp).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_confirm_not_landed_is_502(self, async_client: AsyncClient, db_session: AsyncSession):
        import json as _json

        from models.database import Document

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 x")
            tmp = f.name
        try:
            doc = Document(filename="c.pdf", file_path=tmp, status="completed")
            db_session.add(doc)
            await db_session.commit()
            await db_session.refresh(doc)
            p = SimbaIngestProposal(document_id=doc.id, filename="c.pdf", status=SIMBA_PROPOSAL_PENDING)
            db_session.add(p)
            await db_session.commit()
            await db_session.refresh(p)

            from main import app
            mock = AsyncMock()
            mock.execute_tool = AsyncMock(return_value={
                "success": True,
                "message": _json.dumps({"uebertragen": 0, "fehlgeschlagen": 1,
                                        "ergebnisse": [{"ok": False, "status": 401, "response": "no"}]}),
            })
            app.state.mcp_manager = mock
            try:
                resp = await async_client.post(
                    f"/api/simba-ingest/{p.id}/confirm",
                    json={"category": "Belege", "type": "Ausgangsrechnung"},
                )
            finally:
                app.state.mcp_manager = None
            assert resp.status_code == 502
            await db_session.refresh(p)
            assert p.status == SIMBA_PROPOSAL_PENDING  # stays pending on failure
        finally:
            Path(tmp).unlink(missing_ok=True)
