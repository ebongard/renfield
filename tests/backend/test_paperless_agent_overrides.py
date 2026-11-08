"""The agent's own Paperless values go through the taxonomy validation.

``forward_attachment_to_paperless`` accepts ``correspondent`` /
``document_type`` / ``tags`` from the agent. They used to be merged over the
VALIDATED extraction afterwards, so a tag name that does not exist in
Paperless reached ``upload_document`` raw -- and Paperless refused the whole
upload after the user had approved the confirm card, whose decision list
never named those tags (2026-11-08, a fresh instance with an empty taxonomy:
four agent tags, one created, nothing filed).

Now the overrides replace the LLM's values BEFORE ``validate_extraction``:
a known name resolves, an unknown one becomes a resolution the card asks
about, and nothing unvalidated is left in ``post_fuzzy``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.chat_upload_tool import forward_attachment_to_paperless
from services.paperless_metadata_extractor import (
    PaperlessMetadataExtractor,
    PaperlessTaxonomy,
)
from tests.backend.test_chat_upload_tool_paperless import (
    _make_session_factory,
    _upload_stub,
)

pytestmark = [pytest.mark.unit]

_LLM_JSON = json.dumps(
    {
        "title": "Police",
        "correspondent": "Irgendwer",
        "document_type": "Vertrag",
        "tags": ["vertrag"],
    }
)


def _patched_extractor(taxonomy: PaperlessTaxonomy):
    """Patch the extractor's I/O seams; the validation stays real."""
    return patch.multiple(
        PaperlessMetadataExtractor,
        _load_upload=AsyncMock(),
        _extract_text=AsyncMock(return_value="Versicherungsschein ..."),
        _fetch_taxonomy=AsyncMock(return_value=taxonomy),
        _fetch_learned_examples=AsyncMock(return_value=[]),
        _call_llm=AsyncMock(return_value=_LLM_JSON),
    )


@pytest.mark.asyncio
async def test_unknown_agent_tags_become_resolutions_not_raw_upload_tags(tmp_path):
    upload = _upload_stub(tmp_path, filename="police.pdf", size=200_000)
    mcp = MagicMock()
    mcp.execute_tool = AsyncMock()
    session_factory = _make_session_factory(upload, capture_writes=True)

    with (
        _patched_extractor(PaperlessTaxonomy()),
        patch(
            "services.chat_upload_tool._get_confirms_used",
            AsyncMock(return_value=0),
        ),
        patch("services.database.AsyncSessionLocal", session_factory),
    ):
        result = await forward_attachment_to_paperless(
            {
                "attachment_id": 42,
                "correspondent": "ACME Versicherung AG",
                "tags": ["Versicherung", "Kfz", "2026"],
            },
            mcp_manager=mcp,
            session_id="s",
            user_id=1,
        )

    assert result["data"]["action_required"] == "paperless_confirm"
    pending = next(
        w
        for w in session_factory.captured_adds  # type: ignore[attr-defined]
        if type(w).__name__ == "PaperlessPendingConfirm"
    )
    # Nothing unvalidated survives into the upload's field set ...
    assert pending.post_fuzzy_output["tags"] == []
    assert pending.post_fuzzy_output["correspondent"] is None
    # ... the agent's names are the decisions the card asks about (the
    # agent wins over the LLM's "vertrag" / "Irgendwer").
    asked = [(r["field"], r["extracted_value"]) for r in pending.proposals]
    assert ("correspondent", "ACME Versicherung AG") in asked
    assert [v for f, v in asked if f == "tag"] == ["Versicherung", "Kfz", "2026"]
    assert mcp.execute_tool.await_count == 0


@pytest.mark.asyncio
async def test_silent_path_uploads_only_known_agent_tags(tmp_path):
    upload = _upload_stub(tmp_path, filename="police.pdf", size=200_000)
    mcp = MagicMock()
    mcp.execute_tool = AsyncMock(return_value={"success": True, "message": '{"task_id": "t-1"}'})

    with (
        _patched_extractor(PaperlessTaxonomy(tags=["Kfz"])),
        patch(
            "services.chat_upload_tool._get_confirms_used",
            AsyncMock(return_value=10_000),
        ),
        patch(
            "services.database.AsyncSessionLocal",
            _make_session_factory(upload),
        ),
        patch("services.chat_upload_tool._poll_paperless_task", AsyncMock(return_value=(None, None))),
    ):
        await forward_attachment_to_paperless(
            {"attachment_id": 42, "tags": ["Kfz", "Unbekannt"]},
            mcp_manager=mcp,
            session_id="s",
            user_id=1,
        )

    uploads = [c for c in mcp.execute_tool.await_args_list if c.args[0] == "mcp.paperless.upload_document"]
    assert uploads, "no upload_document call"
    assert uploads[0].args[1]["tags"] == ["Kfz"]


@pytest.mark.asyncio
async def test_override_wins_over_llm_and_a_comma_string_is_split():
    extractor = PaperlessMetadataExtractor(mcp_manager=MagicMock())
    taxonomy = PaperlessTaxonomy(tags=["rechnung", "2026"], document_types=["Rechnung"])
    with _patched_extractor(taxonomy):
        result = await extractor.extract_from_doc_text(
            "text",
            overrides={"tags": "rechnung, 2026", "document_type": "Rechnung", "title": ""},
        )
    assert result.error is None
    assert result.metadata.tags == ["rechnung", "2026"]
    assert result.metadata.document_type == "Rechnung"
    assert result.metadata.title == "Police"  # an empty override does not erase


@pytest.mark.asyncio
async def test_no_overrides_leaves_the_llm_values():
    extractor = PaperlessMetadataExtractor(mcp_manager=MagicMock())
    with _patched_extractor(PaperlessTaxonomy(tags=["vertrag"])):
        result = await extractor.extract_from_doc_text("text")
    assert result.metadata.tags == ["vertrag"]


@pytest.mark.asyncio
async def test_a_malformed_override_is_dropped_not_fatal():
    extractor = PaperlessMetadataExtractor(mcp_manager=MagicMock())
    with _patched_extractor(PaperlessTaxonomy(tags=["vertrag"])):
        result = await extractor.extract_from_doc_text(
            "text",
            overrides={"tags": [1, None, "vertrag"], "correspondent": 42},
        )
    assert result.error is None
    assert result.metadata.tags == ["vertrag"]
