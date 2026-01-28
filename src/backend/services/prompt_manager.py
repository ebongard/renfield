"""
Prompt Manager — Loads and manages externalized prompts from YAML files.

Enables prompt customization without code deployment.

Usage:
    from services.prompt_manager import prompt_manager

    # Get a prompt
    system_prompt = prompt_manager.get("chat", "system_prompt")

    # Get a prompt with variable substitution
    prompt = prompt_manager.get("intent", "extraction_prompt", message="Hello")

    # Get nested config
    options = prompt_manager.get_config("agent", "llm_options")
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from loguru import logger


class PromptManager:
    """
    Manages externalized prompts from YAML files.

    Prompts are loaded from the 'prompts' directory relative to the backend root.
    Supports variable substitution using str.format().
    """

    def __init__(self, prompts_dir: Optional[str] = None):
        """
        Initialize the PromptManager.

        Args:
            prompts_dir: Optional path to prompts directory.
                        Defaults to 'prompts' relative to this file's parent.
        """
        if prompts_dir:
            self._prompts_dir = Path(prompts_dir)
        else:
            # Default: prompts/ relative to src/backend/
            self._prompts_dir = Path(__file__).parent.parent / "prompts"

        self._cache: Dict[str, Dict[str, Any]] = {}
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
            with open(path, "r", encoding="utf-8") as f:
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

    def get(self, file: str, key: str, default: str = "", **kwargs) -> str:
        """
        Get a prompt string with optional variable substitution.

        Args:
            file: The prompt file name (without .yaml extension)
            key: The key within the file
            default: Default value if key not found
            **kwargs: Variables to substitute in the prompt

        Returns:
            The prompt string with variables substituted

        Example:
            prompt_manager.get("chat", "system_prompt")
            prompt_manager.get("intent", "room_context_template", room_name="Kitchen")
        """
        file_data = self._cache.get(file, {})
        prompt = file_data.get(key, default)

        if not prompt:
            logger.warning(f"Prompt not found: {file}.{key}")
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

    def get_config(self, file: str, key: str, default: Any = None) -> Any:
        """
        Get a non-string config value (dict, list, etc.).

        Args:
            file: The prompt file name (without .yaml extension)
            key: The key within the file
            default: Default value if key not found

        Returns:
            The config value as-is (dict, list, etc.)
        """
        file_data = self._cache.get(file, {})
        return file_data.get(key, default)

    def get_all(self, file: str) -> Dict[str, Any]:
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
prompt_manager = PromptManager()
