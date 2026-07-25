"""
Tests for Paperless Document Audit Service.

Tests:
- OCR quality heuristics (static method, pure functions)
- Missing fields heuristic
- Content completeness heuristic
- LLM response parsing
- MCP result parsing
- Result-to-dict conversion (V1 + V2 fields)
- Service start/stop behavior
- Integration-style tests with mocked MCP + DB
- Audit run logic
- Apply/skip/reprocess operations (V1 + V2 fields)
- Duplicate detection
- Correspondent normalization
- API route handler behavior
"""

import base64
import json
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_glue.services.paperless_audit_service import PaperlessAuditService


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_mcp_manager():
    """Mock MCPManager for audit service tests."""
    manager = MagicMock()
    manager.has_server = MagicMock(return_value=True)
    manager.execute_tool = AsyncMock(return_value={"success": True, "message": "{}"})
    return manager


@pytest.fixture
def mock_db_factory():
    """Return an async context manager that yields a mock session."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()

    class _FakeCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    factory = MagicMock(return_value=_FakeCtx())
    factory._mock_session = mock_session  # expose for assertions
    return factory


@pytest.fixture
def service(mock_mcp_manager, mock_db_factory):
    """PaperlessAuditService with mocked dependencies."""
    return PaperlessAuditService(mock_mcp_manager, mock_db_factory)


# ============================================================================
# OCR Quality Heuristics
# ============================================================================


class TestCheckOcrQuality:
    """Test _check_ocr_quality static method — pure heuristic, no I/O."""

    @pytest.mark.unit
    def test_good_text(self):
        """Normal readable text should get quality 5."""
        text = (
            "Sehr geehrte Damen und Herren,\n\n"
            "hiermit sende ich Ihnen die angeforderten Unterlagen.\n\n"
            "Mit freundlichen Gruessen\nMax Mustermann"
        )
        score, issues = PaperlessAuditService._check_ocr_quality(text)
        assert score == 5
        assert issues == "OK"

    @pytest.mark.unit
    def test_empty_text(self):
        """Empty content should get quality 1."""
        score, issues = PaperlessAuditService._check_ocr_quality("")
        assert score == 1
        assert "minimal" in issues.lower() or "no" in issues.lower()

    @pytest.mark.unit
    def test_none_text(self):
        """None content should get quality 1."""
        score, issues = PaperlessAuditService._check_ocr_quality(None)
        assert score == 1

    @pytest.mark.unit
    def test_minimal_text(self):
        """Very short text (<20 chars) should get quality 1."""
        score, issues = PaperlessAuditService._check_ocr_quality("abc")
        assert score == 1
        assert "minimal" in issues.lower() or "no" in issues.lower()

    @pytest.mark.unit
    def test_garbled_no_spaces(self):
        """Garbled text with very few spaces should be detected."""
        # Long string with almost no spaces
        text = "abcdefghijklmnopqrstuvwxyz" * 10
        score, issues = PaperlessAuditService._check_ocr_quality(text)
        assert score < 5
        assert "spaces" in issues.lower() or "garbled" in issues.lower()

    @pytest.mark.unit
    def test_repeated_characters_not_flagged(self):
        """The 'Repeated characters' rule was removed (0 true positives on the
        real corpus — only redaction masks and zero-padding). A same-char run
        in otherwise-clean text now scores 5."""
        text = "Normal text here. Then aaaaaaaaa and more normal text follows after that section."
        score, issues = PaperlessAuditService._check_ocr_quality(text)
        assert score == 5
        assert "repeated" not in issues.lower()

    @pytest.mark.unit
    def test_high_special_chars(self):
        """Text with high special character ratio should be detected."""
        text = "!!@@##$$%%^^&&**(()){{}}||\\//~~``" * 5
        score, issues = PaperlessAuditService._check_ocr_quality(text)
        assert score < 5
        assert "special" in issues.lower()

    @pytest.mark.unit
    def test_fragmented_short_lines(self):
        """Text with many very short lines should be detected."""
        text = "\n".join(["ab cd ef"] * 20)
        score, issues = PaperlessAuditService._check_ocr_quality(text)
        assert score < 5
        assert "fragmented" in issues.lower() or "short" in issues.lower()

    @pytest.mark.unit
    def test_multiple_issues_lower_score(self):
        """Multiple OCR issues should stack and lower the score.

        All-symbol garbage trips both "garbled" (no spaces) and "high special
        char ratio" => two stacked issues => 3. There is no "Repeated
        characters" rule (removed: 0 true positives on the real corpus).
        """
        text = "!!!!!!@@@@@$$$$$%%%%%^^^^^^" * 5
        score, issues = PaperlessAuditService._check_ocr_quality(text)
        assert score <= 3
        assert "special" in issues.lower()
        assert "spaces" in issues.lower() or "garbled" in issues.lower()

    @pytest.mark.unit
    def test_score_never_below_one(self):
        """Score should never go below 1."""
        text = "!@#$%^" * 50  # many issues
        score, _ = PaperlessAuditService._check_ocr_quality(text)
        assert score >= 1

    @pytest.mark.unit
    def test_normal_space_ratio(self):
        """Text with normal space ratio should not trigger garbled warning."""
        text = "Dies ist ein ganz normaler deutscher Text mit Leerzeichen."
        score, issues = PaperlessAuditService._check_ocr_quality(text)
        assert "garbled" not in issues.lower()


# ============================================================================
# LLM Response Parsing
# ============================================================================


class TestParseLlmResponse:
    """Test _parse_llm_response static method."""

    @pytest.mark.unit
    def test_valid_json(self):
        """Valid JSON should be parsed correctly."""
        data = {
            "suggested_title": "Rechnung 2024",
            "changes_needed": True,
            "confidence": 0.85,
        }
        result = PaperlessAuditService._parse_llm_response(json.dumps(data))
        assert result["suggested_title"] == "Rechnung 2024"
        assert result["changes_needed"] is True
        assert result["confidence"] == 0.85

    @pytest.mark.unit
    def test_markdown_code_block(self):
        """JSON wrapped in ```json ... ``` should be extracted."""
        raw = '```json\n{"suggested_title": "Invoice", "changes_needed": false}\n```'
        result = PaperlessAuditService._parse_llm_response(raw)
        assert result is not None
        assert result["suggested_title"] == "Invoice"
        assert result["changes_needed"] is False

    @pytest.mark.unit
    def test_markdown_code_block_no_lang(self):
        """JSON wrapped in ``` ... ``` without language tag."""
        raw = '```\n{"suggested_title": "Test", "changes_needed": true}\n```'
        result = PaperlessAuditService._parse_llm_response(raw)
        assert result is not None
        assert result["suggested_title"] == "Test"

    @pytest.mark.unit
    def test_json_with_surrounding_text(self):
        """JSON embedded in surrounding LLM chatter should be extracted."""
        raw = 'Here is my analysis:\n{"changes_needed": true, "confidence": 0.9}\nHope that helps!'
        result = PaperlessAuditService._parse_llm_response(raw)
        assert result is not None
        assert result["changes_needed"] is True

    @pytest.mark.unit
    def test_invalid_json(self):
        """Garbage input should return None."""
        result = PaperlessAuditService._parse_llm_response("this is not json at all")
        assert result is None

    @pytest.mark.unit
    def test_empty_string(self):
        """Empty string should return None."""
        result = PaperlessAuditService._parse_llm_response("")
        assert result is None

    @pytest.mark.unit
    def test_partial_json(self):
        """Truncated JSON should return None."""
        result = PaperlessAuditService._parse_llm_response('{"title": "test", "changes')
        assert result is None

    @pytest.mark.unit
    def test_whitespace_padding(self):
        """JSON with whitespace padding should parse fine."""
        raw = '   \n  {"changes_needed": false}  \n  '
        result = PaperlessAuditService._parse_llm_response(raw)
        assert result is not None
        assert result["changes_needed"] is False


# ============================================================================
# MCP Result Parsing
# ============================================================================


class TestParseMcpResult:
    """Test _parse_mcp_result static method."""

    @pytest.mark.unit
    def test_valid_inner_json(self):
        """Valid inner JSON in message field should be parsed."""
        result = {
            "success": True,
            "message": json.dumps({"results": [{"id": 1}, {"id": 2}]}),
        }
        parsed = PaperlessAuditService._parse_mcp_result(result)
        assert parsed is not None
        assert len(parsed["results"]) == 2

    @pytest.mark.unit
    def test_empty_message(self):
        """Empty message should return None."""
        result = {"success": True, "message": ""}
        parsed = PaperlessAuditService._parse_mcp_result(result)
        assert parsed is None

    @pytest.mark.unit
    def test_missing_message_key(self):
        """Missing message key should return None."""
        result = {"success": True}
        parsed = PaperlessAuditService._parse_mcp_result(result)
        assert parsed is None

    @pytest.mark.unit
    def test_non_json_message(self):
        """Non-JSON message string should return None."""
        result = {"success": True, "message": "Not JSON content"}
        parsed = PaperlessAuditService._parse_mcp_result(result)
        assert parsed is None

    @pytest.mark.unit
    def test_none_message(self):
        """None message should return None."""
        result = {"success": True, "message": None}
        parsed = PaperlessAuditService._parse_mcp_result(result)

    @pytest.mark.unit
    def test_truncated_response_with_suffix(self):
        """Truncated JSON with appended text suffix should be recovered."""
        inner_json = json.dumps({"results": [{"id": 1}, {"id": 2}]})
        truncated = inner_json + '\n[... Showing 2 of 500 results]'
        result = {"success": True, "message": truncated}
        parsed = PaperlessAuditService._parse_mcp_result(result)
        assert parsed is not None
        assert len(parsed["results"]) == 2

    @pytest.mark.unit
    def test_truncated_response_unrecoverable(self):
        """Badly truncated JSON that can't be recovered should return None."""
        result = {"success": True, "message": '{"results": [{"id": 1}, {"id"'}
        parsed = PaperlessAuditService._parse_mcp_result(result)
        assert parsed is None
        assert parsed is None


