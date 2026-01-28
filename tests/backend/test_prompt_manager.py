"""
Tests for PromptManager — YAML-based prompt externalization.
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from services.prompt_manager import PromptManager, SafeDict


# ============================================================================
# SafeDict
# ============================================================================

class TestSafeDict:
    """Test SafeDict for partial string formatting."""

    @pytest.mark.unit
    def test_existing_key(self):
        """Should return value for existing keys."""
        d = SafeDict({"name": "Alice"})
        assert d["name"] == "Alice"

    @pytest.mark.unit
    def test_missing_key(self):
        """Should return key wrapped in braces for missing keys."""
        d = SafeDict({"name": "Alice"})
        assert d["missing"] == "{missing}"

    @pytest.mark.unit
    def test_format_map_partial(self):
        """Should allow partial substitution."""
        template = "Hello {name}, your score is {score}!"
        d = SafeDict({"name": "Bob"})
        result = template.format_map(d)
        assert result == "Hello Bob, your score is {score}!"


# ============================================================================
# PromptManager Loading
# ============================================================================

class TestPromptManagerLoading:
    """Test YAML file loading."""

    @pytest.mark.unit
    def test_load_single_file(self, tmp_path):
        """Should load a single YAML file."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
greeting: "Hello, World!"
farewell: "Goodbye!"
""")
        manager = PromptManager(str(prompts_dir))

        assert "test" in manager.list_files()
        assert manager.get("test", "greeting") == "Hello, World!"
        assert manager.get("test", "farewell") == "Goodbye!"

    @pytest.mark.unit
    def test_load_multiple_files(self, tmp_path):
        """Should load multiple YAML files."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "chat.yaml").write_text("system_prompt: 'You are helpful.'")
        (prompts_dir / "agent.yaml").write_text("thinking: 'Analyzing...'")

        manager = PromptManager(str(prompts_dir))

        assert set(manager.list_files()) == {"chat", "agent"}
        assert manager.get("chat", "system_prompt") == "You are helpful."
        assert manager.get("agent", "thinking") == "Analyzing..."

    @pytest.mark.unit
    def test_missing_directory(self, tmp_path):
        """Should handle missing directory gracefully."""
        manager = PromptManager(str(tmp_path / "nonexistent"))
        assert manager.list_files() == []

    @pytest.mark.unit
    def test_empty_yaml(self, tmp_path):
        """Should handle empty YAML files."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "empty.yaml").write_text("")

        manager = PromptManager(str(prompts_dir))
        # Empty file shouldn't be in cache
        assert "empty" not in manager.list_files()

    @pytest.mark.unit
    def test_invalid_yaml(self, tmp_path):
        """Should handle invalid YAML gracefully."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "invalid.yaml").write_text("this: is: not: valid: yaml:")

        manager = PromptManager(str(prompts_dir))
        # Invalid file shouldn't crash, just skip
        assert "invalid" not in manager.list_files()


# ============================================================================
# PromptManager.get()
# ============================================================================

class TestPromptManagerGet:
    """Test prompt retrieval with variable substitution."""

    @pytest.mark.unit
    def test_simple_get(self, tmp_path):
        """Should return prompt string."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("hello: 'Hello!'")

        manager = PromptManager(str(prompts_dir))
        assert manager.get("test", "hello") == "Hello!"

    @pytest.mark.unit
    def test_variable_substitution(self, tmp_path):
        """Should substitute variables in prompts."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("greeting: 'Hello, {name}!'")

        manager = PromptManager(str(prompts_dir))
        result = manager.get("test", "greeting", name="Alice")
        assert result == "Hello, Alice!"

    @pytest.mark.unit
    def test_multiple_variables(self, tmp_path):
        """Should substitute multiple variables."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text(
            "template: 'User {user} asked: {question}'"
        )

        manager = PromptManager(str(prompts_dir))
        result = manager.get("test", "template", user="Bob", question="How are you?")
        assert result == "User Bob asked: How are you?"

    @pytest.mark.unit
    def test_partial_substitution(self, tmp_path):
        """Should leave unsubstituted variables as-is."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text(
            "template: '{name} has {count} items'"
        )

        manager = PromptManager(str(prompts_dir))
        result = manager.get("test", "template", name="Carol")
        # {count} should remain since not provided
        assert result == "Carol has {count} items"

    @pytest.mark.unit
    def test_missing_file_returns_default(self, tmp_path):
        """Should return default for missing file."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        manager = PromptManager(str(prompts_dir))
        result = manager.get("missing", "key", default="fallback")
        assert result == "fallback"

    @pytest.mark.unit
    def test_missing_key_returns_default(self, tmp_path):
        """Should return default for missing key."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("existing: 'value'")

        manager = PromptManager(str(prompts_dir))
        result = manager.get("test", "missing", default="not found")
        assert result == "not found"

    @pytest.mark.unit
    def test_multiline_prompt(self, tmp_path):
        """Should handle multiline YAML strings."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
multiline: |
  Line 1
  Line 2
  Line 3
""")
        manager = PromptManager(str(prompts_dir))
        result = manager.get("test", "multiline")
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result


