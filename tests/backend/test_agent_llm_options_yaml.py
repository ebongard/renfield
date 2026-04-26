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

    The only acceptable place for such literals is inside the
    `_DEFAULT_LLM_OPTIONS*` module-level constant blocks.
    """
    src = AGENT_SERVICE_PY.read_text()

    # Find every line that contains both "temperature" and "num_predict"
    # in a dict literal — the tell-tale shape for an LLM options dict.
    offending: list[tuple[int, str]] = []
    in_default_block = False
    for lineno, line in enumerate(src.splitlines(), start=1):
        stripped = line.strip()

        # Track entry/exit of _DEFAULT_LLM_OPTIONS_* dict definitions —
        # those are the only place inline literal LLM options are allowed.
        if stripped.startswith("_DEFAULT_LLM_OPTIONS"):
            in_default_block = True
            continue
        if in_default_block and stripped == "}":
            in_default_block = False
            continue
        if in_default_block:
            continue

        # Outside _DEFAULT_LLM_OPTIONS blocks: flag any line with the
        # literal-LLM-options shape.
        if '"temperature"' in stripped and '"num_predict"' in stripped:
            offending.append((lineno, stripped))

    assert not offending, (
        "agent_service.py contains inline literal LLM options dicts at "
        "call sites — these must route through prompt_manager.get_config() "
        "with module-level _DEFAULT_LLM_OPTIONS_* as the fallback. "
        f"Offending lines: {offending}"
    )
