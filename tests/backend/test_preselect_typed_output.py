"""The tool preselection bound to a JSON schema (`AGENT_PRESELECT_TYPED`).

The occasion, observed live on 2026-10-28: to "scanne nach x-idra" the
preselection model answered in the shape of a tool CALL (`"query": "xidra"`
with a `"reason"`, without closing brackets) instead of with the list. The
answer was unparsable, the `except` branch returned `None`, and so ALL 33 tools
stood in the agent prompt -- the preselection was not imprecise, it had no
effect.

In the measurement run the same input hit 5/5 correct against the same endpoint
and the same model (90/95 overall). So the misfire was not in the prompt but in
the FREE FORM of the answer -- and that is by construction, not by chance.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

import pytest

from services.agent_service import _build_preselect_schema

pytestmark = pytest.mark.unit


def test_the_schema_binds_a_root_array_so_the_prompt_stays_untouched():
    """🛑 A root array, not an object: the prompt already asks for an array and
    the parser expects one. An object would have forced a prompt change -- then
    flag-off and flag-on differ in TWO variables and the comparison on the
    harness is no longer a comparison."""
    s = _build_preselect_schema(["mcp.a.b", "internal.c"])
    assert s["type"] == "array", s
    assert s["items"]["type"] == "string"


def test_only_the_offered_names_are_allowed():
    """`enum` is the stronger half: an invented name cannot come into being at
    all, instead of falling away silently further down at the registry
    filter."""
    namen = ["mcp.scanner.scan_document", "internal.knowledge_search"]
    s = _build_preselect_schema(namen)
    assert s["items"]["enum"] == namen


def test_maxItems_is_present_because_uniqueItems_is_NOT_enforced():
    """🛑 The finding that makes this binding viable in the first place --
    measured against the live endpoint (cuda.local:8081, qwen3.6):

    `uniqueItems` is accepted but NOT enforced. A model that is pushed towards
    a different shape then repeats the same VALID name until `num_predict` is
    used up -- and the answer comes back TRUNCATED. Unparsable as before, only
    for a new reason. Two out of three adversarial prompts did exactly that.

    With `maxItems` the grammar bounds the array and all three came back
    parsable and valid. Without that key the binding MOVES the failure instead
    of removing it.
    """
    s = _build_preselect_schema(["mcp.a.b"])
    assert "maxItems" in s, (
        "ohne maxItems verschiebt die Bindung den Fehlermodus nur — "
        "siehe Docstring, am lebenden Endpunkt gemessen")
    assert s["maxItems"] >= 5, (
        "der Prompt fragt nach 3-5 Werkzeugen und `_with_required_companions` "
        f"legt noch zu — {s['maxItems']} laesst keinen Spielraum")


def test_an_empty_offer_does_not_produce_a_schema_that_matches_nothing():
    """Without tools the preselection is not called anyway (`<= 6` turns back
    before that). But building it must not raise -- otherwise an edge case
    decides whether the whole path is reachable."""
    s = _build_preselect_schema([])
    assert s["items"]["enum"] == []


def test_the_flag_is_dark_by_default():
    """A new feature is dark. The path with `false` has to be the old one."""
    from utils.config import Settings

    assert Settings.model_fields["agent_preselect_typed"].default is False


def test_the_binding_is_the_ONLY_difference_the_flag_makes():
    """🛑 Checked structurally on the AST, not on the source text: the flag
    branch may set `format` and nothing else. Were it to set something on the
    prompt or on the sampling options as well, the measured comparison (95 %
    bound against 95 % unbound) would be worthless, because it would be
    measuring two things at once.
    """
    from services import agent_service

    # 🛑 `dedent` is mandatory: `getsource` of a METHOD yields indented code,
    # and `ast.parse` raises `IndentationError` on it. This test failed on that
    # before it ever checked anything.
    quelle = textwrap.dedent(
        inspect.getsource(agent_service.AgentService._preselect_tools))
    baum = ast.parse(ast.unparse(ast.parse(quelle)))  # comments fall away

    zuweisungen: list[str] = []
    for knoten in ast.walk(baum):
        if not isinstance(knoten, ast.If):
            continue
        bedingung = ast.unparse(knoten.test)
        if "typed_format" not in bedingung:
            continue
        for k in ast.walk(knoten):
            if isinstance(k, ast.Subscript) and isinstance(k.ctx, ast.Store):
                zuweisungen.append(ast.unparse(k))

    assert zuweisungen, "der Flag-Zweig wurde nicht gefunden — Test oder Code driftet"
    assert all("format" in z for z in zuweisungen), (
        f"der Flag-Zweig setzt mehr als `format`: {zuweisungen}")


def test_the_harness_measures_the_SAME_schema_as_production():
    """🛑 A harness that sends something other than production measures
    something else. `bin/measure-preselect.py` carries the schema as a literal
    so it runs without importing the backend -- and that is exactly why it can
    drift.

    What is compared are the keys that carry the guarantee: `type`, `maxItems`
    and the shape of `items`. The `enum` values themselves come from the prompt
    at runtime in the harness and cannot be equal here.
    """
    skript = pathlib.Path(__file__).resolve().parents[2] / "bin" / "measure-preselect.py"
    baum = ast.parse(skript.read_text(encoding="utf-8"))

    kandidaten = [
        d for d in ast.walk(baum)
        if isinstance(d, ast.Dict)
        and {ast.unparse(k).strip("'\"") for k in d.keys if isinstance(k, ast.Constant)}
        >= {"type", "maxItems", "items"}
    ]
    assert kandidaten, "im Pruefstand steht kein Schema mit type/maxItems/items"

    stand = _build_preselect_schema(["x"])
    for d in kandidaten:
        paare = {
            k.value: v for k, v in zip(d.keys, d.values, strict=False)
            if isinstance(k, ast.Constant)
        }
        assert paare["type"].value == stand["type"]
        assert paare["maxItems"].value == stand["maxItems"], (
            f"maxItems driftet: Pruefstand {paare['maxItems'].value} "
            f"gegen Produktion {stand['maxItems']}")


class TestTheEnumMatchesTheOffer:
    """🛑 The offer and the permitted answer have to be the SAME set.

    `get_tool_names()` can carry names whose registry entry is `None`. Those
    drop out when the prompt lines are built and then do not stand in the
    prompt -- so building the `enum` from `tool_names` permitted a name the
    model never saw. And `filtered` below only checks `name in _tools`, not the
    VALUE: a `None` tool would get through. Found in the `/review` of my own
    diff, before the flag went on.
    """

    @pytest.mark.unit
    async def test_a_None_tool_is_in_neither_the_prompt_nor_the_enum(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from services.agent_service import AgentService
        from services.agent_tools import AgentToolRegistry
        from utils.config import settings

        registry = MagicMock(spec=AgentToolRegistry)
        tools: dict = {}
        for i in range(8):
            w = MagicMock()
            w.description = f"Beschreibung {i}"
            tools[f"mcp.server.tool_{i}"] = w
        tools["mcp.server.kaputt"] = None          # exactly the case
        registry._tools = tools
        registry.get_tool_names.return_value = list(tools.keys())

        rolle = MagicMock()
        rolle.max_steps, rolle.prompt_key = 5, "agent_prompt"
        rolle.model = rolle.ollama_url = None
        rolle.name = "general"
        agent = AgentService(registry, role=rolle)

        antwort = MagicMock()
        antwort.message.content = '["mcp.server.tool_0"]'
        client = AsyncMock()
        client.chat = AsyncMock(return_value=antwort)

        monkeypatch.setattr(settings, "agent_preselect_typed", True)
        await agent._preselect_tools("Test", client, "qwen3.6")

        kwargs = client.chat.await_args.kwargs
        prompt = kwargs["messages"][0]["content"]
        enum = kwargs["format"]["items"]["enum"]

        assert "mcp.server.kaputt" not in prompt, "der kaputte Name steht im Prompt"
        assert "mcp.server.kaputt" not in enum, (
            "das enum erlaubt einen Namen, den das Modell nie gesehen hat")
        # Negative control: the check is not trivially true -- the HEALTHY
        # names do stand in both.
        assert "mcp.server.tool_0" in enum and "mcp.server.tool_0" in prompt

    @pytest.mark.unit
    async def test_the_flag_off_path_sends_no_format_at_all(self, monkeypatch):
        """Dark means the old path byte for byte -- no `format` in the call."""
        from unittest.mock import AsyncMock, MagicMock

        from services.agent_service import AgentService
        from services.agent_tools import AgentToolRegistry
        from utils.config import settings

        registry = MagicMock(spec=AgentToolRegistry)
        tools = {}
        for i in range(8):
            w = MagicMock()
            w.description = f"Beschreibung {i}"
            tools[f"mcp.server.tool_{i}"] = w
        registry._tools = tools
        registry.get_tool_names.return_value = list(tools.keys())
        rolle = MagicMock()
        rolle.max_steps, rolle.prompt_key = 5, "agent_prompt"
        rolle.model = rolle.ollama_url = None
        rolle.name = "general"
        agent = AgentService(registry, role=rolle)

        antwort = MagicMock()
        antwort.message.content = '["mcp.server.tool_0"]'
        client = AsyncMock()
        client.chat = AsyncMock(return_value=antwort)

        monkeypatch.setattr(settings, "agent_preselect_typed", False)
        await agent._preselect_tools("Test", client, "qwen3.6")
        assert "format" not in client.chat.await_args.kwargs
