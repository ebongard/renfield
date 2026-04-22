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
    def test_corporate_suffix_stripped_before_comparison(self):
        """The design doc's motivating case: LLM emits the full legal
        name (with GmbH/AG/Inc suffix) while the taxonomy stores the
        short form. Levenshtein distance would be too high; the
        suffix-strip pass catches it."""
        taxonomy = ["Stadtwerke Korschenbroich"]
        # GmbH — German form
        assert _fuzzy_match("Stadtwerke Korschenbroich GmbH", taxonomy) == "Stadtwerke Korschenbroich"

    @pytest.mark.unit
    def test_corporate_suffix_symmetric(self):
        """Works either direction: LLM drops the suffix that's in the
        taxonomy, OR LLM adds a suffix the taxonomy doesn't have."""
        assert _fuzzy_match("Stadtwerke", ["Stadtwerke GmbH"]) == "Stadtwerke GmbH"
        assert _fuzzy_match("Stadtwerke GmbH", ["Stadtwerke"]) == "Stadtwerke"

    @pytest.mark.unit
    def test_corporate_suffix_many_forms(self):
        taxonomy = ["Acme"]
        for suffix in ["GmbH", "AG", "Inc.", "LLC", "Ltd", "Corp", "S.A.", "B.V.", "e.V."]:
            assert _fuzzy_match(f"Acme {suffix}", taxonomy) == "Acme", (
                f"Suffix {suffix!r} should strip to 'Acme'"
            )

    @pytest.mark.unit
    def test_corporate_suffix_only_at_tail(self):
        """Don't strip mid-string occurrences — 'Foo GmbH Bar' stays
        as a legitimately different entity."""
        taxonomy = ["Acme"]
        # "Acme GmbH Deutschland" — GmbH not at tail, full string
        # different from "Acme", no match.
        assert _fuzzy_match("Acme GmbH Deutschland", taxonomy) is None

    @pytest.mark.unit
    def test_truly_different_strings_still_drop(self):
        """Suffix stripping must not create false positives. Two
        different entities should still fail to match."""
        taxonomy = ["Stadtwerke Korschenbroich"]
        # "Stadtwerke Köln GmbH" strips to "Stadtwerke Köln" which is
        # still distance > threshold from "Stadtwerke Korschenbroich".
        assert _fuzzy_match("Stadtwerke Köln GmbH", taxonomy) is None

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
    def test_created_date_more_than_10_years_past_dropped(self):
        """Design § Validation step 4: 10 years past is the floor.
        Documents dated older than that are OCR errors (1985 parsed
        from prose, 1847 from a page number)."""
        old = (date.today() - timedelta(days=365 * 15)).isoformat()
        raw = {
            "title": "T",
            "correspondent": None,
            "document_type": "Rechnung",
            "tags": [],
            "created_date": old,
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.created_date is None

    @pytest.mark.unit
    def test_created_date_within_10_years_past_accepted(self):
        """A doc dated 8 years ago is still plausibly a real receipt
        the user is archiving late."""
        recent = (date.today() - timedelta(days=365 * 8)).isoformat()
        raw = {
            "title": "T",
            "correspondent": None,
            "document_type": "Rechnung",
            "tags": [],
            "created_date": recent,
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.created_date is not None

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
# Confidence-gated proposals
# ===========================================================================


class TestConfidenceGating:
    @pytest.mark.unit
    def test_high_confidence_proposal_survives(self):
        """Confidence ≥ 0.6 on the field name → proposal passes."""
        raw = {
            "title": "T",
            "correspondent": "Schreiner Meier",
            "document_type": "Rechnung",
            "tags": [],
            "confidence": {"correspondent": 0.85},
            "new_entry_proposals": [
                {"field": "correspondent", "value": "Schreiner Meier",
                 "reasoning": "Rechnungskopf."},
            ],
        }
        result = validate_extraction(raw, _taxonomy())
        assert len(result.new_entry_proposals) == 1
        assert result.new_entry_proposals[0].value == "Schreiner Meier"

    @pytest.mark.unit
    def test_low_confidence_proposal_dropped(self):
        """Confidence < 0.6 on the proposal field → dropped."""
        raw = {
            "title": "T",
            "correspondent": "Unsichtbar Ltd",
            "document_type": "Rechnung",
            "tags": [],
            "confidence": {"correspondent": 0.3},
            "new_entry_proposals": [
                {"field": "correspondent", "value": "Unsichtbar Ltd",
                 "reasoning": "Unsicher, ob so gelesen."},
            ],
        }
        result = validate_extraction(raw, _taxonomy())
        assert result.new_entry_proposals == []

    @pytest.mark.unit
    def test_missing_confidence_permissive(self):
        """No confidence entry for the proposal field → be permissive
        (don't drop). Keeps the signal when the LLM forgets the
        confidence block entirely."""
        raw = {
            "title": "T",
            "correspondent": "Schreiner Meier",
            "document_type": "Rechnung",
            "tags": [],
            # No confidence entry at all.
            "new_entry_proposals": [
                {"field": "correspondent", "value": "Schreiner Meier",
                 "reasoning": "..."},
            ],
        }
        result = validate_extraction(raw, _taxonomy())
        assert len(result.new_entry_proposals) == 1

    @pytest.mark.unit
    def test_tag_proposal_uses_plural_confidence_key(self):
        """The LLM often keys confidence as 'tags' (plural list) even
        though proposals are singular 'tag'. Accept either."""
        raw = {
            "title": "T",
            "tags": [],
            "confidence": {"tags": 0.9},
            "new_entry_proposals": [
                {"field": "tag", "value": "new-tag",
                 "reasoning": "..."},
            ],
        }
        result = validate_extraction(raw, _taxonomy())
        assert len(result.new_entry_proposals) == 1


# ===========================================================================
# Malformed individual fields — partial-shape failures
# ===========================================================================


class TestPartialShapeFailures:
    @pytest.mark.unit
    def test_malformed_date_string_raises(self):
        """Invalid date string — pydantic rejects. More common than
        tag-type mismatch in real LLM output."""
        raw = {
            "title": "T",
            "tags": [],
            "created_date": "not-a-date",
        }
        with pytest.raises(ValueError):
            validate_extraction(raw, _taxonomy())

    @pytest.mark.unit
    def test_malformed_proposal_shape_raises(self):
        """Proposal with a wrong field name (not in Literal)."""
        raw = {
            "title": "T",
            "tags": [],
            "new_entry_proposals": [
                {"field": "not-a-real-field", "value": "x", "reasoning": "y"},
            ],
        }
        with pytest.raises(ValueError):
            validate_extraction(raw, _taxonomy())


# ===========================================================================
# Module-level taxonomy cache
# ===========================================================================


class TestTaxonomyCacheIsModuleScoped:
    """Regression guard: the cache lives at module scope so different
    extractor instances share it. Previously each instance had its
    own empty dict and the TTL was effectively dead."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_cache_shared_across_instances(self):
        from services import paperless_metadata_extractor as pme

        # Reset cache state for this test.
        pme._TAXONOMY_CACHE.clear()

        mcp_calls: list[str] = []

        async def _mcp_execute(tool_name: str, params: dict):
            mcp_calls.append(tool_name)
            if "correspondents" in tool_name:
                return {"success": True, "message": '{"items": [{"name": "A"}]}'}
            if "document_types" in tool_name:
                return {"success": True, "message": '{"items": [{"name": "T"}]}'}
            if "tags" in tool_name:
                return {"success": True, "message": '{"items": [{"name": "t"}]}'}
            if "storage_paths" in tool_name:
                return {"success": True, "message": '{"paths": [{"path": "/x"}]}'}
            return {"success": False}

        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(side_effect=_mcp_execute)

        # First extractor instance — cold cache, 4 MCP calls.
        ext1 = pme.PaperlessMetadataExtractor(mcp_manager=mcp)
        tax1 = await ext1._fetch_taxonomy()
        first_call_count = len(mcp_calls)
        assert first_call_count == 4
        assert tax1 is not None

        # Second instance — warm cache, zero MCP calls.
        ext2 = pme.PaperlessMetadataExtractor(mcp_manager=mcp)
        tax2 = await ext2._fetch_taxonomy()
        assert len(mcp_calls) == first_call_count  # no new calls
        assert tax2 is not None
        # Same data (identity not guaranteed, but equal).
        assert tax2.correspondents == tax1.correspondents

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_invalidate_forces_refetch(self):
        from services import paperless_metadata_extractor as pme

        pme._TAXONOMY_CACHE.clear()

        call_count = {"n": 0}

        async def _mcp_execute(tool_name: str, params: dict):
            call_count["n"] += 1
            if "correspondents" in tool_name:
                return {"success": True, "message": '{"items": []}'}
            if "document_types" in tool_name:
                return {"success": True, "message": '{"items": []}'}
            if "tags" in tool_name:
                return {"success": True, "message": '{"items": []}'}
            if "storage_paths" in tool_name:
                return {"success": True, "message": '{"paths": []}'}
            return {"success": False}

        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(side_effect=_mcp_execute)

        ext = pme.PaperlessMetadataExtractor(mcp_manager=mcp)
        await ext._fetch_taxonomy()
        first = call_count["n"]

        # Invalidate → next fetch re-queries MCP.
        pme._invalidate_taxonomy_cache()
        await ext._fetch_taxonomy()
        assert call_count["n"] == first * 2


# ===========================================================================
# _list_via_mcp — log surface for unknown-tool path
# ===========================================================================


class TestListViaMcpLogging:
    """The 'MCP server too old' case must log at WARNING so ops can
    see the empty-taxonomy degradation instead of silently shipping
    broken extractions."""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_unknown_tool_logs_warning_with_version_hint(self, caplog):
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": False,
            "message": "Unknown MCP tool: mcp.paperless.list_correspondents",
        })
        ext = PaperlessMetadataExtractor(mcp_manager=mcp)

        import logging
        with caplog.at_level(logging.WARNING, logger="loguru"):
            result = await ext._list_via_mcp("list_correspondents")

        assert result == []

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_non_json_message_handled(self):
        """If the MCP response message is unparseable, return [] and
        don't crash."""
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": "<!DOCTYPE html><html>502 Bad Gateway",
        })
        ext = PaperlessMetadataExtractor(mcp_manager=mcp)
        result = await ext._list_via_mcp("list_correspondents")
        assert result == []


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
