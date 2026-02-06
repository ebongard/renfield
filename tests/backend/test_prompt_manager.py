"""
Tests for PromptManager — YAML-based prompt externalization.
"""

import asyncio

import pytest

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

    @pytest.mark.unit
    def test_load_yaml_only_comments(self, tmp_path):
        """Should handle YAML file containing only comments."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "comments.yaml").write_text("""
# This is a comment
# Another comment
# No actual data here
""")
        manager = PromptManager(str(prompts_dir))
        # File with only comments parses as None, should not be in cache
        assert "comments" not in manager.list_files()

    @pytest.mark.unit
    def test_load_empty_yaml_file(self, tmp_path):
        """Should handle completely empty YAML file."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "blank.yaml").write_text("")

        manager = PromptManager(str(prompts_dir))
        assert "blank" not in manager.list_files()


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
    def test_get_nonexistent_key_returns_default(self, tmp_path):
        """Should return default for a key that doesn't exist in a loaded file."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("key1: 'value1'")

        manager = PromptManager(str(prompts_dir))
        result = manager.get("test", "totally_missing_key", default="my_default")
        assert result == "my_default"

    @pytest.mark.unit
    def test_get_with_special_chars_in_variables(self, tmp_path):
        """Should handle variables with braces, unicode, and special chars."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("tmpl: 'Input: {user_input}'")

        manager = PromptManager(str(prompts_dir))
        # Unicode characters
        result = manager.get("test", "tmpl", user_input="Hallo Welt!")
        assert result == "Input: Hallo Welt!"

        # Special chars
        result = manager.get("test", "tmpl", user_input="<script>alert(1)</script>")
        assert result == "Input: <script>alert(1)</script>"

    @pytest.mark.unit
    def test_get_with_very_long_value(self, tmp_path):
        """Should handle substitution with 10K+ character strings."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("tmpl: 'Data: {data}'")

        manager = PromptManager(str(prompts_dir))
        long_value = "x" * 10_000
        result = manager.get("test", "tmpl", data=long_value)
        assert result == f"Data: {long_value}"
        assert len(result) > 10_000

    @pytest.mark.unit
    def test_get_with_empty_string_variable(self, tmp_path):
        """Should substitute with empty string when variable is empty."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("tmpl: 'Hello {name}!'")

        manager = PromptManager(str(prompts_dir))
        result = manager.get("test", "tmpl", name="")
        assert result == "Hello !"

    @pytest.mark.unit
    def test_get_unsupported_language_falls_back(self, tmp_path):
        """Should fall back to default language for unsupported language code."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
de:
  greeting: "Hallo!"
en:
  greeting: "Hello!"
""")
        manager = PromptManager(str(prompts_dir), default_lang="de")
        # Request with unsupported language "fr" should fall back to default "de"
        result = manager.get("test", "greeting", lang="fr")
        assert result == "Hallo!"

    @pytest.mark.unit
    def test_get_empty_language_string(self, tmp_path):
        """Should use default language when lang is empty string."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
de:
  msg: "Deutsche Nachricht"
""")
        manager = PromptManager(str(prompts_dir), default_lang="de")
        # Empty string is falsy, so `lang or self._default_lang` uses default
        result = manager.get("test", "msg", lang="")
        assert result == "Deutsche Nachricht"

    @pytest.mark.unit
    def test_get_with_newlines_in_variable(self, tmp_path):
        """Should handle newline characters in variable values."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("tmpl: 'Content: {data}'")

        manager = PromptManager(str(prompts_dir))
        result = manager.get("test", "tmpl", data="line1\nline2\nline3")
        assert result == "Content: line1\nline2\nline3"
        assert result.count("\n") == 2

    @pytest.mark.unit
    def test_get_with_null_bytes_in_variable(self, tmp_path):
        """Should handle null bytes in variable values without crashing."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("tmpl: 'Data: {data}'")

        manager = PromptManager(str(prompts_dir))
        result = manager.get("test", "tmpl", data="before\x00after")
        assert "before" in result
        assert "after" in result

    @pytest.mark.unit
    def test_get_with_many_variables(self, tmp_path):
        """Should substitute many variables simultaneously."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        # Build a template with 20 variables
        var_names = [f"var{i}" for i in range(20)]
        template_parts = [f"{{{v}}}" for v in var_names]
        template = " ".join(template_parts)
        (prompts_dir / "test.yaml").write_text(f"tmpl: '{template}'")

        manager = PromptManager(str(prompts_dir))
        kwargs = {v: f"val{i}" for i, v in enumerate(var_names)}
        result = manager.get("test", "tmpl", **kwargs)

        for i in range(20):
            assert f"val{i}" in result
        # No leftover {varN} placeholders
        assert "{var" not in result

    @pytest.mark.unit
    def test_get_nested_key_nonexistent(self, tmp_path):
        """Should return default when key exists only at wrong language level."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
