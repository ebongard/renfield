"""
Unit tests for the inline extraction + cold-start confirm additions
to ``forward_attachment_to_paperless`` (PR 2b).

Covers the three paths the tool now takes:
    1. skip_metadata=True (or auto-heuristic) → bare upload, no extract.
    2. user past cold-start cap → silent extract + upload.
    3. user inside cold-start → return action_required=paperless_confirm
       with a persisted pending row.

All tests are pure-unit with heavy mocking of the DB session,
extractor, and MCP manager. Integration against real Postgres lives in
tests/backend/test_paperless_confirm_flow_integration.py (deferred).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.chat_upload_tool import (
    _COLD_START_CONFIRM_N,
    _extraction_to_upload_params,
    _infer_lang,
    _render_confirm_message,
    _should_auto_skip_metadata,
    forward_attachment_to_paperless,
)


# ===========================================================================
# _should_auto_skip_metadata — heuristic
# ===========================================================================


class TestAutoSkipHeuristic:
    @pytest.mark.unit
    def test_screenshot_filename_triggers_skip(self):
        assert _should_auto_skip_metadata("screenshot.png", 1024) is True
        assert _should_auto_skip_metadata("Screen Shot 2026-04-22.png", 1024) is True
        assert _should_auto_skip_metadata("IMG_12345.jpg", 1024) is True
        assert _should_auto_skip_metadata("photo.png", 1024) is True
        assert _should_auto_skip_metadata("meme.gif", 1024) is True

    @pytest.mark.unit
    def test_small_image_triggers_skip(self):
        # 100 KB PNG — likely a meme / sticker / photo.
        assert _should_auto_skip_metadata("random.png", 100 * 1024) is True
        assert _should_auto_skip_metadata("random.jpg", 400 * 1024) is True

    @pytest.mark.unit
    def test_large_image_does_not_skip(self):
        # 2 MB image — likely a scanned document.
        assert _should_auto_skip_metadata("scan.png", 2 * 1024 * 1024) is False

    @pytest.mark.unit
    def test_pdf_never_auto_skips(self):
        """PDFs are always real documents regardless of size."""
        assert _should_auto_skip_metadata("invoice.pdf", 10 * 1024) is False
        assert _should_auto_skip_metadata("tiny.pdf", 1024) is False

    @pytest.mark.unit
    def test_docx_never_auto_skips(self):
        assert _should_auto_skip_metadata("contract.docx", 1024) is False

    @pytest.mark.unit
    def test_text_files_never_auto_skip(self):
        assert _should_auto_skip_metadata("notes.txt", 100) is False
        assert _should_auto_skip_metadata("notes.md", 100) is False

    @pytest.mark.unit
    def test_benign_image_filename_with_large_size(self):
        """A real scanned doc named 'mein_dokument.jpg' at 3 MB is NOT
        a screenshot — the filename regex doesn't match and size is
        over the image-autoskip threshold."""
        assert _should_auto_skip_metadata("mein_dokument.jpg", 3 * 1024 * 1024) is False


# ===========================================================================
# _infer_lang
# ===========================================================================


class TestInferLang:
    @pytest.mark.unit
    def test_default_is_de(self):
        upload = SimpleNamespace(filename="rechnung.pdf")
        assert _infer_lang(upload) == "de"

    @pytest.mark.unit
    def test_english_hints_flip_to_en(self):
        for name in ["invoice.pdf", "receipt_2026.pdf", "statement.pdf",
                     "contract.pdf", "letter.pdf"]:
            upload = SimpleNamespace(filename=name)
            assert _infer_lang(upload) == "en", f"{name!r} should be en"


# ===========================================================================
# _extraction_to_upload_params
# ===========================================================================


class TestExtractionToUploadParams:
    @pytest.mark.unit
    def test_full_extraction_translates(self):
        post_fuzzy = {
            "title": "Nebenkostenabrechnung 2025",
            "correspondent": "Stadtwerke",
            "document_type": "Rechnung",
            "tags": ["wohnung", "nebenkosten-2025"],
            "storage_path": "/wohnung/betriebskosten",
            "created_date": "2026-02-14",
            "confidence": {"title": 0.9},  # not forwarded
        }
        result = _extraction_to_upload_params(post_fuzzy)
        assert result == {
            "title": "Nebenkostenabrechnung 2025",
            "correspondent": "Stadtwerke",
            "document_type": "Rechnung",
            "tags": ["wohnung", "nebenkosten-2025"],
            "storage_path": "/wohnung/betriebskosten",
            "created_date": "2026-02-14",
        }

    @pytest.mark.unit
    def test_missing_fields_omitted_not_null(self):
        """Don't forward null values; MCP upload_document treats missing
        as 'caller has no opinion', null would be an error."""
        post_fuzzy = {
            "title": "T",
            "correspondent": None,
            "tags": [],
            "storage_path": None,
        }
        result = _extraction_to_upload_params(post_fuzzy)
        assert result == {"title": "T"}
        assert "correspondent" not in result
        assert "tags" not in result
        assert "storage_path" not in result

    @pytest.mark.unit
    def test_empty_tags_list_omitted(self):
        """Empty tags list is treated as 'no opinion', not 'clear all'."""
        result = _extraction_to_upload_params({"tags": []})
        assert "tags" not in result


# ===========================================================================
# _render_confirm_message
# ===========================================================================


class TestRenderConfirmMessage:
    @pytest.mark.unit
    def test_full_metadata_renders(self):
        msg = _render_confirm_message(
            filename="rechnung.pdf",
            metadata={
                "title": "Nebenkostenabrechnung 2025",
                "correspondent": "Stadtwerke Korschenbroich",
                "document_type": "Nebenkostenabrechnung",
                "tags": ["wohnung", "nebenkosten-2025"],
                "storage_path": "/wohnung/betriebskosten",
                "created_date": "2026-02-14",
            },
            proposals=[],
        )
        assert "rechnung.pdf" in msg
        assert "Stadtwerke Korschenbroich" in msg
        assert "Nebenkostenabrechnung" in msg
        assert "wohnung, nebenkosten-2025" in msg
        assert "/wohnung/betriebskosten" in msg
        assert "2026-02-14" in msg
        assert "keine" in msg  # no proposals
        assert "ja" in msg and "nein" in msg

    @pytest.mark.unit
    def test_missing_fields_render_as_dash(self):
        msg = _render_confirm_message(
            filename="f.pdf",
            metadata={
                "title": "T",
                "correspondent": None,
                "document_type": None,
                "tags": [],
                "storage_path": None,
                "created_date": None,
            },
            proposals=[],
        )
        # Five "—" markers (one per missing field).
        assert msg.count("—") >= 5

    @pytest.mark.unit
    def test_proposals_listed(self):
        # Use dict-shaped proposals (pending_confirms JSONB form).
        proposals = [
            {"field": "correspondent", "value": "Schreiner Meier",
             "reasoning": "..."},
            {"field": "tag", "value": "handwerker", "reasoning": "..."},
        ]
        msg = _render_confirm_message(
            filename="f.pdf", metadata={"title": "T"}, proposals=proposals,
        )
        assert "Schreiner Meier" in msg
        assert "handwerker" in msg
        assert "correspondent" in msg
        assert "tag" in msg

    @pytest.mark.unit
    def test_proposals_from_pydantic_objects(self):
        """Confirm message also accepts pydantic NewEntryProposal objects
        (the raw extractor output shape). Duck-typed via hasattr."""
        p = SimpleNamespace(field="correspondent", value="Test", reasoning="x")
        msg = _render_confirm_message(filename="f", metadata={}, proposals=[p])
        assert "Test" in msg


# ===========================================================================
# forward_attachment_to_paperless — integration with mocked deps
# ===========================================================================


def _upload_stub(tmp_path, *, filename="doc.pdf", size=10_000):
    """Build a ChatUpload-shaped mock with a real file on disk."""
    file = tmp_path / filename
    file.write_bytes(b"%PDF-1.4 " + b"x" * max(size - 9, 0))
    upload = MagicMock()
    upload.id = 42
    upload.filename = filename
    upload.file_path = str(file)
    upload.file_size = size
    return upload


class TestForwardAttachmentSkipPath:
    """skip_metadata=True (explicit or auto-heuristic) → direct upload."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_explicit_skip_metadata_bypasses_extractor(self, tmp_path):
        upload = _upload_stub(tmp_path)
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": '{"task_id": "t-1", "title": "doc.pdf"}',
        })

        # _load_upload is the DB lookup. Patch at the SQLAlchemy layer:
        # the function fetches a ChatUpload from AsyncSessionLocal.
        # Rather than mock SQLAlchemy, patch the module-level session
        # factory to yield a context-manager that returns our upload.
        async def _fake_session():
            return _make_session_with_upload(upload)

        with patch("services.chat_upload_tool.Path") as _p:
            _p.return_value.is_file = MagicMock(return_value=True)
            with patch("services.database.AsyncSessionLocal", _make_session_factory(upload)):
                result = await forward_attachment_to_paperless(
                    {"attachment_id": 42, "skip_metadata": True},
                    mcp_manager=mcp,
                    session_id="test-session",
                    user_id=1,
                )

        assert result["success"] is True
        assert result["action_taken"] is True
        # Extractor was NOT called.
        for call in mcp.execute_tool.await_args_list:
            assert "extract" not in str(call).lower()
        assert "Sent to Paperless" in result["message"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_auto_skip_on_screenshot_filename(self, tmp_path):
        upload = _upload_stub(tmp_path, filename="Screenshot_2026.png", size=150_000)
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True, "message": '{"task_id": "t"}',
        })

        with patch("services.database.AsyncSessionLocal", _make_session_factory(upload)):
            result = await forward_attachment_to_paperless(
                {"attachment_id": 42},  # no skip_metadata param
                mcp_manager=mcp,
                session_id="s",
                user_id=1,
            )

        assert result["success"] is True
        # Only one MCP call (the upload) — no extractor calls.
        assert mcp.execute_tool.await_count == 1