# ============================================================================
# PromptManager.get_config()
# ============================================================================

class TestPromptManagerGetConfig:
    """Test non-string config retrieval."""

    @pytest.mark.unit
    def test_get_dict(self, tmp_path):
        """Should return dict configs."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
llm_options:
  temperature: 0.7
  max_tokens: 500
""")
        manager = PromptManager(str(prompts_dir))
        config = manager.get_config("test", "llm_options")

        assert config == {"temperature": 0.7, "max_tokens": 500}

    @pytest.mark.unit
    def test_get_list(self, tmp_path):
        """Should return list configs."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
keywords:
  - hello
  - world
  - test
""")
        manager = PromptManager(str(prompts_dir))
        config = manager.get_config("test", "keywords")

        assert config == ["hello", "world", "test"]

    @pytest.mark.unit
    def test_get_config_default(self, tmp_path):
        """Should return default for missing config."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        manager = PromptManager(str(prompts_dir))
        config = manager.get_config("missing", "key", default={"fallback": True})

        assert config == {"fallback": True}


# ============================================================================
# PromptManager.get_all() and list_keys()
# ============================================================================

class TestPromptManagerUtilities:
    """Test utility methods."""

    @pytest.mark.unit
    def test_get_all(self, tmp_path):
        """Should return all prompts from a file."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
key1: value1
key2: value2
key3: value3
""")
        manager = PromptManager(str(prompts_dir))
        all_prompts = manager.get_all("test")

        assert all_prompts == {"key1": "value1", "key2": "value2", "key3": "value3"}

    @pytest.mark.unit
    def test_get_all_missing_file(self, tmp_path):
        """Should return empty dict for missing file."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        manager = PromptManager(str(prompts_dir))
        assert manager.get_all("nonexistent") == {}

    @pytest.mark.unit
    def test_list_keys(self, tmp_path):
        """Should list all keys in a file."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
alpha: a
beta: b
gamma: c
""")
        manager = PromptManager(str(prompts_dir))
        keys = manager.list_keys("test")

        assert set(keys) == {"alpha", "beta", "gamma"}

    @pytest.mark.unit
    def test_list_keys_missing_file(self, tmp_path):
        """Should return empty list for missing file."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        manager = PromptManager(str(prompts_dir))
        assert manager.list_keys("nonexistent") == []


# ============================================================================
# PromptManager.reload()
# ============================================================================

class TestPromptManagerReload:
    """Test hot-reloading of prompts."""

    @pytest.mark.unit
    def test_reload(self, tmp_path):
        """Should reload prompts from disk."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        yaml_file = prompts_dir / "test.yaml"
        yaml_file.write_text("message: 'Original'")

        manager = PromptManager(str(prompts_dir))
        assert manager.get("test", "message") == "Original"

        # Update file
        yaml_file.write_text("message: 'Updated'")
        manager.reload()

        assert manager.get("test", "message") == "Updated"

    @pytest.mark.unit
    def test_reload_adds_new_file(self, tmp_path):
        """Should pick up new files on reload."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        manager = PromptManager(str(prompts_dir))
        assert "new" not in manager.list_files()

        # Add new file
        (prompts_dir / "new.yaml").write_text("key: value")
        manager.reload()

        assert "new" in manager.list_files()
        assert manager.get("new", "key") == "value"
