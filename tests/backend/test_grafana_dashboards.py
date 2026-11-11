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
from tests.backend.metrics_registry import _is_collector, live_metrics, registered_series

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
    grouping: set[str] = field(default_factory=set)
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
        elif text == "{":
            parsed.problems.append("selector without a series name")
        elif text == "[":
            closing = next((i for i in range(index, len(tokens)) if tokens[i][1] == "]"), len(tokens))
            parsed.windows.append("".join(t for _, t in tokens[index + 1:closing]))
            index = closing
        index += 1
    return parsed


def expression_problems(expr: str, series: dict[str, frozenset[str]]) -> list[str]:
    """Everything wrong with `expr`, given the series that exist."""
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
    if "histogram_quantile" in parsed.calls:
        for name in parsed.series:
            if not name.endswith("_bucket"):
                problems.append(f"histogram_quantile over {name!r}, not a _bucket series")
        if "le" not in parsed.grouping:
            problems.append("histogram_quantile without grouping by le")
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


def test_every_expression_reads_registered_series_with_their_labels(series):
    findings = [
        f"{file} / {panel}: {problem}"
        for file, panel, target in _targets()
        for problem in expression_problems(target["expr"], series)
    ]
    assert findings == []


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


def test_the_reference_expression_is_clean(series):
    assert expression_problems(GOOD, series) == []


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
        (GOOD.replace("(le, server)", "(server)"), "histogram_quantile without grouping by le"),
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
        "quantile-not-bucket", "quantile-without-le", "histogram-base-name", "rate-without-window",
        "unknown-function", "unreadable", "anonymous-selector", "no-series",
    ],
)
def test_NEGATIVE_a_broken_expression_is_reported(series, expr, finding):
    assert finding in expression_problems(expr, series)


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
