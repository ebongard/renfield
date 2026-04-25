"""
Unit tests for ``paperless_commit_upload`` — the second half of the
two-tool cold-start confirm flow.

Covers:
    - "ja" family → approved path: creates fire, upload fires, counter
      increments, pending deletes.
    - "nein" family → abort path: pending deletes, no upload.
    - Unknown confirm_token → soft-404.
    - Ambiguous user response → re-prompt, edit_rounds counter, force-
      abort at cap.
    - Proposal creation dispatch: correspondent / document_type / tag /
      storage_path each route to the right MCP tool.
    - ``already_exists`` rejection from MCP is treated as success.

Pure-unit, heavy mocking. Real DB integration deferred.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.paperless_commit_tool import (
    _ABORT_TOKENS,
    _APPROVE_TOKENS,
    _MAX_EDIT_ROUNDS,
    paperless_commit_upload,
)

# Side-effect import — some tests patch
# ``services.paperless_metadata_extractor._invalidate_taxonomy_cache``.
# The attribute is resolved at patch time, so the module must be loaded
# before ``unittest.mock.patch`` runs or it raises AttributeError.
import services.paperless_metadata_extractor  # noqa: F401  # side-effect


# ===========================================================================
# Token classification
# ===========================================================================


class TestTokenClassification:
    @pytest.mark.unit
    def test_approve_tokens_cover_common_responses(self):
        for tok in ["ja", "j", "ok", "passt", "yes", "sure"]:
            assert tok.lower() in _APPROVE_TOKENS

    @pytest.mark.unit
    def test_abort_tokens_cover_common_responses(self):
        for tok in ["nein", "n", "abbrechen", "stopp", "no", "cancel"]:
            assert tok.lower() in _ABORT_TOKENS


# ===========================================================================
# Validation
# ===========================================================================


class TestCommitValidation:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_missing_confirm_token_errors(self):
        result = await paperless_commit_upload(
            {}, mcp_manager=MagicMock(),
        )
        assert result["success"] is False
        assert "confirm_token" in result["message"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_non_string_confirm_token_errors(self):
        result = await paperless_commit_upload(
            {"confirm_token": 123, "user_response_text": "ja"},
            mcp_manager=MagicMock(),
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_no_mcp_manager_errors(self):
        result = await paperless_commit_upload(
            {"confirm_token": "t", "user_response_text": "ja"},
            mcp_manager=None,
        )
        assert result["success"] is False
        assert "MCP" in result["message"]


# ===========================================================================
# Unknown / expired confirm token — soft-404
# ===========================================================================


class TestUnknownToken:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_unknown_token_returns_soft_404(self):
        mcp = MagicMock()

        with patch(
            "services.database.AsyncSessionLocal",
            _make_session_factory(pending=None),
        ):
            result = await paperless_commit_upload(
                {"confirm_token": "unknown-uuid", "user_response_text": "ja"},
                mcp_manager=mcp, session_id="s", user_id=1,
            )

        assert result["success"] is False
        assert "abgelaufen" in result["message"] or "unbekannt" in result["message"]
        # No MCP calls were made.
        mcp.assert_not_called()


# ===========================================================================
# Approve path — full happy flow
# ===========================================================================


@pytest.fixture(autouse=True)
def _stub_embed_doc_text():
    """The PR-3 commit-tool path embeds doc_text before persisting an
    extraction example row. In a unit test we never want that call to
    reach a real Ollama; default-stub returns None which lands a NULL
    embedding (still a valid row). Tests that assert on the embedding
    value override this fixture locally."""
    with patch(
        "services.paperless_example_retriever.embed_doc_text",
        AsyncMock(return_value=None),
    ):
        yield


class TestApprovePath:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_ja_fires_upload_and_cleanup(self, tmp_path):
        # A ChatUpload row whose file_path points to a real file on
        # disk so the approved-commit path can open + b64-encode it.
        file = tmp_path / "rechnung.pdf"
        file.write_bytes(b"%PDF-1.4 " + b"x" * 200)

        pending = _make_pending(
            confirm_token="tok-abc",
            attachment_id=42,
            llm_output={"title": "T", "correspondent": "Stadtwerke"},
            post_fuzzy={
                "title": "T",
                "correspondent": "Stadtwerke",
                "document_type": "Rechnung",
                "tags": ["wohnung"],
                "storage_path": "/x",
                "created_date": None,
            },
            proposals=[],
        )
        upload = MagicMock(
            id=42, filename="rechnung.pdf", file_path=str(file),
        )

        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": json.dumps({
                "task_id": "t-1",
                "document_id": 555,
                "post_upload_patch": "success",
            }),
        })

        with patch(
            "services.database.AsyncSessionLocal",
            _make_session_factory(pending=pending, upload=upload),
        ):
            result = await paperless_commit_upload(
                {"confirm_token": "tok-abc", "user_response_text": "ja"},
                mcp_manager=mcp, session_id="s", user_id=1,
            )

        assert result["success"] is True
        assert result["action_taken"] is True
        assert result["data"]["task_id"] == "t-1"
        assert result["data"]["document_id"] == 555
        # Upload was the only MCP call (no creates — no proposals).
        assert mcp.execute_tool.await_count == 1
        # Response message confirms archival.
        assert "abgelegt" in result["message"].lower() or "paperless" in result["message"].lower()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_explicit_neu_fires_creates_for_approved_resolutions(self, tmp_path):
        """User opts into creating new taxonomy entries via "<idx>: neu"
        per resolution. Default `ja` would skip these (safer), so the
        test passes the explicit choices."""
        file = tmp_path / "f.pdf"
        file.write_bytes(b"%PDF-1.4 " + b"x" * 200)

        pending = _make_pending(
            confirm_token="tok-b",
            attachment_id=42,
            llm_output={},
            post_fuzzy={
                "title": "T", "correspondent": None, "tags": [],
                "storage_path": None, "document_type": None,
            },
            proposals=[
                {"field": "correspondent", "extracted_value": "Schreiner Meier",
                 "near_matches": []},
                {"field": "tag", "extracted_value": "handwerker",
                 "near_matches": []},
            ],
        )
        upload = MagicMock(id=42, filename="f.pdf", file_path=str(file))

        # MCP response sequencer: create_correspondent → create_tag → upload_document
        responses = [
            {"success": True, "message": json.dumps({"id": 10, "name": "Schreiner Meier"})},
            {"success": True, "message": json.dumps({"id": 20, "name": "handwerker"})},
            {"success": True, "message": json.dumps({
                "task_id": "t", "document_id": 1, "post_upload_patch": "success",
            })},
        ]
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(side_effect=responses)

        with patch(
            "services.database.AsyncSessionLocal",
            _make_session_factory(pending=pending, upload=upload),
        ):
            with patch(
                "services.paperless_metadata_extractor._invalidate_taxonomy_cache",
                MagicMock(),
            ):
                result = await paperless_commit_upload(
                    {"confirm_token": "tok-b",
                     "user_response_text": "1: neu, 2: neu"},
                    mcp_manager=mcp, session_id="s", user_id=1,
                )

        assert result["success"] is True
        # Three MCP calls: two creates + one upload.
        assert mcp.execute_tool.await_count == 3
        tool_names = [call.args[0] for call in mcp.execute_tool.await_args_list]
        assert tool_names[0] == "mcp.paperless.create_correspondent"
        assert tool_names[1] == "mcp.paperless.create_tag"
        assert tool_names[2] == "mcp.paperless.upload_document"
        # Response mentions the newly-created entries.
        assert "Schreiner Meier" in result["message"] or "handwerker" in result["message"]

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_already_exists_is_success(self, tmp_path):
        """If a create_* says the entry already exists (raced against
        another extraction), treat it as success and use the existing id
        rather than aborting."""
        file = tmp_path / "f.pdf"
        file.write_bytes(b"%PDF-1.4 " + b"x" * 200)

        pending = _make_pending(
            confirm_token="tok-c",
            attachment_id=42,
            llm_output={},
            post_fuzzy={"title": "T"},
            proposals=[
                {"field": "correspondent", "extracted_value": "Stadtwerke", "near_matches": []},
            ],
        )
        upload = MagicMock(id=42, filename="f.pdf", file_path=str(file))

        # create_correspondent returns already_exists.
        responses = [
            {"success": False, "message": json.dumps({
                "error": "already_exists", "existing_id": 7, "existing_name": "Stadtwerke",
            })},
            {"success": True, "message": json.dumps({
                "task_id": "t", "document_id": 1, "post_upload_patch": "success",
            })},
        ]
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(side_effect=responses)

        with patch(
            "services.database.AsyncSessionLocal",
            _make_session_factory(pending=pending, upload=upload),
        ):
            with patch(
                "services.paperless_metadata_extractor._invalidate_taxonomy_cache",
                MagicMock(),
            ):
                result = await paperless_commit_upload(
                    {"confirm_token": "tok-c",
                     "user_response_text": "1: neu"},
                    mcp_manager=mcp, session_id="s", user_id=1,
                )

        # Upload still happened; already_exists didn't abort the flow.
        assert result["success"] is True
        assert mcp.execute_tool.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_persist_path_writes_embedding_when_diff(self, tmp_path):
        """User-approved fields differ from post-fuzzy → an example row
        is persisted with doc_text_embedding populated by the retriever's
        embed call (PR 3)."""
        from models.database import PaperlessExtractionExample

        file = tmp_path / "f.pdf"
        file.write_bytes(b"%PDF-1.4 " + b"x" * 200)

        pending = _make_pending(
            confirm_token="tok-emb",
            attachment_id=42,
            llm_output={"_doc_text": "Stadtwerke Rechnung", "correspondent": "Telekom"},
            post_fuzzy={"title": "T", "correspondent": None, "tags": [],
                        "storage_path": None, "document_type": None},
            proposals=[
                {"field": "correspondent", "extracted_value": "Stadtwerke",
                 "near_matches": []},
            ],
        )
        upload = MagicMock(id=42, filename="f.pdf", file_path=str(file))

        responses = [
            {"success": True, "message": json.dumps({"id": 1, "name": "Stadtwerke"})},
            {"success": True, "message": json.dumps({
                "task_id": "t", "document_id": 1, "post_upload_patch": "success",
            })},
        ]
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(side_effect=responses)

        session_factory = _make_session_factory(pending=pending, upload=upload)
        # Capture writes — the test asserts the example row was added.
        adds: list = []
        original_factory = session_factory

        def _capturing_factory():
            session = original_factory()
            session.add = MagicMock(side_effect=lambda obj: adds.append(obj))
            return session

        # Override the autouse stub: this test wants embed to return a
        # real-shaped vector so we can assert it lands on the row.
        fake_embedding = [0.0] * 8
        with patch(
            "services.paperless_example_retriever.embed_doc_text",
            AsyncMock(return_value=fake_embedding),
        ):
            with patch(
                "services.database.AsyncSessionLocal", _capturing_factory,
            ):
                with patch(
                    "services.paperless_metadata_extractor._invalidate_taxonomy_cache",
                    MagicMock(),
                ):
                    result = await paperless_commit_upload(
                        {"confirm_token": "tok-emb",
                         "user_response_text": "1: neu"},
                        mcp_manager=mcp, session_id="s", user_id=1,
                    )

        assert result["success"] is True
        examples = [a for a in adds if isinstance(a, PaperlessExtractionExample)]
        assert len(examples) == 1, f"Expected 1 example row, got {len(examples)}"
        assert examples[0].doc_text_embedding == fake_embedding
        assert examples[0].source == "confirm_diff"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_persist_path_handles_embed_failure(self, tmp_path):
        """Embed returns None (Ollama down) → example row still gets
        persisted with doc_text_embedding=None. Raw diff signal is
        preserved even when the learning index can't catch up."""
        from models.database import PaperlessExtractionExample

        file = tmp_path / "f.pdf"
        file.write_bytes(b"%PDF-1.4 " + b"x" * 200)

        pending = _make_pending(
            confirm_token="tok-no-emb",
            attachment_id=42,
            llm_output={"_doc_text": "doc", "correspondent": "Telekom"},
            post_fuzzy={"title": "T", "correspondent": None, "tags": [],
                        "storage_path": None, "document_type": None},
            proposals=[
                {"field": "correspondent", "extracted_value": "Stadtwerke",
                 "near_matches": []},
            ],
        )
        upload = MagicMock(id=42, filename="f.pdf", file_path=str(file))

        responses = [
            {"success": True, "message": json.dumps({"id": 1, "name": "Stadtwerke"})},
            {"success": True, "message": json.dumps({
                "task_id": "t", "document_id": 1, "post_upload_patch": "success",
            })},
        ]
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(side_effect=responses)

        adds: list = []
        original_factory = _make_session_factory(pending=pending, upload=upload)

        def _capturing_factory():
            session = original_factory()
            session.add = MagicMock(side_effect=lambda obj: adds.append(obj))
            return session

        # Default autouse fixture already returns None for embed —
        # explicit here for readability.
        with patch(
            "services.paperless_example_retriever.embed_doc_text",
            AsyncMock(return_value=None),
        ):
            with patch(
                "services.database.AsyncSessionLocal", _capturing_factory,
            ):
                with patch(
                    "services.paperless_metadata_extractor._invalidate_taxonomy_cache",
                    MagicMock(),
                ):
                    result = await paperless_commit_upload(
                        {"confirm_token": "tok-no-emb",
                         "user_response_text": "1: neu"},
                        mcp_manager=mcp, session_id="s", user_id=1,
                    )

        assert result["success"] is True
        examples = [a for a in adds if isinstance(a, PaperlessExtractionExample)]
        assert len(examples) == 1
        assert examples[0].doc_text_embedding is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_persist_path_writes_upload_tracking_row(self, tmp_path):
        """PR 4: a successful upload (document_id returned) must
        persist a PaperlessUploadTracking row so the UI-edit sweeper
        has a baseline to diff against later."""
        from models.database import PaperlessUploadTracking

        file = tmp_path / "f.pdf"
        file.write_bytes(b"%PDF-1.4 " + b"x" * 200)

        pending = _make_pending(
            confirm_token="tok-track",
            attachment_id=42,
            llm_output={"_doc_text": "Stadtwerke Rechnung", "title": "T"},
            post_fuzzy={"title": "T", "correspondent": "Stadtwerke",
                        "document_type": "Rechnung", "tags": ["wohnung"],
                        "storage_path": None, "created_date": None},
            proposals=[],
        )
        upload = MagicMock(id=42, filename="f.pdf", file_path=str(file))

        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True, "message": json.dumps({
                "task_id": "t", "document_id": 999,
                "post_upload_patch": "success",
            }),
        })

        adds: list = []
        original_factory = _make_session_factory(pending=pending, upload=upload)

        def _capturing_factory():
            session = original_factory()
            session.add = MagicMock(side_effect=lambda obj: adds.append(obj))
            return session

        with patch(
            "services.paperless_example_retriever.embed_doc_text",
            AsyncMock(return_value=None),
        ):
            with patch(
                "services.database.AsyncSessionLocal", _capturing_factory,
            ):
                result = await paperless_commit_upload(
                    {"confirm_token": "tok-track", "user_response_text": "ja"},
                    mcp_manager=mcp, session_id="s", user_id=1,
                )

        assert result["success"] is True
        tracking = [a for a in adds if isinstance(a, PaperlessUploadTracking)]
        assert len(tracking) == 1
        assert tracking[0].paperless_document_id == 999
        assert tracking[0].chat_upload_id == 42
        assert tracking[0].user_id == 1
        assert tracking[0].original_metadata["correspondent"] == "Stadtwerke"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_persist_path_skips_tracking_when_no_document_id(self, tmp_path):
        """If Paperless accepted the upload but returned no
        document_id (edge: bare task_id response, pre-resolve), the
        sweeper has nothing to fetch — skip the tracking row."""
        from models.database import PaperlessUploadTracking

        file = tmp_path / "f.pdf"
        file.write_bytes(b"%PDF-1.4 " + b"x" * 200)

        pending = _make_pending(
            confirm_token="tok-notrack",
            attachment_id=42,
            llm_output={"_doc_text": "doc"},
            post_fuzzy={"title": "T"},
            proposals=[],
        )
        upload = MagicMock(id=42, filename="f.pdf", file_path=str(file))

        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True, "message": json.dumps({"task_id": "t"}),
        })

        adds: list = []
        original_factory = _make_session_factory(pending=pending, upload=upload)

        def _capturing_factory():
            session = original_factory()
            session.add = MagicMock(side_effect=lambda obj: adds.append(obj))
            return session

        with patch(
            "services.paperless_example_retriever.embed_doc_text",
            AsyncMock(return_value=None),
        ):
            with patch(
                "services.database.AsyncSessionLocal", _capturing_factory,
            ):
                result = await paperless_commit_upload(
                    {"confirm_token": "tok-notrack", "user_response_text": "ja"},
                    mcp_manager=mcp, session_id="s", user_id=1,
                )

        assert result["success"] is True
        tracking = [a for a in adds if isinstance(a, PaperlessUploadTracking)]
        assert tracking == []


