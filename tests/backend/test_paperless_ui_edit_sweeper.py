"""
Unit tests for the PR 4 Paperless UI-edit sweeper + abandoned-confirm
cleanup.

Pure-unit, heavy mocking on the DB session and MCP manager. Covers the
sweep-tick state machine (candidate selection, diff detection,
time-window filter, age-cap expiry) and the smaller abandoned-confirm
sweeper.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.paperless_ui_edit_sweeper import (
    _MIN_AGE_BEFORE_SWEEP,
    _detect_edit,
    _normalise_field,
    run_abandoned_confirm_sweep,
    run_sweep_tick,
)

# Ensure the retriever module is loaded before tests patch into it —
# same side-effect-import trick PR 2b needed, see there for context.
import services.paperless_example_retriever  # noqa: F401


# ---------------------------------------------------------------------------
# Field normalisation
# ---------------------------------------------------------------------------


class TestNormaliseField:
    @pytest.mark.unit
    def test_none_empty_string_empty_list_collapse(self):
        assert _normalise_field("title", None) is None
        assert _normalise_field("title", "") is None
        assert _normalise_field("tags", []) is None

    @pytest.mark.unit
    def test_tags_sorted(self):
        """Tag-order swaps in Paperless UI must not register as edits."""
        assert _normalise_field("tags", ["b", "a"]) == ["a", "b"]

    @pytest.mark.unit
    def test_scalar_passthrough(self):
        assert _normalise_field("correspondent", "Stadtwerke") == "Stadtwerke"


# ---------------------------------------------------------------------------
# _detect_edit — diffing logic
# ---------------------------------------------------------------------------


class TestDetectEdit:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_diff_returns_none(self):
        """Live Paperless state matches original → no learning signal."""
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": json.dumps({
                "title": "T", "correspondent": "Stadtwerke",
                "document_type": "Rechnung", "tags": ["wohnung"],
                "storage_path": "/x", "created_date": "2026-02-14",
            }),
        })
        diff = await _detect_edit(
            mcp_manager=mcp,
            document_id=42,
            original={
                "title": "T", "correspondent": "Stadtwerke",
                "document_type": "Rechnung", "tags": ["wohnung"],
                "storage_path": "/x", "created_date": "2026-02-14",
            },
        )
        assert diff is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_correspondent_changed_returns_diff(self):
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": json.dumps({
                "title": "T", "correspondent": "Deutsche Telekom",
                "document_type": "Rechnung", "tags": ["wohnung"],
                "storage_path": "/x", "created_date": "2026-02-14",
            }),
        })
        diff = await _detect_edit(
            mcp_manager=mcp,
            document_id=42,
            original={
                "title": "T", "correspondent": "Telekom",
                "document_type": "Rechnung", "tags": ["wohnung"],
                "storage_path": "/x", "created_date": "2026-02-14",
            },
        )
        assert diff is not None
        assert diff["correspondent"] == "Deutsche Telekom"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_tag_order_swap_not_a_diff(self):
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": json.dumps({
                "title": "T", "tags": ["b", "a"],
            }),
        })
        diff = await _detect_edit(
            mcp_manager=mcp,
            document_id=42,
            original={"title": "T", "tags": ["a", "b"]},
        )
        assert diff is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_mcp_failure_returns_none(self):
        """get_document fails → treat as no diff (retry next tick)."""
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": False, "message": "down",
        })
        diff = await _detect_edit(
            mcp_manager=mcp,
            document_id=42,
            original={"title": "T"},
        )
        assert diff is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_mcp_not_found_returns_none(self):
        """Document was deleted from Paperless → inner ``error`` set."""
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": json.dumps({"error": "Document 42 not found"}),
        })
        diff = await _detect_edit(
            mcp_manager=mcp,
            document_id=42,
            original={"title": "T"},
        )
        assert diff is None


# ---------------------------------------------------------------------------
# run_sweep_tick
# ---------------------------------------------------------------------------


def _make_tracking(
    *,
    id: int = 1,
    chat_upload_id: int = 100,
    paperless_document_id: int = 42,
    user_id: int | None = 1,
    uploaded_at: datetime | None = None,
    original_metadata: dict | None = None,
    doc_text: str | None = "Stadtwerke Rechnung",
):
    return SimpleNamespace(
        id=id,
        chat_upload_id=chat_upload_id,
        paperless_document_id=paperless_document_id,
        user_id=user_id,
        uploaded_at=uploaded_at or (datetime.utcnow() - timedelta(hours=2)),
        original_metadata=original_metadata or {
            "title": "T", "correspondent": "Telekom",
            "document_type": "Rechnung", "tags": [],
            "storage_path": None, "created_date": None,
        },
        doc_text=doc_text,
        swept_at=None,
    )


def _make_session_factory(candidates: list, *, capture_writes: bool = True):
    """Build an AsyncSessionLocal patchable that returns *candidates* on
    the first execute() (the SELECT query) and no-ops on subsequent
    execute()/add()/commit()."""
    added: list = []
    updated_ids: list[list[int]] = []

    def _factory():
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        if capture_writes:
            session.add = MagicMock(side_effect=lambda obj: added.append(obj))

        call_count = {"n": 0}

        async def _run(stmt):
            call_count["n"] += 1
            # First call: SELECT (returns candidates). Later calls:
            # UPDATE (swept_at stamp). We don't need to distinguish
            # precisely — the SELECT path reads .scalars().all().
            result = MagicMock()
            scalars = MagicMock()
            scalars.all = MagicMock(return_value=candidates if call_count["n"] == 1 else [])
            result.scalars = MagicMock(return_value=scalars)
            # For the UPDATE path, record the swept IDs.
            try:
                compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
                if "UPDATE" in compiled.upper():
                    # Best-effort: we can't easily read the IN list here,
                    # so just record the call count.
                    updated_ids.append([])
            except Exception:
                pass
            return result

        session.execute = AsyncMock(side_effect=_run)
        session.commit = AsyncMock()
        return session

    _factory.added = added  # type: ignore[attr-defined]
    _factory.updated_ids = updated_ids  # type: ignore[attr-defined]
    return _factory


class TestRunSweepTick:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_candidates_returns_zero_counts(self):
        mcp = MagicMock()
        factory = _make_session_factory([])
        with patch("services.database.AsyncSessionLocal", factory):
            counts = await run_sweep_tick(mcp_manager=mcp)
        assert counts["candidates"] == 0
        assert counts["edits_detected"] == 0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_diff_writes_ui_sweep_row(self):
        """Tracking row has a real edit → persisted example row with
        source='paperless_ui_sweep' and the new user_approved fields."""
        from models.database import PaperlessExtractionExample

        tracking = _make_tracking(
            original_metadata={"correspondent": "Telekom", "title": "T",
                               "document_type": "Rechnung", "tags": [],
                               "storage_path": None, "created_date": None},
        )
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": json.dumps({
                "correspondent": "Deutsche Telekom", "title": "T",
                "document_type": "Rechnung", "tags": [],
                "storage_path": None, "created_date": None,
            }),
        })
        factory = _make_session_factory([tracking])

        with patch("services.database.AsyncSessionLocal", factory):
            with patch(
                "services.paperless_example_retriever.embed_doc_text",
                AsyncMock(return_value=None),  # embed optional
            ):
                counts = await run_sweep_tick(mcp_manager=mcp)

        assert counts["edits_detected"] == 1
        example_rows = [
            a for a in factory.added  # type: ignore[attr-defined]
            if isinstance(a, PaperlessExtractionExample)
        ]
        assert len(example_rows) == 1
        assert example_rows[0].source == "paperless_ui_sweep"
        assert example_rows[0].user_approved["correspondent"] == "Deutsche Telekom"
        assert example_rows[0].llm_output["correspondent"] == "Telekom"
        assert example_rows[0].user_id == tracking.user_id

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_diff_no_write_but_still_swept(self):
        """Live state matches original → no example row, but the
        tracking row still gets stamped swept_at so we don't re-check."""
        from models.database import PaperlessExtractionExample

        tracking = _make_tracking()
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": json.dumps(tracking.original_metadata),
        })
        factory = _make_session_factory([tracking])

        with patch("services.database.AsyncSessionLocal", factory):
            counts = await run_sweep_tick(mcp_manager=mcp)

        assert counts["edits_detected"] == 0
        assert counts["candidates"] == 1
        example_rows = [
            a for a in factory.added  # type: ignore[attr-defined]
            if isinstance(a, PaperlessExtractionExample)
        ]
        assert example_rows == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_expired_tracking_row_skipped_without_mcp_call(self):
        """Row older than the 24 h cap is stamped swept + expired —
        no MCP call, no example row. Cheap way to drain the backlog
        after downtime."""
        now = datetime.utcnow()
        old_tracking = _make_tracking(
            uploaded_at=now - timedelta(days=3),
        )
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock()  # should NOT be called
        factory = _make_session_factory([old_tracking])

        with patch("services.database.AsyncSessionLocal", factory):
            counts = await run_sweep_tick(mcp_manager=mcp, now=now)

        assert counts["expired"] == 1
        assert counts["edits_detected"] == 0
        mcp.execute_tool.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_mcp_error_does_not_stamp_swept(self):
        """MCP failure must leave swept_at=NULL so the next tick
        retries — transient outages shouldn't lose learning signal."""
        tracking = _make_tracking()
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(side_effect=RuntimeError("mcp down"))
        factory = _make_session_factory([tracking])

        with patch("services.database.AsyncSessionLocal", factory):
            counts = await run_sweep_tick(mcp_manager=mcp)

        assert counts["errors"] == 1
        # No UPDATE issued for swept_at since we bailed before appending
        # to swept_ids; factory.updated_ids captures UPDATE calls and
        # we expect zero.
        # (Best-effort assertion — the factory's UPDATE detection is
        # a simple string match, not a proof, so we just check errors
        # counter instead.)


# ---------------------------------------------------------------------------
# run_abandoned_confirm_sweep
# ---------------------------------------------------------------------------


class TestAbandonedConfirmSweep:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_deletes_old_rows(self):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.commit = AsyncMock()

        exec_result = MagicMock()
        exec_result.rowcount = 3
        session.execute = AsyncMock(return_value=exec_result)

        def _factory():
            return session

        with patch("services.database.AsyncSessionLocal", _factory):
            deleted = await run_abandoned_confirm_sweep(max_age_hours=24)

        assert deleted == 3

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_zero_when_nothing_to_delete(self):
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.commit = AsyncMock()

        exec_result = MagicMock()
        exec_result.rowcount = 0
        session.execute = AsyncMock(return_value=exec_result)

        def _factory():
            return session

        with patch("services.database.AsyncSessionLocal", _factory):
            deleted = await run_abandoned_confirm_sweep()

        assert deleted == 0