# ============================================================================
# Result-to-Dict Conversion
# ============================================================================


class TestResultToDict:
    """Test _result_to_dict static method."""

    @pytest.mark.unit
    def test_full_result(self):
        """All fields should be present in the dict."""
        mock_result = MagicMock()
        mock_result.id = 42
        mock_result.paperless_doc_id = 100
        mock_result.current_title = "Old Title"
        mock_result.current_correspondent = "Company A"
        mock_result.current_document_type = "Invoice"
        mock_result.current_tags = ["finance", "2024"]
        mock_result.current_date = "2024-01-15"
        mock_result.current_storage_path = "invoices/2024"
        mock_result.current_custom_fields = [{"field": 1, "value": "100"}]
        mock_result.suggested_title = "New Title"
        mock_result.suggested_correspondent = "Company B"
        mock_result.suggested_document_type = "Receipt"
        mock_result.suggested_tags = ["finance", "receipt"]
        mock_result.suggested_date = "2024-01-20"
        mock_result.suggested_storage_path = "receipts/2024"
        mock_result.suggested_custom_fields = {"Amount": "100"}
        mock_result.detected_language = "de"
        mock_result.missing_fields = ["tags"]
        mock_result.duplicate_group_id = "abc123"
        mock_result.duplicate_score = 0.95
        mock_result.content_completeness = 4
        mock_result.completeness_issues = "OK"
        mock_result.content_hash = "abc123def456"
        mock_result.ocr_quality = 4
        mock_result.ocr_issues = "OK"
        mock_result.confidence = 0.92
        mock_result.changes_needed = True
        mock_result.reasoning = "Title is wrong"
        mock_result.status = "pending"
        mock_result.audited_at = datetime(2024, 6, 15, 10, 30, 0)
        mock_result.applied_at = None
        mock_result.audit_run_id = "run-abc-123"

        d = PaperlessAuditService._result_to_dict(mock_result)

        assert d["id"] == 42
        assert d["paperless_doc_id"] == 100
        assert d["current_title"] == "Old Title"
        assert d["suggested_title"] == "New Title"
        assert d["current_date"] == "2024-01-15"
        assert d["suggested_date"] == "2024-01-20"
        assert d["current_storage_path"] == "invoices/2024"
        assert d["suggested_storage_path"] == "receipts/2024"
        assert d["current_custom_fields"] == [{"field": 1, "value": "100"}]
        assert d["suggested_custom_fields"] == {"Amount": "100"}
        assert d["detected_language"] == "de"
        assert d["missing_fields"] == ["tags"]
        assert d["duplicate_group_id"] == "abc123"
        assert d["duplicate_score"] == 0.95
        assert d["content_completeness"] == 4
        assert d["completeness_issues"] == "OK"
        assert d["content_hash"] == "abc123def456"
        assert d["ocr_quality"] == 4
        assert d["confidence"] == 0.92
        assert d["changes_needed"] is True
        assert d["status"] == "pending"
        assert d["audited_at"] == "2024-06-15T10:30:00"
        assert d["applied_at"] is None
        assert d["audit_run_id"] == "run-abc-123"

    @pytest.mark.unit
    def test_none_timestamps(self):
        """Both timestamps None should produce None in dict."""
        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.paperless_doc_id = 1
        mock_result.current_title = None
        mock_result.current_correspondent = None
        mock_result.current_document_type = None
        mock_result.current_tags = None
        mock_result.current_date = None
        mock_result.current_storage_path = None
        mock_result.current_custom_fields = None
        mock_result.suggested_title = None
        mock_result.suggested_correspondent = None
        mock_result.suggested_document_type = None
        mock_result.suggested_tags = None
        mock_result.suggested_date = None
        mock_result.suggested_storage_path = None
        mock_result.suggested_custom_fields = None
        mock_result.detected_language = None
        mock_result.missing_fields = None
        mock_result.duplicate_group_id = None
        mock_result.duplicate_score = None
        mock_result.content_completeness = None
        mock_result.completeness_issues = None
        mock_result.content_hash = None
        mock_result.ocr_quality = None
        mock_result.ocr_issues = None
        mock_result.confidence = None
        mock_result.changes_needed = False
        mock_result.reasoning = None
        mock_result.status = "pending"
        mock_result.audited_at = None
        mock_result.applied_at = None
        mock_result.audit_run_id = None

        d = PaperlessAuditService._result_to_dict(mock_result)
        assert d["audited_at"] is None
        assert d["applied_at"] is None
        assert d["current_date"] is None
        assert d["detected_language"] is None


# ============================================================================
# Service Start/Stop
# ============================================================================


class TestServiceStart:
    """Test service start behavior."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_start_without_paperless_mcp(self, mock_db_factory):
        """start() should return silently when Paperless MCP is not configured."""
        manager = MagicMock()
        manager.has_server = MagicMock(return_value=False)

        svc = PaperlessAuditService(manager, mock_db_factory)
        await svc.start()

        # No execute_tool call should happen
        manager.execute_tool = AsyncMock()
        manager.execute_tool.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_start_with_paperless_mcp(self, service, mock_mcp_manager):
        """start() should verify connectivity via search_documents."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": json.dumps({"results": []}),
        }
        await service.start()
        mock_mcp_manager.execute_tool.assert_called_once_with(
            "mcp.paperless.search_documents", {"max_results": 1}
        )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_start_connectivity_failure(self, service, mock_mcp_manager):
        """start() should handle MCP connectivity failure gracefully."""
        mock_mcp_manager.execute_tool.return_value = {"success": False}
        await service.start()
        # Should not raise

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_start_exception(self, service, mock_mcp_manager):
        """start() should catch exceptions during connectivity check."""
        mock_mcp_manager.execute_tool.side_effect = Exception("Connection refused")
        await service.start()
        # Should not raise

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_stop(self, service):
        """stop() should set running to False."""
        service._running = True
        await service.stop()
        assert service._running is False


# ============================================================================
# Get Status
# ============================================================================


class TestGetStatus:
    """Test get_status method."""

    @pytest.mark.unit
    def test_initial_status(self, service):
        """Initial status should show not running, zero progress."""
        status = service.get_status()
        assert status["running"] is False
        assert status["progress"] == 0
        assert status["total"] == 0
        assert status["current_doc_id"] is None

    @pytest.mark.unit
    def test_running_status(self, service):
        """Status during an active audit."""
        service._running = True
        service._progress = {"current": 5, "total": 10, "current_doc_id": 42}
        status = service.get_status()
        assert status["running"] is True
        assert status["progress"] == 5
        assert status["total"] == 10
        assert status["current_doc_id"] == 42


# ============================================================================
# Fetch All Doc IDs
# ============================================================================


