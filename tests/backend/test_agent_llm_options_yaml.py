"""
Regression guards for W6 fix — LLM options must be sourced from
`prompts/agent.yaml`, not hardcoded inline literals at call sites.

Background — WICHTIG audit W6: temperature/top_p/num_predict were
hardcoded as inline-literal fallbacks at four locations in
`services/agent_service.py`. Three of those (main, retry, summary)
already had a `prompt_manager.get_config(...) or {literal}` pattern
where YAML actually wins, but the inline literals duplicated the YAML
values and risked drift. The fourth (tool pre-selection at line 723
pre-fix) had no YAML route at all — pure hardcoded literal.

The fix:
  - Add `llm_options_tool_preselect` block to agent.yaml.
  - Extract the four fallback literals to module-level
    `_DEFAULT_LLM_OPTIONS*` constants in agent_service.py.
  - Route all four call sites through
    `prompt_manager.get_config(...) or _DEFAULT_LLM_OPTIONS_*`.

These tests check the contract: agent.yaml must declare all four
`llm_options*` keys with the expected shape, and agent_service.py
must not reintroduce inline literal `temperature/top_p/num_predict`
dicts at call sites.

Source-file inspection rather than runtime invocation — test environment
doesn't reliably load services.agent_service (engine + asyncpg).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_YAML = REPO_ROOT / "src" / "backend" / "prompts" / "agent.yaml"
AGENT_SERVICE_PY = REPO_ROOT / "src" / "backend" / "services" / "agent_service.py"

_REQUIRED_KEYS = (
    "llm_options",
    "llm_options_retry",
    "llm_options_summary",
    "llm_options_tool_preselect",
)


@pytest.mark.unit
def test_agent_yaml_declares_all_llm_option_blocks():
    """agent.yaml must declare all four llm_options* keys.

    Each block must be a dict with at least `temperature` and `num_predict`.
    The pre-selection block intentionally omits `top_p` (deterministic
    classification call), so we don't require it here.
    """
    config = yaml.safe_load(AGENT_YAML.read_text())

    for key in _REQUIRED_KEYS:
        assert key in config, (
            f"prompts/agent.yaml is missing required LLM-options block "
            f"`{key}` — call sites in agent_service.py read it via "
            f"prompt_manager.get_config(); without the YAML key, callers "
            f"silently fall back to module-level _DEFAULT_LLM_OPTIONS_*"
        )
        block = config[key]
        assert isinstance(block, dict), f"`{key}` must be a dict, got {type(block).__name__}"
        assert "temperature" in block, f"`{key}` must declare temperature"
        assert "num_predict" in block, f"`{key}` must declare num_predict"


@pytest.mark.unit
def test_agent_service_has_no_inline_llm_option_literals_at_call_sites():
    """agent_service.py call sites must not reintroduce inline literal
    `{"temperature": ..., "num_predict": ...}` dicts.

    The only acceptable place for such literals is inside the module-level
    `_DEFAULT_LLM_OPTIONS*` constant assignments. We use an AST walk
    (rather than line-by-line brace tracking) so the test is robust to
    single-line dict refactors, comments, and stylistic edits.
    """
    import ast

    src = AGENT_SERVICE_PY.read_text()
    tree = ast.parse(src)

    # Step 1 — collect line ranges of every module-level
    # `_DEFAULT_LLM_OPTIONS*` assignment. Any literal dict whose lineno
    # falls inside one of these ranges is allowed (it's the constant
    # itself).
    allowed_ranges: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.startswith("_DEFAULT_LLM_OPTIONS")
                ):
                    end_lineno = getattr(node, "end_lineno", node.lineno)
                    allowed_ranges.append((node.lineno, end_lineno))

    assert allowed_ranges, (
        "Expected at least one module-level _DEFAULT_LLM_OPTIONS* "
        "assignment in agent_service.py — these are the canonical home "
        "for the fallback dicts. None found."
    )

    def _is_allowed(lineno: int) -> bool:
        return any(lo <= lineno <= hi for lo, hi in allowed_ranges)

    # Step 2 — walk every dict literal in the file. Flag any whose keys
    # include "temperature" AND "num_predict" (the LLM-options shape) and
    # whose location is NOT inside a _DEFAULT_LLM_OPTIONS* assignment.
    offending: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            k.value
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if {"temperature", "num_predict"}.issubset(keys) and not _is_allowed(node.lineno):
            offending.append(node.lineno)

    assert not offending, (
        "agent_service.py contains inline literal LLM-options dicts "
        "(keys: temperature + num_predict) at call sites. These must "
        "route through `_llm_options_or_default()` with the module-level "
        "_DEFAULT_LLM_OPTIONS_* constants as the fallback. "
        f"Offending lines: {offending}"
    )
