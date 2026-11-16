"""Chat document links (`CHAT_DOCUMENT_LINKS_ENABLED`): a source chip opens its document.

The intent these tests hold:

* flag OFF -> the sources of a turn are exactly what they were before;
* flag ON  -> Paperless tool results and the direct RAG path produce sources,
  and every link is built by the SERVER from the configured PUBLIC base and an
  integer id -- never from anything a tool result or the model supplied, and
  never from the backend-facing API URL.

Access to knowledge-base documents is proven against real Postgres in
`test_chat_document_links_pg.py`.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.websocket.chat_handler as ch
import api.websocket.document_sources as ds
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.unit]

PUBLIC = "https://paperless.example.org"
INTERNAL = "http://paperless-svc.cluster.local:8000"
SEARCH = "mcp.paperless.search_documents"
GET = "mcp.paperless.get_document"


def _mcp(payload) -> list:
    """An MCP tool result as `agent_tool_results` carries it: content blocks
    with the JSON as TEXT (`mcp_client` -> `AgentStep.data`)."""
    return [{"type": "text", "text": json.dumps(payload)}]


def _search(*docs: dict) -> tuple:
    return (SEARCH, _mcp({"summary": {"total_count": len(docs)}, "results": list(docs)}))


def _hit(doc_id, title="Beleg") -> dict:
    return {"id": doc_id, "title": title, "created": "2026-01-02", "snippet": "…"}


@pytest.fixture
def public_base(monkeypatch):
    monkeypatch.setattr(settings, "paperless_public_url", PUBLIC, raising=False)
    monkeypatch.setattr(settings, "paperless_api_url", INTERNAL, raising=False)


@pytest.fixture
def no_public_base(monkeypatch):
    """Only the backend-facing URL is configured -- the state of both instances
    today, and on one of them a cluster-internal name."""
    monkeypatch.setattr(settings, "paperless_public_url", None, raising=False)
    monkeypatch.setattr(settings, "paperless_api_url", INTERNAL, raising=False)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_document_links_enabled", True, raising=False)


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(settings, "chat_document_links_enabled", False, raising=False)


# --------------------------------------------------------------------------- #
# Paperless tool results -> sources
# --------------------------------------------------------------------------- #
class TestPaperlessSources:
    async def test_documents_turn_with_only_paperless_hits_yields_linked_sources(
        self, flag_on, public_base,
    ):
        """THE reported case: the agent searched Paperless, answered, and the
        chat offered nothing to open."""
        sources = await ch._turn_sources([_search(_hit(11, "Rechnung"), _hit(12, "Vertrag"))], [], 7)

        assert sources == [
            {"kind": "paperless", "paperless_document_id": 11, "title": "Rechnung",
             "paperless_url": f"{PUBLIC}/documents/11/details"},
            {"kind": "paperless", "paperless_document_id": 12, "title": "Vertrag",
             "paperless_url": f"{PUBLIC}/documents/12/details"},
        ]

    async def test_no_public_base_means_no_link_and_never_the_internal_name(
        self, flag_on, no_public_base,
    ):
        sources = await ch._turn_sources([_search(_hit(11)), (GET, _mcp(_hit(12)))], [], 7)

        assert [s["paperless_document_id"] for s in sources] == [12, 11]
        assert all("paperless_url" not in s for s in sources)
        assert INTERNAL not in json.dumps(sources)
        assert "cluster.local" not in json.dumps(sources)

    def test_the_one_builder_keeps_its_fallback_for_everybody_else(self, no_public_base):
        """ONE function builds every Paperless link. The document endpoints
        keep the API-URL fallback they had; only `public_only` drops it."""
        from api.routes.knowledge import paperless_document_url

        assert paperless_document_url(5) == f"{INTERNAL}/documents/5/details"
        assert paperless_document_url(5, public_only=True) is None

    def test_with_a_public_base_both_forms_agree(self, public_base):
        from api.routes.knowledge import paperless_document_url

        assert paperless_document_url(5, public_only=True) == paperless_document_url(5)
        assert paperless_document_url(5) == f"{PUBLIC}/documents/5/details"

    def test_trailing_slash_of_the_base_does_not_double(self, monkeypatch):
        monkeypatch.setattr(settings, "paperless_public_url", PUBLIC + "/", raising=False)
        [source] = ds.paperless_sources([_search(_hit(5))])
        assert source["paperless_url"] == f"{PUBLIC}/documents/5/details"

    @pytest.mark.parametrize("bad_id", [
        "11", "11/../../admin", "11?x=1", "https://evil.example/11", 11.0, 11.5,
        True, False, None, 0, -3, 2**31, [11], {"id": 11},
    ])
    def test_an_id_that_is_not_a_plain_integer_is_no_source(self, public_base, bad_id):
        assert ds.paperless_sources([_search(_hit(bad_id))]) == []
        assert ds.paperless_sources([(GET, _mcp(_hit(bad_id)))]) == []

    def test_a_url_inside_a_tool_result_is_never_adopted(self, public_base):
        """Whatever a result says about links -- the link is ours."""
        evil = "https://evil.example/steal"
        poisoned = {
            **_hit(11, evil),
            "url": evil, "paperless_url": evil, "link": evil, "href": evil,
            "base_url": evil, "download_url": evil,
        }
        sources = ds.paperless_sources([_search(poisoned), (GET, _mcp({**poisoned, "id": 12}))])

        assert [s["paperless_url"] for s in sources] == [
            f"{PUBLIC}/documents/12/details", f"{PUBLIC}/documents/11/details",
        ]
        for source in sources:
            assert set(source) == {"kind", "paperless_document_id", "title", "paperless_url"}
            # The URL-shaped TITLE stays what it is: display text.
            assert source["title"] == evil

    def test_a_base_in_the_tool_parameters_cannot_move_the_link(self, public_base):
        """Tool PARAMETERS are model-written; they are not even an input here --
        `agent_tool_results` carries (tool name, result) only."""
        import inspect

        assert list(inspect.signature(ds.paperless_sources).parameters) == ["tool_results"]

    def test_fetched_documents_are_never_capped_and_come_first(self, public_base):
        results = [
            _search(*[_hit(i) for i in range(100, 110)]),
            *[(GET, _mcp(_hit(i))) for i in (1, 2, 3, 4, 5)],
        ]
        ids = [s["paperless_document_id"] for s in ds.paperless_sources(results)]

        assert ids == [1, 2, 3, 4, 5, 100, 101, 102]
        assert ds.PAPERLESS_SEARCH_SOURCE_CAP == 3

    def test_the_cap_is_per_turn_and_shared_rank_wise_between_searches(self, public_base):
        """Three searches in one turn: one hit each, not three of the first."""
        results = [
            _search(_hit(10), _hit(11), _hit(12)),
            _search(_hit(20), _hit(21)),
            _search(_hit(30)),
        ]
        ids = [s["paperless_document_id"] for s in ds.paperless_sources(results)]
        assert ids == [10, 20, 30]

    def test_a_repeated_document_takes_one_chip_and_no_cap_slot(self, public_base):
        results = [
            (GET, _mcp(_hit(10, "A"))),
            _search(_hit(10, "A"), _hit(11), _hit(12)),
            _search(_hit(10, "A"), _hit(11), _hit(13)),
            (GET, _mcp(_hit(10, "A"))),
        ]
        ids = [s["paperless_document_id"] for s in ds.paperless_sources(results)]
        # 10 fetched; rank 0: 10 (dup), 10 (dup); rank 1: 11, 11 (dup); rank 2: 12, 13.
        assert ids == [10, 11, 12, 13]

    def test_the_order_depends_on_the_results_alone(self, public_base):
        results = [_search(_hit(3), _hit(1)), (GET, _mcp(_hit(2))), _search(_hit(9))]
        first = ds.paperless_sources(results)
        assert ds.paperless_sources(results) == first
        assert [s["paperless_document_id"] for s in first] == [2, 3, 9, 1]

    @pytest.mark.parametrize("data", [
        None, "", "not json", [], [{"type": "text", "text": "{\"results\": [{\"id\": 1"}],
        [{"type": "text", "text": "[1, 2]"}], [{"type": "image", "data": "x"}],
        {"results": "nope"}, {"results": [None, 3, "x", []]}, {"error": "Document 5 not found"},
        _mcp({"error": "PAPERLESS_API_URL not configured"}),
    ])
    def test_a_result_of_another_shape_is_no_source_and_no_error(self, public_base, data):
        assert ds.paperless_sources([(SEARCH, data), (GET, data)]) == []

    def test_a_non_text_block_beside_the_json_does_not_cost_the_chips(self, public_base):
        """`mcp_client` records every content block as {"type", "text"} and
        stores `text: None` for one without text (image, embedded resource)."""
        blocks = [
            {"type": "image", "text": None},
            {"type": "text", "text": json.dumps({"results": [_hit(11)]})},
            {"type": "resource", "text": None},
        ]
        [source] = ds.paperless_sources([(SEARCH, blocks)])
        assert source["paperless_document_id"] == 11

    def test_a_text_block_without_text_is_skipped_not_a_type_error(self, public_base):
        blocks = [
            {"type": "text", "text": None},
            {"type": "text", "text": json.dumps(_hit(12))},
        ]
        [source] = ds.paperless_sources([(GET, blocks)])
        assert source["paperless_document_id"] == 12
        assert ch._parse_mcp_raw_data([{"type": "text", "text": None}]) is None

    def test_other_tools_are_not_read_even_with_the_same_shape(self, public_base):
        results = [
            ("mcp.paperless.list_all_documents", _mcp({"results": [_hit(1)]})),
            ("mcp.paperless.update_document", _mcp(_hit(2))),
            ("mcp.evil.get_document", _mcp(_hit(3))),
            ("mcp.paperless.search_documents_x", _mcp({"results": [_hit(4)]})),
        ]
        assert ds.paperless_sources(results) == []

    def test_a_title_is_bounded_and_single_line(self, public_base):
        [source] = ds.paperless_sources([_search(_hit(1, "A\n\nB\t" + "x" * 500))])
        assert source["title"].startswith("A B x")
        assert len(source["title"]) == 200

    def test_a_missing_title_is_empty_not_invented(self, public_base):
        [source] = ds.paperless_sources([(GET, _mcp({"id": 4, "title": None}))])
        assert source["title"] == ""


# --------------------------------------------------------------------------- #
# Flag off: byte-identical
# --------------------------------------------------------------------------- #
class TestFlagOff:
    def test_the_flag_ships_dark(self):
        from utils.config import Settings

        assert Settings.model_fields["chat_document_links_enabled"].default is False

    async def test_sources_are_exactly_the_knowledge_search_sources(
        self, flag_off, public_base, monkeypatch,
    ):
        """Paperless results, RAG results and a Paperless id on the document
        change NOTHING while the flag is off -- and no lookup runs."""
        lookup = AsyncMock(side_effect=AssertionError("no lookup with the flag off"))
        monkeypatch.setattr(ds, "_visible_meta", lookup)
        kb = [
            {"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2},
            {"document_id": 9, "filename": "b.pdf", "title": "B", "tier": None},
        ]
        tool_results = [
            ("internal.knowledge_search", {"results_count": 2, "sources": kb}),
            _search(_hit(11), _hit(12)),
            (GET, _mcp(_hit(13))),
        ]
        rag_results = [{"document": {"id": 21, "filename": "c.pdf", "title": "C"}}]

        before = ch._extract_agent_sources(tool_results)
        after = await ch._turn_sources(tool_results, rag_results, 7)

        assert json.dumps(after, sort_keys=False) == json.dumps(before, sort_keys=False)
        assert after == kb
        # The very same dict objects: nothing was rebuilt on the way.
        assert all(a is b for a, b in zip(after, kb, strict=True))
        lookup.assert_not_called()

    async def test_a_paperless_only_turn_has_no_sources(self, flag_off, public_base):
        assert await ch._turn_sources([_search(_hit(11))], [], 7) == []

    async def test_a_direct_rag_turn_has_no_sources(self, flag_off, public_base):
        rag_results = [{"document": {"id": 21, "filename": "c.pdf", "title": "C"}}]
        assert await ch._turn_sources([], rag_results, 7) == []


# --------------------------------------------------------------------------- #
# Knowledge-base sources get their open target
# --------------------------------------------------------------------------- #
def _meta(**by_id):
    """A stand-in for `_visible_document_meta`: only the ids named are visible."""
    async def lookup(doc_ids, user_id):
        lookup.calls.append((set(doc_ids), user_id))
        return {int(k[1:]): v for k, v in by_id.items() if int(k[1:]) in doc_ids}
    lookup.calls = []
    return lookup


def _row(title="T", paperless=None, tier=2, filename="f.pdf") -> dict:
    return {"title": title, "filename": filename, "tier": tier, "paperless_document_id": paperless}


class TestKnowledgeBaseTargets:
    async def test_a_filed_document_gets_the_paperless_link(self, flag_on, public_base, monkeypatch):
        lookup = _meta(d7=_row(paperless=70), d9=_row(paperless=None))
        monkeypatch.setattr(ds, "_visible_meta", lookup)
        kb = [
            {"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2},
            {"document_id": 9, "filename": "b.pdf", "title": "B", "tier": 0},
        ]

        sources = await ch._turn_sources(
            [("internal.knowledge_search", {"sources": kb})], [], 42,
        )

        assert sources == [
            {**kb[0], "paperless_url": f"{PUBLIC}/documents/70/details"},
            kb[1],
        ]
        # The identity of the TURN decides what is visible, nothing else.
        assert lookup.calls == [({7, 9}, 42)]

    async def test_a_link_already_inside_a_source_is_dropped(self, flag_on, public_base, monkeypatch):
        monkeypatch.setattr(ds, "_visible_meta", _meta(d7=_row(paperless=None)))
        kb = [{"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2,
               "paperless_url": "https://evil.example/x"}]

        [source] = await ch._turn_sources([("internal.knowledge_search", {"sources": kb})], [], 1)

        assert "paperless_url" not in source

    async def test_a_document_not_visible_now_keeps_its_chip_but_gets_no_link(
        self, flag_on, public_base, monkeypatch,
    ):
        monkeypatch.setattr(ds, "_visible_meta", _meta())  # nothing visible
        kb = [{"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2}]

        assert await ch._turn_sources([("internal.knowledge_search", {"sources": kb})], [], 1) == kb

    async def test_a_failing_lookup_costs_the_links_not_the_chips(
        self, flag_on, public_base, monkeypatch,
    ):
        monkeypatch.setattr(ds, "_visible_meta", AsyncMock(side_effect=RuntimeError("db down")))
        kb = [{"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2}]
        tool_results = [("internal.knowledge_search", {"sources": kb}), _search(_hit(11))]
        rag_results = [{"document": {"id": 21}}]

        sources = await ch._turn_sources(tool_results, rag_results, 1)

        # KB chip without link, no RAG chip (visibility unknown), Paperless chip intact.
        assert sources == [
            kb[0],
            {"kind": "paperless", "paperless_document_id": 11, "title": "Beleg",
             "paperless_url": f"{PUBLIC}/documents/11/details"},
        ]

    async def test_a_crash_in_the_new_path_falls_back_to_the_old_sources(
        self, flag_on, monkeypatch,
    ):
        monkeypatch.setattr(ds, "turn_sources", AsyncMock(side_effect=RuntimeError("boom")))
        kb = [{"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2}]

        assert await ch._turn_sources([("internal.knowledge_search", {"sources": kb})], [], 1) == kb

    async def test_the_same_document_from_both_systems_is_one_chip(
        self, flag_on, public_base, monkeypatch,
    ):
        """Knowledge-base document 7 IS Paperless document 70."""
        monkeypatch.setattr(ds, "_visible_meta", _meta(d7=_row(paperless=70)))
        kb = [{"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2}]
        tool_results = [
            ("internal.knowledge_search", {"sources": kb}),
            _search(_hit(70, "A"), _hit(71, "B")),
        ]

        sources = await ch._turn_sources(tool_results, [], 1)

        assert [(s.get("document_id"), s.get("paperless_document_id")) for s in sources] == [
            (7, None), (None, 71),
        ]

    async def test_one_chip_per_document_also_without_a_public_base(
        self, flag_on, no_public_base, monkeypatch,
    ):
        """The de-duplication keys on the id, not on a link that may be absent."""
        monkeypatch.setattr(ds, "_visible_meta", _meta(d7=_row(paperless=70)))
        kb = [{"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2}]
        tool_results = [("internal.knowledge_search", {"sources": kb}), _search(_hit(70))]

        assert await ch._turn_sources(tool_results, [], 1) == kb


# --------------------------------------------------------------------------- #
# The direct RAG path
# --------------------------------------------------------------------------- #
def _rag_result(doc_id, title="chunk title") -> dict:
    return {
        "chunk": {"id": doc_id * 10, "content": "…"},
        "document": {"id": doc_id, "filename": f"{doc_id}.pdf", "title": title, "circle_tier": 0},
        "similarity": 0.9,
    }


class TestRagPathSources:
    async def test_results_become_sources_through_the_visibility_lookup(
        self, flag_on, public_base, monkeypatch,
    ):
        lookup = _meta(d21=_row("Bescheid", paperless=210, tier=1, filename="21.pdf"),
                       d22=_row("Brief", tier=4, filename="22.pdf"))
        monkeypatch.setattr(ds, "_visible_meta", lookup)
        # 21 twice (two chunks of one document), 23 not visible.
        rag_results = [_rag_result(21), _rag_result(23), _rag_result(21), _rag_result(22)]

        sources = await ch._turn_sources([], rag_results, 42)

        assert sources == [
            {"document_id": 21, "filename": "21.pdf", "title": "Bescheid", "tier": 1,
             "paperless_url": f"{PUBLIC}/documents/210/details"},
            {"document_id": 22, "filename": "22.pdf", "title": "Brief", "tier": 4},
        ]
        assert lookup.calls == [({21, 22, 23}, 42)]

    async def test_a_document_of_both_paths_is_one_chip(self, flag_on, public_base, monkeypatch):
        monkeypatch.setattr(ds, "_visible_meta", _meta(d7=_row(), d8=_row()))
        kb = [{"document_id": 7, "filename": "a.pdf", "title": "A", "tier": 2}]

        sources = await ch._turn_sources(
            [("internal.knowledge_search", {"sources": kb})], [_rag_result(7), _rag_result(8)], 1,
        )

        assert [s["document_id"] for s in sources] == [7, 8]

    @pytest.mark.parametrize("results", [
        None, [], [None], [{"document": None}], [{"document": {"id": "21"}}],
        [{"document": {"id": True}}], [{"chunk": {}}],
    ])
    async def test_results_without_a_document_id_are_no_source(
        self, flag_on, public_base, monkeypatch, results,
    ):
        lookup = AsyncMock(side_effect=AssertionError("nothing to look up"))
        monkeypatch.setattr(ds, "_visible_meta", lookup)
        assert await ch._turn_sources([], results, 1) == []


class TestStreamRagResponseHandsOutItsResults:
    """`_stream_rag_response` reports the results the answer was generated FROM."""

    @staticmethod
    async def _gen(chunks):
        for chunk in chunks:
            yield chunk

    def _ollama(self):
        ollama = AsyncMock()
        ollama.chat_stream_with_rag = MagicMock(return_value=self._gen(["Antwort"]))
        ollama.chat_stream = MagicMock(return_value=self._gen(["plain"]))
        return ollama

    async def _run(self, search_results, session_state=None, content="Was steht im Bescheid?"):
        from api.websocket.shared import ConversationSessionState

        state = session_state or ConversationSessionState()
        rag = AsyncMock()
        rag.search.return_value = search_results
        rag.format_context_from_results = MagicMock(return_value="KONTEXT")
        out: list = []
        session = MagicMock()
        session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(ch, "AsyncSessionLocal", session), \
             patch("services.rag_service.RAGService", return_value=rag), \
             patch.object(settings, "rag_enabled", True), \
             patch("services.output_gate.stream_or_gate", AsyncMock(return_value="Antwort")):
            text = await ch._stream_rag_response(
                content, None, self._ollama(), state, AsyncMock(),
                user_id=42, rag_results_out=out,
            )
        return text, out, state, rag

    async def test_a_fresh_search_hands_out_its_results(self):
        results = [_rag_result(21), _rag_result(22)]
        text, out, _state, rag = await self._run(results)

        assert text == "Antwort"
        assert out == results
        assert rag.search.await_args.kwargs["user_id"] == 42

    async def test_no_hit_means_no_results_although_an_answer_streams(self):
        _text, out, _state, _rag = await self._run([])
        assert out == []

    async def test_a_follow_up_hands_out_the_cached_results(self):
        from api.websocket.shared import ConversationSessionState

        state = ConversationSessionState()
        cached = [_rag_result(21)]
        state.update_rag_context("KONTEXT", cached, "Was steht im Bescheid?", kb_id=None)
        with patch.object(ch, "is_followup_question", return_value=True):
            _text, out, _state, rag = await self._run([_rag_result(99)], state, "Und wann?")

        assert out == cached
        rag.search.assert_not_called()

    async def test_without_a_sink_nothing_changes(self):
        """Every existing caller: the parameter is optional."""
        from api.websocket.shared import ConversationSessionState

        rag = AsyncMock()
        rag.search.return_value = [_rag_result(21)]
        rag.format_context_from_results = MagicMock(return_value="KONTEXT")
        session = MagicMock()
        session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        session.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(ch, "AsyncSessionLocal", session), \
             patch("services.rag_service.RAGService", return_value=rag), \
             patch.object(settings, "rag_enabled", True), \
             patch("services.output_gate.stream_or_gate", AsyncMock(return_value="Antwort")):
            text = await ch._stream_rag_response(
                "Frage", None, self._ollama(), ConversationSessionState(), AsyncMock(),
            )
        assert text == "Antwort"


# --------------------------------------------------------------------------- #
# The `done` frame of a real turn
# --------------------------------------------------------------------------- #
class _Role:
    """The routed role, as far as the turn reads it; every other attribute is
    None (no sub-intent, no model override)."""

    def __init__(self, name: str):
        self.name = name
        self.route_layer = "test"
        self.route_confidence = 1.0

    def __getattr__(self, _name):
        return None


def _turn(monkeypatch, role_name: str) -> list[dict]:
    """Run ONE chat turn through the real WebSocket endpoint and return every
    frame up to and including `done`.

    Only the edges are replaced: authentication, the router's verdict, the
    retrieval + generation inside `_stream_rag_response`, and the visibility
    lookup. Which sources reach the `done` frame is decided by the real
    handler.
    """
    async def _fake_auth(_ws, _token=None):
        return {"authenticated": True, "user_id": 42, "auth_method": "jwt"}

    class _AllowAll:
        def check(self, _ip):
            return True, ""

    async def _fake_stream(content, kb_id, ollama, state, websocket, **kwargs):
        # A call site that does not pass the sink produces no sources -- which
        # is exactly what the assertions below would then report.
        sink = kwargs.get("rag_results_out")
        if sink is not None:
            sink.extend([_rag_result(21), _rag_result(23)])
        await websocket.send_json({"type": "stream", "content": "Antwort"})
        return "Antwort"

    # No database in this test: the handler's own sessions (user row lookup)
    # answer "no such row".
    db = AsyncMock()
    no_row = MagicMock()
    no_row.scalar_one_or_none.return_value = None
    db.execute.return_value = no_row
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(ch, "AsyncSessionLocal", session_factory)

    monkeypatch.setattr(ch, "authenticate_websocket", _fake_auth)
    monkeypatch.setattr(ch, "get_rate_limiter", lambda: _AllowAll())
    monkeypatch.setattr(ch, "_stream_rag_response", _fake_stream)
    monkeypatch.setattr(ch, "_retrieve_memory_context", AsyncMock(return_value=""))
    monkeypatch.setattr(ch, "_fetch_document_context", AsyncMock(return_value=""))
    monkeypatch.setattr(
        ds, "_visible_meta",
        _meta(d21=_row("Bescheid", paperless=210, tier=1, filename="21.pdf")),
    )
    monkeypatch.setattr(settings, "agent_enabled", True, raising=False)
    monkeypatch.setattr(settings, "rag_enabled", True, raising=False)
    monkeypatch.setattr(settings, "agent_orchestrator_enabled", False, raising=False)
    monkeypatch.setattr(settings, "followup_chips_enabled", False, raising=False)

    app = FastAPI()
    app.state.ollama = SimpleNamespace(
        default_lang="de",
        # Only the legacy path asks the model for ranked intents.
        extract_ranked_intents=AsyncMock(return_value=[
            {"intent": "general.conversation", "parameters": {}, "confidence": 1.0},
        ]),
    )
    app.state.mcp_manager = None
    # `LEGACY` = no router: the ranked-intent path, the third caller of
    # `_stream_rag_response`.
    app.state.agent_router = None if role_name == LEGACY else SimpleNamespace(
        classify_with_context=AsyncMock(return_value=_Role(role_name)),
        role_for_intent=lambda _intent: None,
        roles={},
    )
    app.include_router(ch.router)

    frames: list[dict] = []
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_json({"type": "text", "content": "Was steht in meinem Bescheid?", "use_rag": True})
        for _ in range(50):
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("type") == "done":
                break
    return frames


LEGACY = "<legacy ranked-intent path>"


class TestDoneFrame:
    @pytest.mark.parametrize("role_name", ["knowledge", "conversation", LEGACY])
    def test_a_direct_rag_turn_carries_sources_on_done(
        self, monkeypatch, flag_on, public_base, role_name,
    ):
        frames = _turn(monkeypatch, role_name)

        done = frames[-1]
        assert done["type"] == "done", [f.get("type") for f in frames]
        # 21 is visible, 23 is not: one chip, with the server-built link.
        assert done["sources"] == [
            {"document_id": 21, "filename": "21.pdf", "title": "Bescheid", "tier": 1,
             "paperless_url": f"{PUBLIC}/documents/210/details"},
        ]

    @pytest.mark.parametrize("role_name", ["knowledge", "conversation", LEGACY])
    def test_with_the_flag_off_done_has_no_sources_key(
        self, monkeypatch, flag_off, public_base, role_name,
    ):
        frames = _turn(monkeypatch, role_name)

        done = frames[-1]
        assert done["type"] == "done", [f.get("type") for f in frames]
        assert "sources" not in done