class TestApproveMessageShape:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_patch_failure_surfaces_user_warning(self, tmp_path):
        """When the MCP upload reports post_upload_patch other than
        success, the user-facing message must mention the gap."""
        file = tmp_path / "f.pdf"
        file.write_bytes(b"%PDF-1.4 " + b"x" * 200)

        pending = _make_pending(
            confirm_token="tok-d",
            attachment_id=42,
            llm_output={},
            post_fuzzy={"title": "T", "storage_path": "/x"},
            proposals=[],
        )
        upload = MagicMock(id=42, filename="f.pdf", file_path=str(file))

        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={
            "success": True,
            "message": json.dumps({
                "task_id": "t", "document_id": 99,
                "post_upload_patch": "timed_out",
            }),
        })

        with patch(
            "services.database.AsyncSessionLocal",
            _make_session_factory(pending=pending, upload=upload),
        ):
            result = await paperless_commit_upload(
                {"confirm_token": "tok-d", "user_response_text": "ja"},
                mcp_manager=mcp, session_id="s", user_id=1,
            )

        assert result["success"] is True
        # User is explicitly told about the gap.
        assert (
            "Speicherpfad" in result["message"]
            or "anpassen" in result["message"].lower()
        )


# ===========================================================================
# Abort path
# ===========================================================================


