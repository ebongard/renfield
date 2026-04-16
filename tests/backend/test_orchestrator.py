"""Tests for Cross-MCP Query Orchestrator."""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "ollama" not in sys.modules:
    sys.modules["ollama"] = MagicMock()

from services.orchestrator import QueryOrchestrator


def _make_role(name: str, has_loop: bool = True, servers: list | None = None):
    role = MagicMock()
    role.name = name
    role.has_agent_loop = has_loop
    role.description = {"de": f"{name} Beschreibung", "en": f"{name} description"}
    role.mcp_servers = servers
    role.internal_tools = None
    role.max_steps = 5
    role.prompt_key = "agent_prompt"
    role.model = None
    role.ollama_url = None
    return role


def _make_router(roles: list):
    router = MagicMock()
    router.roles = {r.name: r for r in roles}
    return router


def _make_ollama(response_text: str):
    mock = MagicMock()
    mock.default_lang = "de"
    mock_response = MagicMock()
    mock_response.message.content = response_text
    mock.client = AsyncMock()
    mock.client.chat = AsyncMock(return_value=mock_response)
    return mock


class TestDetectMultiDomain:

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_single_domain_returns_none(self):
        roles = [_make_role("smart_home"), _make_role("media"), _make_role("conversation", has_loop=False)]
        router = _make_router(roles)
        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain("Wie wird das Wetter?", ollama)
            assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_multi_domain_detected(self):
        roles = [_make_role("smart_home", servers=["homeassistant"]), _make_role("media", servers=["jellyfin"])]
        router = _make_router(roles)
        response = json.dumps([
            {"role": "smart_home", "query": "Licht einschalten"},
            {"role": "media", "query": "Musik spielen"},
        ])
        ollama = _make_ollama(response)
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain(
                "Mach Licht an und spiel Musik", ollama
            )
            assert result is not None
            assert len(result) == 2
            assert result[0]["role"] == "smart_home"
            assert result[1]["role"] == "media"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_invalid_role_filtered(self):
        roles = [_make_role("smart_home")]
        router = _make_router(roles)
        response = json.dumps([
            {"role": "smart_home", "query": "Licht an"},
            {"role": "nonexistent", "query": "something"},
        ])
        ollama = _make_ollama(response)
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain("test", ollama)
            # Only 1 valid role → not multi-domain
            assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        import asyncio
        roles = [_make_role("smart_home"), _make_role("media")]
        router = _make_router(roles)
        ollama = MagicMock()
        ollama.client = AsyncMock()
        ollama.client.chat = AsyncMock(side_effect=asyncio.TimeoutError())
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain("test", ollama)
            assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_json_parse_error_returns_none(self):
        roles = [_make_role("smart_home"), _make_role("media")]
        router = _make_router(roles)
        ollama = _make_ollama("not valid json at all")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_router_url = None
            s.agent_ollama_url = None
            s.agent_router_model = None
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            result = await orchestrator.detect_multi_domain("test", ollama)
            assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_extracts_json_array_from_prose_response(self):
        """3B router models often wrap JSON in prose — the extractor must cope.

        Real failure observed in prod (renfield#374): llama3.2:3b returned a
        German explanation paragraph with the JSON array embedded mid-text
        followed by another sentence. The detector previously crashed on
        json.loads(). Now we find the first `[` and last `]` and parse that
        slice.
        """
        roles = [_make_role("smart_home", servers=["homeassistant"]), _make_role("media", servers=["jellyfin"])]
        router = _make_router(roles)
        prose_wrapped = (
            'Die Anfrage benoetigt mehrere Aktionen:\n\n'
            '[{"role":"smart_home","query":"Licht an"},'
            '{"role":"media","query":"Musik spielen"}]\n\n'
            'Bitte beachten Sie diese Aufteilung.'
        )
        ollama = _make_ollama(prose_wrapped)
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_ollama_url = None
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0
            s.agent_orchestrator_parallel = True

            result = await orchestrator.detect_multi_domain("Mach Licht an und spiel Musik", ollama)
            assert result is not None
            assert len(result) == 2
            assert {sq["role"] for sq in result} == {"smart_home", "media"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_planner_uses_primary_role_model(self):
        """Detection must call the heavy agent model, not the small router.

        Reva ran qwen3.5:27b (the role model) as planner in production. Small
        router models can't reliably emit pure JSON. This test pins that the
        first agent-loop role's `model` is what gets passed to client.chat.
        """
        primary = _make_role("smart_home", servers=["homeassistant"])
        primary.model = "qwen3.5:27b"
        primary.ollama_url = "http://cuda.local:8081/v1"
        roles = [primary, _make_role("media", servers=["jellyfin"])]
        router = _make_router(roles)
        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}), \
             patch("services.orchestrator.get_agent_client") as gac:
            pm.get.return_value = "detect prompt"
            s.ollama_model = "fallback-model"
            s.agent_ollama_url = "http://fallback:1234"
            s.agent_router_timeout = 10.0

            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.message.content = "null"
            mock_client.chat = AsyncMock(return_value=mock_resp)
            gac.return_value = (mock_client, "http://cuda.local:8081/v1")

            await orchestrator.detect_multi_domain("test", ollama)

            gac.assert_called_once_with(fallback_url="http://cuda.local:8081/v1")
            mock_client.chat.assert_called_once()
            assert mock_client.chat.call_args.kwargs["model"] == "qwen3.5:27b"


