"""
Prompt Manager — Loads and manages externalized prompts from YAML files.

Enables prompt customization without code deployment.
Supports multilingual prompts (de, en) with automatic fallback.

Usage:
    from services.prompt_manager import prompt_manager

    # Get a prompt (uses default language)
    system_prompt = prompt_manager.get("chat", "system_prompt")

    # Get a prompt in a specific language
    system_prompt = prompt_manager.get("chat", "system_prompt", lang="en")

    # Get a prompt with variable substitution
    prompt = prompt_manager.get("intent", "extraction_prompt", lang="de", message="Hello")

    # Get nested config (language-independent)
    options = prompt_manager.get_config("agent", "llm_options")

YAML Structure for multilingual prompts:
    de:
      system_prompt: "German prompt..."
    en:
      system_prompt: "English prompt..."
    llm_options:  # Non-language config at root level
      temperature: 0.7
"""

from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# Supported languages
SUPPORTED_LANGUAGES = ["de", "en"]
DEFAULT_LANGUAGE = "de"


class PromptManager:
    """
    Manages externalized prompts from YAML files.

    Prompts are loaded from the 'prompts' directory relative to the backend root.
    Supports variable substitution using str.format().
    Supports multilingual prompts with de/en language keys.
    """

    def __init__(self, prompts_dir: str | None = None, default_lang: str = DEFAULT_LANGUAGE):
        """
        Initialize the PromptManager.

        Args:
            prompts_dir: Optional path to prompts directory.
                        Defaults to 'prompts' relative to this file's parent.
            default_lang: Default language for prompts (de or en).
        """
        if prompts_dir:
            self._prompts_dir = Path(prompts_dir)
        else:
            # Default: prompts/ relative to src/backend/
            self._prompts_dir = Path(__file__).parent.parent / "prompts"

        self._default_lang = default_lang
        self._cache: dict[str, dict[str, Any]] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Load all YAML files from the prompts directory."""
        if not self._prompts_dir.exists():
            logger.warning(f"Prompts directory not found: {self._prompts_dir}")
            return

        for yaml_file in self._prompts_dir.glob("*.yaml"):
            self._load_file(yaml_file)

        logger.info(f"Loaded {len(self._cache)} prompt file(s) from {self._prompts_dir}")

    def _load_file(self, path: Path) -> None:
        """Load a single YAML file into the cache."""
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if data:
                name = path.stem  # filename without extension
                self._cache[name] = data
                logger.debug(f"Loaded prompts from {path.name}: {list(data.keys())}")
        except Exception as e:
            logger.error(f"Failed to load prompts from {path}: {e}")

    def reload(self) -> None:
        """Reload all prompts from disk."""
        self._cache.clear()
        self._load_all()
        logger.info("Prompts reloaded")

    def get(self, file: str, key: str, default: str = "", lang: str | None = None, **kwargs) -> str:
        """
        Get a prompt string with optional variable substitution.

        Supports multilingual prompts with automatic fallback:
        1. Try requested language (lang parameter)
        2. Fall back to default language
        3. Fall back to root-level key (for backwards compatibility)

        Args:
            file: The prompt file name (without .yaml extension)
            key: The key within the file
            default: Default value if key not found
            lang: Language code (de, en). If None, uses default language.
            **kwargs: Variables to substitute in the prompt

        Returns:
            The prompt string with variables substituted

        Example:
            prompt_manager.get("chat", "system_prompt")  # Default language
            prompt_manager.get("chat", "system_prompt", lang="en")  # English
            prompt_manager.get("intent", "room_context_template", lang="de", room_name="Kitchen")
        """
        file_data = self._cache.get(file, {})
        lang = lang or self._default_lang

        # Try language-specific lookup first
        prompt = self._get_localized(file_data, key, lang)

        # Fall back to default language if not found
        if prompt is None and lang != self._default_lang:
            prompt = self._get_localized(file_data, key, self._default_lang)

        # Fall back to root-level key (backwards compatibility)
        if prompt is None:
            prompt = file_data.get(key)

        if prompt is None:
            logger.warning(f"Prompt not found: {file}.{key} (lang={lang})")
            return default

        if not isinstance(prompt, str):
            logger.warning(f"Prompt {file}.{key} is not a string: {type(prompt)}")
            return str(prompt)

        if kwargs:
            try:
                # Use format_map for partial substitution (missing keys stay as {key})
                prompt = prompt.format_map(SafeDict(kwargs))
            except Exception as e:
                logger.warning(f"Failed to substitute variables in {file}.{key}: {e}")

        return prompt

    def _get_localized(self, file_data: dict, key: str, lang: str) -> str | None:
        """Get a localized prompt from language-nested structure."""
        lang_data = file_data.get(lang)
        if isinstance(lang_data, dict):
            return lang_data.get(key)
        return None

    def get_config(self, file: str, key: str, default: Any = None, lang: str | None = None) -> Any:
        """
        Get a non-string config value (dict, list, etc.).

        Configs are typically language-independent and stored at root level.
        If lang is specified, will also check language-specific section.

        Args:
            file: The prompt file name (without .yaml extension)
            key: The key within the file
            default: Default value if key not found
            lang: Optional language code for language-specific configs

        Returns:
            The config value as-is (dict, list, etc.)
        """
        file_data = self._cache.get(file, {})

        # Try language-specific first if lang specified
        if lang:
            lang_data = file_data.get(lang)
            if isinstance(lang_data, dict) and key in lang_data:
                return lang_data[key]

        # Fall back to root-level config
        return file_data.get(key, default)

    def set_default_language(self, lang: str) -> None:
        """
        Set the default language for prompt lookups.

        Args:
            lang: Language code (de, en)
        """
        if lang in SUPPORTED_LANGUAGES:
            self._default_lang = lang
            logger.info(f"PromptManager default language set to: {lang}")
        else:
            logger.warning(f"Unsupported language: {lang}, keeping {self._default_lang}")

    @property
    def default_language(self) -> str:
        """Get the current default language."""
        return self._default_lang

    @property
    def supported_languages(self) -> list[str]:
        """Get list of supported languages."""
        return SUPPORTED_LANGUAGES.copy()

    def get_all(self, file: str) -> dict[str, Any]:
        """
        Get all prompts/configs from a file.

        Args:
            file: The prompt file name (without .yaml extension)

        Returns:
            Dictionary of all prompts/configs in the file
        """
        return self._cache.get(file, {})

    def list_files(self) -> list:
        """List all loaded prompt files."""
        return list(self._cache.keys())

    def list_keys(self, file: str) -> list:
        """List all keys in a prompt file."""
        return list(self._cache.get(file, {}).keys())


class SafeDict(dict):
    """
    A dict subclass that returns the key wrapped in braces for missing keys.
    Used for partial string formatting.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


# Global instance
# Note: Default language will be set from settings when OllamaService initializes
prompt_manager = PromptManager()


def init_prompt_manager_language() -> None:
    """
    Initialize the prompt manager with language from settings.
    Called during application startup.
    """
    try:
        from utils.config import settings
        if settings.default_language in SUPPORTED_LANGUAGES:
            prompt_manager.set_default_language(settings.default_language)
    except Exception as e:
        logger.debug(f"Could not load language from settings: {e}")