de:
  only_german: "Nur Deutsch"
en:
  only_english: "Only English"
""")
        manager = PromptManager(str(prompts_dir), default_lang="de")
        # Request a key that exists in en but not de, using lang=de
        result = manager.get("test", "only_english", lang="de", default="nope")
        assert result == "nope"

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

    @pytest.mark.unit
    def test_get_config_nonexistent_returns_none(self, tmp_path):
        """Should return None for non-existent config key."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("existing_key: 'value'")

        manager = PromptManager(str(prompts_dir))
        config = manager.get_config("test", "nonexistent_key")

        assert config is None

    @pytest.mark.unit
    def test_get_config_nested_dict(self, tmp_path):
        """Should return deeply nested config structures."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
deep_config:
  level1:
    level2:
      level3:
        value: 42
        list_val:
          - a
          - b
""")
        manager = PromptManager(str(prompts_dir))
        config = manager.get_config("test", "deep_config")

        assert config == {
            "level1": {
                "level2": {
                    "level3": {
                        "value": 42,
                        "list_val": ["a", "b"]
                    }
                }
            }
        }


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

    @pytest.mark.unit
    def test_list_keys_after_reload(self, tmp_path):
        """Should update keys list after file change and reload."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        yaml_file = prompts_dir / "test.yaml"
        yaml_file.write_text("key1: value1\nkey2: value2")

        manager = PromptManager(str(prompts_dir))
        assert set(manager.list_keys("test")) == {"key1", "key2"}

        # Modify file to add a new key and remove one
        yaml_file.write_text("key2: value2\nkey3: value3\nkey4: value4")
        manager.reload()

        keys = set(manager.list_keys("test"))
        assert keys == {"key2", "key3", "key4"}
        assert "key1" not in keys


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


# ============================================================================
# File System Edge Cases
# ============================================================================

class TestPromptManagerFileSystem:
    """Test file system edge cases."""

    @pytest.mark.unit
    def test_file_deleted_before_reload(self, tmp_path):
        """Should handle file deletion gracefully on reload."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        yaml_file = prompts_dir / "test.yaml"
        yaml_file.write_text("msg: 'Hello'")

        manager = PromptManager(str(prompts_dir))
        assert manager.get("test", "msg") == "Hello"

        # Delete the file and reload
        yaml_file.unlink()
        manager.reload()

        # File gone: key should no longer be available
        assert "test" not in manager.list_files()
        assert manager.get("test", "msg", default="gone") == "gone"

    @pytest.mark.unit
    def test_file_becomes_unreadable(self, tmp_path):
        """Should handle file that becomes unreadable mid-operation."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        yaml_file = prompts_dir / "test.yaml"
        yaml_file.write_text("msg: 'Hello'")

        manager = PromptManager(str(prompts_dir))
        assert manager.get("test", "msg") == "Hello"

        # Make the file unreadable, then reload
        yaml_file.chmod(0o000)
        try:
            manager.reload()
            # After failed reload, cached data should be cleared
            # (reload clears cache first, then re-loads)
            assert manager.get("test", "msg", default="fallback") == "fallback"
        finally:
            # Restore permissions for cleanup
            yaml_file.chmod(0o644)

    @pytest.mark.unit
    def test_file_replaced_with_invalid_yaml(self, tmp_path):
        """Should handle valid file replaced with invalid content on reload."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        yaml_file = prompts_dir / "test.yaml"
        yaml_file.write_text("msg: 'Valid'")

        manager = PromptManager(str(prompts_dir))
        assert manager.get("test", "msg") == "Valid"

        # Replace with invalid YAML
        yaml_file.write_text("{{invalid yaml content[[")
        manager.reload()

        # Invalid file should be skipped, cache cleared for this file
        assert manager.get("test", "msg", default="nope") == "nope"

    @pytest.mark.unit
    def test_yaml_with_binary_encoding(self, tmp_path):
        """Should handle file with invalid encoding gracefully."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        yaml_file = prompts_dir / "binary.yaml"
        # Write raw bytes that are not valid UTF-8
        yaml_file.write_bytes(b"key: \xff\xfe invalid bytes")

        manager = PromptManager(str(prompts_dir))
        # Should not crash, file is skipped
        assert "binary" not in manager.list_files()


# ============================================================================
# Concurrent Access
# ============================================================================

class TestPromptManagerConcurrency:
    """Test concurrent access to PromptManager."""

    @pytest.mark.unit
    async def test_concurrent_reads(self, tmp_path):
        """Multiple tasks reading prompts simultaneously should all succeed."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
