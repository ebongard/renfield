"""Tests for Backend i18n -- translation system for non-prompt strings."""

import pytest

from utils.i18n import _flatten, _translations, load_translations, t


@pytest.fixture(autouse=True)
def _load_i18n():
    """Load translations before each test."""
    load_translations()
    yield
    _translations.clear()


class TestLoadTranslations:

    @pytest.mark.unit
    def test_loads_de_and_en(self):
        assert "de" in _translations
        assert "en" in _translations

    @pytest.mark.unit
    def test_flattens_nested_keys(self):
        assert "error.request_blocked" in _translations["de"]
        assert "agent.thinking" in _translations["en"]

    @pytest.mark.unit
    def test_missing_directory_no_error(self):
        load_translations("config/nonexistent")
        # Should not raise, just log


class TestTranslate:

    @pytest.mark.unit
    def test_basic_de(self):
        result = t("error.request_blocked", "de")
        assert "Anfrage" in result

    @pytest.mark.unit
    def test_basic_en(self):
        result = t("error.request_blocked", "en")
        assert "cannot" in result.lower()

    @pytest.mark.unit
    def test_variable_substitution(self):
        result = t("error.tool_timeout", "de", tool="get_states")
        assert "get_states" in result

    @pytest.mark.unit
    def test_missing_variable_safe(self):
        result = t("error.tool_timeout", "de")
        assert "{tool}" in result  # SafeDict preserves missing keys

    @pytest.mark.unit
    def test_fallback_to_en(self):
        """Unknown lang falls back to English."""
        result = t("error.request_blocked", "fr")
        assert "cannot" in result.lower()

    @pytest.mark.unit
    def test_fallback_to_key(self):
        """Unknown key returns the key itself."""
        result = t("nonexistent.key", "de")
        assert result == "nonexistent.key"

    @pytest.mark.unit
    def test_multiple_variables(self):
        result = t("media.volume_set", "de", level=50)
        assert "50" in result


class TestFlatten:

    @pytest.mark.unit
    def test_simple(self):
        result: dict = {}
        _flatten({"a": "1", "b": "2"}, "", result)
        assert result == {"a": "1", "b": "2"}

    @pytest.mark.unit
    def test_nested(self):
        result: dict = {}
        _flatten({"error": {"timeout": "T/O", "blocked": "Blocked"}}, "", result)
        assert result == {"error.timeout": "T/O", "error.blocked": "Blocked"}

    @pytest.mark.unit
    def test_deep_nesting(self):
        result: dict = {}
        _flatten({"a": {"b": {"c": "deep"}}}, "", result)
        assert result == {"a.b.c": "deep"}
