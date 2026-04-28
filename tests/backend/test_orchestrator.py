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
    async def test_post_hook_fires(self):
        """post_orchestration fires with synthesized final_answer + sub_results.

        Note: pre_orchestration does NOT fire inside ``run_orchestrated``.
        It is fired upstream by the caller (chat_handler / Teams transport)
        before sub_queries are determined, so plugins can inject a
        pre-computed plan. Firing again here would create double-firing
        semantics handlers would have to guard against. See the
        orchestrator-uplift design doc for the rationale.
        """
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

        # pre_orchestration is NOT fired inside run_orchestrated anymore.
        assert len(captured["pre"]) == 0

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


# ============================================================================
# _run_sub_agent: handle list-shaped step.data
# ============================================================================

class TestRunSubAgentListData:
    """Sub-agent step tagging must survive list/scalar step.data payloads.

    Real prod failure: JQL search returns step.data as a list, sub-agent
    crashed with `list indices must be integers or slices, not str` because
    the tag injection assumed dict.
    """

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_list_step_data_does_not_crash_sub_agent(self):
        from services.agent_service import AgentStep
        from services.orchestrator import QueryOrchestrator

        # Build a sub-agent that yields one list-data step + a final_answer
        async def _fake_run(*args, **kwargs):
            yield AgentStep(
                step_number=1, step_type="tool_result",
                tool="jira_search", success=True,
                data=[{"key": "REVA-42"}, {"key": "REVA-43"}],  # LIST not dict
            )
            yield AgentStep(
                step_number=2, step_type="final_answer",
                content="Found 2 tickets",
            )

        primary = _make_role("jira", servers=["jira"])
        primary.has_agent_loop = True
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            result = await orchestrator._run_sub_agent(
                {"role": "jira", "query": "find tickets"},
                _make_ollama("ok"),
                MagicMock(),
                "de",
            )

        # Sub-agent completed, list-shaped data preserved as-is
        assert result["role"] == "jira"
        assert result["answer"] == "Found 2 tickets"
        assert len(result["steps"]) == 2
        assert result["steps"][0].data == [{"key": "REVA-42"}, {"key": "REVA-43"}]
        # Dict-shaped final_answer step got tagged
        assert result["steps"][1].data == {"sub_agent_role": "jira"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_dict_step_data_still_tagged(self):
        """Regression: dict-shaped data must still get sub_agent_role injected."""
        from services.agent_service import AgentStep
        from services.orchestrator import QueryOrchestrator

        async def _fake_run(*args, **kwargs):
            yield AgentStep(
                step_number=1, step_type="tool_result",
                tool="get_release", success=True,
                data={"id": "REL-100", "status": "QA"},
            )

        primary = _make_role("release", servers=["release"])
        primary.has_agent_loop = True
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"),
                MagicMock(),
                "de",
            )

        assert result["steps"][0].data["sub_agent_role"] == "release"
        assert result["steps"][0].data["id"] == "REL-100"


# ============================================================================
# Phase 1 (orchestrator-uplift) — extend_orchestrator_roles hook
# ============================================================================

class TestExtendOrchestratorRolesHook:
    """Plugins can extend the planner's role list via extend_orchestrator_roles."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handler_role_names_added_to_planner_prompt(self):
        """A handler returning extra role names must merge them into the role list."""
        from utils.hooks import register_hook

        # Two roles: smart_home (has_loop=True) and release (has_loop=False, would
        # be excluded by the default agent-loop filter). The hook adds "release"
        # so it appears in the planner prompt.
        smart = _make_role("smart_home", has_loop=True, servers=["homeassistant"])
        release = _make_role("release", has_loop=False, servers=["release"])
        router = _make_router([smart, release])

        async def role_extender(**kw):
            return {"release"}

        register_hook("extend_orchestrator_roles", role_extender)

        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_ollama_url = None
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            await orchestrator.detect_multi_domain("test", ollama)

            # Inspect the kwargs passed to prompt_manager.get — role_descriptions
            # should mention BOTH smart_home and release even though release has
            # no agent loop.
            kwargs = pm.get.call_args.kwargs
            descriptions = kwargs.get("role_descriptions", "")
            assert "smart_home" in descriptions
            assert "release" in descriptions

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handler_unknown_role_name_silently_ignored(self):
        """Names that don't exist in router.roles must be dropped without error."""
        from utils.hooks import register_hook

        smart = _make_role("smart_home", has_loop=True, servers=["homeassistant"])
        router = _make_router([smart])

        async def role_extender(**kw):
            return {"smart_home", "made_up_role_name", "another_unknown"}

        register_hook("extend_orchestrator_roles", role_extender)

        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_ollama_url = None
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            await orchestrator.detect_multi_domain("test", ollama)

            descriptions = pm.get.call_args.kwargs.get("role_descriptions", "")
            assert "smart_home" in descriptions
            assert "made_up_role_name" not in descriptions
            assert "another_unknown" not in descriptions

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handler_non_iterable_return_does_not_crash(self):
        """A misbehaving handler returning a non-iterable must be ignored."""
        from utils.hooks import register_hook

        smart = _make_role("smart_home", has_loop=True, servers=["homeassistant"])
        router = _make_router([smart])

        async def bad_extender(**kw):
            return 42  # non-iterable, non-None

        register_hook("extend_orchestrator_roles", bad_extender)

        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_ollama_url = None
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            # Must not raise — non-iterable handler return is logged and ignored.
            result = await orchestrator.detect_multi_domain("test", ollama)
            assert result is None  # planner returned "null"

            descriptions = pm.get.call_args.kwargs.get("role_descriptions", "")
            assert "smart_home" in descriptions  # default eligibility still applies

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handler_returns_none_uses_default_set(self):
        """A handler returning None means 'no extension' — default eligibility wins."""
        from utils.hooks import register_hook

        smart = _make_role("smart_home", has_loop=True, servers=["homeassistant"])
        media = _make_role("media", has_loop=True, servers=["jellyfin"])
        router = _make_router([smart, media])

        async def noop_extender(**kw):
            return None

        register_hook("extend_orchestrator_roles", noop_extender)

        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_ollama_url = None
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            await orchestrator.detect_multi_domain("test", ollama)

            descriptions = pm.get.call_args.kwargs.get("role_descriptions", "")
            assert "smart_home" in descriptions
            assert "media" in descriptions