class TestFetchAllDocIds:
    """Test _fetch_all_doc_ids method with date-based pagination."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_single_page_fetch(self, service, mock_mcp_manager):
        """Should extract IDs when all fit in one page."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": json.dumps({
                "summary": {"total_matching": 3},
                "results": [
                    {"id": 1, "created": "2024-01-03"},
                    {"id": 2, "created": "2024-01-02"},
                    {"id": 3, "created": "2024-01-01"},
                ],
            }),
        }
        ids = await service._fetch_all_doc_ids()
        assert ids == [1, 2, 3]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_multi_page_pagination(self, service, mock_mcp_manager):
        """Should paginate using created_before when results span multiple pages."""
        page1 = {
            "success": True,
            "message": json.dumps({
                "summary": {"total_matching": 4},
                "results": [
                    {"id": 1, "created": "2024-01-04"},
                    {"id": 2, "created": "2024-01-03"},
                ],
            }),
        }
        page2 = {
            "success": True,
            "message": json.dumps({
                "summary": {"total_matching": 4},
                "results": [
                    {"id": 3, "created": "2024-01-02"},
                    {"id": 4, "created": "2024-01-01"},
                ],
            }),
        }
        mock_mcp_manager.execute_tool.side_effect = [page1, page2]
        ids = await service._fetch_all_doc_ids()
        assert ids == [1, 2, 3, 4]
        # Second call should include created_before from oldest date of page1
        call_args = mock_mcp_manager.execute_tool.call_args_list
        assert call_args[1][0][1]["created_before"] == "2024-01-03"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_failed_fetch(self, service, mock_mcp_manager):
        """Should return empty list on MCP failure."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": False,
            "message": "Server error",
        }
        ids = await service._fetch_all_doc_ids()
        assert ids == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_results(self, service, mock_mcp_manager):
        """Should return empty list when no documents found."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": json.dumps({"summary": {"total_matching": 0}, "results": []}),
        }
        ids = await service._fetch_all_doc_ids()
        assert ids == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_missing_id_field(self, service, mock_mcp_manager):
        """Documents without 'id' should be skipped."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": json.dumps({
                "summary": {"total_matching": 3},
                "results": [
                    {"id": 1, "created": "2024-01-03"},
                    {"title": "no-id", "created": "2024-01-02"},
                    {"id": 3, "created": "2024-01-01"},
                ],
            }),
        }
        ids = await service._fetch_all_doc_ids()
        assert ids == [1, 3]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unparseable_message(self, service, mock_mcp_manager):
        """Unparseable MCP message should return empty list."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": "not json",
        }
        ids = await service._fetch_all_doc_ids()
        assert ids == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_deduplication(self, service, mock_mcp_manager):
        """Should deduplicate IDs across pages."""
        page1 = {
            "success": True,
            "message": json.dumps({
                "summary": {"total_matching": 3},
                "results": [
                    {"id": 1, "created": "2024-01-03"},
                    {"id": 2, "created": "2024-01-02"},
                ],
            }),
        }
        # Page 2 overlaps with page 1 (id=2 appears again)
        page2 = {
            "success": True,
            "message": json.dumps({
                "summary": {"total_matching": 3},
                "results": [
                    {"id": 2, "created": "2024-01-02"},
                    {"id": 3, "created": "2024-01-01"},
                ],
            }),
        }
        mock_mcp_manager.execute_tool.side_effect = [page1, page2]
        ids = await service._fetch_all_doc_ids()
        assert ids == [1, 2, 3]


# ============================================================================
# Fetch Available Metadata
# ============================================================================


