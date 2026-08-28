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

def _doc(source=FOLDER_INGEST_SOURCE, filename="scan_2026.pdf", atom_id=None):
    from models.database import Document

    # spec=Document: a real Document has NO ``user_id`` column, so a regression to
    # ``doc.user_id`` (the bug this feature shipped with) raises AttributeError
    # instead of silently returning a phantom mock attribute.
    d = MagicMock(spec=Document)
    d.id = 1
    d.source = source
    d.filename = filename
    d.atom_id = atom_id
    return d


async def _run_hook(doc, existing=None, atom_owner=None, user_id=None):
    responses = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=doc)),
        MagicMock(scalar_one_or_none=MagicMock(return_value=existing)),
    ]
    # An atom-backed doc (not short-circuited by an existing proposal) triggers the
    # owner-resolution Atom query.
    if getattr(doc, "atom_id", None) and existing is None:
        atom = MagicMock(owner_user_id=atom_owner) if atom_owner is not None else None
        responses.append(MagicMock(scalar_one_or_none=MagicMock(return_value=atom)))

    db = MagicMock()
    db.execute = AsyncMock(side_effect=responses)
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
        await review.simba_ingest_post_hook(
            document_id=1, field_text="Rechnung", lang="de", user_id=user_id
        )
    return db


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hook_creates_proposal_owner_from_atom():
    """Owner = the document's atom owner (authoritative) — NOT doc.user_id
    (which doesn't exist on Document; the shipped-then-fixed bug)."""
    db = await _run_hook(_doc(atom_id="atom-1"), atom_owner=7)
    db.add.assert_called_once()
    prop = db.add.call_args.args[0]
    assert isinstance(prop, SimbaIngestProposal)
    assert prop.suggested_category == "Belege" and prop.suggested_type == "Ausgangsrechnung"
    assert prop.user_id == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hook_owner_falls_back_to_ingesting_user():
    """No atom → owner falls back to the ingesting user_id the hook was given."""
    db = await _run_hook(_doc(atom_id=None), user_id=9)
    db.add.assert_called_once()
    assert db.add.call_args.args[0].user_id == 9


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
# Ownership gate (_owns) — the auth-bypass fix
# --------------------------------------------------------------------------

def _user(uid, admin=False):
    u = MagicMock()
    u.id = uid
    u.get_permissions.return_value = ["admin"] if admin else ["ha.read"]
    return u


def _prop(owner):
    p = MagicMock(spec=SimbaIngestProposal)
    p.user_id = owner
    return p


@pytest.mark.unit
def test_owns_auth_off_sees_everything():
    with patch("services.simba_ingest_review.settings") as ms:
        ms.auth_enabled = False
        assert review._owns(_prop(7), None) is True
        assert review._owns(_prop(None), None) is True


@pytest.mark.unit
def test_owns_auth_on_unauthenticated_denied():
    with patch("services.simba_ingest_review.settings") as ms:
        ms.auth_enabled = True
        assert review._owns(_prop(7), None) is False
        assert review._owns(_prop(None), None) is False


@pytest.mark.unit
def test_owns_auth_on_owner_only():
    with patch("services.simba_ingest_review.settings") as ms:
        ms.auth_enabled = True
        assert review._owns(_prop(7), _user(7)) is True
        # A different logged-in user cannot see someone else's proposal.
        assert review._owns(_prop(7), _user(8)) is False


