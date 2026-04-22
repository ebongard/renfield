"""
Unit tests for PaperlessMetadataExtractor — pure-unit coverage of the
non-IO pieces (fuzzy match, pruning, validation, prompt render, JSON
parsing) plus one end-to-end extract() call with every dependency mocked.

Full integration with a real Paperless + Docling + LLM is deferred to
the eval suite (see docs/design/paperless-llm-metadata.md § Eval corpus).

All tests @pytest.mark.unit — no network, no DB engine.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.paperless_metadata_extractor import (
    ExtractionResult,
    NewEntryProposal,
    PaperlessMetadata,
    PaperlessMetadataExtractor,
    PaperlessTaxonomy,
    _fuzzy_match,
    _normalise,
    _parse_llm_json,
    prune_taxonomy,
    render_prompt,
    validate_extraction,
)


# ===========================================================================
# _normalise
# ===========================================================================


class TestNormalise:
    @pytest.mark.unit
    def test_casefolds(self):
        assert _normalise("Stadtwerke") == "stadtwerke"

    @pytest.mark.unit
    def test_strips_surrounding_whitespace(self):
        assert _normalise("  Finanzamt  ") == "finanzamt"

    @pytest.mark.unit
    def test_nfkc_normalises_compatibility_chars(self):
        # Fullwidth exclamation → ASCII (both folded). Mainly guards
        # against ligatures and fullwidth CJK Paperless admins might
        # have entered copy-paste.
        assert _normalise("ＡＢＣ") == "abc"

    @pytest.mark.unit
    def test_umlauts_preserved(self):
        # NFKC doesn't fold Umlauts to ae/oe/ue. Fuzzy matching on the
        # canonical Umlaut form is what we want.
        assert _normalise("Müller") == "müller"

    @pytest.mark.unit
    def test_empty_returns_empty(self):
        assert _normalise("") == ""
        assert _normalise(None) == ""  # None-tolerant


# ===========================================================================
# _fuzzy_match
# ===========================================================================


class TestFuzzyMatch:
    @pytest.mark.unit
    def test_exact_hit_returns_canonical(self):
        taxonomy = ["Stadtwerke Korschenbroich", "Finanzamt Neuss"]
        assert _fuzzy_match("Stadtwerke Korschenbroich", taxonomy) == "Stadtwerke Korschenbroich"

    @pytest.mark.unit
    def test_case_insensitive_hit(self):
        taxonomy = ["Stadtwerke Korschenbroich"]
        assert _fuzzy_match("stadtwerke korschenbroich", taxonomy) == "Stadtwerke Korschenbroich"

    @pytest.mark.unit
    def test_near_match_rewrites_to_canonical(self):
        taxonomy = ["Stadtwerke Korschenbroich"]
        # "Korschenbroic" vs "Korschenbroich" = distance 1, ratio ~0.04
        # Both thresholds satisfied → canonical wins.
        assert _fuzzy_match("Stadtwerke Korschenbroic", taxonomy) == "Stadtwerke Korschenbroich"

    @pytest.mark.unit
    def test_distance_over_threshold_drops(self):
        taxonomy = ["Stadtwerke Korschenbroich"]
        # "Stadtwerke Korschenbroich GmbH" adds " GmbH" = 5 chars. Over
        # the max-distance threshold → no match.
        assert _fuzzy_match("Stadtwerke Korschenbroich GmbH", taxonomy) is None

    @pytest.mark.unit
    def test_short_string_ratio_guard(self):
        """For 3-letter names, edit distance 2 would be 67% — the ratio
        cap kicks in before the distance cap."""
        taxonomy = ["Bob"]
        # "Alice" vs "Bob" — distance 4, over both caps. No match.
        assert _fuzzy_match("Alice", taxonomy) is None
        # "Bo" vs "Bob" — distance 1, ratio 0.33 > 0.2 → no match.
        assert _fuzzy_match("Bo", taxonomy) is None

    @pytest.mark.unit
    def test_ambiguous_multi_match_drops_to_none(self):
        """Two near-matches within threshold → ambiguous, caller
        surfaces proposal instead of arbitrarily picking one."""
        taxonomy = ["Telekom DE", "Telekom DK"]
        # "Telekom DX" is distance 1 from both. Ambiguous.
        assert _fuzzy_match("Telekom DX", taxonomy) is None

    @pytest.mark.unit
    def test_empty_inputs_return_none(self):
        assert _fuzzy_match("", ["A", "B"]) is None
        assert _fuzzy_match("A", []) is None
        assert _fuzzy_match(None, ["A"]) is None

    @pytest.mark.unit
    def test_umlaut_near_match(self):
        taxonomy = ["Müller"]
        # "Muller" (ASCII) vs "Müller" (Umlaut) — distance 1, ratio OK.
        assert _fuzzy_match("Muller", taxonomy) == "Müller"


# ===========================================================================
# prune_taxonomy
# ===========================================================================


class TestPruneTaxonomy:
    @pytest.mark.unit
    def test_no_recency_returns_first_n(self):
        """Cold start — no recency signal. Keep the first N entries."""
        correspondents = [f"C{i}" for i in range(30)]
        result = prune_taxonomy(
            correspondents=correspondents,
            document_types=["Rechnung"],
            tags=[],
            storage_paths=[],
            top_correspondents=5,
        )
        assert result.correspondents == ["C0", "C1", "C2", "C3", "C4"]

    @pytest.mark.unit
    def test_recency_reorders_within_cap(self):
        """Most-recent entries should appear first, padded from the
        remaining list until the cap is filled."""
        correspondents = ["A", "B", "C", "D", "E"]
        # Recency says "D was used recently, then A, then B."
        result = prune_taxonomy(
            correspondents=correspondents,
            document_types=[],
            tags=[],
            storage_paths=[],
            recent_correspondent_ids=["D", "A", "B"],
            top_correspondents=4,
        )
        # First 3 from recency, then 4th from remaining (C, dropping E).
        assert result.correspondents == ["D", "A", "B", "C"]

    @pytest.mark.unit
    def test_document_types_never_pruned(self):
        """document_types + storage_paths are included in full because
        they're typically < 30 and small strings."""
        document_types = [f"Type{i}" for i in range(100)]
        result = prune_taxonomy(
            correspondents=[], document_types=document_types,
            tags=[], storage_paths=[],
        )
        assert result.document_types == document_types

    @pytest.mark.unit
    def test_storage_paths_never_pruned(self):
        paths = [f"/path/{i}" for i in range(50)]
        result = prune_taxonomy(
            correspondents=[], document_types=[],
            tags=[], storage_paths=paths,
        )
        assert result.storage_paths == paths

    @pytest.mark.unit
    def test_tags_pruned_by_recency(self):
        """Same recency logic applies to tags."""
        tags = [f"t{i}" for i in range(30)]
        result = prune_taxonomy(
            correspondents=[], document_types=[], tags=tags, storage_paths=[],
            recent_tag_ids=["t25", "t24", "t23"],
            top_tags=5,
        )
        assert result.tags[:3] == ["t25", "t24", "t23"]
        assert len(result.tags) == 5

    @pytest.mark.unit
    def test_recency_entries_not_in_list_are_skipped(self):
        """If the recency signal names entries that aren't in the
        current taxonomy (deleted after the recency snapshot), skip
        them rather than crash."""
        result = prune_taxonomy(
            correspondents=["A", "B", "C"],
            document_types=[], tags=[], storage_paths=[],
            recent_correspondent_ids=["DELETED", "A", "GONE"],
            top_correspondents=5,
        )
        # Only A survives from recency; rest padded in order.
        assert result.correspondents == ["A", "B", "C"]


