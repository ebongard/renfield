"""An empty preselect answer means "this task needs no tool" (#1599).

Until now `[]` fell into the same branch as an unusable answer, and the agent
got ALL tools (108 in the household) for exactly the turns that need none --
arithmetic, small talk. Measured on 2026-11-20 over a month of
agent_trajectories: 34 of 35 such turns called no tool at all.

Behind `AGENT_PRESELECT_EMPTY_MEANS_NONE` (dark) a cleanly parsed EMPTY list
offers none. Three things must hold, and each has its own test here:

1. Off is the old behaviour: `[]` -> `None` (all tools).
2. On, only an empty LIST takes the new path; every other unusable answer
   still falls back to all tools.
3. On, a user correction still pins its tools -- it is the way out of a
   wrong `[]`, and the old code returned before the pinning ran.

And the result `{}` has to stay "none" all the way down: every consumer of
`_preselected_tools` must tell `{}` from `None` without truthiness.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger as loguru_logger

from services.agent_service import AgentService
from services.agent_tools import AgentToolRegistry, ToolDefinition
from utils.config import settings

pytestmark = [pytest.mark.unit, pytest.mark.backend]

ALL_TOOLS = [
    "mcp.scanner.scan_document",
    "mcp.scanner.list_pending_scans",
    "mcp.paperless.search_documents",
    "internal.knowledge_search",
    "mcp.email.send",
    "internal.ingest_status",
    "mcp.weather.forecast",
    "mcp.homeassistant.turn_on",
]


def _service() -> AgentService:
    svc = AgentService.__new__(AgentService)
    r = AgentToolRegistry.__new__(AgentToolRegistry)
    r._tools = {n: ToolDefinition(name=n, description=f"does {n}") for n in ALL_TOOLS}
    svc.tool_registry = r
    return svc


def _client(answer):
    """A client whose preselect answer is `answer` (text) or raises it."""
    async def chat(**kwargs):
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(message=SimpleNamespace(content=answer))
    return SimpleNamespace(chat=chat)


@pytest.fixture
def prompt(monkeypatch):
    import services.agent_service as mod
    monkeypatch.setattr(mod.prompt_manager, "get", lambda *a, **k: "waehle die Werkzeuge")


@pytest.fixture
def log_lines():
    lines: list[tuple[str, str]] = []
    sink = loguru_logger.add(lambda m: lines.append((m.record["level"].name, str(m))), level="INFO")
    yield lines
    loguru_logger.remove(sink)


@pytest.mark.asyncio
async def test_off_an_empty_list_still_means_all_tools(prompt, monkeypatch):
    monkeypatch.setattr(settings, "agent_preselect_empty_means_none", False)
    assert await _service()._preselect_tools("26 plus 16?", _client("[]"), "m") is None


@pytest.mark.asyncio
async def test_on_an_empty_list_offers_none(prompt, monkeypatch, log_lines):
    monkeypatch.setattr(settings, "agent_preselect_empty_means_none", True)
    chosen = await _service()._preselect_tools("26 plus 16?", _client("[]"), "m")
    assert chosen == {}
    assert chosen is not None, "None would mean ALL tools"
    infos = [m for lvl, m in log_lines if lvl == "INFO" and "needs no tool" in m]
    assert len(infos) == 1 and f"none of {len(ALL_TOOLS)}" in infos[0]
    assert not [m for lvl, m in log_lines if lvl == "WARNING"], (
        "an answer is not a failure -- no WARNING")


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    "{}",                                   # an empty OBJECT is not the list
    '"keins"',                              # free text
    "[",                                    # broken JSON
    '["nonexistent.tool"]',                 # only unknown names
    TimeoutError(),
])
async def test_on_every_other_unusable_answer_still_means_all_tools(prompt, monkeypatch, answer):
    monkeypatch.setattr(settings, "agent_preselect_empty_means_none", True)
    assert await _service()._preselect_tools("x", _client(answer), "m") is None


@pytest.mark.asyncio
async def test_on_a_correction_still_pins_its_server_after_an_empty_list(prompt, monkeypatch):
    """The way out of a wrong `[]`: "Korrigieren & neu beantworten"."""
    monkeypatch.setattr(settings, "agent_preselect_empty_means_none", True)
    chosen = await _service()._preselect_tools(
        "scanne nach x-idra", _client("[]"), "m",
        corrected_intent="mcp.scanner.list_pending_scans",
    )
    assert set(chosen) == {"mcp.scanner.scan_document", "mcp.scanner.list_pending_scans"}


def test_none_offered_reaches_the_agent_prompt_and_the_schema_as_none():
    r = _service().tool_registry
    assert r.build_tools_prompt({}) == "KEINE TOOLS VERFÜGBAR."
    assert r.build_tools_schema({}) == []
    # ...and None still means all -- the two must never be confused.
    assert all(n in r.build_tools_prompt(None) for n in ALL_TOOLS)


# --- the class: no consumer may read `_preselected_tools` by truthiness -------

_SOURCE = Path(__file__).resolve().parents[2] / "src/backend/services/agent_service.py"


def _truthiness_reads(source: str) -> list[int]:
    """Lines where `self._preselected_tools` is used as a condition."""
    def is_it(node):
        return isinstance(node, ast.Attribute) and node.attr == "_preselected_tools"

    hits = []
    for node in ast.walk(ast.parse(source)):
        tests = []
        if isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert)):
            tests.append(node.test)
        elif isinstance(node, ast.BoolOp):
            tests.extend(node.values)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            tests.append(node.operand)
        elif isinstance(node, ast.comprehension):
            tests.extend(node.ifs)
        hits.extend(t.lineno for t in tests if is_it(t))
    return hits


def test_no_consumer_reads_the_selection_by_truthiness():
    """`{}` (none) is falsy like `None` (all) -- `if self._preselected_tools:`
    would turn "no tool needed" back into "all tools". The one such read
    before #1599 sat on the step log's tool count."""
    assert _truthiness_reads(_SOURCE.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("line", [
    "if self._preselected_tools:\n    pass",
    "x = a if not self._preselected_tools else b",
    "ok = self._preselected_tools and y",
])
def test_the_truthiness_check_fires(line):
    """Negative control: the shapes it exists for."""
    assert _truthiness_reads(line) != []


def test_the_truthiness_check_lets_the_explicit_forms_through():
    assert _truthiness_reads(
        "if self._preselected_tools is not None:\n    pass\n"
        "if isinstance(self._preselected_tools, dict):\n    pass\n"
    ) == []