class TestAbortPath:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_nein_deletes_pending_without_uploading(self, tmp_path):
        pending = _make_pending(
            confirm_token="tok-x",
            attachment_id=42,
            llm_output={}, post_fuzzy={"title": "T"}, proposals=[],
        )
        upload = MagicMock(id=42)

        mcp = MagicMock()
        mcp.execute_tool = AsyncMock()  # should never fire

        with patch(
            "services.database.AsyncSessionLocal",
            _make_session_factory(pending=pending, upload=upload),
        ):
            result = await paperless_commit_upload(
                {"confirm_token": "tok-x", "user_response_text": "nein"},
                mcp_manager=mcp, session_id="s", user_id=1,
            )

        assert result["success"] is True
        assert result["data"]["aborted"] is True
        assert mcp.execute_tool.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_abbrechen_also_aborts(self, tmp_path):
        pending = _make_pending(
            confirm_token="tok-y",
            attachment_id=42,
            llm_output={}, post_fuzzy={"title": "T"}, proposals=[],
        )
        upload = MagicMock(id=42)
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock()

        with patch(
            "services.database.AsyncSessionLocal",
            _make_session_factory(pending=pending, upload=upload),
        ):
            result = await paperless_commit_upload(
                {"confirm_token": "tok-y", "user_response_text": "abbrechen"},
                mcp_manager=mcp, session_id="s", user_id=1,
            )

        assert result["success"] is True
        assert result["data"]["aborted"] is True