@pytest.mark.unit
def test_owns_auth_on_null_owner_admin_only():
    with patch("services.simba_ingest_review.settings") as ms:
        ms.auth_enabled = True
        # Null-owner (folder-ingest with no atom owner) — admins only, not every user.
        with patch("models.permissions.has_permission", lambda perms, p: "admin" in perms):
            assert review._owns(_prop(None), _user(8, admin=True)) is True
            assert review._owns(_prop(None), _user(8, admin=False)) is False


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
    async def test_confirm_second_call_is_409_and_no_reupload(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """Claim-before-act: once a proposal is UPLOADED, a second confirm must
        NOT trigger a second (irreversible) upload — it 409s."""
        import json as _json

        from models.database import Document

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 x")
            tmp = f.name
        try:
            doc = Document(filename="d.pdf", file_path=tmp, status="completed")
            db_session.add(doc)
            await db_session.commit()
            await db_session.refresh(doc)
            p = SimbaIngestProposal(document_id=doc.id, filename="d.pdf", status=SIMBA_PROPOSAL_PENDING)
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
                r1 = await async_client.post(
                    f"/api/simba-ingest/{p.id}/confirm",
                    json={"category": "Belege", "type": "Ausgangsrechnung"},
                )
                r2 = await async_client.post(
                    f"/api/simba-ingest/{p.id}/confirm",
                    json={"category": "Belege", "type": "Ausgangsrechnung"},
                )
            finally:
                app.state.mcp_manager = None
            assert r1.status_code == 200
            assert r2.status_code == 409
            # The irreversible upload happened exactly once.
            assert mock.execute_tool.await_count == 1
            # And truncate=False was passed so a truncated envelope can't misread landing.
            assert mock.execute_tool.await_args.kwargs.get("truncate") is False
        finally:
            Path(tmp).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_confirm_upload_error_reverts_to_pending(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """An upload exception reverts the claim (UPLOADING → PENDING) so the
        owner can retry — the proposal must not strand in UPLOADING."""
        from models.database import Document

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 x")
            tmp = f.name
        try:
            doc = Document(filename="e.pdf", file_path=tmp, status="completed")
            db_session.add(doc)
            await db_session.commit()
            await db_session.refresh(doc)
            p = SimbaIngestProposal(document_id=doc.id, filename="e.pdf", status=SIMBA_PROPOSAL_PENDING)
            db_session.add(p)
            await db_session.commit()
            await db_session.refresh(p)

            from main import app
            mock = AsyncMock()
            mock.execute_tool = AsyncMock(side_effect=RuntimeError("portal down"))
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
            assert p.status == SIMBA_PROPOSAL_PENDING  # claim reverted, retryable
        finally:
            Path(tmp).unlink(missing_ok=True)

    @pytest.mark.backend
    async def test_reject_twice_second_is_404(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """The conditional UPDATE makes a double-resolve safe: the second reject
        finds no PENDING row → 404."""
        p = SimbaIngestProposal(
            document_id=456, filename="f.pdf", status=SIMBA_PROPOSAL_PENDING,
        )
        db_session.add(p)
        await db_session.commit()
        await db_session.refresh(p)

        r1 = await async_client.post(f"/api/simba-ingest/{p.id}/reject")
        r2 = await async_client.post(f"/api/simba-ingest/{p.id}/reject")
        assert r1.status_code == 200
        assert r2.status_code == 404

    @pytest.mark.backend
    async def test_routes_require_auth_when_enabled(self, db_session: AsyncSession, monkeypatch):
        """The routes 401 an unauthenticated caller when auth is on — proving
        _require_user is actually wired in (not just the _owns predicate)."""
        from fastapi import HTTPException

        from api.routes import simba_ingest as routes
        from utils.config import settings as cfg

        monkeypatch.setattr(cfg, "auth_enabled", True)
        req = MagicMock()

        with pytest.raises(HTTPException) as e1:
            await routes.list_proposals(db=db_session, user=None)
        assert e1.value.status_code == 401
        with pytest.raises(HTTPException) as e2:
            await routes.reject_proposal(proposal_id=1, db=db_session, user=None)
        assert e2.value.status_code == 401
        with pytest.raises(HTTPException) as e3:
            await routes.confirm_proposal(
                proposal_id=1,
                body=routes.SimbaConfirmRequest(category="Belege", type="Ausgangsrechnung"),
                request=req,
                db=db_session,
                user=None,
            )
        assert e3.value.status_code == 401

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