class TestForwardAttachmentColdStart:
    """Cold-start window: extraction → persist pending → return confirm."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_returns_action_required_when_inside_window(self, tmp_path):
        upload = _upload_stub(tmp_path, filename="rechnung.pdf", size=200_000)
        mcp = MagicMock()
        # Extractor will run, then the confirm persists. No MCP calls
        # for the upload itself on this path.
        mcp.execute_tool = AsyncMock()

        # Mock the extractor to return a clean result.
        extractor_result = SimpleNamespace(
            metadata=SimpleNamespace(
                title="T",
                correspondent="Stadtwerke",
                document_type="Rechnung",
                tags=["wohnung"],
                storage_path="/x",
                created_date=None,
                new_entry_proposals=[],
                model_dump=lambda **_kw: {
                    "title": "T",
                    "correspondent": "Stadtwerke",
                    "document_type": "Rechnung",
                    "tags": ["wohnung"],
                    "storage_path": "/x",
                    "created_date": None,
                    "new_entry_proposals": [],
                },
            ),
            doc_text="doc text",
            error=None,
        )

        with patch(
            "services.chat_upload_tool._run_extraction",
            AsyncMock(return_value=extractor_result),
        ):
            with patch(
                "services.chat_upload_tool._get_confirms_used",
                AsyncMock(return_value=0),  # Fresh user — cold-start window
            ):
                with patch(
                    "services.database.AsyncSessionLocal",
                    _make_session_factory(upload, capture_writes=True),
                ):
                    result = await forward_attachment_to_paperless(
                        {"attachment_id": 42},
                        mcp_manager=mcp, session_id="s", user_id=1,
                    )

        assert result["success"] is True
        assert result["action_taken"] is False  # upload NOT committed yet
        assert result["data"]["action_required"] == "paperless_confirm"
        assert "confirm_token" in result["data"]
        assert len(result["data"]["confirm_token"]) == 36  # uuid4 str
        # Preview message contains the extracted fields.
        assert "Stadtwerke" in result["message"]
        assert "Rechnung" in result["message"]
        # NO upload happened yet.
        assert mcp.execute_tool.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_cold_start_with_user_id_none_persists_pending(self, tmp_path):
        """AUTH_ENABLED=false: user_id is None. Pending row must still
        persist (user_id column is nullable) — regression guard for B1
        (previous code passed ``user_id or 0`` which triggered an FK
        violation against a non-existent user row 0)."""
        upload = _upload_stub(tmp_path, filename="rechnung.pdf", size=200_000)
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock()

        extractor_result = SimpleNamespace(
            metadata=SimpleNamespace(
                title="T", correspondent="Stadtwerke",
                document_type="Rechnung", tags=["wohnung"],
                storage_path="/x", created_date=None,
                new_entry_proposals=[],
                model_dump=lambda **_kw: {
                    "title": "T", "correspondent": "Stadtwerke",
                    "document_type": "Rechnung", "tags": ["wohnung"],
                    "storage_path": "/x", "created_date": None,
                    "new_entry_proposals": [],
                },
            ),
            doc_text="doc",
            error=None,
        )

        session_factory = _make_session_factory(upload, capture_writes=True)

        with patch(
            "services.chat_upload_tool._run_extraction",
            AsyncMock(return_value=extractor_result),
        ):
            with patch(
                "services.chat_upload_tool._get_confirms_used",
                AsyncMock(return_value=0),
            ):
                with patch(
                    "services.database.AsyncSessionLocal",
                    session_factory,
                ):
                    result = await forward_attachment_to_paperless(
                        {"attachment_id": 42},
                        mcp_manager=mcp, session_id="s", user_id=None,
                    )

        assert result["success"] is True
        assert result["data"]["action_required"] == "paperless_confirm"
        # Grab the persisted PaperlessPendingConfirm — user_id must be
        # None, not 0.
        writes = session_factory.captured_adds  # type: ignore[attr-defined]
        pending_rows = [w for w in writes if type(w).__name__ == "PaperlessPendingConfirm"]
        assert pending_rows, "Pending confirm row was not persisted"
        assert pending_rows[0].user_id is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_created_date_serialised_as_iso_string_for_json_columns(
        self, tmp_path,
    ):
        """Regression for prod 2026-04-24: ``forward_attachment_to_paperless``
        persisted ``post_fuzzy_output`` / ``llm_output`` via
        ``pydantic.model_dump()`` (default mode) — which leaves
        ``created_date`` as a ``datetime.date`` object. SQLAlchemy's JSON
        encoder can't handle it, the INSERT 500s with
        ``TypeError: Object of type date is not JSON serializable``,
        and the tool reports ``Forward to Paperless failed: …``.

        Fix: ``model_dump(mode="json")`` forces ISO-string serialisation
        of every date in the payload BEFORE it reaches the JSON column.
        This test asserts the persisted dict is JSON-serialisable.
        """
        import json
        from datetime import date
        import services.chat_upload_tool as cut_mod

        upload = _upload_stub(tmp_path, filename="rechnung.pdf", size=200_000)
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock()

        # Real pydantic model with a real date — so model_dump(mode="json")
        # matters. SimpleNamespace mocks would hide the bug.
        from services.paperless_metadata_extractor import (
            ExtractionResult, PaperlessMetadata,
        )
        extractor_result = ExtractionResult(
            metadata=PaperlessMetadata(
                title="Rechnung 2026",
                correspondent="Anthropic, PBC",
                document_type="Rechnung",
                tags=["Rechnung", "2026"],
                created_date=date(2026, 4, 24),
                new_entry_proposals=[],
            ),
            doc_text="doc",
            error=None,
        )

        session_factory = _make_session_factory(upload, capture_writes=True)

        with patch(
            "services.chat_upload_tool._run_extraction",
            AsyncMock(return_value=extractor_result),
        ), patch(
            "services.chat_upload_tool._get_confirms_used",
            AsyncMock(return_value=0),
        ), patch(
            "services.database.AsyncSessionLocal",
            session_factory,
        ):
            result = await forward_attachment_to_paperless(
                {"attachment_id": 42},
                mcp_manager=mcp, session_id="s", user_id=None,
            )

        assert result["success"] is True, (
            f"forward_attachment_to_paperless failed: {result}"
        )
        writes = session_factory.captured_adds  # type: ignore[attr-defined]
        pending_rows = [
            w for w in writes if type(w).__name__ == "PaperlessPendingConfirm"
        ]
        assert pending_rows, "Pending confirm row was not persisted"
        row = pending_rows[0]
        # Every dict that flows into a JSON column must round-trip through
        # json.dumps without a TypeError.
        for field_name in ("llm_output", "post_fuzzy_output", "proposals"):
            payload = getattr(row, field_name)
            try:
                json.dumps(payload)
            except TypeError as exc:
                pytest.fail(
                    f"{field_name} contains a non-JSON-serialisable value "
                    f"({exc}). Full payload: {payload!r}"
                )
        # Specifically: created_date is an ISO string, not a date object.
        assert row.post_fuzzy_output.get("created_date") == "2026-04-24"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_silent_upload_when_past_cold_start_cap(self, tmp_path):
        upload = _upload_stub(tmp_path)
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True, "message": '{"task_id": "t", "document_id": 99}',
        })

        extractor_result = SimpleNamespace(
            metadata=SimpleNamespace(
                title="T", correspondent="Stadtwerke",
                document_type="Rechnung", tags=["wohnung"],
                storage_path="/x", created_date=None,
                new_entry_proposals=[],
                model_dump=lambda **_kw: {
                    "title": "T", "correspondent": "Stadtwerke",
                    "document_type": "Rechnung", "tags": ["wohnung"],
                    "storage_path": "/x",
                },
            ),
            doc_text="doc",
            error=None,
        )

        with patch(
            "services.chat_upload_tool._run_extraction",
            AsyncMock(return_value=extractor_result),
        ):
            with patch(
                "services.chat_upload_tool._get_confirms_used",
                AsyncMock(return_value=_COLD_START_CONFIRM_N),  # Past cap
            ):
                with patch(
                    "services.database.AsyncSessionLocal",
                    _make_session_factory(upload),
                ):
                    result = await forward_attachment_to_paperless(
                        {"attachment_id": 42},
                        mcp_manager=mcp, session_id="s", user_id=1,
                    )

        # Silent upload — no confirm token in response.
        assert result["success"] is True
        assert result["action_taken"] is True
        assert "action_required" not in result.get("data", {})
        # Extraction fields were forwarded to the upload call.
        mcp.execute_tool.assert_awaited_once()
        call_params = mcp.execute_tool.await_args.args[1]
        assert call_params["correspondent"] == "Stadtwerke"
        assert call_params["storage_path"] == "/x"


class TestForwardAttachmentExtractionFailure:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_extractor_error_falls_back_to_bare_upload(self, tmp_path):
        upload = _upload_stub(tmp_path)
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True, "message": '{"task_id": "t"}',
        })

        # Extractor returns an error result — fallback path should
        # fire a bare upload and annotate the success message.
        extractor_result = SimpleNamespace(
            metadata=SimpleNamespace(
                title=None, correspondent=None, document_type=None,
                tags=[], storage_path=None, created_date=None,
                new_entry_proposals=[],
                model_dump=lambda **_kw: {},
            ),
            doc_text="",
            error="Konnte Dokument nicht lesen (OCR lieferte keinen Text).",
        )

        with patch(
            "services.chat_upload_tool._run_extraction",
            AsyncMock(return_value=extractor_result),
        ):
            with patch(
                "services.chat_upload_tool._get_confirms_used",
                AsyncMock(return_value=0),
            ):
                with patch(
                    "services.database.AsyncSessionLocal",
                    _make_session_factory(upload),
                ):
                    result = await forward_attachment_to_paperless(
                        {"attachment_id": 42},
                        mcp_manager=mcp, session_id="s", user_id=1,
                    )

        assert result["success"] is True
        assert "ohne Metadaten-Extraktion" in result["message"]
        # Upload DID fire, just without extracted fields.
        mcp.execute_tool.assert_awaited_once()


class TestForwardAttachmentValidation:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_missing_attachment_id_errors(self):
        result = await forward_attachment_to_paperless({}, mcp_manager=MagicMock())
        assert result["success"] is False
        assert "attachment_id" in result["message"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_non_integer_attachment_id_errors(self):
        result = await forward_attachment_to_paperless(
            {"attachment_id": "abc"}, mcp_manager=MagicMock(),
        )
        assert result["success"] is False
        assert "integer" in result["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_mcp_manager_errors(self):
        result = await forward_attachment_to_paperless(
            {"attachment_id": 1}, mcp_manager=None,
        )
        assert result["success"] is False
        assert "MCP" in result["message"]


# ===========================================================================
# Test helpers — DB session mocking
# ===========================================================================


def _make_session_with_upload(upload):
    """Build an AsyncSessionLocal-like async context manager that returns
    a DB session whose execute() pipeline yields ``upload`` for
    ChatUpload lookups."""
    session = AsyncMock()

    def _execute_result(query):
        scalars = MagicMock()
        scalars.scalar_one_or_none = MagicMock(return_value=upload)
        return scalars

    session.execute = AsyncMock(side_effect=lambda q: _execute_result(q))
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.get = AsyncMock(return_value=upload)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_session_factory(upload, *, capture_writes: bool = False):
    """Return a patchable replacement for AsyncSessionLocal."""
    captured_adds: list = []

    def _factory():
        session = _make_session_with_upload(upload)
        if capture_writes:
            def _capture_add(obj):
                captured_adds.append(obj)
            session.add = MagicMock(side_effect=_capture_add)
        return session

    _factory.captured_adds = captured_adds
    return _factory