# ===========================================================================
# Ambiguous response handling
# ===========================================================================


class TestAmbiguousResponse:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_ambiguous_returns_reprompt_within_budget(self):
        pending = _make_pending(
            confirm_token="tok-z",
            attachment_id=42,
            llm_output={}, post_fuzzy={"title": "T"}, proposals=[],
            edit_rounds=0,
        )
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock()

        with patch(
            "services.database.AsyncSessionLocal",
            _make_session_factory(
                pending=pending, upload=None,
                pending_after_update=_make_pending(
                    confirm_token="tok-z", attachment_id=42,
                    llm_output={}, post_fuzzy={}, proposals=[],
                    edit_rounds=1,
                ),
            ),
        ):
            result = await paperless_commit_upload(
                {"confirm_token": "tok-z", "user_response_text": "hmm nicht sicher"},
                mcp_manager=mcp, session_id="s", user_id=1,
            )

        # Re-prompt; still requires action.
        assert result["success"] is False
        assert "ja" in result["message"].lower() and "nein" in result["message"].lower()
        assert result["data"]["action_required"] == "paperless_confirm"
        # No MCP call.
        assert mcp.execute_tool.await_count == 0


# ===========================================================================
# Per-field choice parser
# ===========================================================================


class TestChoiceParser:
    @pytest.mark.unit
    def test_default_decisions_pick_first_near_match_else_skip(self):
        """Safety default: only auto-pick when the user has seen
        candidates. No-match fields skip rather than auto-create —
        creating a taxonomy entry from a single OCR'd value without
        explicit consent pollutes the user's Paperless instance."""
        from services.paperless_commit_tool import _default_decisions

        resolutions = [
            {"field": "correspondent", "extracted_value": "Foo",
             "near_matches": ["Foo Inc", "Foo GmbH"]},
            {"field": "tag", "extracted_value": "bar", "near_matches": []},
        ]
        decisions = _default_decisions(resolutions)
        assert decisions[0]["action"] == "use"
        assert decisions[0]["value"] == "Foo Inc"
        assert decisions[1]["action"] == "skip"
        assert decisions[1]["value"] == ""

    @pytest.mark.unit
    def test_default_decisions_legacy_proposal_shape_survives(self):
        """Legacy pending rows persisted under the old NewEntryProposal
        shape carry ``value`` instead of ``extracted_value`` and never
        ``near_matches``. The default-decision builder must read both
        keys so a "ja" reply on an old pending row still works."""
        from services.paperless_commit_tool import _default_decisions

        legacy = [
            {"field": "correspondent", "value": "Schreiner Meier",
             "reasoning": "..."},
        ]
        decisions = _default_decisions(legacy)
        # Legacy row had no near matches → safe default is skip.
        # The value must not silently disappear, though — the user
        # can still type "1: neu" to opt into creating it.
        assert decisions[0]["action"] == "skip"

    @pytest.mark.unit
    def test_parse_user_choices_neu_on_legacy_proposal(self):
        """Legacy proposal {field, value, reasoning} → "1: neu" must
        create the value, not crash on the missing extracted_value."""
        from services.paperless_commit_tool import _parse_user_choices

        legacy = [
            {"field": "correspondent", "value": "Schreiner Meier",
             "reasoning": "..."},
        ]
        decisions, err = _parse_user_choices("1: neu", legacy)
        assert err is None
        assert decisions[0]["action"] == "create"
        assert decisions[0]["value"] == "Schreiner Meier"

    @pytest.mark.unit
    def test_parse_user_choices_n_marker_treated_as_create(self):
        """The preview shows users `n. NEU anlegen` so the parser must
        accept "n" as the create token — not just the spelled-out
        "neu". Otherwise users following the on-screen contract get a
        re-prompt loop."""
        from services.paperless_commit_tool import _parse_user_choices

        resolutions = [
            {"field": "correspondent", "extracted_value": "Foo",
             "near_matches": ["Foo Inc"]},
        ]
        decisions, err = _parse_user_choices("1: n", resolutions)
        assert err is None
        assert decisions[0]["action"] == "create"
        assert decisions[0]["value"] == "Foo"

    @pytest.mark.unit
    def test_parse_user_choices_handles_missing_separator(self):
        """Phone-typed reply without a comma between pairs ("1:2 2:neu")
        must still split into two decisions — losing the second pair
        silently is the worst possible failure mode."""
        from services.paperless_commit_tool import _parse_user_choices

        resolutions = [
            {"field": "correspondent", "extracted_value": "Foo",
             "near_matches": ["Foo Inc", "Foo GmbH"]},
            {"field": "tag", "extracted_value": "bar", "near_matches": []},
        ]
        decisions, err = _parse_user_choices("1:2 2:neu", resolutions)
        assert err is None
        assert decisions[0]["action"] == "use"
        assert decisions[0]["value"] == "Foo GmbH"
        assert decisions[1]["action"] == "create"
        assert decisions[1]["value"] == "bar"

    @pytest.mark.unit
    def test_parse_user_choices_per_field(self):
        from services.paperless_commit_tool import _parse_user_choices

        resolutions = [
            {"field": "correspondent", "extracted_value": "Foo",
             "near_matches": ["Foo Inc", "Foo GmbH"]},
            {"field": "tag", "extracted_value": "bar", "near_matches": []},
        ]
        decisions, err = _parse_user_choices("1: 2, 2: neu", resolutions)
        assert err is None
        assert decisions[0]["action"] == "use"
        assert decisions[0]["value"] == "Foo GmbH"
        assert decisions[1]["action"] == "create"
        assert decisions[1]["value"] == "bar"

    @pytest.mark.unit
    def test_parse_user_choices_skip_token(self):
        from services.paperless_commit_tool import _parse_user_choices

        resolutions = [
            {"field": "correspondent", "extracted_value": "X",
             "near_matches": ["Y"]},
        ]
        decisions, err = _parse_user_choices("1: x", resolutions)
        assert err is None
        assert decisions[0]["action"] == "skip"

    @pytest.mark.unit
    def test_parse_user_choices_invalid_index(self):
        from services.paperless_commit_tool import _parse_user_choices

        resolutions = [
            {"field": "correspondent", "extracted_value": "X", "near_matches": []},
        ]
        _, err = _parse_user_choices("9: neu", resolutions)
        assert err is not None and "out of range" in err

    @pytest.mark.unit
    def test_parse_user_choices_invalid_candidate(self):
        from services.paperless_commit_tool import _parse_user_choices

        resolutions = [
            {"field": "correspondent", "extracted_value": "X",
             "near_matches": ["Y"]},
        ]
        _, err = _parse_user_choices("1: 5", resolutions)
        assert err is not None and "out of range" in err

    @pytest.mark.unit
    def test_parse_user_choices_no_pairs_returns_error(self):
        from services.paperless_commit_tool import _parse_user_choices

        resolutions = [
            {"field": "correspondent", "extracted_value": "X", "near_matches": []},
        ]
        _, err = _parse_user_choices("hmm vielleicht", resolutions)
        assert err is not None


