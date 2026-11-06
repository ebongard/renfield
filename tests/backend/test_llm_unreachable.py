"""An unreachable LLM server gets a clear answer and an actionable log (#121).

When Ollama / the OpenAI-compatible server is down, the user used to read the
raw transport error ("Entschuldigung, es gab einen Fehler: [Errno 111]
Connection refused", "LLM-Fehler: All connection attempts failed") and, once
the breaker opened, "... Fehler: Circuit Breaker aktiv" — German in an English
chat. Intended behaviour:

- ONE classification (``is_llm_unreachable``) for "could not connect at all";
  a slow server (read timeout), a server that answered with an error, or a
  dropped connection are NOT "unreachable" and keep their own message.
- The operator log names the URL and how to check it.
- The user gets a plain sentence (de/en) WITHOUT the internal URL: under auth
  every household member reads it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from loguru import logger

from services.ollama_service import OllamaService
from services.prompt_manager import prompt_manager
from utils import llm_client
from utils.circuit_breaker import agent_circuit_breaker, llm_circuit_breaker
from utils.llm_client import (
    configured_llm_url,
    is_llm_unreachable,
    log_llm_unreachable,
)

OLLAMA = "http://ollama.internal:11434"
_REQ = httpx.Request("POST", f"{OLLAMA}/api/chat")


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("All connection attempts failed", request=_REQ)


def _chained(outer: BaseException, cause: BaseException) -> BaseException:
    outer.__cause__ = cause
    return outer


@pytest.fixture(autouse=True)
def _reset_breakers():
    llm_circuit_breaker.reset()
    agent_circuit_breaker.reset()
    yield
    llm_circuit_breaker.reset()
    agent_circuit_breaker.reset()


@pytest.fixture
def log_lines():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG", format="{level} {message}")
    yield lines
    logger.remove(sink)


def _unreachable(lang: str) -> str:
    return prompt_manager.get("chat", "error_llm_unreachable", lang=lang)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestIsLlmUnreachable:
    @pytest.mark.unit
    @pytest.mark.parametrize("exc", [
        _connect_error(),
        httpx.ConnectTimeout("timed out", request=_REQ),
        ConnectionRefusedError(111, "Connection refused"),
        # ollama-python wraps the httpx error into a bare ConnectionError
        ConnectionError("Failed to connect to Ollama. Please check that Ollama is running."),
        # openai: connection error caused by a connect failure
        _chained(openai.APIConnectionError(request=_REQ), _connect_error()),
        # openai: connect timeout is flattened into APITimeoutError
        _chained(openai.APITimeoutError(request=_REQ), httpx.ConnectTimeout("t", request=_REQ)),
        # openai without any chained cause: "Connection error." is all there is
        openai.APIConnectionError(request=_REQ),
        # wrapped by our own code further up
        _chained(RuntimeError("chat failed"), _connect_error()),
    ])
    def test_connection_failures_are_unreachable(self, exc):
        assert is_llm_unreachable(exc) is True

    @pytest.mark.unit
    @pytest.mark.parametrize("exc", [
        # slow, not down
        httpx.ReadTimeout("read timed out", request=_REQ),
        _chained(openai.APITimeoutError(request=_REQ), httpx.ReadTimeout("t", request=_REQ)),
        # the server was reached and dropped the connection
        ConnectionResetError(104, "Connection reset by peer"),
        BrokenPipeError(32, "Broken pipe"),
        _chained(openai.APIConnectionError(request=_REQ),
                 httpx.RemoteProtocolError("peer closed connection", request=_REQ)),
        # the server answered
        openai.InternalServerError(
            "boom", response=httpx.Response(500, request=_REQ), body=None,
        ),
        ValueError("bad JSON"),
        TimeoutError(),
    ])
    def test_other_failures_are_not(self, exc):
        assert is_llm_unreachable(exc) is False

    @pytest.mark.unit
    def test_a_cyclic_chain_terminates(self):
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__cause__, b.__cause__ = b, a
        assert is_llm_unreachable(a) is False


# ---------------------------------------------------------------------------
# Operator log
# ---------------------------------------------------------------------------


class TestLogLlmUnreachable:
    @pytest.mark.unit
    def test_names_the_request_url_and_how_to_check(self, log_lines):
        log_llm_unreachable(_connect_error(), where="Chat", configured_url="http://other:1")
        text = "\n".join(log_lines)
        assert text.startswith("ERROR")
        assert "cannot connect to http://ollama.internal:11434." in text
        assert "curl http://ollama.internal:11434/api/tags" in text
        assert "/v1/models" in text
        assert "OLLAMA_URL" in text
        # the request URL wins over the configured one
        assert "http://other:1" not in text

    @pytest.mark.unit
    def test_never_logs_path_query_or_credentials(self, log_lines):
        req = httpx.Request("POST", "http://user:secret@llm.lan:8080/v1/chat/completions?key=abc")
        log_llm_unreachable(httpx.ConnectError("x", request=req), where="Chat", configured_url="")
        text = "\n".join(log_lines)
        assert "cannot connect to http://llm.lan:8080." in text
        assert "secret" not in text and "key=abc" not in text

    @pytest.mark.unit
    def test_falls_back_to_the_configured_url(self, log_lines):
        log_llm_unreachable(
            ConnectionError("Failed to connect to Ollama"), where="Chat",
            configured_url="http://configured:11434/",
        )
        assert "cannot connect to http://configured:11434." in "\n".join(log_lines)

    @pytest.mark.unit
    def test_an_exception_without_request_is_skipped(self, log_lines):
        # httpx raises RuntimeError on `.request` when none was attached
        log_llm_unreachable(
            httpx.ConnectError("no request"), where="Chat", configured_url="http://cfg:1",
        )
        assert "cannot connect to http://cfg:1." in "\n".join(log_lines)

    @pytest.mark.unit
    def test_an_url_without_port(self, log_lines):
        req = httpx.Request("GET", "https://llm.example/v1/models")
        log_llm_unreachable(httpx.ConnectError("x", request=req), where="Chat", configured_url="")
        assert "cannot connect to https://llm.example." in "\n".join(log_lines)


class TestConfiguredLlmUrl:
    @pytest.mark.unit
    def test_ollama_by_default(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_openai_base_url", None)
        monkeypatch.setattr(llm_client.settings, "ollama_url", OLLAMA)
        assert configured_llm_url("chat") == OLLAMA

    @pytest.mark.unit
    def test_openai_compat_when_the_tier_routes_there(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "llm_openai_base_url", "http://cuda.lan:8080/v1")
        monkeypatch.setattr(llm_client, "use_openai_for_tier", lambda tier: True)
        assert configured_llm_url("chat") == "http://cuda.lan:8080/v1"

    @pytest.mark.unit
    def test_vision_has_its_own_url(self, monkeypatch):
        monkeypatch.setattr(llm_client.settings, "ollama_vision_url", "http://vision:11434")
        assert configured_llm_url("vision") == "http://vision:11434"


# ---------------------------------------------------------------------------
# What the user sees — OllamaService
# ---------------------------------------------------------------------------


def _service(chat_side_effect) -> OllamaService:
    svc = OllamaService.__new__(OllamaService)
    svc.client = MagicMock()
    svc.client.chat = AsyncMock(side_effect=chat_side_effect)
    svc.chat_model = "m"
    svc.rag_model = "m"
    svc.model = "m"
    svc.default_lang = "de"
    return svc


async def _collect(gen) -> str:
    return "".join([c async for c in gen])


class TestOllamaServiceAnswers:
    @pytest.mark.unit
    @pytest.mark.parametrize("lang", ["de", "en"])
    async def test_chat_unreachable(self, lang, log_lines):
        out = await _service(_connect_error()).chat("Hallo", lang=lang)
        assert out == _unreachable(lang)
        assert "connection attempts" not in out and "ollama.internal" not in out
        assert any("cannot connect to http://ollama.internal:11434" in line for line in log_lines)

    @pytest.mark.unit
    async def test_chat_other_error_keeps_the_generic_fallback(self, log_lines):
        out = await _service(ValueError("model exploded")).chat("Hallo", lang="en")
        assert out == prompt_manager.get("chat", "error_fallback", lang="en", error="model exploded")
        assert any("Chat error: model exploded" in line for line in log_lines)

    @pytest.mark.unit
    async def test_chat_stream_unreachable(self):
        out = await _collect(_service(ConnectionRefusedError(111, "refused")).chat_stream("Hi", lang="en"))
        assert out == _unreachable("en")

    @pytest.mark.unit
    async def test_rag_chat_unreachable(self):
        out = await _service(_connect_error()).chat_with_rag("Hi", rag_context="ctx", lang="de")
        assert out == _unreachable("de")

    @pytest.mark.unit
    async def test_rag_stream_unreachable(self):
        out = await _collect(_service(_connect_error()).chat_stream_with_rag("Hi", lang="en"))
        assert out == _unreachable("en")

    @pytest.mark.unit
    async def test_rag_stream_other_error_unchanged(self):
        out = await _collect(_service(ValueError("x")).chat_stream_with_rag("Hi", lang="en"))
        assert out == prompt_manager.get("chat", "error_fallback", lang="en", error="x")

    @pytest.mark.unit
    async def test_vision_stream_unreachable(self, monkeypatch):
        import services.ollama_service as mod
        monkeypatch.setattr(mod.settings, "ollama_vision_model", "vision-m")
        monkeypatch.setattr("utils.llm_client.get_vision_client", lambda: None)
        out = await _collect(_service(_connect_error()).chat_stream_with_image("Was?", "iVBORw0KGgo", lang="de"))
        assert out == _unreachable("de")

    @pytest.mark.unit
    @pytest.mark.parametrize("lang", ["de", "en"])
    async def test_open_breaker_says_unavailable_in_the_chat_language(self, lang, monkeypatch):
        monkeypatch.setattr(llm_circuit_breaker, "allow_request", AsyncMock(return_value=False))
        svc = _service(AssertionError("must not be called"))
        expected = prompt_manager.get("chat", "error_service_unavailable", lang=lang)
        assert await svc.chat("Hi", lang=lang) == expected
        assert await _collect(svc.chat_stream("Hi", lang=lang)) == expected
        assert "Circuit Breaker" not in expected
        svc.client.chat.assert_not_called()

    @pytest.mark.unit
    async def test_open_breaker_on_the_vision_path(self, monkeypatch):
        import services.ollama_service as mod
        monkeypatch.setattr(mod.settings, "ollama_vision_model", "vision-m")
        monkeypatch.setattr(llm_circuit_breaker, "allow_request", AsyncMock(return_value=False))
        out = await _collect(_service(AssertionError()).chat_stream_with_image("x", "iVBORw0KGgo", lang="en"))
        assert out == prompt_manager.get("chat", "error_service_unavailable", lang="en")

    @pytest.mark.unit
    def test_both_languages_carry_the_sentence_without_a_placeholder(self):
        for lang in ("de", "en"):
            text = _unreachable(lang)
            assert text and "{" not in text


# ---------------------------------------------------------------------------
# What the user sees — the agent loop
# ---------------------------------------------------------------------------


class TestAgentLoopUnreachable:
    async def _run(self, monkeypatch, exc, lang="de"):
        from services.agent_service import AgentService
        from services.agent_tools import AgentToolRegistry

        client = MagicMock()
        client.supports_native_tools = False
        client.chat = AsyncMock(side_effect=exc)
        monkeypatch.setattr(
            "services.agent_service.get_agent_client",
            lambda *a, **kw: (client, OLLAMA),
        )
        mcp = MagicMock()
        mcp.get_all_tools.return_value = []
        mcp.get_connected_server_names.return_value = []
        registry = AgentToolRegistry(mcp_manager=mcp, _init_only=True)
        ollama = MagicMock()
        ollama.client = client
        agent = AgentService(registry, max_steps=2)
        return [s async for s in agent.run(
            message="Hallo", ollama=ollama, executor=AsyncMock(), lang=lang,
        )]

    @pytest.mark.unit
    @pytest.mark.parametrize("lang", ["de", "en"])
    async def test_plain_answer_and_log(self, monkeypatch, log_lines, lang):
        steps = await self._run(monkeypatch, ConnectionError("Failed to connect to Ollama"), lang=lang)
        error = next(s for s in steps if s.step_type == "error")
        final = steps[-1]
        assert error.content == _unreachable(lang)
        assert final.step_type == "final_answer"
        assert final.content == _unreachable(lang)
        assert "Failed to connect" not in final.content
        assert any(
            "LLM server unreachable (agent step" in line and OLLAMA in line for line in log_lines
        )

    @pytest.mark.unit
    async def test_other_llm_errors_keep_their_message(self, monkeypatch):
        steps = await self._run(monkeypatch, RuntimeError("model crashed"), lang="en")
        error = next(s for s in steps if s.step_type == "error")
        assert error.content == prompt_manager.get("agent", "error_llm_failed", lang="en", error="model crashed")
        assert "model crashed" in steps[-1].content


class TestBuildFallbackAnswer:
    @pytest.mark.unit
    @pytest.mark.parametrize("lang", ["de", "en"])
    def test_content_override(self, lang):
        from services.agent_service import AgentContext, AgentService
        from services.agent_tools import AgentToolRegistry

        mcp = MagicMock()
        mcp.get_all_tools.return_value = []
        agent = AgentService(AgentToolRegistry(mcp_manager=mcp, _init_only=True))
        ctx = AgentContext(original_message="x")
        step = agent._build_fallback_answer(ctx, 1, "raw", lang=lang, content="Klartext")
        assert step.content == "Klartext"
        assert ctx.outcome == "llm_error"
