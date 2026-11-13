"""The Grafana dashboards in `config/monitoring/grafana/` (#82).

Nobody loads these files into a Grafana on the way to a merge, so everything
that can be checked without one is checked here:

- each file is a provisionable dashboard (unique uid and title, schema
  version, no hardcoded data source);
- each panel expression only names series the backend really registers (read
  from the live collectors, not from a list), with labels those series carry;
- quantiles read `_bucket` series grouped by `le`, range windows are
  `$__rate_interval`;
- each series a panel reads has a recorder that something calls. Three
  registered metrics had no caller for months; a panel on one of those would
  have been an empty panel that looks like a quiet system.

The expression reader is deliberately small and strict: a token it does not
know is a finding, never something to skip. It is not a PromQL parser, and the
negative controls at the end prove each finding can fire.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

import utils.metrics as metrics_module
from tests.backend.metrics_registry import (
    _is_collector,
    gauge_series,
    live_metrics,
    registered_series,
)

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"
GRAFANA = REPO / "config" / "monitoring" / "grafana"
DASHBOARDS = sorted((GRAFANA / "dashboards").glob("*.json"))

DATASOURCE_VARIABLE = "${datasource}"
RATE_WINDOW = "$__rate_interval"

#: Labels Prometheus attaches to every scraped series.
TARGET_LABELS = frozenset({"job", "instance"})
#: Series Prometheus writes itself, one per scrape target.
PROMETHEUS_SERIES = {"up": TARGET_LABELS}

AGGREGATIONS = frozenset({"sum", "min", "max", "avg", "count", "topk", "bottomk"})
FUNCTIONS = frozenset({"rate", "increase", "histogram_quantile", "vector"})
GROUPING = frozenset({"by", "without"})
SET_OPERATORS = frozenset({"or", "and", "unless"})

_TOKEN = re.compile(
    r"""
      (?P<space>\s+)
    | (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<variable>\$[A-Za-z_][A-Za-z0-9_]*)
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<name>[A-Za-z_:][A-Za-z0-9_:]*)
    | (?P<operator>=~|!~|!=|==|>=|<=|[-+*/%^(){}\[\],=<>])
    """,
    re.VERBOSE,
)


@dataclass
class Expression:
    """What one panel expression refers to."""

    selectors: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    #: Series read as a range vector (`name{...}[window]`), i.e. fed to rate().
    ranged: list[str] = field(default_factory=list)
    #: Labels named in `by (...)` and `without (...)` together.
    grouping: set[str] = field(default_factory=set)
    #: Labels named in `by (...)` only: the ones that survive.
    kept: set[str] = field(default_factory=set)
    windows: list[str] = field(default_factory=list)
    calls: set[str] = field(default_factory=set)
    variables: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)

    @property
    def series(self) -> list[str]:
        return [name for name, _ in self.selectors]


def read_expression(expr: str) -> Expression:
    """Tokenise `expr` and collect series, labels, windows and calls."""
    parsed = Expression()
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expr):
        match = _TOKEN.match(expr, position)
        if match is None:
            parsed.problems.append(f"unreadable text at {position}: {expr[position:position + 20]!r}")
            return parsed
        if match.lastgroup != "space":
            tokens.append((match.lastgroup, match.group()))
        position = match.end()

    def value(index: int) -> str | None:
        return tokens[index][1] if index < len(tokens) else None

    def label_list(index: int) -> tuple[set[str], int]:
        """Names in the parenthesised list opening at `index`."""
        names: set[str] = set()
        if value(index) != "(":
            parsed.problems.append("grouping without a label list")
            return names, index
        index += 1
        while index < len(tokens) and tokens[index][1] != ")":
            kind, text = tokens[index]
            if kind == "name":
                names.add(text)
            elif text != ",":
                parsed.problems.append(f"unexpected {text!r} in a label list")
            index += 1
        return names, index + 1

    index = 0
    while index < len(tokens):
        kind, text = tokens[index]
        if kind == "variable":
            parsed.variables.add(text)
        elif kind == "name":
            if text in GROUPING:
                names, index = label_list(index + 1)
                parsed.grouping |= names
                if text == "by":
                    parsed.kept |= names
                continue
            if text in SET_OPERATORS:
                pass
            elif value(index + 1) == "(" or (text in AGGREGATIONS and value(index + 1) in GROUPING):
                if text not in AGGREGATIONS | FUNCTIONS:
                    parsed.problems.append(f"unknown function {text!r}")
                parsed.calls.add(text)
            else:
                labels: set[str] = set()
                if value(index + 1) == "{":
                    index += 2
                    while index < len(tokens) and tokens[index][1] != "}":
                        matcher_kind, matcher_text = tokens[index]
                        if matcher_kind == "name":
                            labels.add(matcher_text)
                            if value(index + 1) not in {"=", "!=", "=~", "!~"}:
                                parsed.problems.append(f"label {matcher_text!r} without a matcher")
                            if tokens[index + 2][0] != "string":
                                parsed.problems.append(f"label {matcher_text!r} without a quoted value")
                            else:
                                parsed.variables |= set(
                                    re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*", tokens[index + 2][1])
                                )
                            index += 2
                        elif matcher_text != ",":
                            parsed.problems.append(f"unexpected {matcher_text!r} in a selector")
                        index += 1
                parsed.selectors.append((text, frozenset(labels)))
                if value(index + 1) == "[":
                    parsed.ranged.append(text)
        elif text == "{":
            parsed.problems.append("selector without a series name")
        elif text == "[":
            closing = next((i for i in range(index, len(tokens)) if tokens[i][1] == "]"), len(tokens))
            parsed.windows.append("".join(t for _, t in tokens[index + 1:closing]))
            index = closing
        index += 1
    return parsed


def expression_problems(
    expr: str,
    series: dict[str, frozenset[str]],
    gauges: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Everything wrong with `expr`, given the series that exist and which of
    them are gauges."""
    parsed = read_expression(expr)
    problems = list(parsed.problems)
    if not parsed.selectors:
        problems.append("reads no series")
    for name, selector_labels in parsed.selectors:
        if name not in series:
            problems.append(f"unknown series {name!r}")
            continue
        available = series[name] | TARGET_LABELS
        for label in sorted(selector_labels - available):
            problems.append(f"{name} has no label {label!r} (selector)")
        for label in sorted(parsed.grouping - available):
            problems.append(f"{name} has no label {label!r} (grouping)")
    for window in parsed.windows:
        if window != RATE_WINDOW:
            problems.append(f"range window [{window}] instead of [{RATE_WINDOW}]")
    if parsed.calls & {"rate", "increase"} and not parsed.windows:
        problems.append("rate() without a range window")
    for name in parsed.ranged:
        if name in gauges:
            problems.append(f"rate()/increase() over the gauge {name!r}")
    if "histogram_quantile" in parsed.calls:
        for name in parsed.series:
            if not name.endswith("_bucket"):
                problems.append(f"histogram_quantile over {name!r}, not a _bucket series")
        if "le" not in parsed.kept:
            problems.append("histogram_quantile without an aggregation by le")
    return problems