class TestFetchAvailableMetadata:
    """Test _fetch_available_metadata method."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_extracts_metadata(self, service, mock_mcp_manager):
        """Should extract unique types, correspondents, tags from results."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": json.dumps({
                "results": [
                    {
                        "document_type": "Invoice",
                        "correspondent": "Company A",
                        "tags": ["finance", "2024"],
                    },
                    {
                        "document_type": "Receipt",
                        "correspondent": "Company A",
                        "tags": ["finance"],
                    },
                ],
                "summary": {
                    "top_correspondents": [{"name": "Company B"}],
                    "top_document_types": [{"name": "Letter"}],
                },
            }),
        }
        meta = await service._fetch_available_metadata()
        assert "Invoice" in meta["types"]
        assert "Receipt" in meta["types"]
        assert "Letter" in meta["types"]
        assert "Company A" in meta["correspondents"]
        assert "Company B" in meta["correspondents"]
        assert "finance" in meta["tags"]
        assert "2024" in meta["tags"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_fetches_storage_paths_and_custom_fields(self, service, mock_mcp_manager):
        """Should also fetch storage paths and custom field definitions."""
        search_response = {
            "success": True,
            "message": json.dumps({
                "results": [{"document_type": "Invoice", "correspondent": "A", "tags": ["x"]}],
                "summary": {},
            }),
        }
        sp_response = {
            "success": True,
            "message": json.dumps({"paths": [{"id": 1, "path": "invoices/"}]}),
        }
        cf_response = {
            "success": True,
            "message": json.dumps({"fields": [{"id": 1, "name": "Amount", "data_type": "monetary"}]}),
        }
        mock_mcp_manager.execute_tool.side_effect = [search_response, sp_response, cf_response]

        meta = await service._fetch_available_metadata()
        assert "invoices/" in meta["storage_paths"]
        assert len(meta["custom_fields"]) == 1
        assert meta["custom_fields"][0]["name"] == "Amount"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_results(self, service, mock_mcp_manager):
        """Should return empty lists when no metadata found."""
        search_response = {
            "success": True,
            "message": json.dumps({"results": []}),
        }
        # Storage paths and custom fields also return empty
        sp_response = {"success": True, "message": json.dumps({"paths": []})}
        cf_response = {"success": True, "message": json.dumps({"fields": []})}
        mock_mcp_manager.execute_tool.side_effect = [search_response, sp_response, cf_response]

        meta = await service._fetch_available_metadata()
        assert meta["types"] == []
        assert meta["correspondents"] == []
        assert meta["tags"] == []
        assert meta["storage_paths"] == []
        assert meta["custom_fields"] == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unparseable_falls_back(self, service, mock_mcp_manager):
        """Should return empty lists when MCP result is unparseable."""
        mock_mcp_manager.execute_tool.return_value = {
            "success": True,
            "message": "not json",
        }
        meta = await service._fetch_available_metadata()
        assert meta["types"] == []
        assert meta["correspondents"] == []
        assert meta["tags"] == []
        assert meta["storage_paths"] == []
        assert meta["custom_fields"] == []


# ============================================================================
# Apply Fix
# ============================================================================


class TestApplyFix:
    """Test _apply_fix method."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_apply_fix_with_changes(self, service, mock_mcp_manager, mock_db_factory):
        """Should call MCP update_document when changes exist."""
        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.paperless_doc_id = 42
        mock_result.current_title = "Old Title"
        mock_result.suggested_title = "New Title"
        mock_result.current_correspondent = "A"
        mock_result.suggested_correspondent = "B"
        mock_result.current_document_type = "Invoice"
        mock_result.suggested_document_type = "Invoice"  # same — no change
        mock_result.current_tags = ["tag1"]
        mock_result.suggested_tags = ["tag1"]  # same — no change
        mock_result.current_date = "2024-01-15"
        mock_result.suggested_date = "2024-01-15"  # same — no change
        mock_result.current_storage_path = None
        mock_result.suggested_storage_path = None
        mock_result.suggested_custom_fields = None

        # _apply_fix now resolve-or-creates the correspondent before updating: "B"
        # is not in the existing taxonomy (only "A"), so it is auto-created and its
        # created name flows into the update. update_document stays the last call.
        async def _execute(tool, params, **_kw):
            if tool == "mcp.paperless.list_correspondents":
                return {"success": True, "message": json.dumps({"items": [{"name": "A"}]})}
            if tool == "mcp.paperless.create_correspondent":
                return {"success": True, "message": json.dumps({"id": 99, "name": params["name"]})}
            return {"success": True}

        mock_mcp_manager.execute_tool.side_effect = _execute

        # Mock DB lookup for status update
        mock_session = mock_db_factory._mock_session
        mock_db_result = MagicMock()
        mock_scalar = MagicMock(return_value=mock_db_result)
        mock_session.execute.return_value = MagicMock(scalar_one_or_none=mock_scalar)

        success = await service._apply_fix(mock_result)

        assert success is True
        # Verify MCP was called with the changed fields
        call_args = mock_mcp_manager.execute_tool.call_args
        assert call_args[0][0] == "mcp.paperless.update_document"
        params = call_args[0][1]
        assert params["document_id"] == 42
        assert params["title"] == "New Title"
        assert params["correspondent"] == "B"  # resolve-or-created name
        assert "document_type" not in params  # unchanged
        assert "tags" not in params  # unchanged
        assert "created_date" not in params  # unchanged

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_apply_fix_autocreates_document_type_and_tags(
        self, service, mock_mcp_manager, mock_db_factory
    ):
        """A genuinely-new document_type/tag is resolve-or-created and its created
        name flows into the update — the taxonomy-autocreate folded onto the audit's
        apply path (consolidation). update_document stays the last call."""
        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.paperless_doc_id = 42
        mock_result.current_title = "Same"
        mock_result.suggested_title = "Same"
        mock_result.current_correspondent = None
        mock_result.suggested_correspondent = None
        mock_result.current_document_type = "Invoice"
        mock_result.suggested_document_type = "Contract"  # new type — not in taxonomy
        mock_result.current_tags = ["old"]
        mock_result.suggested_tags = ["urgent"]  # new tag — not in taxonomy
        mock_result.current_date = None
        mock_result.suggested_date = None
        mock_result.current_storage_path = None
        mock_result.suggested_storage_path = None
        mock_result.suggested_custom_fields = None

        created: dict[str, str] = {}

        async def _execute(tool, params, **_kw):
            if tool == "mcp.paperless.list_document_types":
                return {"success": True, "message": json.dumps({"items": [{"name": "Invoice"}]})}
            if tool == "mcp.paperless.list_tags":
                return {"success": True, "message": json.dumps({"items": [{"name": "old"}]})}
            if tool == "mcp.paperless.create_document_type":
                created["document_type"] = params["name"]
                return {"success": True, "message": json.dumps({"id": 7, "name": params["name"]})}
            if tool == "mcp.paperless.create_tag":
                created["tag"] = params["name"]
                return {"success": True, "message": json.dumps({"id": 8, "name": params["name"]})}
            return {"success": True}

        mock_mcp_manager.execute_tool.side_effect = _execute

        mock_session = mock_db_factory._mock_session
        mock_db_result = MagicMock()
        mock_scalar = MagicMock(return_value=mock_db_result)
        mock_session.execute.return_value = MagicMock(scalar_one_or_none=mock_scalar)

        success = await service._apply_fix(mock_result)

        assert success is True
        assert created == {"document_type": "Contract", "tag": "urgent"}
        params = mock_mcp_manager.execute_tool.call_args[0][1]  # last call = update_document
        assert params["document_type"] == "Contract"
        assert params["tags"] == ["urgent"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_apply_fix_returns_false_on_resolve_failure_not_silent_success(
        self, service, mock_mcp_manager, mock_db_factory
    ):
        """A transport/create FAILURE while resolving a taxonomy value must NOT be
        reported as an applied fix — _apply_fix returns False so the row stays
        pending for retry (distinct from a guardrail-skip, which is a success)."""
        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.paperless_doc_id = 42
        mock_result.current_title = "Same"
        mock_result.suggested_title = "Same"
        mock_result.current_correspondent = "A"
        mock_result.suggested_correspondent = "B"  # only change; create will fail
        mock_result.current_document_type = None
        mock_result.suggested_document_type = None
        mock_result.current_tags = None
        mock_result.suggested_tags = None
        mock_result.current_date = None
        mock_result.suggested_date = None
        mock_result.current_storage_path = None
        mock_result.suggested_storage_path = None
        mock_result.suggested_custom_fields = None

        update_called = []

        async def _execute(tool, params, **_kw):
            if tool == "mcp.paperless.list_correspondents":
                return {"success": True, "message": json.dumps({"items": [{"name": "A"}]})}
            if tool == "mcp.paperless.create_correspondent":
                # transport/create failure — NOT already_exists, no id
                return {"success": True, "message": json.dumps({"error": "boom"})}
            if tool == "mcp.paperless.update_document":
                update_called.append(params)
                return {"success": True, "message": "{}"}
            return {"success": True, "message": "{}"}

        mock_mcp_manager.execute_tool.side_effect = _execute

        mock_session = mock_db_factory._mock_session
        mock_db_result = MagicMock()
        mock_scalar = MagicMock(return_value=mock_db_result)
        mock_session.execute.return_value = MagicMock(scalar_one_or_none=mock_scalar)

        success = await service._apply_fix(mock_result)

        assert success is False  # NOT a silent success
        assert update_called == []  # no partial write

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_apply_fix_with_v2_fields(self, service, mock_mcp_manager, mock_db_factory):
        """Should include date, storage_path, custom_fields in MCP call."""
        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.paperless_doc_id = 42
        mock_result.current_title = "Same"
        mock_result.suggested_title = "Same"
        mock_result.current_correspondent = None
        mock_result.suggested_correspondent = None
        mock_result.current_document_type = None
        mock_result.suggested_document_type = None
        mock_result.current_tags = None
        mock_result.suggested_tags = None
        mock_result.current_date = "2024-01-15"
        mock_result.suggested_date = "2024-02-20"  # changed
        mock_result.current_storage_path = None
        mock_result.suggested_storage_path = "invoices/2024"  # new
        mock_result.suggested_custom_fields = {"Amount": "99.50"}  # new

        mock_mcp_manager.execute_tool.return_value = {"success": True}

        mock_session = mock_db_factory._mock_session
        mock_db_result = MagicMock()
        mock_scalar = MagicMock(return_value=mock_db_result)
        mock_session.execute.return_value = MagicMock(scalar_one_or_none=mock_scalar)

        success = await service._apply_fix(mock_result)

        assert success is True
        params = mock_mcp_manager.execute_tool.call_args[0][1]
        assert params["created_date"] == "2024-02-20"
        assert params["storage_path"] == "invoices/2024"
        assert params["custom_fields"] == {"Amount": "99.50"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_apply_fix_no_changes(self, service, mock_mcp_manager):
        """Should skip MCP call when all suggested values match current."""
        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.paperless_doc_id = 42
        mock_result.current_title = "Same"
        mock_result.suggested_title = "Same"
        mock_result.current_correspondent = "Same"
        mock_result.suggested_correspondent = "Same"
        mock_result.current_document_type = "Same"
        mock_result.suggested_document_type = "Same"
        mock_result.current_tags = ["a"]
        mock_result.suggested_tags = ["a"]
        mock_result.current_date = "2024-01-15"
        mock_result.suggested_date = "2024-01-15"
        mock_result.current_storage_path = "path/a"
        mock_result.suggested_storage_path = "path/a"
        mock_result.suggested_custom_fields = None

        result = await service._apply_fix(mock_result)

        assert result is True
        mock_mcp_manager.execute_tool.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_apply_fix_none_suggested(self, service, mock_mcp_manager):
        """Should skip when suggested values are None."""
        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.paperless_doc_id = 42
        mock_result.current_title = "Title"
        mock_result.suggested_title = None
        mock_result.current_correspondent = "Corp"
        mock_result.suggested_correspondent = None
        mock_result.current_document_type = "Invoice"
        mock_result.suggested_document_type = None
        mock_result.current_tags = ["a"]
        mock_result.suggested_tags = None
        mock_result.current_date = "2024-01-15"
        mock_result.suggested_date = None
        mock_result.current_storage_path = "path"
        mock_result.suggested_storage_path = None
        mock_result.suggested_custom_fields = None

        result = await service._apply_fix(mock_result)

        assert result is True
        mock_mcp_manager.execute_tool.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_apply_fix_mcp_failure(self, service, mock_mcp_manager, mock_db_factory):
        """Should set status to 'failed' on MCP error."""
        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.paperless_doc_id = 42
        mock_result.current_title = "Old"
        mock_result.suggested_title = "New"
        mock_result.current_correspondent = None
        mock_result.suggested_correspondent = None
        mock_result.current_document_type = None
        mock_result.suggested_document_type = None
        mock_result.current_tags = None
        mock_result.suggested_tags = None
        mock_result.current_date = None
        mock_result.suggested_date = None
        mock_result.current_storage_path = None
        mock_result.suggested_storage_path = None
        mock_result.suggested_custom_fields = None

        mock_mcp_manager.execute_tool.return_value = {
            "success": False,
            "message": "Permission denied",
        }

        mock_session = mock_db_factory._mock_session
        mock_db_result = MagicMock()
        mock_scalar = MagicMock(return_value=mock_db_result)
        mock_session.execute.return_value = MagicMock(scalar_one_or_none=mock_scalar)

        success = await service._apply_fix(mock_result)

        assert success is False
        assert mock_db_result.status == "failed"


# ============================================================================
# Run Audit
# ============================================================================


class TestRunAudit:
    """Test run_audit method."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_already_running(self, service):
        """run_audit_background should return 'already_running' when audit is in progress."""
        service._running = True
        result = await service.run_audit_background()
        assert result == "already_running"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_with_specific_doc_ids(self, service, mock_mcp_manager):
        """Should use provided document_ids instead of fetching all."""
        with patch.object(service, "_fetch_all_doc_ids", new_callable=AsyncMock) as mock_fetch, \
             patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta, \
             patch.object(service, "_analyze_document", new_callable=AsyncMock) as mock_analyze, \
             patch("ha_glue.services.paperless_audit_service.settings") as mock_settings:

            mock_settings.paperless_audit_fix_mode = "review"
            mock_settings.paperless_audit_confidence_threshold = 0.8
            mock_settings.paperless_audit_batch_delay = 0

            mock_meta.return_value = {"types": [], "correspondents": [], "tags": []}

            mock_result = MagicMock()
            mock_result.changes_needed = False
            mock_analyze.return_value = mock_result

            result = await service.run_audit(document_ids=[10, 20])

            mock_fetch.assert_not_called()
            assert result["total"] == 2
            assert result["processed"] == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_changes_found_counted(self, service):
        """Should count documents with changes_needed."""
        with patch.object(service, "_fetch_all_doc_ids", new_callable=AsyncMock) as mock_fetch, \
             patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta, \
             patch.object(service, "_analyze_document", new_callable=AsyncMock) as mock_analyze, \
             patch("ha_glue.services.paperless_audit_service.settings") as mock_settings:

            mock_settings.paperless_audit_fix_mode = "review"
            mock_settings.paperless_audit_confidence_threshold = 0.8
            mock_settings.paperless_audit_batch_delay = 0

            mock_fetch.return_value = [1, 2, 3]
            mock_meta.return_value = {"types": [], "correspondents": [], "tags": []}

            results = []
            for i, needs_changes in enumerate([True, False, True]):
                r = MagicMock()
                r.changes_needed = needs_changes
                r.confidence = 0.9
                results.append(r)

            mock_analyze.side_effect = results

            result = await service.run_audit(mode="full")

            assert result["changes_found"] == 2
            assert result["processed"] == 3

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_auto_all_fix_mode(self, service):
        """auto_all mode should apply fixes for all changed documents."""
        with patch.object(service, "_fetch_all_doc_ids", new_callable=AsyncMock) as mock_fetch, \
             patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta, \
             patch.object(service, "_analyze_document", new_callable=AsyncMock) as mock_analyze, \
             patch.object(service, "_apply_fix", new_callable=AsyncMock) as mock_fix, \
             patch("ha_glue.services.paperless_audit_service.settings") as mock_settings:

            mock_settings.paperless_audit_fix_mode = "review"
            mock_settings.paperless_audit_confidence_threshold = 0.8
            mock_settings.paperless_audit_batch_delay = 0

            mock_fetch.return_value = [1]
            mock_meta.return_value = {"types": [], "correspondents": [], "tags": []}

            mock_result = MagicMock()
            mock_result.changes_needed = True
            mock_result.confidence = 0.9
            mock_analyze.return_value = mock_result

            await service.run_audit(mode="full", fix_mode="auto_all")

            # _apply_fix now also receives the batch-fetched taxonomy kwarg; assert
            # on the positional result arg and ignore the taxonomy value.
            mock_fix.assert_called_once()
            assert mock_fix.call_args.args == (mock_result,)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_auto_threshold_below(self, service):
        """auto_threshold should NOT apply when confidence is below threshold."""
        with patch.object(service, "_fetch_all_doc_ids", new_callable=AsyncMock) as mock_fetch, \
             patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta, \
             patch.object(service, "_analyze_document", new_callable=AsyncMock) as mock_analyze, \
             patch.object(service, "_apply_fix", new_callable=AsyncMock) as mock_fix, \
             patch("ha_glue.services.paperless_audit_service.settings") as mock_settings:

            mock_settings.paperless_audit_fix_mode = "review"
            mock_settings.paperless_audit_confidence_threshold = 0.8
            mock_settings.paperless_audit_batch_delay = 0

            mock_fetch.return_value = [1]
            mock_meta.return_value = {"types": [], "correspondents": [], "tags": []}

            mock_result = MagicMock()
            mock_result.changes_needed = True
            mock_result.confidence = 0.5  # below threshold of 0.8
            mock_analyze.return_value = mock_result

            await service.run_audit(mode="full", fix_mode="auto_threshold", confidence_threshold=0.8)

            mock_fix.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_auto_threshold_above(self, service):
        """auto_threshold should apply when confidence meets threshold."""
        with patch.object(service, "_fetch_all_doc_ids", new_callable=AsyncMock) as mock_fetch, \
             patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta, \
             patch.object(service, "_analyze_document", new_callable=AsyncMock) as mock_analyze, \
             patch.object(service, "_apply_fix", new_callable=AsyncMock) as mock_fix, \
             patch("ha_glue.services.paperless_audit_service.settings") as mock_settings:

            mock_settings.paperless_audit_fix_mode = "review"
            mock_settings.paperless_audit_confidence_threshold = 0.8
            mock_settings.paperless_audit_batch_delay = 0

            mock_fetch.return_value = [1]
            mock_meta.return_value = {"types": [], "correspondents": [], "tags": []}

            mock_result = MagicMock()
            mock_result.changes_needed = True
            mock_result.confidence = 0.85
            mock_analyze.return_value = mock_result

            await service.run_audit(mode="full", fix_mode="auto_threshold", confidence_threshold=0.8)

            mock_fix.assert_called_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_running_flag_reset_on_completion(self, service):
        """_running should be reset to False after audit completes."""
        with patch.object(service, "_fetch_all_doc_ids", new_callable=AsyncMock) as mock_fetch, \
             patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta, \
             patch("ha_glue.services.paperless_audit_service.settings") as mock_settings:

            mock_settings.paperless_audit_fix_mode = "review"
            mock_settings.paperless_audit_confidence_threshold = 0.8
            mock_settings.paperless_audit_batch_delay = 0

            mock_fetch.return_value = []
            mock_meta.return_value = {"types": [], "correspondents": [], "tags": []}

            await service.run_audit()
            assert service._running is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_running_flag_reset_on_error(self, service):
        """_running should be reset even if an exception occurs."""
        with patch.object(service, "_fetch_all_doc_ids", new_callable=AsyncMock) as mock_fetch, \
             patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta, \
             patch("ha_glue.services.paperless_audit_service.settings") as mock_settings:

            mock_settings.paperless_audit_fix_mode = "review"
            mock_settings.paperless_audit_confidence_threshold = 0.8
            mock_settings.paperless_audit_batch_delay = 0

            mock_fetch.side_effect = Exception("DB error")

            with pytest.raises(Exception, match="DB error"):
                await service.run_audit()

            assert service._running is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_cancel_stops_processing(self, service):
        """Setting _cancel_requested should stop the loop."""
        call_count = 0

        async def mock_analyze(doc_id, run_id, meta):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                service._cancel_requested = True
            result = MagicMock()
            result.changes_needed = False
            return result

        with patch.object(service, "_fetch_all_doc_ids", new_callable=AsyncMock) as mock_fetch, \
             patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta, \
             patch.object(service, "_analyze_document", side_effect=mock_analyze), \
             patch("ha_glue.services.paperless_audit_service.settings") as mock_settings:

            mock_settings.paperless_audit_fix_mode = "review"
            mock_settings.paperless_audit_confidence_threshold = 0.8
            mock_settings.paperless_audit_batch_delay = 0

            mock_fetch.return_value = [1, 2, 3, 4, 5]
            mock_meta.return_value = {"types": [], "correspondents": [], "tags": []}

            result = await service.run_audit(mode="full")

            # Should have processed 2 and then stopped
            assert result["processed"] == 2
            assert result["total"] == 5


# ============================================================================
# Apply/Skip/Reprocess Operations
# ============================================================================


class TestApplyResults:
    """Test apply_results method."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_apply_results_calls_fix(self, service, mock_db_factory):
        """Should call _apply_fix for each pending result with changes."""
        mock_result = MagicMock()
        mock_result.id = 1
        mock_result.paperless_doc_id = 42

        mock_session = mock_db_factory._mock_session
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_result]
        mock_session.execute.return_value = MagicMock(scalars=MagicMock(return_value=mock_scalars))

        with patch.object(service, "_apply_fix", new_callable=AsyncMock, return_value=True):
            result = await service.apply_results([1])
            assert result["applied"] == 1
            assert result["failed"] == 0


class TestSkipResults:
    """Test skip_results method."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_skip_sets_status(self, service, mock_db_factory):
        """Should set status to 'skipped' for matching results."""
        mock_result = MagicMock()
        mock_result.status = "pending"

        mock_session = mock_db_factory._mock_session
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_result]
        mock_session.execute.return_value = MagicMock(scalars=MagicMock(return_value=mock_scalars))

        result = await service.skip_results([1])
        assert result["skipped"] == 1
        assert mock_result.status == "skipped"