greeting: "Hello {name}!"
farewell: "Goodbye {name}!"
info: "You have {count} items."
""")
        manager = PromptManager(str(prompts_dir))

        async def read_prompt(key, **kwargs):
            # Simulate async context (yield to event loop)
            await asyncio.sleep(0)
            return manager.get("test", key, **kwargs)

        results = await asyncio.gather(
            read_prompt("greeting", name="Alice"),
            read_prompt("farewell", name="Bob"),
            read_prompt("info", count="42"),
            read_prompt("greeting", name="Charlie"),
            read_prompt("farewell", name="Diana"),
            read_prompt("greeting", name="Eve"),
            read_prompt("info", count="99"),
            read_prompt("farewell", name="Frank"),
            read_prompt("greeting", name="Grace"),
            read_prompt("info", count="0"),
        )

        assert results[0] == "Hello Alice!"
        assert results[1] == "Goodbye Bob!"
        assert results[2] == "You have 42 items."
        assert results[3] == "Hello Charlie!"
        assert results[4] == "Goodbye Diana!"
        assert len(results) == 10

    @pytest.mark.unit
    async def test_concurrent_reads_with_reload(self, tmp_path):
        """Reads interleaved with a reload should not crash."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        yaml_file = prompts_dir / "test.yaml"
        yaml_file.write_text("msg: 'Original'")

        manager = PromptManager(str(prompts_dir))

        async def reader():
            await asyncio.sleep(0)
            return manager.get("test", "msg", default="missing")

        async def reloader():
            await asyncio.sleep(0)
            yaml_file.write_text("msg: 'Updated'")
            manager.reload()

        # Run reads and a reload concurrently
        results = await asyncio.gather(
            reader(),
            reader(),
            reloader(),
            reader(),
            reader(),
            return_exceptions=True,
        )

        # No exceptions should have been raised
        for r in results:
            assert not isinstance(r, Exception), f"Got unexpected exception: {r}"

        # The reloader returns None; readers return strings
        string_results = [r for r in results if isinstance(r, str)]
        for r in string_results:
            assert r in ("Original", "Updated", "missing")


# ============================================================================
# Language Handling
# ============================================================================

class TestPromptManagerLanguage:
    """Test multilingual prompt handling edge cases."""

    @pytest.mark.unit
    def test_set_unsupported_language_keeps_default(self, tmp_path):
        """Should keep current default when setting unsupported language."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        manager = PromptManager(str(prompts_dir), default_lang="de")
        manager.set_default_language("fr")
        assert manager.default_language == "de"

    @pytest.mark.unit
    def test_set_valid_language(self, tmp_path):
        """Should update default language when setting supported language."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        manager = PromptManager(str(prompts_dir), default_lang="de")
        manager.set_default_language("en")
        assert manager.default_language == "en"

    @pytest.mark.unit
    def test_language_fallback_chain(self, tmp_path):
        """Should follow full fallback: requested lang -> default lang -> root."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
de:
  only_de: "Deutsch"
en:
  only_en: "English"
root_only: "Root level"
""")
        manager = PromptManager(str(prompts_dir), default_lang="de")

        # lang=en, key exists in en -> use en
        assert manager.get("test", "only_en", lang="en") == "English"

        # lang=en, key not in en, not in de -> fall to root
        assert manager.get("test", "root_only", lang="en") == "Root level"

        # lang=fr (unsupported), key only in de -> fall back to default lang de
        assert manager.get("test", "only_de", lang="fr") == "Deutsch"

        # lang=fr, key not in fr, not in de, not root -> default
        assert manager.get("test", "only_en", lang="fr", default="nope") == "nope"

    @pytest.mark.unit
    def test_get_config_with_lang(self, tmp_path):
        """Should retrieve language-specific config when lang is provided."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.yaml").write_text("""
de:
  options:
    temp: 0.5
en:
  options:
    temp: 0.9
options:
  temp: 0.7
""")
        manager = PromptManager(str(prompts_dir))

        # With lang specified, should get language-specific
        config_de = manager.get_config("test", "options", lang="de")
        assert config_de == {"temp": 0.5}

        config_en = manager.get_config("test", "options", lang="en")
        assert config_en == {"temp": 0.9}

        # Without lang, should get root-level
        config_root = manager.get_config("test", "options")
        assert config_root == {"temp": 0.7}

    @pytest.mark.unit
    def test_supported_languages_property(self, tmp_path):
        """Should return a copy of supported languages list."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        manager = PromptManager(str(prompts_dir))

        langs = manager.supported_languages
        assert "de" in langs
        assert "en" in langs
        # Modifying the returned list should not affect internal state
        langs.append("fr")
        assert "fr" not in manager.supported_languages
