"""Tests for reference_resolver — entity ID recognition for context-aware routing.

Covers:
- load_entity_patterns(): YAML loading, missing-file fallback, malformed YAML
- compile_patterns(): regex compilation, invalid regex handling, cache reset
- resolve_references(): single/multi entity, single-domain inference, cross-domain
"""

import re
from pathlib import Path

import pytest

from services import reference_resolver
from services.reference_resolver import (
    EntityMatch,
    ResolvedMessage,
    compile_patterns,
    load_entity_patterns,
    resolve_references,
)


@pytest.fixture(autouse=True)
def reset_compiled_cache():
    """Each test starts with a clean pattern cache."""
    reference_resolver._compiled.clear()
    yield
    reference_resolver._compiled.clear()


# ============================================================================
# load_entity_patterns
# ============================================================================

class TestLoadEntityPatterns:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_entity_patterns(tmp_path / "does-not-exist.yaml") == {}

    def test_default_path_when_absent(self, monkeypatch, tmp_path):
        """Default path is resolved relative to CWD; missing → {}."""
        monkeypatch.chdir(tmp_path)
        assert load_entity_patterns() == {}

    def test_loads_yaml_domains(self, tmp_path):
        path = tmp_path / "entity_patterns.yaml"
        path.write_text(
            "domains:\n"
            "  jira:\n"
            "    patterns:\n"
            "      - regex: '[A-Z]+-\\d+'\n"
            "        entity_type: issue\n"
        )
        result = load_entity_patterns(path)
        assert "jira" in result
        assert result["jira"]["patterns"][0]["entity_type"] == "issue"

    def test_missing_domains_key_returns_empty(self, tmp_path):
        """YAML without a top-level `domains:` key → {}."""
        path = tmp_path / "empty.yaml"
        path.write_text("other_key: value\n")
        assert load_entity_patterns(path) == {}

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        assert load_entity_patterns(path) == {}

    def test_malformed_yaml_returns_empty(self, tmp_path):
        """Corrupt YAML is logged + swallowed — never raises."""
        path = tmp_path / "broken.yaml"
        path.write_text("domains:\n  jira: [unclosed\n")
        assert load_entity_patterns(path) == {}

    def test_accepts_string_path(self, tmp_path):
        path = tmp_path / "patterns.yaml"
        path.write_text("domains:\n  reva:\n    patterns: []\n")
        result = load_entity_patterns(str(path))
        assert "reva" in result


# ============================================================================
# compile_patterns
# ============================================================================

class TestCompilePatterns:
    def test_compiles_valid_regex(self):
        compile_patterns({
            "jira": {
                "patterns": [
                    {"regex": r"[A-Z]+-\d+", "entity_type": "issue"},
                ]
            }
        })
        assert "jira" in reference_resolver._compiled
        regex, entity_type = reference_resolver._compiled["jira"][0]
        assert isinstance(regex, re.Pattern)
        assert entity_type == "issue"

    def test_invalid_regex_is_skipped(self):
        """A broken pattern logs + drops, the valid sibling survives."""
        compile_patterns({
            "mixed": {
                "patterns": [
                    {"regex": "[unclosed", "entity_type": "bad"},
                    {"regex": r"\d+", "entity_type": "good"},
                ]
            }
        })
        assert len(reference_resolver._compiled["mixed"]) == 1
        assert reference_resolver._compiled["mixed"][0][1] == "good"

    def test_domain_without_patterns_omitted(self):
        """A domain whose patterns all fail compilation isn't registered."""
        compile_patterns({
            "all_bad": {"patterns": [{"regex": "[unclosed", "entity_type": "x"}]}
        })
        assert "all_bad" not in reference_resolver._compiled

    def test_empty_regex_is_skipped(self):
        compile_patterns({
            "jira": {
                "patterns": [
                    {"regex": "", "entity_type": "empty"},
                    {"regex": r"\d+", "entity_type": "num"},
                ]
            }
        })
        assert len(reference_resolver._compiled["jira"]) == 1

    def test_missing_entity_type_defaults_to_unknown(self):
        compile_patterns({
            "x": {"patterns": [{"regex": r"\d+"}]}
        })
        assert reference_resolver._compiled["x"][0][1] == "unknown"

    def test_non_dict_domain_config_ignored(self):
        """`cfg` that isn't a dict means no patterns — treated as empty."""
        compile_patterns({"weird": "not a dict"})
        assert "weird" not in reference_resolver._compiled

    def test_recompile_clears_previous(self):
        """Second compile_patterns call fully replaces cache — no leftovers."""
        compile_patterns({"jira": {"patterns": [{"regex": r"\d+", "entity_type": "n"}]}})
        assert "jira" in reference_resolver._compiled
        compile_patterns({"reva": {"patterns": [{"regex": r"[A-Z]+", "entity_type": "w"}]}})
        assert "jira" not in reference_resolver._compiled
        assert "reva" in reference_resolver._compiled