def _make_audit_row(doc_id=42, result_id=7, ocr_quality=4):
    row = MagicMock()
    row.id = result_id
    row.paperless_doc_id = doc_id
    row.ocr_quality = ocr_quality
    return row


def _list_query_result(rows):
    scalars = MagicMock()
    scalars.all.return_value = rows
    return MagicMock(scalars=MagicMock(return_value=scalars))


def _mcp_router(mcp, responses, sink=None):
    """Route ``execute_tool`` by tool name, recording calls into ``sink``.

    Accepts extra args/kwargs (e.g. ``truncate=False`` on download_document).
    """
    async def _call(tool, params, *args, **kwargs):
        if sink is not None:
            sink.append((tool, params))
        return responses.get(tool, {"success": True, "message": "{}"})
    mcp.execute_tool.side_effect = _call


def _rows_query_result(rows):
    """Wrap a list of tuple-rows as an ``execute(...).all()`` result."""
    return MagicMock(all=MagicMock(return_value=rows))


def _scalars_result(values):
    """Wrap a list as an ``execute(...).scalars().all()`` result."""
    scalars = MagicMock()
    scalars.all.return_value = values
    return MagicMock(scalars=MagicMock(return_value=scalars))


# ============================================================================
# Low-Quality OCR signal  (Admin UX for low-quality OCR documents)
# ============================================================================