def legend_labels(legend: str) -> set[str]:
    return set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", legend))


# =============================================================================
# The dashboards as they are in the repository
# =============================================================================


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _panels(dashboard: dict) -> list[dict]:
    """Every panel, including those nested in a collapsed row."""
    panels = []
    for panel in dashboard["panels"]:
        panels.append(panel)
        panels.extend(panel.get("panels", []))
    return panels


def _targets() -> list[tuple[str, str, dict]]:
    """(file, panel title, target) for every query of every dashboard."""
    return [
        (path.name, panel["title"], target)
        for path in DASHBOARDS
        for panel in _panels(_load(path))
        for target in panel.get("targets", [])
    ]


def _datasource_references(node: object) -> list[object]:
    found = []
    if isinstance(node, dict):
        for key, child in node.items():
            if key == "datasource":
                found.append(child)
            found.extend(_datasource_references(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_datasource_references(child))
    return found


@pytest.fixture(scope="module")
def series() -> dict[str, frozenset[str]]:
    return {**registered_series(), **PROMETHEUS_SERIES}


@pytest.fixture(scope="module")
def gauges() -> dict[str, frozenset[str]]:
    return gauge_series()


def test_there_are_dashboards_to_check():
    """Guards every parametrised test below against an empty glob."""
    assert [path.name for path in DASHBOARDS] == [
        "renfield-integrations.json",
        "renfield-llm-agent.json",
        "renfield-overview.json",
    ]
    assert len(_targets()) >= 40


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
class TestDashboardFile:
    def test_is_a_provisionable_dashboard(self, path):
        dashboard = _load(path)

        assert dashboard["uid"] == path.stem, "uid and file name are kept equal"
        assert re.fullmatch(r"[a-z0-9-]{1,40}", dashboard["uid"])
        assert dashboard["title"].startswith("Renfield / ")
        assert isinstance(dashboard["schemaVersion"], int) and dashboard["schemaVersion"] >= 36
        assert "id" not in dashboard, "a numeric id binds the file to one Grafana"
        assert "__inputs" not in dashboard, "an export for manual import only; provisioning ignores it"

    def test_has_no_hardcoded_datasource(self, path):
        dashboard = _load(path)

        variables = {v["name"]: v for v in dashboard["templating"]["list"]}
        assert variables["datasource"]["type"] == "datasource"
        assert variables["datasource"]["query"] == "prometheus"
        references = _datasource_references(dashboard)
        assert references, "no data source reference found at all"
        assert all(
            reference == {"type": "prometheus", "uid": DATASOURCE_VARIABLE} for reference in references
        ), references

    def test_every_panel_is_complete(self, path):
        panels = _panels(_load(path))

        ids = [panel["id"] for panel in panels]
        assert len(ids) == len(set(ids)), "duplicate panel id"
        titles = [panel["title"] for panel in panels]
        assert len(titles) == len(set(titles)), "duplicate panel title"
        for panel in panels:
            position = panel["gridPos"]
            assert position["x"] + position["w"] <= 24, panel["title"]
            if panel["type"] == "row":
                continue
            assert panel["description"].strip(), f"{panel['title']}: no description"
            assert panel["targets"], f"{panel['title']}: no query"
            assert panel["datasource"] == {"type": "prometheus", "uid": DATASOURCE_VARIABLE}
            ref_ids = [target["refId"] for target in panel["targets"]]
            assert len(ref_ids) == len(set(ref_ids)), panel["title"]

    def test_every_variable_used_is_defined(self, path):
        dashboard = _load(path)
        defined = {f"${variable['name']}" for variable in dashboard["templating"]["list"]}

        for panel in _panels(dashboard):
            for target in panel.get("targets", []):
                used = read_expression(target["expr"]).variables - {RATE_WINDOW}
                assert used <= defined, f"{panel['title']}: undefined {sorted(used - defined)}"
                assert "$job" in used, f"{panel['title']}: not scoped to the job variable"

    def test_variable_queries_name_real_series_and_labels(self, path, series):
        for variable in _load(path)["templating"]["list"]:
            if variable["type"] != "query":
                continue
            query = variable["query"]["query"]
            assert query == variable["definition"]
            match = re.fullmatch(r"label_values\((\w+), (\w+)\)", query)
            assert match, f"unsupported variable query: {query}"
            name, label = match.groups()
            assert name in series, f"${variable['name']}: unknown series {name}"
            assert label in series[name] | TARGET_LABELS, f"${variable['name']}: {name} has no {label}"


def test_dashboard_uids_and_titles_are_unique():
    dashboards = [_load(path) for path in DASHBOARDS]

    assert len({d["uid"] for d in dashboards}) == len(dashboards)
    assert len({d["title"] for d in dashboards}) == len(dashboards)


def test_every_expression_reads_registered_series_with_their_labels(series, gauges):
    findings = [
        f"{file} / {panel}: {problem}"
        for file, panel, target in _targets()
        for problem in expression_problems(target["expr"], series, set(gauges))
    ]
    assert findings == []


# =============================================================================
# A gauge without labels: "never measured" must not read as zero
# =============================================================================


def unmeasured_display_problems(panel: dict, unlabelled_gauges: set[str]) -> list[str]:
    """What is missing on `panel` if it reads a gauge without labels.

    Such a gauge is NaN until its job first measured (see `_init_metrics`).
    The panel has to say what that state is called, for the absent series
    (`noValue`) and for NaN (a special value mapping); a stat panel left to
    its defaults would be free to colour it like a number.
    """
    read = {
        name
        for target in panel.get("targets", [])
        for name in read_expression(target["expr"]).series
        if name in unlabelled_gauges
    }
    if not read:
        return []
    problems = []
    defaults = panel["fieldConfig"]["defaults"]
    if not str(defaults.get("noValue", "")).strip():
        problems.append(f"{panel['title']}: reads {sorted(read)} without a noValue text")
    nan_texts = [
        mapping["options"]["result"].get("text", "")
        for mapping in defaults.get("mappings", [])
        if mapping.get("type") == "special" and mapping["options"].get("match") in {"nan", "null+nan"}
    ]
    if not any(text.strip() for text in nan_texts):
        problems.append(f"{panel['title']}: reads {sorted(read)} without a NaN value mapping")
    return problems


def test_every_panel_on_an_unlabelled_gauge_names_the_unmeasured_state(gauges):
    unlabelled = {name for name, labels in gauges.items() if not labels}
    assert unlabelled == {
        "renfield_mcp_health_problem_servers",
        "renfield_memory_active_total",
        "renfield_kg_conflation_candidates",
    }
    panels = [panel for path in DASHBOARDS for panel in _panels(_load(path)) if panel["type"] != "row"]
    affected = [
        panel for panel in panels
        if any(
            name in unlabelled
            for target in panel["targets"]
            for name in read_expression(target["expr"]).series
        )
    ]
    assert len(affected) == 4, "two problem-server stats, active memories, conflation candidates"
    assert [p for panel in panels for p in unmeasured_display_problems(panel, unlabelled)] == []


def test_NEGATIVE_a_panel_without_unmeasured_handling_is_reported():
    panel = {
        "title": "MCP servers with a problem",
        "type": "stat",
        "fieldConfig": {"defaults": {"unit": "short"}},
        "targets": [{"expr": 'max(renfield_mcp_health_problem_servers{job=~"$job"})'}],
    }
    unlabelled = {"renfield_mcp_health_problem_servers"}

    assert unmeasured_display_problems(panel, unlabelled) == [
        "MCP servers with a problem: reads ['renfield_mcp_health_problem_servers'] without a noValue text",
        "MCP servers with a problem: reads ['renfield_mcp_health_problem_servers'] without a NaN value mapping",
    ]
    # A value mapping for an ordinary value is not NaN handling.
    panel["fieldConfig"]["defaults"] = {
        "noValue": "no verdict",
        "mappings": [{"type": "value", "options": {"0": {"text": "none"}}}],
    }
    assert len(unmeasured_display_problems(panel, unlabelled)) == 1
    # A labelled gauge is primed per label value and is not this case.
    assert unmeasured_display_problems(panel, {"renfield_memory_active_total"}) == []


def test_the_monitor_heartbeat_stat_is_red_at_zero():
    """Ticks keep counting while every tick fails, but a monitor that is off
    or stuck ticks at 0: the one stall signal the problem count has."""
    stats = [
        panel
        for path in DASHBOARDS
        for panel in _panels(_load(path))
        if panel["title"] == "MCP health monitor ticks per minute"
    ]
    assert len(stats) == 2
    for panel in stats:
        base, above = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
        assert (base["value"], base["color"]) == (None, "red")
        assert above["color"] == "green" and 0 < above["value"] <= 0.1


def test_every_legend_label_survives_the_aggregation(series):
    for file, panel, target in _targets():
        parsed = read_expression(target["expr"])
        wanted = legend_labels(target.get("legendFormat", ""))
        assert wanted <= parsed.grouping, f"{file} / {panel}: legend uses {sorted(wanted - parsed.grouping)}"


def test_the_backend_series_set_is_the_real_registry(series):
    """The reference the dashboards are held against is not hand-written: it
    follows the collectors, with the exposition suffixes."""
    assert "renfield_http_requests_total" in series
    assert series["renfield_http_request_duration_seconds_bucket"] == {"method", "endpoint", "le"}
    assert series["renfield_mcp_tool_duration_seconds_count"] == {"server", "outcome"}
    assert "renfield_mcp_tool_duration_seconds" not in series, "a histogram has no bare series"
    assert len(series) > 30


# =============================================================================
# A panel must have a data source in the code
# =============================================================================


def _recorders_by_series() -> dict[str, set[str]]:
    """Series name -> the functions of `utils.metrics` that write to it.

    Read from the syntax tree: a function "writes" a collector when it names
    the module global holding it. Initialisation and the 0-priming helpers do
    not count; a primed series nobody records is exactly an empty panel.
    """
    with live_metrics():
        collector_of_global = {
            name: value for name, value in vars(metrics_module).items() if _is_collector(value)
        }
        series_of_global: dict[str, set[str]] = {}
        for name, collector in collector_of_global.items():
            (described,) = collector.describe()
            suffixes = {
                "counter": ("_total",),
                "gauge": ("",),
                "histogram": ("_bucket", "_sum", "_count"),
            }[described.type]
            series_of_global[name] = {described.name + suffix for suffix in suffixes}

    recorders: dict[str, set[str]] = {}
    tree = ast.parse((BACKEND / "utils" / "metrics.py").read_text())
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name == "_init_metrics" or node.name.startswith("_prime_"):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id in series_of_global:
                for series_name in series_of_global[inner.id]:
                    recorders.setdefault(series_name, set()).add(node.name)
    return recorders


def _functions_in_use(candidates: set[str]) -> set[str]:
    """Those of `candidates` that backend code refers to outside their own
    definition: called, or handed over as a callback. An import alone is not a
    use, and neither is a mention in a comment or a docstring."""
    used: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        if path.relative_to(BACKEND).parts[0] in {"satellite", "wakeword-models"}:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in candidates:
                used.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in candidates:
                used.add(node.attr)
    return used


def test_every_series_on_a_dashboard_is_recorded_somewhere(series):
    recorders = _recorders_by_series()
    in_use = _functions_in_use({name for names in recorders.values() for name in names})

    on_dashboards = {
        name
        for _file, _panel, target in _targets()
        for name in read_expression(target["expr"]).series
        if name not in PROMETHEUS_SERIES
    }
    assert len(on_dashboards) >= 20
    orphans = sorted(name for name in on_dashboards if not recorders.get(name, set()) & in_use)
    assert orphans == [], f"panels read series that nothing records: {orphans}"


def test_NEGATIVE_a_recorder_without_a_caller_is_not_in_use():
    """🛑 Negative control for the test above. The function exists in
    `utils.metrics`, is imported nowhere and called nowhere: the state the
    WebSocket, LLM and MCP recorders were in."""
    assert "set_kg_conflation_candidates" in _functions_in_use({"set_kg_conflation_candidates"})
    assert _functions_in_use({"record_something_nobody_calls"}) == set()
    # A definition is not a use, and neither is the name in prose: this
    # function is defined, and named in two docstrings, but called nowhere.
    assert _functions_in_use({"_prime_llm_response_truncated_typo", "test_NEGATIVE"}) == set()
    recorders = _recorders_by_series()
    assert recorders["renfield_websocket_connections"] == {
        "record_websocket_connect", "record_websocket_disconnect",
    }, "priming must not count as recording"


# =============================================================================
# What only showed once the files were loaded into a Grafana
#
# Each check below pins one defect class found on first load (Grafana 13.3,
# Prometheus 3.16): the expressions were valid and every series existed, and
# the panels were still wrong. The negative controls hold the JSON as it was.
# =============================================================================

#: Panel types that draw a value's TEXT: a value mapping or a `noValue` text
#: is what the reader sees. A timeseries draws a line and keeps the mapping
#: for the y axis ticks only, so a NaN mapped to "no scan yet" is an empty
#: graph there. Only types seen rendering it are listed.
RENDERS_VALUE_TEXT = frozenset({"stat", "state-timeline"})


def _split_top_level(text: str, separator: str) -> list[str]:
    """`text` cut at every `separator` outside brackets and strings. The
    separator is one operator character, or a keyword such as `or`."""
    parts: list[str] = []
    depth = 0
    start = position = 0
    keyword = separator.isalpha()
    while position < len(text):
        char = text[position]
        if char == '"':
            position = _TOKEN.match(text, position).end()
            continue
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
        elif depth == 0 and text.startswith(separator, position):
            before = text[position - 1] if position else " "
            after = text[position + len(separator): position + len(separator) + 1]
            if not keyword or not (before.isalnum() or before == "_" or after.isalnum() or after == "_"):
                parts.append(text[start:position])
                start = position + len(separator)
                position = start
                continue
        position += 1
    parts.append(text[start:])
    return [part.strip() for part in parts]


def _unwrap(text: str) -> str:
    """`text` without the pair of parentheses that encloses all of it."""
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for position, char in enumerate(text):
            depth += (char == "(") - (char == ")")
            if depth == 0 and position < len(text) - 1:
                return text
        text = text[1:-1].strip()
    return text


def ratio_problems(expr: str) -> list[str]:
    """What is wrong with `expr` if it divides a subset by its total.

    A numerator that selects on a label the denominator does not (an outcome,
    a status class) reads series that do not exist until the first such
    event. Without them the division has no left side: the panel says "No
    data" exactly while nothing failed, and in a grouped ratio the groups
    without a failure are missing. The numerator therefore needs a fallback
    built from the denominator's own series, times zero. `vector(0)` only
    serves an ungrouped ratio: it carries no labels to match a group with.
    """
    sides = _split_top_level(expr, "/")
    if len(sides) == 1:
        return []
    if len(sides) != 2:
        return ["more than one division: not a ratio this check can read"]
    numerator, denominator = (read_expression(side) for side in sides)
    total_labels = {label for _, labels in denominator.selectors for label in labels}
    narrowing = sorted({label for _, labels in numerator.selectors for label in labels} - total_labels)
    if not narrowing:
        return []

    fallbacks = []
    for branch in _split_top_level(_unwrap(sides[0]), "or"):
        parsed = read_expression(branch)
        if any(labels - total_labels for _, labels in parsed.selectors):
            continue
        fallbacks.append((branch, parsed))
    if not fallbacks:
        return [f"the numerator selects on {narrowing} without a zero fallback"]
    problems = []
    for branch, parsed in fallbacks:
        if not parsed.selectors:
            if re.sub(r"\s", "", branch) != "vector(0)":
                problems.append(f"fallback {branch!r} reads no series and is not vector(0)")
            elif denominator.kept:
                problems.append(
                    f"vector(0) has no {sorted(denominator.kept)} label: a group without the event stays missing"
                )
            continue
        if not re.search(r"\*\s*0$", branch):
            problems.append(f"fallback {branch!r} is not multiplied by 0")
        if parsed.kept != denominator.kept:
            problems.append(f"fallback groups by {sorted(parsed.kept)}, the total by {sorted(denominator.kept)}")
    return problems


def _ratio_targets() -> list[tuple[str, str]]:
    return [
        (panel, target["expr"])
        for _file, panel, target in _targets()
        if len(_split_top_level(target["expr"], "/")) > 1
    ]


def test_every_share_of_a_total_reads_zero_while_nothing_failed():
    ratios = _ratio_targets()
    # Every division on the dashboards. The orchestrator coverage divides two
    # counters without labels, which exist from the start: nothing to fall back to.
    assert sorted(panel for panel, _ in ratios) == [
        "Orchestrator domain coverage",
        "Share of 5xx responses",
        "Share of runs that did not end in a final answer",
        "Share of tool calls that timed out, by server",
    ]
    assert [f"{panel}: {problem}" for panel, expr in ratios for problem in ratio_problems(expr)] == []
    guarded = [
        panel for panel, expr in ratios
        if len(_split_top_level(_unwrap(_split_top_level(expr, "/")[0]), "or")) > 1
    ]
    assert len(guarded) == 3, "the check above must not pass by finding nothing to check"


_TOOL_CALLS = 'rate(renfield_mcp_tool_duration_seconds_count{job=~"$job", server=~"$server"}[$__rate_interval])'
_TOOL_TIMEOUTS = _TOOL_CALLS.replace('"}', '", outcome="timeout"}')
_REQUESTS = 'rate(renfield_http_requests_total{job=~"$job"}[$__rate_interval])'
_REQUESTS_5XX = _REQUESTS.replace('"}', '", status_code=~"5.."}')


@pytest.mark.parametrize(
    ("expr", "finding"),
    [
        (
            # renfield-integrations.json as merged: only `paperless` was drawn.
            f"sum by (server) ({_TOOL_TIMEOUTS}) / sum by (server) ({_TOOL_CALLS})",
            "the numerator selects on ['outcome'] without a zero fallback",
        ),
        (
            # renfield-overview.json as merged: "No data" without a 5xx.
            f"sum({_REQUESTS_5XX}) / sum({_REQUESTS})",
            "the numerator selects on ['status_code'] without a zero fallback",
        ),
        (
            f"(sum by (server) ({_TOOL_TIMEOUTS}) or vector(0)) / sum by (server) ({_TOOL_CALLS})",
            "vector(0) has no ['server'] label: a group without the event stays missing",
        ),
        (
            # A fallback that is narrowed like the numerator is absent with it.
            f"(sum({_REQUESTS_5XX}) or sum({_REQUESTS_5XX}) * 0) / sum({_REQUESTS})",
            "the numerator selects on ['status_code'] without a zero fallback",
        ),
        (
            f"(sum({_REQUESTS_5XX}) or sum({_REQUESTS})) / sum({_REQUESTS})",
            f"fallback 'sum({_REQUESTS})' is not multiplied by 0",
        ),
        (
            f"(sum by (server) ({_TOOL_TIMEOUTS}) or sum({_TOOL_CALLS}) * 0) / sum by (server) ({_TOOL_CALLS})",
            "fallback groups by [], the total by ['server']",
        ),
    ],
    ids=["grouped-as-merged", "ungrouped-as-merged", "vector0-in-a-grouped-ratio",
         "narrowed-fallback", "fallback-not-zeroed", "fallback-grouped-differently"],
)
def test_NEGATIVE_a_share_without_a_usable_zero_fallback_is_reported(expr, finding):
    assert finding in ratio_problems(expr)


def test_a_ratio_with_a_fallback_and_one_without_a_subset_are_clean():
    assert ratio_problems(f"(sum({_REQUESTS_5XX}) or vector(0)) / sum({_REQUESTS})") == []
    assert ratio_problems(f"(sum({_REQUESTS_5XX}) or sum({_REQUESTS}) * 0) / sum({_REQUESTS})") == []
    assert ratio_problems("sum(rate(a_total[$__rate_interval])) / sum(rate(b_total[$__rate_interval]))") == []
    assert ratio_problems(f"sum({_REQUESTS}) * 60") == []
    # A slash inside a label value is not a division.
    assert _split_top_level('sum(x_total{endpoint="/api/a"})', "/") == ['sum(x_total{endpoint="/api/a"})']
    assert _split_top_level("a_or_b or c", "or") == ["a_or_b", "c"]


def value_text_problems(panel: dict) -> list[str]:
    """A panel that names a value in words must be of a type that shows them."""
    if panel["type"] == "row" or panel["type"] in RENDERS_VALUE_TEXT:
        return []
    defaults = panel["fieldConfig"]["defaults"]
    carried = [key for key in ("mappings", "noValue") if defaults.get(key)]
    if not carried:
        return []
    return [f"{panel['title']}: a {panel['type']} panel does not show its {' / '.join(carried)} text"]


def test_every_panel_with_a_value_text_is_of_a_type_that_shows_it():
    panels = [panel for path in DASHBOARDS for panel in _panels(_load(path))]

    assert [problem for panel in panels for problem in value_text_problems(panel)] == []
    worded = {
        panel["title"]: panel["type"]
        for panel in panels
        if panel["type"] != "row" and (
            panel["fieldConfig"]["defaults"].get("mappings") or panel["fieldConfig"]["defaults"].get("noValue")
        )
    }
    assert worded == {
        "MCP servers with a problem": "stat",
        "Active memories": "stat",
        "Knowledge graph conflation candidates": "stat",
        "Circuit breaker state": "state-timeline",
    }


def test_NEGATIVE_a_value_text_on_a_timeseries_is_reported():
    """The three panels as merged: the texts were in the JSON and on no screen."""
    nan_text = {"type": "special", "options": {"match": "null+nan", "result": {"text": "no scan yet"}}}
    conflation = {
        "title": "Knowledge graph conflation candidates",
        "type": "timeseries",
        "fieldConfig": {"defaults": {"unit": "short", "noValue": "no scan yet", "mappings": [nan_text]}},
    }
    breaker = {
        "title": "Circuit breaker state",
        "type": "timeseries",
        "fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {"0": {"text": "closed"}}}]}},
    }
    assert value_text_problems(conflation) == [
        "Knowledge graph conflation candidates: a timeseries panel does not show its mappings / noValue text"
    ]
    assert value_text_problems(breaker) == [
        "Circuit breaker state: a timeseries panel does not show its mappings text"
    ]
    assert value_text_problems({**conflation, "type": "stat"}) == []
    assert value_text_problems({"title": "t", "type": "timeseries", "fieldConfig": {"defaults": {"unit": "s"}}}) == []


def _panel(title: str) -> dict:
    (found,) = [panel for path in DASHBOARDS for panel in _panels(_load(path)) if panel["title"] == title]
    return found


def test_the_measured_gauges_colour_a_count_and_a_tripwire_differently():
    """Conflation candidates are a tripwire (expected 0); the number of active
    memories is a count and no health signal. Unmeasured is orange on both."""
    tripwire = _panel("Knowledge graph conflation candidates")["fieldConfig"]["defaults"]
    base, above = tripwire["thresholds"]["steps"]
    assert (base["value"], base["color"]) == (None, "green")
    assert (above["value"], above["color"]) == (1, "red")
    assert tripwire.get("color", {}).get("mode", "thresholds") == "thresholds"

    count = _panel("Active memories")["fieldConfig"]["defaults"]
    assert count["color"]["mode"] == "fixed"
    assert "thresholds" not in count

    for defaults in (tripwire, count):
        (unmeasured,) = defaults["mappings"]
        assert unmeasured["options"]["result"]["color"] == "orange"


def stale_value_problems(panel: dict, unlabelled_gauges: set[str]) -> list[str]:
    """A stat on a gauge that is NaN until measured must reduce with `last`.

    `lastNotNull` skips NaN: after a backend restart the panel kept showing
    the number from before the restart, in its threshold colour, for as long
    as that sample was inside the dashboard's time range (6 h by default),
    while the gauge itself said "not measured"."""
    if panel["type"] != "stat":
        return []
    read = {
        name
        for target in panel["targets"]
        for name in read_expression(target["expr"]).series
        if name in unlabelled_gauges
    }
    calcs = panel["options"]["reduceOptions"]["calcs"]
    if not read or calcs == ["last"]:
        return []
    return [f"{panel['title']}: reduces {sorted(read)} with {calcs}, which hides a NaN behind an older value"]


def test_a_stat_on_an_unlabelled_gauge_shows_the_newest_sample_even_if_nan(gauges):
    unlabelled = {name for name, labels in gauges.items() if not labels}
    stats = [
        panel for path in DASHBOARDS for panel in _panels(_load(path))
        if panel["type"] == "stat" and any(
            name in unlabelled for target in panel["targets"] for name in read_expression(target["expr"]).series
        )
    ]
    assert len(stats) == 4
    assert [problem for panel in stats for problem in stale_value_problems(panel, unlabelled)] == []
    # NEGATIVE: the reducer as merged.
    merged = {**stats[0], "options": {"reduceOptions": {"calcs": ["lastNotNull"]}}}
    assert stale_value_problems(merged, unlabelled) == [
        f"{stats[0]['title']}: reduces {sorted(unlabelled & set(read_expression(stats[0]['targets'][0]['expr']).series))}"
        " with ['lastNotNull'], which hides a NaN behind an older value"
    ]
    # A counter rate has no NaN-until-measured state and may skip gaps.
    assert stale_value_problems(_panel("Backend targets up"), unlabelled) == []


def _circuit_breaker_gauge_values() -> set[int]:
    """Every number `record_circuit_breaker_state` can set, from its source:
    the state map and the default for a state name that is not in it."""
    tree = ast.parse((BACKEND / "utils" / "metrics.py").read_text())
    (function,) = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "record_circuit_breaker_state"
    ]
    (state_map,) = [node for node in ast.walk(function) if isinstance(node, ast.Dict)]
    (lookup,) = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get" and len(node.args) == 2
    ]
    return {*ast.literal_eval(state_map).values(), ast.literal_eval(lookup.args[1])}


