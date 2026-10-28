"""Die Werkzeug-Vorauswahl an ein JSON-Schema gebunden (`AGENT_PRESELECT_TYPED`).

Anlass, am 2026-10-28 live beobachtet: auf „scanne nach x-idra" antwortete das
Vorauswahl-Modell mit der Form eines Werkzeug-AUFRUFS (`"query": "xidra"` samt
`"reason"`, ohne schliessende Klammern) statt mit der Liste. Die Antwort war
unparsbar, der `except`-Zweig gab `None` zurueck, und damit standen ALLE 33
Werkzeuge im Agent-Prompt — die Vorauswahl war nicht ungenau, sie war wirkungslos.

Dieselbe Eingabe traf im Prueflauf gegen denselben Endpunkt und dasselbe Modell
5/5 richtig (90/95 gesamt). Der Fehlgriff lag also nicht am Prompt, sondern an
der FREIEN FORM der Antwort — und die ist bauartbedingt, nicht zufaellig.
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
    """🛑 Wurzel-Array, nicht Objekt: der Prompt bittet bereits um ein Array und
    der Parser erwartet eines. Ein Objekt haette eine Prompt-Aenderung erzwungen
    — dann unterscheiden sich Flag-aus und Flag-an in ZWEI Variablen und der
    Vergleich am Pruefstand ist keiner mehr."""
    s = _build_preselect_schema(["mcp.a.b", "internal.c"])
    assert s["type"] == "array", s
    assert s["items"]["type"] == "string"


def test_only_the_offered_names_are_allowed():
    """`enum` ist die staerkere Haelfte: ein erfundener Name kann nicht
    entstehen, statt erst unten am Registry-Filter still wegzufallen."""
    namen = ["mcp.scanner.scan_document", "internal.knowledge_search"]
    s = _build_preselect_schema(namen)
    assert s["items"]["enum"] == namen


def test_maxItems_is_present_because_uniqueItems_is_NOT_enforced():
    """🛑 Der Befund, der diese Bindung erst tragfaehig macht — gemessen gegen
    den lebenden Endpunkt (cuda.local:8081, qwen3.6):

    `uniqueItems` wird angenommen, aber NICHT erzwungen. Ein Modell, das zu
    einer anderen Form gedraengt wird, wiederholt dann denselben GUELTIGEN Namen
    bis `num_predict` aufgebraucht ist — und die Antwort kommt ABGESCHNITTEN
    zurueck. Unparsbar wie vorher, nur aus einem neuen Grund. Zwei von drei
    boshaften Prompts taten genau das.

    Mit `maxItems` begrenzt die Grammatik das Array und alle drei kamen
    parsebar und gueltig zurueck. Ohne diesen Schluessel VERSCHIEBT die
    Bindung den Fehler, statt ihn zu beseitigen.
    """
    s = _build_preselect_schema(["mcp.a.b"])
    assert "maxItems" in s, (
        "ohne maxItems verschiebt die Bindung den Fehlermodus nur — "
        "siehe Docstring, am lebenden Endpunkt gemessen")
    assert s["maxItems"] >= 5, (
        "der Prompt fragt nach 3-5 Werkzeugen und `_with_required_companions` "
        f"legt noch zu — {s['maxItems']} laesst keinen Spielraum")


def test_an_empty_offer_does_not_produce_a_schema_that_matches_nothing():
    """Ohne Werkzeuge wird die Vorauswahl ohnehin nicht gerufen (`<= 6` kehrt
    vorher um). Der Bau darf aber nicht werfen — sonst entscheidet ein
    Randfall ueber die Erreichbarkeit des ganzen Pfads."""
    s = _build_preselect_schema([])
    assert s["items"]["enum"] == []


def test_the_flag_is_dark_by_default():
    """Neue Funktion = dunkel. Der Pfad mit `false` muss der alte sein."""
    from utils.config import Settings

    assert Settings.model_fields["agent_preselect_typed"].default is False


def test_the_binding_is_the_ONLY_difference_the_flag_makes():
    """🛑 Strukturell am AST geprueft, nicht am Quelltext: der Flag-Zweig darf
    ausschliesslich `format` setzen. Setzte er daneben etwas am Prompt oder an
    den Sampling-Optionen, waere der gemessene Vergleich (95 % gebunden gegen
    95 % ungebunden) wertlos, weil er zwei Dinge auf einmal messen wuerde.
    """
    from services import agent_service

    # 🛑 `dedent` ist Pflicht: `getsource` einer METHODE liefert eingerueckten
    # Code, und `ast.parse` wirft darauf `IndentationError`. Dieser Test ist
    # daran gescheitert, bevor er je etwas geprueft hat.
    quelle = textwrap.dedent(
        inspect.getsource(agent_service.AgentService._preselect_tools))
    baum = ast.parse(ast.unparse(ast.parse(quelle)))  # Kommentare fallen weg

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
    """🛑 Ein Pruefstand, der etwas anderes schickt als die Produktion, misst
    etwas anderes. `bin/measure-preselect.py` traegt das Schema als Literal,
    damit es ohne Backend-Import laeuft — und genau darum kann es driften.

    Verglichen werden die Schluessel, die die Zusicherung tragen: `type`,
    `maxItems` und die Form von `items`. Die `enum`-Werte selbst stehen im
    Pruefstand zur Laufzeit aus dem Prompt und koennen hier nicht gleich sein.
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
    """🛑 Angebot und erlaubte Antwort muessen DIESELBE Menge sein.

    `get_tool_names()` kann Namen tragen, deren Registry-Eintrag `None` ist.
    Die fallen beim Bau der Prompt-Zeilen heraus und stehen dann nicht im
    Prompt — das `enum` aus `tool_names` zu bauen erlaubte also einen Namen,
    den das Modell nie gesehen hat. Und `filtered` unten prueft nur
    `name in _tools`, nicht den WERT: ein `None`-Werkzeug kaeme durch.
    Gefunden im `/review` des eigenen Diffs, bevor das Flag anging.
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
        tools["mcp.server.kaputt"] = None          # genau der Fall
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
        # Negativkontrolle: die Pruefung ist nicht trivial wahr — die GESUNDEN
        # Namen stehen sehr wohl in beiden.
        assert "mcp.server.tool_0" in enum and "mcp.server.tool_0" in prompt

    @pytest.mark.unit
    async def test_the_flag_off_path_sends_no_format_at_all(self, monkeypatch):
        """Dunkel heisst byteweise der alte Pfad — kein `format` im Aufruf."""
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