class TestLowQualityLookup:
    """Test _low_quality_lookup — the per-page batch signal resolution.

    Row tuple order matches the SELECT in the service:
        (paperless_document_id, Document.id, status, error_message,
         quality_ignored, chunks_produced, chunks_dropped_low_quality)
    """

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_drop_ratio_at_30_percent_flagged(self, service, mock_db_factory):
        """Exactly 30% dropped IS flagged (boundary inclusive). 3 dropped of 10
        total (7 produced + 3 dropped) = 30%."""
        db = mock_db_factory._mock_session
        db.execute.return_value = _rows_query_result(
            [(100, 1, "completed", None, False, 7, 3)]
        )
        lookup = await service._low_quality_lookup(db, [100])
        entry = lookup[100]
        assert entry["low_quality_ocr"] is True
        assert entry["chunks_dropped"] == 3
        assert entry["chunks_total"] == 10
        assert entry["renfield_document_id"] == 1
        assert entry["quality_ignored"] is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_drop_ratio_above_30_percent_flagged(self, service, mock_db_factory):
        """5 dropped of 10 total = 50% → flagged, pct computable."""
        db = mock_db_factory._mock_session
        db.execute.return_value = _rows_query_result(
            [(100, 1, "completed", None, False, 5, 5)]
        )
        lookup = await service._low_quality_lookup(db, [100])
        entry = lookup[100]
        assert entry["low_quality_ocr"] is True
        # pct the UI would render = round(5/10*100) = 50
        assert round(entry["chunks_dropped"] / entry["chunks_total"] * 100) == 50

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_drop_ratio_below_30_percent_not_flagged(self, service, mock_db_factory):
        """29% dropped is NOT flagged. 29 dropped of 100 total."""
        db = mock_db_factory._mock_session
        db.execute.return_value = _rows_query_result(
            [(100, 1, "completed", None, False, 71, 29)]
        )
        lookup = await service._low_quality_lookup(db, [100])
        assert lookup[100]["low_quality_ocr"] is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_failed_status_signal_flagged(self, service, mock_db_factory):
        """status='failed' AND error_message LIKE 'ocr_quality%' → flagged even
        with no chunk history."""
        db = mock_db_factory._mock_session
        db.execute.return_value = _rows_query_result(
            [(100, 1, "failed", "ocr_quality: too garbled", False, None, None)]
        )
        lookup = await service._low_quality_lookup(db, [100])
        entry = lookup[100]
        assert entry["low_quality_ocr"] is True
        assert entry["chunks_total"] is None
        assert entry["chunks_dropped"] is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_failed_status_other_error_not_flagged(self, service, mock_db_factory):
        """A failed doc whose error is NOT ocr_quality* is not flagged by the
        status signal."""
        db = mock_db_factory._mock_session
        db.execute.return_value = _rows_query_result(
            [(100, 1, "failed", "network timeout", False, None, None)]
        )
        lookup = await service._low_quality_lookup(db, [100])
        assert lookup[100]["low_quality_ocr"] is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_quality_ignored_carried(self, service, mock_db_factory):
        """quality_ignored rides on the entry."""
        db = mock_db_factory._mock_session
        db.execute.return_value = _rows_query_result(
            [(100, 1, "completed", None, True, 1, 9)]
        )
        lookup = await service._low_quality_lookup(db, [100])
        assert lookup[100]["quality_ignored"] is True
        assert lookup[100]["low_quality_ocr"] is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_ids_short_circuits(self, service, mock_db_factory):
        """No paperless_doc_ids → empty map, no query."""
        db = mock_db_factory._mock_session
        lookup = await service._low_quality_lookup(db, [])
        assert lookup == {}
        db.execute.assert_not_called()


class TestInjectLowQuality:
    """Test _inject_low_quality — merges the signal into a result dict."""

    @pytest.mark.unit
    def test_none_signal_defaults_false(self):
        """Paperless-only doc (no Document row) → false/null shape, no badge."""
        d = {"id": 1, "paperless_doc_id": 100}
        PaperlessAuditService._inject_low_quality(d, None)
        assert d["low_quality_ocr"] is False
        assert d["chunks_dropped"] is None
        assert d["chunks_total"] is None
        assert d["quality_ignored"] is False
        assert d["renfield_document_id"] is None

    @pytest.mark.unit
    def test_signal_merged(self):
        d = {"id": 1, "paperless_doc_id": 100}
        PaperlessAuditService._inject_low_quality(d, {
            "renfield_document_id": 5,
            "low_quality_ocr": True,
            "chunks_dropped": 4,
            "chunks_total": 10,
            "quality_ignored": True,
        })
        assert d["low_quality_ocr"] is True
        assert d["chunks_dropped"] == 4
        assert d["chunks_total"] == 10
        assert d["quality_ignored"] is True
        assert d["renfield_document_id"] == 5