def unmapped_states(panel: dict, values: set[int]) -> list[int]:
    mapped = {
        key
        for mapping in panel["fieldConfig"]["defaults"].get("mappings", [])
        if mapping["type"] == "value"
        for key in mapping["options"]
    }
    return sorted(value for value in values if str(value) not in mapped)


def test_the_circuit_breaker_panel_names_every_state_the_gauge_can_hold():
    values = _circuit_breaker_gauge_values()
    assert values == {-1, 0, 1, 2}
    panel = _panel("Circuit breaker state")

    assert unmapped_states(panel, values) == []
    (mapping,) = panel["fieldConfig"]["defaults"]["mappings"]
    assert all(option.get("color") for option in mapping["options"].values()), "a band needs a colour per state"
    # NEGATIVE: the mapping as merged left the unknown state a bare -1.
    merged = {"fieldConfig": {"defaults": {"mappings": [
        {"type": "value", "options": {"0": {"text": "closed"}, "1": {"text": "open"}, "2": {"text": "half open"}}},
    ]}}}
    assert unmapped_states(merged, values) == [-1]


def unranked_quantile_problems(expr: str) -> list[str]:
    """A quantile is NaN for a group without observations in the window;
    ranked by topk/bottomk those groups become legend entries without a line."""
    parsed = read_expression(expr)
    if "histogram_quantile" not in parsed.calls or not parsed.calls & {"topk", "bottomk"}:
        return []
    if re.search(r">\s*0\s*\)\s*$", expr):
        return []
    return ["a ranked quantile keeps the groups without observations (NaN): filter with `> 0`"]