# ============================================================================
# resolve_references
# ============================================================================

class TestResolveReferences:
    def test_empty_message_returns_empty_result(self):
        compile_patterns({"jira": {"patterns": [{"regex": r"[A-Z]+-\d+", "entity_type": "issue"}]}})
        result = resolve_references("")
        assert isinstance(result, ResolvedMessage)
        assert result.entity_matches == []
        assert result.inferred_domain is None

    def test_no_patterns_compiled_returns_empty(self):
        """Without compiled patterns, resolver is a no-op."""
        result = resolve_references("REVA-123 is broken")
        assert result.entity_matches == []
        assert result.inferred_domain is None

    def test_single_entity_match_infers_domain(self):
        compile_patterns({
            "jira": {"patterns": [{"regex": r"[A-Z]+-\d+", "entity_type": "issue"}]}
        })
        result = resolve_references("Can you look at REVA-42?")
        assert len(result.entity_matches) == 1
        match = result.entity_matches[0]
        assert match.id == "REVA-42"
        assert match.domain == "jira"
        assert match.entity_type == "issue"
        assert match.position == "Can you look at ".__len__()  # 16
        assert result.inferred_domain == "jira"

    def test_multiple_matches_same_domain(self):
        compile_patterns({
            "jira": {"patterns": [{"regex": r"[A-Z]+-\d+", "entity_type": "issue"}]}
        })
        result = resolve_references("Compare REVA-42 to REVA-43")
        assert len(result.entity_matches) == 2
        assert {m.id for m in result.entity_matches} == {"REVA-42", "REVA-43"}
        assert result.inferred_domain == "jira"

    def test_cross_domain_no_single_inference(self):
        """Matches across domains leave inferred_domain=None and log a hint."""
        # Use disjoint patterns so each entity matches exactly one domain.
        compile_patterns({
            "jira": {"patterns": [{"regex": r"REVA-\d+", "entity_type": "issue"}]},
            "itsm": {"patterns": [{"regex": r"INC-\d+", "entity_type": "incident"}]},
        })
        result = resolve_references("REVA-42 caused INC-100042")
        domains = {m.domain for m in result.entity_matches}
        assert domains == {"jira", "itsm"}
        assert result.inferred_domain is None
        assert any("Multiple domains" in hint for hint in result.context_hints)

    def test_preserves_original_text(self):
        compile_patterns({
            "jira": {"patterns": [{"regex": r"[A-Z]+-\d+", "entity_type": "issue"}]}
        })
        msg = "What about REVA-1?"
        result = resolve_references(msg)
        assert result.text == msg
        assert result.original == msg

    def test_multiple_patterns_per_domain(self):
        """A single domain can register multiple regex patterns."""
        compile_patterns({
            "reva": {
                "patterns": [
                    {"regex": r"REL-\d+", "entity_type": "release"},
                    {"regex": r"RFC-\d{4}-\d+", "entity_type": "rfc"},
                ]
            }
        })
        result = resolve_references("REL-100 depends on RFC-2026-0087")
        assert len(result.entity_matches) == 2
        types = {m.entity_type for m in result.entity_matches}
        assert types == {"release", "rfc"}
        assert result.inferred_domain == "reva"

    def test_entity_patterns_kwarg_ignored(self):
        """The legacy kwarg is accepted but patterns still come from _compiled."""
        compile_patterns({"jira": {"patterns": [{"regex": r"[A-Z]+-\d+", "entity_type": "issue"}]}})
        result = resolve_references("REVA-5", entity_patterns={"fake": "dict"})
        assert len(result.entity_matches) == 1
        assert result.entity_matches[0].domain == "jira"


# ============================================================================
# EntityMatch / ResolvedMessage dataclass contracts
# ============================================================================

class TestDataclasses:
    def test_entity_match_fields(self):
        m = EntityMatch(id="REVA-1", domain="jira", entity_type="issue", position=0)
        assert m.id == "REVA-1"
        assert m.domain == "jira"
        assert m.entity_type == "issue"
        assert m.position == 0

    def test_resolved_message_defaults(self):
        r = ResolvedMessage(text="hi", original="hi")
        assert r.entity_matches == []
        assert r.inferred_domain is None
        assert r.context_hints == []