class TestGetResultsLowQuality:
    """Test get_results injection + the low_quality_only filter."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_injects_signal_into_each_result(self, service, mock_db_factory):
        """get_results enriches every result dict with the low-quality signal,
        resolved via a single _low_quality_lookup call (no N+1)."""
        db = mock_db_factory._mock_session
        row = _make_audit_row(doc_id=100, result_id=1)
        # execute() is called twice: count query, then the paginated main query.
        db.execute.side_effect = [
            MagicMock(scalar=MagicMock(return_value=1)),  # count
            _scalars_result([row]),                       # page
        ]
        with patch.object(
            service, "_low_quality_lookup", new_callable=AsyncMock,
            return_value={100: {
                "renfield_document_id": 5,
                "low_quality_ocr": True,
                "chunks_dropped": 4,
                "chunks_total": 10,
                "quality_ignored": False,
            }},
        ) as mock_lookup, patch.object(
            service, "_result_to_dict",
            return_value={"id": 1, "paperless_doc_id": 100},
        ):
            out = await service.get_results(page=1, per_page=20)

        # ONE lookup call for the whole page (batch, not per-row).
        mock_lookup.assert_called_once()
        assert out["total"] == 1
        result = out["results"][0]
        assert result["low_quality_ocr"] is True
        assert result["chunks_dropped"] == 4
        assert result["chunks_total"] == 10
        assert result["renfield_document_id"] == 5

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_low_quality_only_adds_filter(self, service, mock_db_factory):
        """low_quality_only=True restricts the page to qualifying docs by
        adding a paperless_doc_id IN (subquery) WHERE clause to BOTH the count
        and the main query."""
        db = mock_db_factory._mock_session
        captured = []

        async def _exec(stmt, *a, **k):
            captured.append(str(stmt))
            if len(captured) == 1:
                return MagicMock(scalar=MagicMock(return_value=0))  # count
            return _scalars_result([])                              # page

        db.execute.side_effect = _exec

        with patch.object(
            service, "_low_quality_lookup", new_callable=AsyncMock, return_value={}
        ):
            await service.get_results(page=1, per_page=20, low_quality_only=True)

        # The compiled count + main queries both reference the documents table
        # (the qualifying subquery) — the filter was applied, not dropped.
        assert captured, "expected execute() to be called"
        assert any("documents" in sql.lower() for sql in captured)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_low_quality_only_off_no_documents_join(self, service, mock_db_factory):
        """Without low_quality_only the main listing query does NOT join the
        documents table (the signal is resolved separately, batched)."""
        db = mock_db_factory._mock_session
        captured = []

        async def _exec(stmt, *a, **k):
            captured.append(str(stmt))
            if len(captured) == 1:
                return MagicMock(scalar=MagicMock(return_value=0))
            return _scalars_result([])

        db.execute.side_effect = _exec

        with patch.object(
            service, "_low_quality_lookup", new_callable=AsyncMock, return_value={}
        ):
            await service.get_results(page=1, per_page=20)

        # Count + page queries are over paperless_audit_results only.
        assert all("documents" not in sql.lower() for sql in captured)


class TestSetQualityIgnored:
    """Test set_quality_ignored — flips Document.quality_ignored."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_flips_flag_and_counts_matched(self, service, mock_db_factory):
        """Resolves audit results → paperless ids → Documents, sets the flag,
        counts only matched docs."""
        db = mock_db_factory._mock_session
        doc1 = MagicMock()
        doc2 = MagicMock()
        db.execute.side_effect = [
            _scalars_result([100, 200]),   # paperless ids for the result_ids
            _scalars_result([doc1, doc2]), # matching Document rows
        ]
        out = await service.set_quality_ignored([1, 2], ignored=True)
        assert out["updated"] == 2
        assert doc1.quality_ignored is True
        assert doc2.quality_ignored is True
        db.commit.assert_awaited()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unignore_sets_false(self, service, mock_db_factory):
        db = mock_db_factory._mock_session
        doc = MagicMock()
        db.execute.side_effect = [
            _scalars_result([100]),
            _scalars_result([doc]),
        ]
        out = await service.set_quality_ignored([1], ignored=False)
        assert out["updated"] == 1
        assert doc.quality_ignored is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_matching_document_counts_zero(self, service, mock_db_factory):
        """Audit rows whose paperless doc has no renfield Document are skipped."""
        db = mock_db_factory._mock_session
        db.execute.side_effect = [
            _scalars_result([100]),  # has a paperless id
            _scalars_result([]),     # but no Document row matches
        ]
        out = await service.set_quality_ignored([1], ignored=True)
        assert out["updated"] == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_result_ids_short_circuits(self, service, mock_db_factory):
        out = await service.set_quality_ignored([], ignored=True)
        assert out["updated"] == 0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_all_paperless_ids_null_counts_zero(self, service, mock_db_factory):
        """If the selected audit rows carry no paperless_doc_id, nothing to do."""
        db = mock_db_factory._mock_session
        db.execute.side_effect = [_scalars_result([None])]
        out = await service.set_quality_ignored([1], ignored=True)
        assert out["updated"] == 0


class TestQualityIgnoreRequest:
    """Validate the new request model."""

    @pytest.mark.unit
    def test_model_fields(self):
        from ha_glue.api.routes.paperless_audit import QualityIgnoreRequest

        req = QualityIgnoreRequest(result_ids=[1, 2], ignored=True)
        assert req.result_ids == [1, 2]
        assert req.ignored is True


class TestReprocessDocuments:
    """Test reprocess_documents — now a local-OCR-then-write-back pipeline
    with a Paperless-native reprocess fallback."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_fallback_to_paperless_when_download_unavailable(
        self, service, mock_mcp_manager, mock_db_factory
    ):
        """No downloadable bytes => delegate to Paperless reprocess (fallback)."""
        mock_db_factory._mock_session.execute.return_value = _list_query_result(
            [_make_audit_row()]
        )
        _mcp_router(mock_mcp_manager, {
            "mcp.paperless.download_document": {"success": True, "message": ""},
            "mcp.paperless.reprocess_document": {"success": True},
        })

        result = await service.reprocess_documents([1])
        assert result["fallback"] == 1
        assert result["failed"] == 0
        assert result["triggered"] == 1  # legacy key = improved + fallback

        mock_mcp_manager.execute_tool.assert_called_with(
            "mcp.paperless.reprocess_document", {"document_id": 42},
        )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_failure_when_fallback_reprocess_fails(
        self, service, mock_mcp_manager, mock_db_factory
    ):
        """Download unavailable AND Paperless reprocess fails => failed."""
        mock_db_factory._mock_session.execute.return_value = _list_query_result(
            [_make_audit_row()]
        )
        _mcp_router(mock_mcp_manager, {
            "mcp.paperless.download_document": {"success": False},
            "mcp.paperless.reprocess_document": {"success": False},
        })

        result = await service.reprocess_documents([1])
        assert result["triggered"] == 0
        assert result["failed"] == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_local_reocr_writes_back_when_improved(
        self, service, mock_mcp_manager, mock_db_factory
    ):
        """Clean local OCR (better than stored) is PATCHed into Paperless content
        and the audit row's quality is bumped."""
        row = _make_audit_row(ocr_quality=4)
        update_row = MagicMock()
        mock_db_factory._mock_session.execute.side_effect = [
            _list_query_result([row]),  # initial list query
            MagicMock(scalar_one_or_none=MagicMock(return_value=update_row)),  # row bump
        ]
        b64 = base64.b64encode(b"%PDF-1.4 fake").decode()
        calls = []
        _mcp_router(mock_mcp_manager, {
            "mcp.paperless.download_document": {
                "success": True,
                "message": json.dumps({"content_base64": b64, "filename": "doc_42.pdf"}),
            },
            "mcp.paperless.update_document": {"success": True, "message": "{}"},
        }, sink=calls)
        service._document_processor = MagicMock()
        service._document_processor.extract_text_only = AsyncMock(
            return_value="Sehr geehrte Damen und Herren, hier ist die saubere Rechnung."
        )

        result = await service.reprocess_documents([7])
        assert result["improved"] == 1
        assert result["fallback"] == 0
        assert result["failed"] == 0

        # download MUST bypass response truncation, or the base64 PDF is
        # byte-cut into unparseable JSON for any real-sized scan.
        dl_calls = [
            c for c in mock_mcp_manager.execute_tool.call_args_list
            if c.args[0] == "mcp.paperless.download_document"
        ]
        assert dl_calls and dl_calls[0].kwargs.get("truncate") is False

        # extract was forced full-page OCR
        _, kwargs = service._document_processor.extract_text_only.call_args
        assert kwargs.get("force_ocr") is True
        # content was written back
        writes = [p for t, p in calls if t == "mcp.paperless.update_document"]
        assert writes and "content" in writes[0]
        # audit row quality bumped to the clean score
        assert update_row.ocr_quality == 5

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_writeback_when_local_ocr_not_better(
        self, service, mock_mcp_manager, mock_db_factory
    ):
        """If forced OCR yields worse text than the stored content, don't
        overwrite — fall back to Paperless reprocess, never PATCH content."""
        mock_db_factory._mock_session.execute.return_value = _list_query_result(
            [_make_audit_row(ocr_quality=5)]
        )
        b64 = base64.b64encode(b"%PDF-1.4 fake").decode()
        calls = []
        _mcp_router(mock_mcp_manager, {
            "mcp.paperless.download_document": {
                "success": True,
                "message": json.dumps({"content_base64": b64, "filename": "doc_42.pdf"}),
            },
            "mcp.paperless.reprocess_document": {"success": True},
        }, sink=calls)
        service._document_processor = MagicMock()
        # No spaces => garbled => scores below the stored 5.
        service._document_processor.extract_text_only = AsyncMock(
            return_value="abcdefghij" * 40
        )

        result = await service.reprocess_documents([7])
        assert result["fallback"] == 1
        assert result["improved"] == 0
        assert not [t for t, _ in calls if t == "mcp.paperless.update_document"]


