"""The pre-selection must log WHAT it chose -- not how much.

🛑 The occasion, measured in the log on 2026-10-26. On "scanne nach xidra" the
agent reached for the Paperless search three times. All the log said was:

    13:20:52  Tool pre-selection: 3/33 tools selected for: 'scanne nach xidra'

From that line it cannot be decided whether `scan_document` was not offered at
all or merely not chosen -- that is, whether the pre-selection or the agent was
wrong. Two entirely different repairs. The diagnosis therefore cost a run of
its own with a log line retrofitted.

A count without names is close to worthless for a diagnosis. This test holds
that the names are there.

🛑 Negative control measured: against the old f-string line
`test_the_log_line_names_the_chosen_tools` fails ("`mcp.scanner.scan_document`
missing from the line"). `test_the_log_line_still_carries_count_and_message`
stays GREEN while it does, and that is deliberate -- the old line carried the
count and the occasion too; it secures the move to loguru, not the names.
"""
import json
from types import SimpleNamespace

import pytest
from loguru import logger

from services.agent_service import AgentService
from services.agent_tools import AgentToolRegistry, ToolDefinition

pytestmark = [pytest.mark.unit, pytest.mark.backend]

#: More than the six below which the pre-selection does not run at all.
ALL_TOOLS = [
    "mcp.scanner.scan_document",
    "mcp.paperless.search_documents",
    "internal.list_rooms",
    "mcp.radio.search_stations",
    "internal.play_radio",
    "mcp.ha.call_service",
    "mcp.email.send",
    "mcp.calendar.list_events",
]
PROMPT_MARKER = "VORAUSWAHL-PROMPT-MARKE"
CHOSEN = ["mcp.scanner.scan_document", "internal.list_rooms"]


def _service() -> AgentService:
    """An instance with a real registry -- only the LLM stage is stubbed."""
    svc = AgentService.__new__(AgentService)
    r = AgentToolRegistry.__new__(AgentToolRegistry)
    r._tools = {
        n: ToolDefinition(name=n, description=f"does {n}") for n in ALL_TOOLS
    }
    svc.tool_registry = r
    return svc


@pytest.fixture
def log_lines():
    """Captures the real loguru lines -- after the arguments were substituted."""
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    yield lines
    logger.remove(sink)


@pytest.fixture
def staged_client(monkeypatch):
    """The stages around the LLM call, so that it reaches the line."""
    import services.agent_service as mod

    # 🛑 The template carries a marker AND the tool list, so a test can check
    # the CONTENT of the prompt excerpt and not merely that a line arrived.
    monkeypatch.setattr(
        mod.prompt_manager, "get",
        lambda *a, **k: f"{PROMPT_MARKER} {k.get('message', '')} {k.get('tool_list', '')}",
    )

    async def chat(**kwargs):
        return SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(CHOSEN))
        )

    return SimpleNamespace(chat=chat)


@pytest.mark.asyncio
async def test_the_log_line_names_the_chosen_tools(log_lines, staged_client):
    chosen = await _service()._preselect_tools(
        "scanne nach xidra", staged_client, "irgendein-modell",
    )
    assert set(chosen) == set(CHOSEN), (
        "the preselection itself did not deliver what was expected — then "
        f"this test checks the wrong thing: {sorted(chosen or [])}")

    # 🛑 Check for "tools selected for:", not for "Tool pre-selection": the
    # PROMPT line and the fallback line carry that prefix too. With
    # `agent_preselect_prompt_dump` switched on, these tests would otherwise
    # grab the wrong line and fail misleadingly -- uncovered by the switch's
    # own negative control.
    line = next((z for z in log_lines if "tools selected for:" in z), None)
    assert line is not None, f"no preselection line in the log: {log_lines}"

    for name in CHOSEN:
        assert name in line, (
            f"`{name}` is missing from the line — then the log cannot "
            f"tell whether the tool was offered:\n{line}")


@pytest.mark.asyncio
async def test_the_log_line_still_carries_count_and_message(log_lines, staged_client):
    """The names are ADDED; the count and the occasion stay.

    🛑 The line was moved from an f-string to loguru arguments. If a `%s` ends
    up there, or an argument stays unsubstituted, a raw `{}` arrives exactly
    here (see `test_loguru_placeholders.py` for the class of defect).
    """
    await _service()._preselect_tools(
        "scanne nach xidra", staged_client, "irgendein-modell",
    )
    line = next(z for z in log_lines if "tools selected for:" in z)
    # 🛑 The input fingerprint MUST be in the line -- it is the entire
    # deliverable of #1413. Without this assertion no test notices the silent
    # case: `logger.info` with one argument too few formats the line cleanly,
    # just without `in=`. The opposite direction (one placeholder too many)
    # raises and is swallowed by the bare `except Exception` -- the
    # pre-selection then falls back to ALL tools and reports that as a model
    # error. Found in the review of #1413.
    assert "(in=" in line, (
        f"the input fingerprint is missing from the line:\n{line}")
    assert f"{len(CHOSEN)}/{len(ALL_TOOLS)}" in line, f"the count is missing:\n{line}"
    assert "scanne nach xidra" in line, f"the occasion is missing:\n{line}"
    assert "{}" not in line, (
        f"an argument was not substituted — the line is blind:\n{line}")


@pytest.mark.asyncio
async def test_the_prompt_dump_is_dark_by_default(log_lines, staged_client):
    """🛑 Dark means: NO line. The prompt carries the user's message IN FULL
    -- the regular line shows only `message[:60]`."""
    await _service()._preselect_tools(
        "scanne nach xidra", staged_client, "irgendein-modell",
    )
    assert not [z for z in log_lines if "PROMPT" in z], (
        "the prompt dump appears without the switch — then the full "
        "message of every turn would stand in the log")


@pytest.mark.asyncio
async def test_the_prompt_dump_shows_the_real_prompt_when_switched_on(
    log_lines, staged_client, monkeypatch,
):
    """Switched on, it yields the prompt a diagnosis can founder on.

    Needed on 2026-10-26: `scanne nach x-idra` reproducibly reached for the
    search, and the input signature established the reproducibility -- but WHAT
    stood in the prompt could be read nowhere, and every reconstruction (6,
    then 10 of 33 tools) chose correctly and thereby proved nothing.
    """
    import services.agent_service as mod
    monkeypatch.setattr(mod.settings, "agent_preselect_prompt_dump", True)

    await _service()._preselect_tools(
        "scanne nach xidra", staged_client, "irgendein-modell",
    )
    lines = [z for z in log_lines if "PROMPT" in z]
    assert lines, "the switch is on, but no line comes"
    assert "(in=" in lines[0], (
        "without the signature the dump cannot be matched to a specific line")
    # 🛑 The CONTENT, not just the existence of the line. This assertion was
    # missing, and a mutation that emitted an EMPTY string instead of the
    # prompt left all ten tests green -- exactly the failure the switch is
    # meant to prevent. Found by mutation testing in the review of #1414.
    assert PROMPT_MARKER in lines[0], (
        f"the dump does not carry the prompt:\n{lines[0][:200]}")