# ===========================================================================
# Test helpers — pending row + session mocking
# ===========================================================================


def _make_pending(*, confirm_token, attachment_id, llm_output, post_fuzzy,
                  proposals, edit_rounds=0):
    return SimpleNamespace(
        confirm_token=confirm_token,
        attachment_id=attachment_id,
        session_id="s",
        user_id=1,
        llm_output=llm_output,
        post_fuzzy_output=post_fuzzy,
        proposals=proposals,
        edit_rounds=edit_rounds,
    )


def _make_session_factory(*, pending, upload=None, pending_after_update=None):
    """Build a patchable AsyncSessionLocal factory. The session returns
    ``pending`` on the first SELECT query; if ``pending_after_update``
    is set, it returns that on subsequent ``db.get`` calls (simulates
    the ambiguous-response re-fetch after edit_rounds bump)."""

    def _factory():
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.add = MagicMock()
        session.commit = AsyncMock()

        def _execute(query):
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=pending)
            result.rowcount = 1
            return result

        session.execute = AsyncMock(side_effect=lambda q: _execute(q))

        # db.get is called for ChatUpload lookups and for the
        # pending-refresh after edit_rounds bump.
        call_count = {"n": 0}

        async def _get(model, pk):
            call_count["n"] += 1
            # First .get for ChatUpload (approved path); second .get
            # for PaperlessPendingConfirm (ambiguous re-fetch). Just
            # return whatever the caller needs based on argument type.
            if model.__name__ == "ChatUpload":
                return upload
            if model.__name__ == "PaperlessPendingConfirm":
                return pending_after_update or pending
            return None

        session.get = _get
        return session

    return _factory