# ============================================================================
# Missing Fields Heuristic
# ============================================================================


class TestCheckMissingFields:
    """Test _check_missing_fields static method."""

    @pytest.mark.unit
    def test_all_present(self):
        """No fields missing when all metadata is present."""
        doc = {
            "correspondent": "Company A",
            "document_type": "Invoice",
            "tags": ["finance"],
            "storage_path": "invoices/",
        }
        missing = PaperlessAuditService._check_missing_fields(doc)
        assert missing == []

    @pytest.mark.unit
    def test_all_missing(self):
        """All four fields reported when all are empty."""
        doc = {}
        missing = PaperlessAuditService._check_missing_fields(doc)
        assert "correspondent" in missing
        assert "document_type" in missing
        assert "tags" in missing
        assert "storage_path" in missing
        assert len(missing) == 4

    @pytest.mark.unit
    def test_partial_missing(self):
        """Only missing fields are reported."""
        doc = {
            "correspondent": "Company A",
            "document_type": None,
            "tags": [],
            "storage_path": "path/",
        }
        missing = PaperlessAuditService._check_missing_fields(doc)
        assert "correspondent" not in missing
        assert "document_type" in missing
        assert "tags" in missing  # empty list is falsy
        assert "storage_path" not in missing

    @pytest.mark.unit
    def test_none_values_are_missing(self):
        """None values should be treated as missing."""
        doc = {
            "correspondent": None,
            "document_type": "Invoice",
            "tags": ["a"],
            "storage_path": None,
        }
        missing = PaperlessAuditService._check_missing_fields(doc)
        assert "correspondent" in missing
        assert "storage_path" in missing
        assert "document_type" not in missing
        assert "tags" not in missing


# ============================================================================
# Content Completeness Heuristic
# ============================================================================


class TestCheckContentCompleteness:
    """Test _check_content_completeness static method."""

    @pytest.mark.unit
    def test_good_content(self):
        """Normal content should get score 5."""
        content = "A" * 2000  # enough content
        score, issues = PaperlessAuditService._check_content_completeness(content, 2)
        assert score == 5
        assert issues == "OK"

    @pytest.mark.unit
    def test_empty_content(self):
        """Empty content should get score 1."""
        score, issues = PaperlessAuditService._check_content_completeness("", 1)
        assert score == 1

    @pytest.mark.unit
    def test_very_low_chars_per_page(self):
        """Very low content per page should be flagged."""
        content = "Short text"  # ~10 chars for 5 pages
        score, issues = PaperlessAuditService._check_content_completeness(content, 5)
        assert score < 5
        assert "chars/page" in issues.lower() or "content per page" in issues.lower()

    @pytest.mark.unit
    def test_incomplete_multi_page(self):
        """Very short content on multi-page document should be flagged."""
        # Must be >10 chars to pass early return, but <50 chars for the multi-page check
        content = "Just some short text."  # ~21 chars, 10 pages
        score, issues = PaperlessAuditService._check_content_completeness(content, 10)
        assert score < 5
        assert "incomplete" in issues.lower() or "content per page" in issues.lower()

    @pytest.mark.unit
    def test_no_page_count(self):
        """Should work without page_count."""
        content = "Some reasonable content that is long enough to not be minimal." * 5
        score, issues = PaperlessAuditService._check_content_completeness(content, None)
        assert score == 5

    @pytest.mark.unit
    def test_page_marker_gaps(self):
        """Missing page markers should be flagged."""
        content = "Seite 1 text here. Seite 3 more text here."
        score, issues = PaperlessAuditService._check_content_completeness(content, 3)
        assert "missing pages" in issues.lower()

    @pytest.mark.unit
    def test_consecutive_page_markers_ok(self):
        """Consecutive page markers should not be flagged."""
        content = "Page 1 here. Page 2 here. Page 3 here."
        score, issues = PaperlessAuditService._check_content_completeness(content, 3)
        assert "missing pages" not in issues.lower()


# ============================================================================
# Correspondent Normalization
# ============================================================================


class TestCorrespondentNormalization:
    """Test run_correspondent_normalization method."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_finds_similar_names(self, service):
        """Should find clusters of similar correspondent names."""
        with patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta:
            mock_meta.return_value = {
                "types": [], "tags": [], "storage_paths": [], "custom_fields": [],
                "correspondents": ["Telekom Deutschland", "Telekom Deutschland GmbH", "Vodafone"],
            }
            result = await service.run_correspondent_normalization(threshold=0.8)

        assert result["total_correspondents"] == 3
        assert len(result["clusters"]) >= 1
        # Telekom variants should cluster
        cluster = result["clusters"][0]
        assert "Telekom" in cluster["canonical"] or any("Telekom" in v["name"] for v in cluster["variants"])

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_no_clusters_when_distinct(self, service):
        """Should return empty clusters when all names are distinct."""
        with patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta:
            mock_meta.return_value = {
                "types": [], "tags": [], "storage_paths": [], "custom_fields": [],
                "correspondents": ["Apple Inc", "Microsoft Corp", "Google LLC"],
            }
            result = await service.run_correspondent_normalization(threshold=0.82)

        assert result["clusters"] == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_correspondents(self, service):
        """Should handle empty correspondent list."""
        with patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta:
            mock_meta.return_value = {
                "types": [], "tags": [], "storage_paths": [], "custom_fields": [],
                "correspondents": [],
            }
            result = await service.run_correspondent_normalization()

        assert result["total_correspondents"] == 0
        assert result["clusters"] == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_strips_legal_suffixes(self, service):
        """GmbH/AG/etc should be stripped before comparison."""
        with patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta:
            mock_meta.return_value = {
                "types": [], "tags": [], "storage_paths": [], "custom_fields": [],
                "correspondents": ["ACME GmbH", "ACME AG"],
            }
            result = await service.run_correspondent_normalization(threshold=0.8)

        # Should cluster because "ACME" == "ACME" after stripping
        assert len(result["clusters"]) == 1


# ============================================================================
# API Route Tests (via _get_service helper)
# ============================================================================


class TestApiRouteHelpers:
    """Test API route helper and request model validation."""

    @pytest.mark.unit
    def test_get_service_raises_503(self):
        """_get_service should raise 503 when service not in app state."""
        from ha_glue.api.routes.paperless_audit import _get_service
        from fastapi import HTTPException

        mock_request = MagicMock()
        mock_request.app.state = MagicMock(spec=[])  # no paperless_audit attr

        with pytest.raises(HTTPException) as exc_info:
            _get_service(mock_request)
        assert exc_info.value.status_code == 503

    @pytest.mark.unit
    def test_get_service_returns_service(self):
        """_get_service should return the service when available."""
        from ha_glue.api.routes.paperless_audit import _get_service

        mock_service = MagicMock()
        mock_request = MagicMock()
        mock_request.app.state.paperless_audit = mock_service

        result = _get_service(mock_request)
        assert result is mock_service

    @pytest.mark.unit
    def test_audit_start_request_defaults(self):
        """AuditStartRequest should have correct defaults."""
        from ha_glue.api.routes.paperless_audit import AuditStartRequest

        req = AuditStartRequest()
        assert req.mode == "new_only"
        assert req.fix_mode is None
        assert req.confidence_threshold is None
        assert req.document_ids is None

    @pytest.mark.unit
    def test_audit_start_request_custom(self):
        """AuditStartRequest should accept custom values."""
        from ha_glue.api.routes.paperless_audit import AuditStartRequest

        req = AuditStartRequest(
            mode="full",
            fix_mode="auto_all",
            confidence_threshold=0.9,
            document_ids=[1, 2, 3],
        )
        assert req.mode == "full"
        assert req.fix_mode == "auto_all"
        assert req.confidence_threshold == 0.9
        assert req.document_ids == [1, 2, 3]