# ============================================================================
# pre/post_orchestration hooks + card emission
# ============================================================================

class TestOrchestrationHooks:
    """`run_orchestrated` must fire pre/post hooks and forward any card payload."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_pre_and_post_hooks_fire(self):
        """Both hook events fire with the expected kwargs."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        captured = {"pre": [], "post": []}

        async def pre_handler(**kw):
            captured["pre"].append(kw)
            return None

        async def post_handler(**kw):
            captured["post"].append(kw)
            return None

        register_hook("pre_orchestration", pre_handler)
        register_hook("post_orchestration", post_handler)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        async def _empty_runner(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch.object(orchestrator, "_run_parallel", _empty_runner), \
             patch("services.orchestrator.settings") as s:
            s.agent_orchestrator_parallel = True

            steps = []
            async for step in orchestrator.run_orchestrated(
                sub_queries=[{"role": "smart_home", "query": "x"}],
                message="Mach Licht an und spiel Musik",
                ollama=_make_ollama("ok"),
                executor=MagicMock(),
                lang="de",
            ):
                steps.append(step)

        assert len(captured["pre"]) == 1
        assert captured["pre"][0]["message"] == "Mach Licht an und spiel Musik"
        assert captured["pre"][0]["lang"] == "de"
        assert "plan" in captured["pre"][0]

        assert len(captured["post"]) == 1
        assert captured["post"][0]["message"] == "Mach Licht an und spiel Musik"
        assert captured["post"][0]["final_answer"] == "ok"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_post_hook_card_emitted_as_step(self):
        """A post_orchestration handler returning {'card': ...} yields a card step."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        card_payload = {"type": "AdaptiveCard", "version": "1.5", "body": [{"type": "TextBlock", "text": "hi"}]}

        async def card_handler(**kw):
            return {"card": card_payload}

        register_hook("post_orchestration", card_handler)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        async def _empty_runner(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="done")

        with patch.object(orchestrator, "_run_parallel", _empty_runner), \
             patch("services.orchestrator.settings") as s:
            s.agent_orchestrator_parallel = True

            steps = []
            async for step in orchestrator.run_orchestrated(
                sub_queries=[{"role": "smart_home", "query": "x"}],
                message="msg",
                ollama=_make_ollama("ok"),
                executor=MagicMock(),
                lang="de",
            ):
                steps.append(step)

        card_steps = [s for s in steps if s.step_type == "card"]
        assert len(card_steps) == 1
        assert card_steps[0].data == {"card": card_payload}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_post_hook_first_card_wins(self):
        """When multiple handlers return a card, only the first lands as a step."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        async def first(**kw):
            return {"card": {"version": "1.5", "marker": "first"}}

        async def second(**kw):
            return {"card": {"version": "1.5", "marker": "second"}}

        register_hook("post_orchestration", first)
        register_hook("post_orchestration", second)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        async def _empty_runner(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="x")

        with patch.object(orchestrator, "_run_parallel", _empty_runner), \
             patch("services.orchestrator.settings") as s:
            s.agent_orchestrator_parallel = True

            cards = []
            async for step in orchestrator.run_orchestrated(
                sub_queries=[{"role": "smart_home", "query": "x"}],
                message="m", ollama=_make_ollama("x"),
                executor=MagicMock(), lang="de",
            ):
                if step.step_type == "card":
                    cards.append(step.data["card"])

        assert len(cards) == 1
        assert cards[0]["marker"] == "first"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_post_hook_failure_does_not_break_orchestration(self):
        """A raising hook is logged and ignored, final_answer still streams."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        async def broken_handler(**kw):
            raise RuntimeError("boom")

        register_hook("post_orchestration", broken_handler)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        async def _empty_runner(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="answered")

        with patch.object(orchestrator, "_run_parallel", _empty_runner), \
             patch("services.orchestrator.settings") as s:
            s.agent_orchestrator_parallel = True

            steps = []
            async for step in orchestrator.run_orchestrated(
                sub_queries=[{"role": "smart_home", "query": "x"}],
                message="m", ollama=_make_ollama("x"),
                executor=MagicMock(), lang="de",
            ):
                steps.append(step)

        assert any(s.step_type == "final_answer" and s.content == "answered" for s in steps)
        assert not any(s.step_type == "card" for s in steps)


# ============================================================================
# step_to_ws_message: card step -> WebSocket payload
# ============================================================================

class TestCardStepWsMessage:
    @pytest.mark.unit
    def test_card_step_serializes_to_card_ws_message(self):
        from services.agent_service import AgentStep, step_to_ws_message
        card = {"type": "AdaptiveCard", "version": "1.5", "body": []}
        step = AgentStep(step_number=100, step_type="card", data={"card": card})

        msg = step_to_ws_message(step)
        assert msg == {"type": "card", "card": card}

    @pytest.mark.unit
    def test_card_step_with_no_data_yields_null_card(self):
        """Defensive: a malformed card step should not raise."""
        from services.agent_service import AgentStep, step_to_ws_message
        msg = step_to_ws_message(AgentStep(step_number=100, step_type="card"))
        assert msg == {"type": "card", "card": None}