# ===========================================================================
# validate_extraction
# ===========================================================================


def _taxonomy() -> PaperlessTaxonomy:
    return PaperlessTaxonomy(
        correspondents=["Stadtwerke Korschenbroich", "Finanzamt Neuss"],
        document_types=["Rechnung", "Steuerbescheid", "Nebenkostenabrechnung"],
        tags=["wohnung", "steuer-2025", "nebenkosten-2025"],
        storage_paths=["/wohnung/betriebskosten", "/steuer/2025"],
    )


class TestValidateExtraction:
    @pytest.mark.unit
    def test_happy_path_all_hits(self):
        raw = {
            "title": "Nebenkostenabrechnung 2025 - Stadtwerke Korschenbroich",
            "correspondent": "Stadtwerke Korschenbroich",
            "document_type": "Nebenkostenabrechnung",
            "tags": ["wohnung", "nebenkosten-2025"],
            "storage_path": "/wohnung/betriebskosten",
            "created_date": "2026-02-14",
            "confidence": {"title": 0.95, "correspondent": 0.98},
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.correspondent == "Stadtwerke Korschenbroich"
        assert result.document_type == "Nebenkostenabrechnung"
        assert result.tags == ["wohnung", "nebenkosten-2025"]
        assert result.storage_path == "/wohnung/betriebskosten"
        assert result.created_date == date(2026, 2, 14)
        assert result.new_entry_proposals == []

    @pytest.mark.unit
    def test_fuzzy_rewrite_silent_on_near_match(self):
        """LLM emits 'Stadtwerke Korschenbroic' (missing 'h'). Fuzzy
        rewrites to canonical — no drop, no proposal."""
        raw = {
            "title": "Test",
            "correspondent": "Stadtwerke Korschenbroic",
            "document_type": "Rechnung",
            "tags": [],
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.correspondent == "Stadtwerke Korschenbroich"

    @pytest.mark.unit
    def test_non_taxonomy_value_dropped_when_no_proposal(self):
        """LLM hallucinates a correspondent not in taxonomy, no
        proposal flagged — field drops to None, nothing surfaces."""
        raw = {
            "title": "Test",
            "correspondent": "Gibt Es Nicht GmbH",
            "document_type": "Rechnung",
            "tags": [],
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.correspondent is None
        assert result.new_entry_proposals == []

    @pytest.mark.unit
    def test_non_taxonomy_value_survives_as_proposal(self):
        """LLM emits a value not in taxonomy AND flags it as a proposal.
        Field stays None (proposal carries the intent), proposal
        survives for user review."""
        raw = {
            "title": "Test",
            "correspondent": "Schreiner Meier",
            "document_type": "Rechnung",
            "tags": [],
            "new_entry_proposals": [
                {"field": "correspondent", "value": "Schreiner Meier",
                 "reasoning": "Rechnungskopf, nicht in Taxonomie."},
            ],
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.correspondent is None
        assert len(result.new_entry_proposals) == 1
        assert result.new_entry_proposals[0].value == "Schreiner Meier"

    @pytest.mark.unit
    def test_tags_non_taxonomy_entries_dropped(self):
        """Tags are list-valued — misses drop silently, hits are kept."""
        raw = {
            "title": "Test",
            "correspondent": None,
            "document_type": "Rechnung",
            "tags": ["wohnung", "made-up-tag", "steuer-2025"],
        }
        result = validate_extraction(raw, _taxonomy())
        assert "wohnung" in result.tags
        assert "steuer-2025" in result.tags
        assert "made-up-tag" not in result.tags

    @pytest.mark.unit
    def test_tags_capped_at_5(self):
        raw = {
            "title": "T",
            "correspondent": None,
            "document_type": "Rechnung",
            "tags": ["wohnung"] * 10,  # silly, but tests cap
        }
        taxonomy = _taxonomy()
        taxonomy.tags.extend(["t1", "t2", "t3", "t4", "t5", "t6", "t7"])
        raw["tags"] = ["wohnung", "t1", "t2", "t3", "t4", "t5", "t6"]
        result = validate_extraction(raw, taxonomy)
        assert len(result.tags) == 5

    @pytest.mark.unit
    def test_created_date_before_1900_dropped(self):
        """Documents dated 1847 are OCR errors."""
        raw = {
            "title": "T",
            "correspondent": None,
            "document_type": "Rechnung",
            "tags": [],
            "created_date": "1847-03-14",
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.created_date is None

    @pytest.mark.unit
    def test_created_date_too_far_future_dropped(self):
        """Year 2189 is also an OCR error."""
        raw = {
            "title": "T",
            "correspondent": None,
            "document_type": "Rechnung",
            "tags": [],
            "created_date": "2189-01-01",
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.created_date is None

    @pytest.mark.unit
    def test_created_date_up_to_one_year_future_accepted(self):
        """Slightly post-dated contracts / receipts are legitimate."""
        future = (date.today() + timedelta(days=30)).isoformat()
        raw = {
            "title": "T",
            "correspondent": None,
            "document_type": "Rechnung",
            "tags": [],
            "created_date": future,
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.created_date is not None

    @pytest.mark.unit
    def test_malformed_schema_raises_value_error(self):
        """A totally wrong shape (e.g. tags as string not list) raises
        ValueError. Caller catches and falls back to bare upload."""
        raw = {
            "title": "T",
            "tags": "not-a-list",  # type mismatch
        }
        with pytest.raises(ValueError):
            validate_extraction(raw, _taxonomy())

    @pytest.mark.unit
    def test_empty_dict_produces_empty_metadata(self):
        """Edge case — LLM emitted literally {} (or close to it). No
        fields set, no crash."""
        result = validate_extraction({}, _taxonomy())
        assert result.correspondent is None
        assert result.document_type is None
        assert result.tags == []
        assert result.storage_path is None
        assert result.created_date is None


# ===========================================================================
# _parse_llm_json
# ===========================================================================


class TestParseLLMJson:
    @pytest.mark.unit
    def test_bare_json(self):
        assert _parse_llm_json('{"a": 1}') == {"a": 1}

    @pytest.mark.unit
    def test_fenced_json(self):
        raw = '```json\n{"a": 1}\n```'
        assert _parse_llm_json(raw) == {"a": 1}

    @pytest.mark.unit
    def test_fenced_no_language(self):
        raw = "```\n{\"a\": 1}\n```"
        assert _parse_llm_json(raw) == {"a": 1}

    @pytest.mark.unit
    def test_prose_around_json(self):
        """LLM sometimes adds 'Sure, here is the JSON:' preamble."""
        raw = 'Sure, here it is: {"a": 1, "b": "x"}. Hope this helps!'
        assert _parse_llm_json(raw) == {"a": 1, "b": "x"}

    @pytest.mark.unit
    def test_empty_string(self):
        assert _parse_llm_json("") is None

    @pytest.mark.unit
    def test_no_braces(self):
        assert _parse_llm_json("no JSON here") is None

    @pytest.mark.unit
    def test_invalid_json(self):
        assert _parse_llm_json("{not valid json}") is None

    @pytest.mark.unit
    def test_non_dict_array(self):
        """LLM emitted an array where we expect an object — refuse."""
        assert _parse_llm_json("[1, 2, 3]") is None


# ===========================================================================
# render_prompt
# ===========================================================================


class TestRenderPrompt:
    @pytest.mark.unit
    def test_injects_taxonomy_and_doc_text(self):
        taxonomy = PaperlessTaxonomy(
            correspondents=["Stadtwerke", "Finanzamt"],
            document_types=["Rechnung"],
            tags=["wohnung"],
            storage_paths=["/x"],
        )
        system, user = render_prompt(
            doc_text="Hello world", taxonomy=taxonomy, lang="de",
        )
        assert "JSON" in system or "json" in system.lower()
        assert "Stadtwerke" in user
        assert "Rechnung" in user
        assert "/x" in user
        assert "Hello world" in user

    @pytest.mark.unit
    def test_empty_taxonomy_renders_gracefully(self):
        """Cold-start, no taxonomy yet — prompt should still render,
        just noting (none) for each dimension."""
        taxonomy = PaperlessTaxonomy()
        _, user = render_prompt(doc_text="doc", taxonomy=taxonomy)
        assert "(none)" in user

    @pytest.mark.unit
    def test_doc_text_truncated_past_cap(self):
        """Very long documents get truncated to the LLM-context cap."""
        long_doc = "A" * 20_000
        taxonomy = PaperlessTaxonomy()
        _, user = render_prompt(doc_text=long_doc, taxonomy=taxonomy)
        # User prompt must not contain the full 20k A's.
        assert "A" * 20_000 not in user


# ===========================================================================
# End-to-end extract() — every dependency mocked
# ===========================================================================


class TestExtractorIntegration:
    def _mock_upload(self, tmp_path):
        """Create a ChatUpload-shaped mock with a real file on disk."""
        file = tmp_path / "test.pdf"
        file.write_text("dummy")
        upload = MagicMock()
        upload.id = 1
        upload.file_path = str(file)
        upload.filename = "test.pdf"
        return upload

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_happy_path_end_to_end(self, tmp_path):
        upload = self._mock_upload(tmp_path)

        llm_response = SimpleNamespace(
            message=SimpleNamespace(
                content='{"title": "T", "correspondent": "Stadtwerke Korschenbroich", '
                        '"document_type": "Rechnung", "tags": ["wohnung"], '
                        '"storage_path": "/wohnung/betriebskosten", '
                        '"created_date": "2026-02-14", "new_entry_proposals": []}',
            ),
        )
        llm_client = MagicMock()
        llm_client.chat = AsyncMock(return_value=llm_response)

        doc_proc = MagicMock()
        doc_proc.extract_text_only = AsyncMock(return_value="Stadtwerke Korschenbroich ...")

        mcp = MagicMock()

        async def _mcp_execute(tool_name: str, params: dict):
            if "correspondents" in tool_name:
                return {"success": True, "message": '{"items": [{"name": "Stadtwerke Korschenbroich"}, {"name": "Finanzamt Neuss"}]}'}
            if "document_types" in tool_name:
                return {"success": True, "message": '{"items": [{"name": "Rechnung"}]}'}
            if "tags" in tool_name:
                return {"success": True, "message": '{"items": [{"name": "wohnung"}]}'}
            if "storage_paths" in tool_name:
                return {"success": True, "message": '{"paths": [{"path": "/wohnung/betriebskosten"}]}'}
            return {"success": False}

        mcp.execute_tool = AsyncMock(side_effect=_mcp_execute)

        extractor = PaperlessMetadataExtractor(
            mcp_manager=mcp, llm_client=llm_client, document_processor=doc_proc,
        )
        # Bypass the DB lookup by pre-resolving the upload.
        extractor._load_upload = AsyncMock(return_value=upload)

        # settings.paperless_extraction_model needs to be set so the
        # model-picker doesn't raise.
        with patch("services.paperless_metadata_extractor.settings") as s:
            s.paperless_extraction_model = "qwen3:8b"
            s.ollama_vision_model = ""
            s.ollama_chat_model = ""
            result = await extractor.extract(
                attachment_id=1, session_id="test-session", lang="de",
            )

        assert result.error is None
        assert result.metadata.correspondent == "Stadtwerke Korschenbroich"
        assert result.metadata.document_type == "Rechnung"
        assert result.metadata.storage_path == "/wohnung/betriebskosten"
        assert result.doc_text.startswith("Stadtwerke")

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_missing_attachment_returns_error(self):
        extractor = PaperlessMetadataExtractor()
        extractor._load_upload = AsyncMock(return_value=None)

        result = await extractor.extract(
            attachment_id=99, session_id="s", lang="de",
        )
        assert result.error is not None
        assert "99" in result.error
        assert result.metadata == PaperlessMetadata()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_ocr_empty_result_returns_error(self, tmp_path):
        upload = self._mock_upload(tmp_path)
        doc_proc = MagicMock()
        doc_proc.extract_text_only = AsyncMock(return_value="")

        extractor = PaperlessMetadataExtractor(document_processor=doc_proc)
        extractor._load_upload = AsyncMock(return_value=upload)

        result = await extractor.extract(
            attachment_id=1, session_id="s", lang="de",
        )
        assert "OCR" in result.error or "Dokument nicht lesen" in result.error

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_taxonomy_fetch_fails_returns_error(self, tmp_path):
        upload = self._mock_upload(tmp_path)
        doc_proc = MagicMock()
        doc_proc.extract_text_only = AsyncMock(return_value="document text")

        # MCP manager that raises on every call
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(side_effect=RuntimeError("paperless down"))

        extractor = PaperlessMetadataExtractor(
            mcp_manager=mcp, document_processor=doc_proc,
        )
        extractor._load_upload = AsyncMock(return_value=upload)

        result = await extractor.extract(
            attachment_id=1, session_id="s", lang="de",
        )
        # _list_via_mcp catches and returns [] for each dimension, so
        # pruning produces an empty taxonomy — which is fine, not an
        # error. The LLM call will fail or return empty, and THAT's
        # the error surface. This test confirms we at least don't
        # crash the whole pipeline on taxonomy fetch failure.
        #
        # If the LLM is not configured either, we get the "no model"
        # error — let's check we reached the LLM-call step by not
        # erroring earlier.
        assert result.error is not None
        assert result.doc_text == "document text"  # OCR did run

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_llm_malformed_response_falls_back_cleanly(self, tmp_path):
        upload = self._mock_upload(tmp_path)

        llm_response = SimpleNamespace(
            message=SimpleNamespace(content="not json at all, just prose"),
        )
        llm_client = MagicMock()
        llm_client.chat = AsyncMock(return_value=llm_response)

        doc_proc = MagicMock()
        doc_proc.extract_text_only = AsyncMock(return_value="doc text")

        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={"success": True, "message": '{"items": []}'})

        extractor = PaperlessMetadataExtractor(
            mcp_manager=mcp, llm_client=llm_client, document_processor=doc_proc,
        )
        extractor._load_upload = AsyncMock(return_value=upload)

        with patch("services.paperless_metadata_extractor.settings") as s:
            s.paperless_extraction_model = "qwen3:8b"
            s.ollama_vision_model = ""
            s.ollama_chat_model = ""
            result = await extractor.extract(
                attachment_id=1, session_id="s", lang="de",
            )

        # Malformed JSON → error surfaced, metadata empty, but OCR
        # text preserved so the caller can still upload bare.
        assert result.error is not None
        assert result.doc_text == "doc text"
        assert result.metadata == PaperlessMetadata()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_model_configured_returns_error(self, tmp_path):
        upload = self._mock_upload(tmp_path)
        doc_proc = MagicMock()
        doc_proc.extract_text_only = AsyncMock(return_value="doc text")

        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={"success": True, "message": '{"items": []}'})

        extractor = PaperlessMetadataExtractor(
            mcp_manager=mcp, document_processor=doc_proc,
        )
        extractor._load_upload = AsyncMock(return_value=upload)

        with patch("services.paperless_metadata_extractor.settings") as s:
            s.paperless_extraction_model = ""
            s.ollama_vision_model = ""
            s.ollama_chat_model = ""
            result = await extractor.extract(
                attachment_id=1, session_id="s", lang="de",
            )

        assert result.error is not None
        assert "model" in result.error.lower() or "fehlgeschlagen" in result.error.lower()


# ===========================================================================
# Pydantic models — basic contract checks
# ===========================================================================


class TestDataModels:
    @pytest.mark.unit
    def test_new_entry_proposal_rejects_invalid_field(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            NewEntryProposal(
                field="not-a-valid-field",
                value="X",
                reasoning="...",
            )

    @pytest.mark.unit
    def test_new_entry_proposal_accepts_all_four_dimensions(self):
        for field in ("correspondent", "document_type", "tag", "storage_path"):
            p = NewEntryProposal(field=field, value="X", reasoning="...")
            assert p.field == field

    @pytest.mark.unit
    def test_extraction_result_defaults(self):
        r = ExtractionResult(metadata=PaperlessMetadata())
        assert r.doc_text == ""
        assert r.error is None