def test_a_ranked_quantile_drops_the_groups_without_observations():
    ranked = [
        (panel, target["expr"])
        for _file, panel, target in _targets()
        if read_expression(target["expr"]).calls & {"topk", "bottomk"}
    ]
    assert [panel for panel, _ in ranked] == ["Slowest endpoints (p95, top 10)"]
    assert [problem for _, expr in ranked for problem in unranked_quantile_problems(expr)] == []
    # NEGATIVE: the expression as merged.
    (_, expr), = ranked
    merged = expr.replace(" > 0)", ")")
    assert merged != expr
    assert unranked_quantile_problems(merged) == [
        "a ranked quantile keeps the groups without observations (NaN): filter with `> 0`"
    ]


def layout_gaps(panels: list[dict]) -> list[str]:
    """Row sections whose panels do not tile the 24 grid columns: a short
    panel beside a tall one leaves a blank block on the dashboard."""
    gaps = []
    sections: list[tuple[str, list[dict]]] = []
    for panel in sorted(panels, key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"])):
        if panel["type"] == "row":
            sections.append((panel["title"], []))
        else:
            sections[-1][1].append(panel)
    for title, members in sections:
        cells = {
            (x, y)
            for panel in members
            for x in range(panel["gridPos"]["x"], panel["gridPos"]["x"] + panel["gridPos"]["w"])
            for y in range(panel["gridPos"]["y"], panel["gridPos"]["y"] + panel["gridPos"]["h"])
        }
        covered = sum(panel["gridPos"]["w"] * panel["gridPos"]["h"] for panel in members)
        if covered != len(cells):
            gaps.append(f"{title}: panels overlap")
        rows = {y for _, y in cells}
        if len(cells) != 24 * (max(rows) - min(rows) + 1):
            gaps.append(f"{title}: {24 * (max(rows) - min(rows) + 1) - len(cells)} empty grid cells")
    return gaps


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_every_row_section_is_tiled_without_a_blank_block(path):
    assert layout_gaps(_load(path)["panels"]) == []


def test_NEGATIVE_a_short_panel_beside_a_tall_one_is_a_gap():
    """The "MCP health" row as merged: two stats of height 4 beside a graph of height 8."""
    def at(x: int, w: int, h: int, kind: str = "stat") -> dict:
        return {"title": f"{kind} {x}", "type": kind, "gridPos": {"x": x, "y": 1, "w": w, "h": h}}

    row = {"title": "MCP health", "type": "row", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}}
    merged = [row, at(0, 6, 4), at(6, 6, 4), at(12, 12, 8, "timeseries")]
    assert layout_gaps(merged) == ["MCP health: 48 empty grid cells"]
    below = {**at(0, 6, 4), "gridPos": {"x": 0, "y": 5, "w": 6, "h": 4}}
    assert layout_gaps([row, at(0, 6, 4), below, at(6, 18, 8, "timeseries")]) == []
    assert layout_gaps([row, at(0, 14, 4), at(12, 12, 4)]) == ["MCP health: panels overlap"]


