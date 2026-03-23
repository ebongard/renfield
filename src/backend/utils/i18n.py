"""
Backend i18n -- Lightweight translation system for non-prompt system strings.

Complements the prompt_manager (which handles LLM prompts) with translations
for error messages, log strings, UI labels, and API responses.

Usage:
    from utils.i18n import t

    msg = t("error.request_blocked", lang="de")
    msg = t("error.tool_timeout", lang="en", tool="get_states")
"""

from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_translations: dict[str, dict[str, str]] = {}


class _SafeDict(dict):
    """Dict that returns '{key}' for missing keys instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def load_translations(config_dir: str | None = None) -> None:
    """Load YAML translation files from config directory.

    Each file is named by language code (e.g. de.yaml, en.yaml).
    Nested keys are flattened with dots: {error: {timeout: "..."}} -> "error.timeout"
    """
    global _translations
    _translations.clear()

    if config_dir is None:
        # Default: relative to backend root (src/backend/)
        config_dir = str(Path(__file__).resolve().parent.parent / "config" / "i18n")
    path = Path(config_dir)
    if not path.exists():
        logger.debug(f"i18n config directory not found: {path}")
        return

    count = 0
    for yaml_file in sorted(path.glob("*.yaml")):
        lang = yaml_file.stem
        try:
            with open(yaml_file) as f:
                raw = yaml.safe_load(f) or {}
            flat: dict[str, str] = {}
            _flatten(raw, "", flat)
            _translations[lang] = flat
            count += len(flat)
        except Exception:
            logger.opt(exception=True).warning(f"Failed to load i18n file: {yaml_file}")

    logger.info(f"i18n loaded: {count} keys across {len(_translations)} language(s)")


def t(key: str, lang: str = "de", **kwargs: Any) -> str:
    """Translate a key with variable substitution.

    Fallback chain: lang -> en -> key itself.
    """
    template = _translations.get(lang, {}).get(key)
    if not template:
        template = _translations.get("en", {}).get(key)
    if not template:
        return key

    if kwargs:
        return template.format_map(_SafeDict(kwargs))
    return template


def _flatten(data: dict, prefix: str, result: dict[str, str]) -> None:
    """Flatten nested dict to dot-separated keys."""
    for key, value in data.items():
        full_key = f"{prefix}{key}" if prefix else key
        if isinstance(value, dict):
            _flatten(value, f"{full_key}.", result)
        elif isinstance(value, str):
            result[full_key] = value