# ============================================================================
# Phase 1 (orchestrator-uplift) — pre_sub_agent / post_sub_agent hooks
# ============================================================================

class TestSubAgentHooks:
    """``_run_sub_agent`` fires pre_sub_agent (with mutable tool_registry) and
    post_sub_agent (whose return-dicts merge into result.plugin_data)."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_pre_sub_agent_fires_with_step_role_registry(self):
        """pre_sub_agent receives the step dict, role name, and tool_registry."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        captured = []

        async def pre_handler(**kw):
            captured.append(kw)
            return None

        register_hook("pre_sub_agent", pre_handler)

        primary = _make_role("smart_home", servers=["homeassistant"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="done")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            await orchestrator._run_sub_agent(
                {"role": "smart_home", "query": "Mach Licht an"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        assert len(captured) == 1
        assert captured[0]["step"] == {"role": "smart_home", "query": "Mach Licht an"}
        assert captured[0]["role"] == "smart_home"
        assert captured[0]["tool_registry"] is not None
        assert captured[0]["lang"] == "de"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_post_sub_agent_fires_with_completed_result(self):
        """post_sub_agent receives the populated result dict (role, query, answer, steps)."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        captured = []

        async def post_handler(**kw):
            captured.append(kw)
            return None

        register_hook("post_sub_agent", post_handler)

        primary = _make_role("media", servers=["jellyfin"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="abgespielt")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            await orchestrator._run_sub_agent(
                {"role": "media", "query": "Spiel Musik"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        assert len(captured) == 1
        result = captured[0]["result"]
        assert result["role"] == "media"
        assert result["query"] == "Spiel Musik"
        assert result["answer"] == "abgespielt"
        assert len(result["steps"]) == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_post_sub_agent_return_dicts_merged_into_plugin_data(self):
        """Each handler's return-dict is merged into result.plugin_data."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        async def first(**kw):
            return {"contacts": [{"name": "Alice"}]}

        async def second(**kw):
            return {"provenance": ["release@1.3.5"]}

        async def third(**kw):
            return None  # non-dict return values are skipped

        register_hook("post_sub_agent", first)
        register_hook("post_sub_agent", second)
        register_hook("post_sub_agent", third)

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        assert result["plugin_data"] == {
            "contacts": [{"name": "Alice"}],
            "provenance": ["release@1.3.5"],
        }

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_pre_sub_agent_can_mutate_tool_registry(self):
        """A handler can mutate the per-task registry (e.g. tool pre-selection)."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        async def preselect_handler(**kw):
            registry = kw["tool_registry"]
            # Simulate a tool-pre-selection mutation.
            registry.preselected = ["get_release", "list_releases"]
            return None

        register_hook("pre_sub_agent", preselect_handler)

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        class _Registry:
            pass

        registry_instance = _Registry()

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            MockReg.create = AsyncMock(return_value=registry_instance)
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent

            await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        assert registry_instance.preselected == ["get_release", "list_releases"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_failure_returns_failed_result(self):
        """A raising ``AgentToolRegistry.create()`` fails the sub-agent cleanly
        with a localized error.

        Continuing with a half-populated registry would cause hard-to-diagnose
        "tool not found" errors downstream that don't trace back to the
        original tool-registry init failure. Better to surface the error here.
        """
        from services.agent_service import AgentStep

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        # The mock agent.run should NOT be called when create() fails.
        agent_run_called = {"value": False}

        async def _fake_run(*args, **kwargs):
            agent_run_called["value"] = True
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            MockReg.create = AsyncMock(
                side_effect=RuntimeError("plugin tool registration failed")
            )
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        # Sub-agent did NOT run (registry creation failed).
        assert agent_run_called["value"] is False
        # Result has the canonical failure shape with a localized error.
        assert result["answer"] == ""
        assert result["error"] is not None
        assert "release" in result["error"].lower() or "Tools" in result["error"]
        assert result["plugin_data"] == {}
        assert result["steps"] == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_failure_localized_english(self):
        """Localized error message picks English for ``lang='en'``."""
        from services.agent_service import AgentStep

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            MockReg.create = AsyncMock(
                side_effect=RuntimeError("registration failed")
            )
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "en",
            )

        assert "failed to load" in result["error"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_pre_sub_agent_failure_does_not_break_sub_agent(self):
        """A raising pre_sub_agent is logged and ignored — the agent still runs."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        async def broken(**kw):
            raise RuntimeError("preselect failed")

        register_hook("pre_sub_agent", broken)

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        assert result["answer"] == "ok"  # sub-agent still completed

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_post_sub_agent_failure_does_not_break_sub_agent(self):
        """A raising post_sub_agent leaves plugin_data empty but doesn't crash."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        async def broken(**kw):
            raise RuntimeError("drain failed")

        register_hook("post_sub_agent", broken)

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        assert result["answer"] == "ok"
        assert result["plugin_data"] == {}


# ============================================================================
# Phase 1 (orchestrator-uplift) — extended planner detection coverage
# ============================================================================

class TestPlannerDetectionExtended:
    """Coverage gaps from the design's Phase 1 test plan."""

    def _patch(self, pm, s):
        pm.get.return_value = "detect prompt"
        s.agent_ollama_url = None
        s.ollama_model = "test-model"
        s.agent_router_timeout = 10.0

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_three_role_plan_detected(self):
        """Planner can return a 3-role plan."""
        roles = [
            _make_role("release", servers=["release"]),
            _make_role("jira", servers=["jira"]),
            _make_role("confluence", servers=["confluence"]),
        ]
        router = _make_router(roles)
        response = json.dumps([
            {"role": "release", "query": "Status Release 1.3.5"},
            {"role": "jira", "query": "Jira-Tickets zu 1.3.5"},
            {"role": "confluence", "query": "Doku zu 1.3.5"},
        ])
        ollama = _make_ollama(response)
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            self._patch(pm, s)
            result = await orchestrator.detect_multi_domain("Bericht 1.3.5", ollama)

        assert result is not None
        assert {sq["role"] for sq in result} == {"release", "jira", "confluence"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_four_role_plan_detected(self):
        """Planner can return a 4-role plan (release + jira + itsm + confluence)."""
        roles = [
            _make_role("release", servers=["release"]),
            _make_role("jira", servers=["jira"]),
            _make_role("itsm", servers=["itsm"]),
            _make_role("confluence", servers=["confluence"]),
        ]
        router = _make_router(roles)
        response = json.dumps([
            {"role": "release", "query": "Status"},
            {"role": "jira", "query": "Tickets"},
            {"role": "itsm", "query": "Incidents"},
            {"role": "confluence", "query": "Doku"},
        ])
        ollama = _make_ollama(response)
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            self._patch(pm, s)
            result = await orchestrator.detect_multi_domain(
                "Vollständiger Status für 1.3.5", ollama,
            )

        assert result is not None
        assert len(result) == 4

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_explicit_null_sentinel_returns_none(self):
        """The planner may emit the literal string 'null' to mean single-role."""
        roles = [_make_role("release"), _make_role("jira")]
        router = _make_router(roles)
        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            self._patch(pm, s)
            result = await orchestrator.detect_multi_domain("test", ollama)

        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_empty_response_returns_none(self):
        """An empty model response is treated as single-role."""
        roles = [_make_role("release"), _make_role("jira")]
        router = _make_router(roles)
        ollama = _make_ollama("")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            self._patch(pm, s)
            result = await orchestrator.detect_multi_domain("test", ollama)

        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_truncated_json_in_prose_returns_none(self):
        """Truncated JSON (no closing bracket) is rejected gracefully."""
        roles = [_make_role("release", servers=["release"]), _make_role("jira", servers=["jira"])]
        router = _make_router(roles)
        # Closing bracket missing — extractor finds no `]`.
        truncated = '[{"role": "release", "query": "Status"}, {"role": "jira"'
        ollama = _make_ollama(truncated)
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            self._patch(pm, s)
            result = await orchestrator.detect_multi_domain("test", ollama)

        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_backslash_in_query_field_handled(self):
        """JSON with backslash-escaped characters in the query field parses cleanly."""
        roles = [_make_role("release", servers=["release"]), _make_role("jira", servers=["jira"])]
        router = _make_router(roles)
        # Query string contains an escaped quote — must round-trip through json.loads.
        response = '[{"role": "release", "query": "Pfad mit \\"Quotes\\""}, ' \
                   '{"role": "jira", "query": "Tickets"}]'
        ollama = _make_ollama(response)
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            self._patch(pm, s)
            result = await orchestrator.detect_multi_domain("test", ollama)

        assert result is not None
        assert len(result) == 2
        assert "Quotes" in result[0]["query"]


# ============================================================================
# Phase 1 (orchestrator-uplift) — parallel sub-agent execution + isolation
# ============================================================================

class TestParallelExecution:
    """``_run_parallel`` covers error isolation and partial-failure metadata."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_one_failing_sub_agent_does_not_kill_others(self):
        """If one sub-agent raises, the other still completes and yields its answer."""
        from services.agent_service import AgentStep

        smart = _make_role("smart_home", servers=["homeassistant"])
        media = _make_role("media", servers=["jellyfin"])
        router = _make_router([smart, media])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _ok(sq, *a, **kw):
            return {
                "role": sq["role"], "query": sq["query"], "answer": "done",
                "steps": [AgentStep(step_number=1, step_type="final_answer", content="done")],
                "plugin_data": {},
            }

        async def _boom(sq, *a, **kw):
            raise RuntimeError("sub-agent failed")

        # First call returns OK, second raises — using gather so this exercises
        # the return_exceptions=True branch.
        async def _fake(sq, *args, **kwargs):
            if sq["role"] == "media":
                return await _boom(sq)
            return await _ok(sq)

        sub_results: list[dict] = []
        with patch.object(orchestrator, "_run_sub_agent", _fake), \
             patch.object(orchestrator, "_emit_combined_answer") as ec:
            async def _empty_combined(*a, **kw):
                if False:
                    yield  # pragma: no cover — generator stub
            ec.return_value = _empty_combined()

            steps = []
            async for step in orchestrator._run_parallel(
                sub_queries=[
                    {"role": "smart_home", "query": "Licht an"},
                    {"role": "media", "query": "Musik"},
                ],
                message="m", ollama=_make_ollama("ok"),
                executor=MagicMock(), lang="de",
                sub_results_out=sub_results,
            ):
                steps.append(step)

        # The failing sub-agent emits an error step but doesn't stop the other.
        error_steps = [s for s in steps if s.step_type == "error"]
        assert len(error_steps) == 1
        assert "media" in error_steps[0].content
        # Both sub-results recorded (even the failed one — with empty answer).
        assert len(sub_results) == 2
        assert any(r["role"] == "smart_home" and r["answer"] == "done" for r in sub_results)
        assert any(r["role"] == "media" and r["answer"] == "" for r in sub_results)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_all_sub_agents_succeed_collects_all_results(self):
        """When every sub-agent returns, all results land in sub_results_out."""
        from services.agent_service import AgentStep

        smart = _make_role("smart_home", servers=["homeassistant"])
        media = _make_role("media", servers=["jellyfin"])
        router = _make_router([smart, media])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _ok(sq, *a, **kw):
            return {
                "role": sq["role"], "query": sq["query"], "answer": f"{sq['role']} ok",
                "steps": [
                    AgentStep(step_number=1, step_type="tool_call", tool="t"),
                    AgentStep(step_number=2, step_type="final_answer", content=f"{sq['role']} ok"),
                ],
                "plugin_data": {},
            }

        sub_results: list[dict] = []
        with patch.object(orchestrator, "_run_sub_agent", _ok), \
             patch.object(orchestrator, "_emit_combined_answer") as ec:
            async def _empty(*a, **kw):
                if False:
                    yield
            ec.return_value = _empty()

            steps = []
            async for step in orchestrator._run_parallel(
                sub_queries=[
                    {"role": "smart_home", "query": "Licht an"},
                    {"role": "media", "query": "Musik"},
                ],
                message="m", ollama=_make_ollama("ok"),
                executor=MagicMock(), lang="de",
                sub_results_out=sub_results,
            ):
                steps.append(step)

        assert {r["role"] for r in sub_results} == {"smart_home", "media"}
        # Tool calls forwarded but per-sub-agent final_answer is suppressed.
        assert any(s.step_type == "tool_call" for s in steps)
        assert not any(s.step_type == "final_answer" for s in steps)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_max_steps_abort_yields_empty_answer_not_exception(self):
        """An agent that hits max_steps without final_answer produces empty answer.

        The convention is: empty `answer` string with a non-empty `steps` list,
        not a raised exception. Synthesizer + post-processing must tolerate this.
        """
        from services.agent_service import AgentStep

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _no_final(*args, **kwargs):
            # Yields tool calls but never a final_answer (max_steps abort).
            yield AgentStep(step_number=1, step_type="tool_call", tool="t1")
            yield AgentStep(step_number=2, step_type="tool_call", tool="t2")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _no_final
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "test"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        assert result["answer"] == ""  # not None, not an exception
        assert len(result["steps"]) == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_invalid_role_returns_empty_result_with_plugin_data(self):
        """A sub-query referring to a role with no agent loop yields the canonical failure shape."""
        primary = _make_role("release", has_loop=False, servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        result = await orchestrator._run_sub_agent(
            {"role": "release", "query": "test"},
            _make_ollama("ok"), MagicMock(), "de",
        )

        # Early-return must include all canonical-shape keys (plugin_data,
        # error) so downstream callers can blindly read them.
        assert result == {
            "role": "release", "query": "test",
            "answer": "", "steps": [], "plugin_data": {}, "error": None,
        }


# ============================================================================
# Phase 1 (orchestrator-uplift) — backwards compat: vanilla Renfield deploy
# ============================================================================

class TestVanillaRenfieldBackwardsCompat:
    """With no plugins registered, behavior must match pre-uplift.

    These tests use *spy* handlers — registered hooks that record their
    invocations and return ``None`` (i.e. they do not mutate any state).
    Spies prove two properties at once:

    1. The new hook fires *do* fire (they are wired up correctly).
    2. With no state-mutating handler in place, the orchestrator's
       behavior is unchanged from the pre-uplift baseline.

    Asserting outcomes alone (e.g. "no card step") would be circumstantial
    — those outcomes also hold if the hook fires don't fire at all. The
    spies make the assertion direct.
    """

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_extend_orchestrator_roles_fires_with_zero_handlers(self):
        """The hook event fires with no handlers — default eligibility wins."""
        from utils.hooks import register_hook

        spy_calls: list[dict] = []

        async def spy(**kw):
            spy_calls.append(kw)
            return None  # observation only — no role extension

        register_hook("extend_orchestrator_roles", spy)

        smart = _make_role("smart_home", has_loop=True, servers=["homeassistant"])
        chat = _make_role("conversation", has_loop=False)
        router = _make_router([smart, chat])
        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_ollama_url = None
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            await orchestrator.detect_multi_domain("test", ollama)

            # Spy received exactly one call — the hook fired.
            assert len(spy_calls) == 1
            assert "roles" in spy_calls[0] and "lang" in spy_calls[0]

            # Default eligibility preserved (spy returned None → no extension).
            descriptions = pm.get.call_args.kwargs.get("role_descriptions", "")
            assert "smart_home" in descriptions
            assert "conversation" not in descriptions

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_pre_and_post_sub_agent_each_fire_once_per_sub_agent(self):
        """pre_sub_agent and post_sub_agent each fire exactly once per ``_run_sub_agent`` call."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        pre_calls: list[dict] = []
        post_calls: list[dict] = []

        async def pre_spy(**kw):
            pre_calls.append(kw)
            return None  # no mutation, no plugin_data contribution

        async def post_spy(**kw):
            post_calls.append(kw)
            return None  # no plugin_data contribution

        register_hook("pre_sub_agent", pre_spy)
        register_hook("post_sub_agent", post_spy)

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        class _Registry:
            pass

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            MockReg.create = AsyncMock(return_value=_Registry())
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        # Each hook fired exactly once (one sub-agent → one fire).
        assert len(pre_calls) == 1
        assert len(post_calls) == 1
        # Post fires AFTER pre (state at post-fire reflects sub-agent completion).
        assert pre_calls[0]["role"] == "release"
        assert post_calls[0]["result"]["answer"] == "ok"
        # Spy returned None → no plugin_data contribution → empty dict preserved.
        assert result["plugin_data"] == {}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_post_orchestration_fires_with_zero_handlers_no_card_step(self):
        """post_orchestration fires once; with no handler returning a card, no card step yields."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        spy_calls: list[dict] = []

        async def spy(**kw):
            spy_calls.append(kw)
            return None  # no card

        register_hook("post_orchestration", spy)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        async def _empty(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch.object(orchestrator, "_run_parallel", _empty), \
             patch("services.orchestrator.settings") as s:
            s.agent_orchestrator_parallel = True

            steps = []
            async for step in orchestrator.run_orchestrated(
                sub_queries=[{"role": "smart_home", "query": "x"}],
                message="m", ollama=_make_ollama("ok"),
                executor=MagicMock(), lang="de",
            ):
                steps.append(step)

        # Hook fired once.
        assert len(spy_calls) == 1
        # No card step yielded (spy returned None, no card payload).
        assert not any(s.step_type == "card" for s in steps)
        # final_answer still streamed normally.
        assert any(s.step_type == "final_answer" for s in steps)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_pre_orchestration_does_not_fire_inside_run_orchestrated(self):
        """``run_orchestrated`` no longer fires ``pre_orchestration`` itself.

        The hook is owned by the upstream caller (chat_handler / Teams
        transport) so plugins can inject a pre-computed plan before
        sub_queries are determined. This test pins that contract: a
        registered ``pre_orchestration`` handler is NOT called from
        within ``run_orchestrated``.
        """
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        pre_calls: list[dict] = []

        async def pre_spy(**kw):
            pre_calls.append(kw)
            return None

        register_hook("pre_orchestration", pre_spy)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        async def _empty(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch.object(orchestrator, "_run_parallel", _empty), \
             patch("services.orchestrator.settings") as s:
            s.agent_orchestrator_parallel = True

            async for _ in orchestrator.run_orchestrated(
                sub_queries=[{"role": "smart_home", "query": "x"}],
                message="m", ollama=_make_ollama("ok"),
                executor=MagicMock(), lang="de",
            ):
                pass

        # The spy was registered but never invoked from inside run_orchestrated.
        assert len(pre_calls) == 0


# ============================================================================
# Phase 1 (orchestrator-uplift) — sequential mode parity with parallel mode
# ============================================================================

class TestSequentialMode:
    """``_run_sequential`` delegates to ``_run_sub_agent`` so the new hook
    surface fires identically to parallel mode."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_sequential_fires_pre_and_post_sub_agent(self):
        """Sequential mode must fire the new hooks per sub-agent (parity with parallel)."""
        from utils.hooks import register_hook

        pre_calls: list[dict] = []
        post_calls: list[dict] = []

        async def pre_spy(**kw):
            pre_calls.append(kw)
            return None

        async def post_spy(**kw):
            post_calls.append(kw)
            return None

        register_hook("pre_sub_agent", pre_spy)
        register_hook("post_sub_agent", post_spy)

        smart = _make_role("smart_home", servers=["homeassistant"])
        media = _make_role("media", servers=["jellyfin"])
        router = _make_router([smart, media])
        orchestrator = QueryOrchestrator(router, MagicMock())

        # Stub _run_sub_agent so the test focuses on hook firing — the
        # real pre/post fires are exercised by TestSubAgentHooks. Here we
        # only verify _run_sequential reaches them via delegation.
        call_count = {"value": 0}

        async def _fake_sub_agent(sq, *a, **kw):
            call_count["value"] += 1
            # Mimic real _run_sub_agent firing the hooks.
            from utils.hooks import run_hooks
            await run_hooks("pre_sub_agent", step=sq, role=sq["role"],
                            tool_registry=MagicMock(), lang="de")
            result = {
                "role": sq["role"], "query": sq["query"],
                "answer": "done", "steps": [], "plugin_data": {},
            }
            await run_hooks("post_sub_agent", step=sq, role=sq["role"],
                            result=result, lang="de")
            return result

        async def _fake_combined(*a, **kw):
            if False:
                yield  # pragma: no cover

        with patch.object(orchestrator, "_run_sub_agent", _fake_sub_agent), \
             patch.object(orchestrator, "_emit_combined_answer", return_value=_fake_combined()):
            sub_results: list[dict] = []
            async for _ in orchestrator._run_sequential(
                sub_queries=[
                    {"role": "smart_home", "query": "Licht"},
                    {"role": "media", "query": "Musik"},
                ],
                message="m", ollama=_make_ollama("ok"),
                executor=MagicMock(), lang="de",
                sub_results_out=sub_results,
            ):
                pass

        # Both hooks fired twice (one per sub-agent), in the right order.
        assert call_count["value"] == 2
        assert len(pre_calls) == 2
        assert len(post_calls) == 2
        assert {c["role"] for c in pre_calls} == {"smart_home", "media"}
        assert {c["role"] for c in post_calls} == {"smart_home", "media"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_sequential_collects_plugin_data(self):
        """Sequential mode collects the same ``plugin_data`` shape as parallel."""
        smart = _make_role("smart_home", servers=["homeassistant"])
        router = _make_router([smart])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_sub_agent(sq, *a, **kw):
            return {
                "role": sq["role"], "query": sq["query"],
                "answer": "ok", "steps": [],
                "plugin_data": {"contacts": [{"name": "Test"}]},
            }

        async def _fake_combined(*a, **kw):
            if False:
                yield  # pragma: no cover

        with patch.object(orchestrator, "_run_sub_agent", _fake_sub_agent), \
             patch.object(orchestrator, "_emit_combined_answer", return_value=_fake_combined()):
            sub_results: list[dict] = []
            async for _ in orchestrator._run_sequential(
                sub_queries=[{"role": "smart_home", "query": "x"}],
                message="m", ollama=_make_ollama("ok"),
                executor=MagicMock(), lang="de",
                sub_results_out=sub_results,
            ):
                pass

        assert len(sub_results) == 1
        assert sub_results[0]["plugin_data"] == {"contacts": [{"name": "Test"}]}


# ============================================================================
# Sub-query shape validation (B5 sink-side)
# ============================================================================

class TestSubQueryValidation:
    """Buggy plugin plans must not crash _run_sub_agent — return failed result."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_non_dict_subquery_returns_failed_result(self):
        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        # Plugin returned a string instead of a dict.
        result = await orchestrator._run_sub_agent(
            "this is not a dict",  # type: ignore[arg-type]
            _make_ollama("ok"), MagicMock(), "de",
        )

        assert result["error"] is not None
        assert "not a dict" in result["error"]
        assert result["role"] == "?"
        assert result["plugin_data"] == {}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_subquery_missing_role_returns_failed_result(self):
        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        result = await orchestrator._run_sub_agent(
            {"query": "no role key"},
            _make_ollama("ok"), MagicMock(), "de",
        )

        assert result["error"] is not None
        assert "missing role" in result["error"].lower()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_subquery_missing_query_returns_failed_result(self):
        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        result = await orchestrator._run_sub_agent(
            {"role": "release"},
            _make_ollama("ok"), MagicMock(), "de",
        )

        assert result["error"] is not None
        assert "missing role or query" in result["error"].lower()


# ============================================================================
# B4 — _emit_combined_answer "all sub-agents failed" branch
# ============================================================================

class TestEmitCombinedAllFailed:
    """The all-failed branch must yield a localized final_answer step.

    Returning silently here would leave full_response="", which gates
    both the WebSocket final bubble AND DB persistence — losing the
    whole turn including the user's message. Tests pin both languages.
    """

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_all_failed_yields_localized_de_message(self):
        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())
        ollama = _make_ollama("")

        # All sub_results have empty answer (all-failed signal).
        sub_results = [
            _failed_sub_result_dict("smart_home", "Licht", "boom"),
            _failed_sub_result_dict("media", "Musik", "boom"),
        ]

        steps = []
        async for step in orchestrator._emit_combined_answer(
            "msg", sub_results, ollama, "de",
        ):
            steps.append(step)

        finals = [s for s in steps if s.step_type == "final_answer"]
        assert len(finals) == 1
        assert finals[0].content  # non-empty
        # Localized — German indicators from the actual all-failed message
        assert any(token in finals[0].content.lower()
                   for token in ("keine", "antwort", "versuch", "betroffen"))

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_all_failed_yields_localized_en_message(self):
        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())
        ollama = _make_ollama("")

        sub_results = [
            _failed_sub_result_dict("smart_home", "lights", "boom"),
            _failed_sub_result_dict("media", "music", "boom"),
        ]

        steps = []
        async for step in orchestrator._emit_combined_answer(
            "msg", sub_results, ollama, "en",
        ):
            steps.append(step)

        finals = [s for s in steps if s.step_type == "final_answer"]
        assert len(finals) == 1
        assert finals[0].content  # non-empty


def _failed_sub_result_dict(role: str, query: str, error: str | None = None) -> dict:
    """Local helper mirroring orchestrator._failed_sub_result for test setup."""
    from services.orchestrator import _failed_sub_result
    return _failed_sub_result(role, query, error=error)


# ============================================================================
# I4 — list-shaped plugin_data fields are concatenated, not overwritten
# ============================================================================

class TestPluginDataMergeSemantics:
    """post_sub_agent contributions: list-shaped fields concatenate; non-list
    fields follow last-writer-wins with a warning."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_list_shaped_field_concatenated_across_handlers(self):
        """Two handlers contributing to ``contacts`` produce a merged list."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        async def first(**kw):
            return {"contacts": [{"name": "Alice"}]}

        async def second(**kw):
            return {"contacts": [{"name": "Bob"}]}

        register_hook("post_sub_agent", first)
        register_hook("post_sub_agent", second)

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        # BOTH contacts present — not last-writer-wins.
        names = [c["name"] for c in result["plugin_data"]["contacts"]]
        assert names == ["Alice", "Bob"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_non_list_field_collision_last_writer_wins(self):
        """Non-list keys: second handler overwrites first."""
        from utils.hooks import register_hook
        from services.agent_service import AgentStep

        async def first(**kw):
            return {"telemetry_run_id": "run-1"}

        async def second(**kw):
            return {"telemetry_run_id": "run-2"}

        register_hook("post_sub_agent", first)
        register_hook("post_sub_agent", second)

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _fake_run(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _fake_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        assert result["plugin_data"]["telemetry_run_id"] == "run-2"


# ============================================================================
# I7 — extend_orchestrator_roles caching
# ============================================================================

class TestExtendOrchestratorRolesCaching:
    """The hook fires once per QueryOrchestrator lifetime, not per request."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_extend_orchestrator_roles_cached_after_first_call(self):
        from utils.hooks import register_hook

        call_count = {"value": 0}

        async def counting_handler(**kw):
            call_count["value"] += 1
            return None

        register_hook("extend_orchestrator_roles", counting_handler)

        smart = _make_role("smart_home", servers=["homeassistant"])
        router = _make_router([smart])
        ollama = _make_ollama("null")
        orchestrator = QueryOrchestrator(router, MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "detect prompt"
            s.agent_ollama_url = None
            s.ollama_model = "test-model"
            s.agent_router_timeout = 10.0

            # Three detect calls — the hook should fire only once.
            await orchestrator.detect_multi_domain("a", ollama)
            await orchestrator.detect_multi_domain("b", ollama)
            await orchestrator.detect_multi_domain("c", ollama)

        assert call_count["value"] == 1


# ============================================================================
# typing_callback (design Resolved-Q2)
# ============================================================================

class TestTypingCallback:
    """run_orchestrated invokes typing_callback once before sub-agents launch."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_typing_callback_fires_before_sub_agents(self):
        from services.agent_service import AgentStep

        events: list[str] = []

        async def typing_cb():
            events.append("typing")

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        async def _empty_runner(*args, **kwargs):
            events.append("sub_agents")
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch.object(orchestrator, "_run_parallel", _empty_runner), \
             patch("services.orchestrator.settings") as s:
            s.agent_orchestrator_parallel = True

            async for _ in orchestrator.run_orchestrated(
                sub_queries=[{"role": "smart_home", "query": "x"}],
                message="m", ollama=_make_ollama("ok"),
                executor=MagicMock(), lang="de",
                typing_callback=typing_cb,
            ):
                pass

        assert events == ["typing", "sub_agents"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_typing_callback_failure_does_not_break_orchestration(self):
        from services.agent_service import AgentStep

        async def broken_typing():
            raise RuntimeError("websocket dead")

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        async def _empty(*args, **kwargs):
            yield AgentStep(step_number=1, step_type="final_answer", content="ok")

        with patch.object(orchestrator, "_run_parallel", _empty), \
             patch("services.orchestrator.settings") as s:
            s.agent_orchestrator_parallel = True

            steps = []
            async for step in orchestrator.run_orchestrated(
                sub_queries=[{"role": "smart_home", "query": "x"}],
                message="m", ollama=_make_ollama("ok"),
                executor=MagicMock(), lang="de",
                typing_callback=broken_typing,
            ):
                steps.append(step)

        # Orchestration completed despite typing_callback failure.
        assert any(s.step_type == "final_answer" for s in steps)


# ============================================================================
# post_sub_agent fires after agent.run crash (try/finally semantics)
# ============================================================================

class TestPostSubAgentFiresAfterCrash:
    """post_sub_agent must fire even when agent.run raises mid-stream."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_post_sub_agent_fires_on_agent_run_crash(self):
        """If agent.run raises after pre_sub_agent fired, post must still fire so
        plugin accumulators (contacts, provenance) get drained instead of leaking."""
        from utils.hooks import register_hook

        post_calls: list[dict] = []

        async def post_spy(**kw):
            post_calls.append(kw)
            return None

        register_hook("post_sub_agent", post_spy)

        primary = _make_role("release", servers=["release"])
        router = _make_router([primary])
        orchestrator = QueryOrchestrator(router, MagicMock())

        async def _crashing_run(*args, **kwargs):
            # Yield one tool_call step before crashing — simulates partial work.
            from services.agent_service import AgentStep
            yield AgentStep(step_number=1, step_type="tool_call", tool="t1")
            raise RuntimeError("agent crashed mid-stream")

        with patch("services.agent_service.AgentService") as MockAS, \
             patch("services.agent_tools.AgentToolRegistry") as MockReg:
            mock_agent = MagicMock()
            mock_agent.run = _crashing_run
            MockAS.return_value = mock_agent
            mock_registry = MagicMock()
            MockReg.create = AsyncMock(return_value=mock_registry)

            result = await orchestrator._run_sub_agent(
                {"role": "release", "query": "status"},
                _make_ollama("ok"), MagicMock(), "de",
            )

        # post_sub_agent fired despite agent.run crashing.
        assert len(post_calls) == 1
        # The result reflects the crash via the error field.
        assert result["error"] is not None
        assert "agent crashed" in result["error"]


# ============================================================================
# Phase 1.5 — synthesis hooks (build_synthesis_context, synthesis_prompt_override)
#               + source-line stripping
# ============================================================================

class TestSynthesisHooks:
    """``_synthesize`` exposes two extension points and one default behavior:
    ``build_synthesis_context`` (append plugin context to the synth prompt's
    collected_data block), ``synthesis_prompt_override`` (replace the entire
    templated prompt), and source-line stripping on the LLM output."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        from utils.hooks import clear_hooks
        clear_hooks()
        yield
        clear_hooks()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_synthesis_context_appended_to_collected_data(self):
        """Plugin's returned text block must be appended to the prompt's collected data."""
        from utils.hooks import register_hook

        captured: dict[str, str] = {}

        async def context_handler(message, sub_results, lang, **_):
            return "<contacts>\n  - Alice\n  - Bob\n</contacts>"

        register_hook("build_synthesis_context", context_handler)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        # Capture the templated prompt by stubbing prompt_manager.get.
        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "DEFAULT_PROMPT"
            s.agent_router_model = "test-model"
            s.ollama_intent_model = "test-model"
            s.ollama_model = "test-model"
            s.orchestrator_synthesis_timeout = 10.0

            sub_results = [
                {"role": "release", "query": "status", "answer": "ok"},
                {"role": "jira", "query": "tickets", "answer": "no tickets"},
            ]

            await orchestrator._synthesize("msg", sub_results, _make_ollama("synthesized"), "de")

            kwargs = pm.get.call_args.kwargs
            captured["sub_results"] = kwargs.get("sub_results", "")

        # Plugin's contacts block must appear after the per-role bullets.
        assert "<contacts>" in captured["sub_results"]
        assert "Alice" in captured["sub_results"]
        # Original bullets still present.
        assert "[release]" in captured["sub_results"]

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_synthesis_context_first_non_none_wins(self):
        """When two handlers return text, the first registration wins."""
        from utils.hooks import register_hook

        async def first_handler(message, sub_results, lang, **_):
            return "BLOCK_FIRST"

        async def second_handler(message, sub_results, lang, **_):
            return "BLOCK_SECOND"

        register_hook("build_synthesis_context", first_handler)
        register_hook("build_synthesis_context", second_handler)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "X"
            s.agent_router_model = s.ollama_intent_model = s.ollama_model = "test-model"
            s.orchestrator_synthesis_timeout = 10.0

            await orchestrator._synthesize(
                "msg",
                [{"role": "release", "query": "q", "answer": "a"}],
                _make_ollama("synthesized"), "de",
            )

            sr = pm.get.call_args.kwargs.get("sub_results", "")

        assert "BLOCK_FIRST" in sr
        assert "BLOCK_SECOND" not in sr

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_synthesis_prompt_override_replaces_template(self):
        """Plugin-supplied prompt is sent verbatim to the LLM; default template is skipped."""
        from utils.hooks import register_hook

        async def override_handler(message, collected_data, lang, **_):
            return f"PLUGIN_PROMPT[message={message}]"

        register_hook("synthesis_prompt_override", override_handler)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        ollama = _make_ollama("synth output")

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "DEFAULT_PROMPT_NEVER_USED"
            s.agent_router_model = s.ollama_intent_model = s.ollama_model = "test-model"
            s.orchestrator_synthesis_timeout = 10.0

            await orchestrator._synthesize(
                "what is the status",
                [{"role": "release", "query": "q", "answer": "a"}],
                ollama, "de",
            )

        # ollama.client.chat received the plugin's prompt, not the default.
        chat_kwargs = ollama.client.chat.call_args.kwargs
        sent_messages = chat_kwargs["messages"]
        assert sent_messages[0]["content"] == "PLUGIN_PROMPT[message=what is the status]"
        # prompt_manager.get was never reached because the override returned non-None.
        assert pm.get.called is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_synthesis_prompt_override_none_falls_through_to_default(self):
        """When all plugins return None, Renfield uses prompt_manager.get default."""
        from utils.hooks import register_hook

        async def noop_handler(message, collected_data, lang, **_):
            return None

        register_hook("synthesis_prompt_override", noop_handler)

        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "DEFAULT_PROMPT"
            s.agent_router_model = s.ollama_intent_model = s.ollama_model = "test-model"
            s.orchestrator_synthesis_timeout = 10.0

            await orchestrator._synthesize(
                "msg",
                [{"role": "release", "query": "q", "answer": "a"}],
                _make_ollama("synth"), "de",
            )

            assert pm.get.called

    @pytest.mark.unit
    def test_strip_source_line_de(self):
        """The DE _Quelle: ..._ pattern is stripped."""
        from services.orchestrator import _strip_source_line
        text = (
            "Hier ist die Antwort.\n"
            "\n"
            "_Quelle: Digital.ai Release_"
        )
        result = _strip_source_line(text)
        assert "Quelle" not in result
        assert "Hier ist die Antwort." in result

    @pytest.mark.unit
    def test_strip_source_line_en(self):
        """The EN _Source: ..._ pattern is stripped."""
        from services.orchestrator import _strip_source_line
        text = "Here is the answer.\n\n_Source: Digital.ai Release_"
        result = _strip_source_line(text)
        assert "Source" not in result
        assert "Here is the answer." in result

    @pytest.mark.unit
    def test_strip_source_line_plural_quellen(self):
        """The DE plural ``Quellen:`` variant is also stripped."""
        from services.orchestrator import _strip_source_line
        text = "Antwort hier.\n\n_Quellen: Release, Jira_"
        assert "Quellen" not in _strip_source_line(text)

    @pytest.mark.unit
    def test_strip_source_line_no_match_passthrough(self):
        """Text without a source line is returned unmodified (modulo trailing ws)."""
        from services.orchestrator import _strip_source_line
        text = "Just an answer with no source line."
        assert _strip_source_line(text) == text

    @pytest.mark.unit
    def test_strip_source_line_inline_quelle_not_stripped(self):
        """Only line-anchored Quelle: matches are stripped — inline mentions stay."""
        from services.orchestrator import _strip_source_line
        text = "Die Quelle: ist offiziell und vertrauenswürdig."
        # Inline (not at line start with surrounding markers) — should pass through.
        # Note: regex is anchored to ^\s* so a sentence starting "Die Quelle:" doesn't match.
        assert "Quelle" in _strip_source_line(text)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_strip_applied_after_synthesis(self):
        """End-to-end: a synth response containing _Quelle: ..._ is stripped before return."""
        orchestrator = QueryOrchestrator(_make_router([]), MagicMock())

        ollama = _make_ollama("Here is the synthesized answer.\n\n_Quelle: Test_")

        with patch("services.orchestrator.prompt_manager") as pm, \
             patch("services.orchestrator.settings") as s, \
             patch("services.orchestrator.get_classification_chat_kwargs", return_value={}):
            pm.get.return_value = "PROMPT"
            s.agent_router_model = s.ollama_intent_model = s.ollama_model = "test-model"
            s.orchestrator_synthesis_timeout = 10.0

            answer = await orchestrator._synthesize(
                "msg",
                [{"role": "release", "query": "q", "answer": "a"}],
                ollama, "de",
            )

        assert "Quelle" not in (answer or "")
        assert "synthesized answer" in (answer or "")


# ============================================================================
# Phase 1 (orchestrator-uplift) — backwards compat: vanilla Renfield deploy
# ============================================================================
