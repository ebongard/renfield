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

import json
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.paperless_audit_service import PaperlessAuditService


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
    def test_repeated_characters(self):
        """Text with repeated characters should be detected."""
        text = "Normal text here. Then aaaaaaaaa and more normal text follows after that section."
        score, issues = PaperlessAuditService._check_ocr_quality(text)
        assert score < 5
        assert "repeated" in issues.lower()

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
        """Multiple OCR issues should stack and lower the score."""
        # Garbled: no spaces + special chars + repeated
        text = "!!!!!!@@@@@$$$$$%%%%%^^^^^^" * 5
        score, issues = PaperlessAuditService._check_ocr_quality(text)
        assert score <= 2

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

        mock_mcp_manager.execute_tool.return_value = {"success": True}

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
        assert params["correspondent"] == "B"
        assert "document_type" not in params  # unchanged
        assert "tags" not in params  # unchanged
        assert "created_date" not in params  # unchanged

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
             patch("services.paperless_audit_service.settings") as mock_settings:

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
             patch("services.paperless_audit_service.settings") as mock_settings:

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
             patch("services.paperless_audit_service.settings") as mock_settings:

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

            mock_fix.assert_called_once_with(mock_result)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_auto_threshold_below(self, service):
        """auto_threshold should NOT apply when confidence is below threshold."""
        with patch.object(service, "_fetch_all_doc_ids", new_callable=AsyncMock) as mock_fetch, \
             patch.object(service, "_fetch_available_metadata", new_callable=AsyncMock) as mock_meta, \
             patch.object(service, "_analyze_document", new_callable=AsyncMock) as mock_analyze, \
             patch.object(service, "_apply_fix", new_callable=AsyncMock) as mock_fix, \
             patch("services.paperless_audit_service.settings") as mock_settings:

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
             patch("services.paperless_audit_service.settings") as mock_settings:

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
             patch("services.paperless_audit_service.settings") as mock_settings:

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
             patch("services.paperless_audit_service.settings") as mock_settings:

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
             patch("services.paperless_audit_service.settings") as mock_settings:

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


class TestReprocessDocuments:
    """Test reprocess_documents method."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_reprocess_calls_mcp(self, service, mock_mcp_manager, mock_db_factory):
        """Should call MCP reprocess_document for each result."""
        mock_result = MagicMock()
        mock_result.paperless_doc_id = 42

        mock_session = mock_db_factory._mock_session
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_result]
        mock_session.execute.return_value = MagicMock(scalars=MagicMock(return_value=mock_scalars))

        mock_mcp_manager.execute_tool.return_value = {"success": True}

        result = await service.reprocess_documents([1])
        assert result["triggered"] == 1
        assert result["failed"] == 0

        mock_mcp_manager.execute_tool.assert_called_with(
            "mcp.paperless.reprocess_document",
            {"document_id": 42},
        )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_reprocess_failure(self, service, mock_mcp_manager, mock_db_factory):
        """Should count failures when MCP call fails."""
        mock_result = MagicMock()
        mock_result.paperless_doc_id = 42

        mock_session = mock_db_factory._mock_session
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_result]
        mock_session.execute.return_value = MagicMock(scalars=MagicMock(return_value=mock_scalars))

        mock_mcp_manager.execute_tool.return_value = {"success": False}

        result = await service.reprocess_documents([1])
        assert result["triggered"] == 0
        assert result["failed"] == 1


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
        from api.routes.paperless_audit import _get_service
        from fastapi import HTTPException

        mock_request = MagicMock()
        mock_request.app.state = MagicMock(spec=[])  # no paperless_audit attr

        with pytest.raises(HTTPException) as exc_info:
            _get_service(mock_request)
        assert exc_info.value.status_code == 503

    @pytest.mark.unit
    def test_get_service_returns_service(self):
        """_get_service should return the service when available."""
        from api.routes.paperless_audit import _get_service

        mock_service = MagicMock()
        mock_request = MagicMock()
        mock_request.app.state.paperless_audit = mock_service

        result = _get_service(mock_request)
        assert result is mock_service

    @pytest.mark.unit
    def test_audit_start_request_defaults(self):
        """AuditStartRequest should have correct defaults."""
        from api.routes.paperless_audit import AuditStartRequest

        req = AuditStartRequest()
        assert req.mode == "new_only"
        assert req.fix_mode is None
        assert req.confidence_threshold is None
        assert req.document_ids is None

    @pytest.mark.unit
    def test_audit_start_request_custom(self):
        """AuditStartRequest should accept custom values."""
        from api.routes.paperless_audit import AuditStartRequest

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