# =============================================================================
# Provisioning
# =============================================================================


def test_provider_file_points_at_the_dashboard_directory():
    provider_file = GRAFANA / "provisioning" / "dashboards" / "renfield.yaml"
    config = yaml.safe_load(provider_file.read_text())

    assert config["apiVersion"] == 1
    (provider,) = config["providers"]
    assert provider["type"] == "file"
    assert provider["folder"] == "Renfield"
    # The directory as it is mounted in the Grafana container; the README
    # shows the mount that puts the committed files there.
    assert provider["options"]["path"] == "/var/lib/grafana/dashboards/renfield"
    readme = (REPO / "config" / "monitoring" / "README.md").read_text()
    assert provider["options"]["path"] in readme
    for path in DASHBOARDS:
        assert path.name in readme, f"README does not mention {path.name}"


# =============================================================================
# 🛑 Negative controls: each finding above can fire
# =============================================================================


GOOD = (
    'histogram_quantile(0.95, sum by (le, server) '
    '(rate(renfield_mcp_tool_duration_seconds_bucket{job=~"$job", server=~"$server"}[$__rate_interval])))'
)


def test_the_reference_expression_is_clean(series, gauges):
    assert expression_problems(GOOD, series, set(gauges)) == []


@pytest.mark.parametrize(
    ("expr", "finding"),
    [
        (
            GOOD.replace("renfield_mcp_tool_duration_seconds_bucket", "renfield_mcp_call_duration_seconds_bucket"),
            "unknown series 'renfield_mcp_call_duration_seconds_bucket'",
        ),
        (
            GOOD.replace("(le, server)", "(le, tool)"),
            "renfield_mcp_tool_duration_seconds_bucket has no label 'tool' (grouping)",
        ),
        (
            GOOD.replace('server=~"$server"', 'user_id="7"'),
            "renfield_mcp_tool_duration_seconds_bucket has no label 'user_id' (selector)",
        ),
        (GOOD.replace("$__rate_interval", "5m"), "range window [5m] instead of [$__rate_interval]"),
        (
            GOOD.replace("_bucket", "_count"),
            "histogram_quantile over 'renfield_mcp_tool_duration_seconds_count', not a _bucket series",
        ),
        (GOOD.replace("(le, server)", "(server)"), "histogram_quantile without an aggregation by le"),
        (GOOD.replace("sum by (le, server)", "sum without (le)"), "histogram_quantile without an aggregation by le"),
        (
            "histogram_quantile(0.95, rate(renfield_mcp_tool_duration_seconds_bucket[$__rate_interval]))",
            "histogram_quantile without an aggregation by le",
        ),
        (
            "sum by (type) (rate(renfield_websocket_connections[$__rate_interval]))",
            "rate()/increase() over the gauge 'renfield_websocket_connections'",
        ),
        (
            'increase(renfield_mcp_health_problem_servers{job=~"$job"}[$__rate_interval])',
            "rate()/increase() over the gauge 'renfield_mcp_health_problem_servers'",
        ),
        (
            "sum(rate(renfield_mcp_tool_duration_seconds{job=~\"$job\"}[$__rate_interval]))",
            "unknown series 'renfield_mcp_tool_duration_seconds'",
        ),
        ("sum(rate(renfield_http_requests_total))", "rate() without a range window"),
        ("holt_winters(renfield_memory_active_total)", "unknown function 'holt_winters'"),
        ("sum(renfield_memory_active_total) # note", "unreadable text at 34: '# note'"),
        ('sum({__name__="renfield_memory_active_total"})', "selector without a series name"),
        ("vector(0)", "reads no series"),
    ],
    ids=[
        "renamed-metric", "missing-grouping-label", "missing-selector-label", "fixed-window",
        "quantile-not-bucket", "quantile-without-le", "quantile-le-aggregated-away",
        "quantile-without-aggregation", "rate-on-gauge", "increase-on-gauge",
        "histogram-base-name", "rate-without-window",
        "unknown-function", "unreadable", "anonymous-selector", "no-series",
    ],
)
def test_NEGATIVE_a_broken_expression_is_reported(series, gauges, expr, finding):
    assert finding in expression_problems(expr, series, set(gauges))


def test_a_gauge_read_as_a_value_is_not_a_rate_finding(series, gauges):
    expr = 'sum by (type) (renfield_websocket_connections{job=~"$job"})'
    assert expression_problems(expr, series, set(gauges)) == []


def test_NEGATIVE_a_legend_label_dropped_by_the_aggregation_is_seen():
    parsed = read_expression("sum by (server) (rate(x_total[$__rate_interval]))")
    assert legend_labels("{{server}} / {{ outcome }}") - parsed.grouping == {"outcome"}


def test_NEGATIVE_a_hardcoded_datasource_is_seen():
    panel = {"datasource": {"type": "prometheus", "uid": "P1809F7CD0C75ACF3"}, "targets": [
        {"datasource": "Prometheus"},
    ]}
    references = _datasource_references(panel)
    assert references == [{"type": "prometheus", "uid": "P1809F7CD0C75ACF3"}, "Prometheus"]
    assert not any(r == {"type": "prometheus", "uid": DATASOURCE_VARIABLE} for r in references)
