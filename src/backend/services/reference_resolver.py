"""Reference resolver — entity ID recognition for context-aware routing.

Scans messages for structured entity IDs (INC-100042, REVA-123, RFC-2026-0087)
using configurable regex patterns per domain. Returns matched entities and the
inferred domain to short-circuit the semantic/LLM router.

Entity patterns are loaded from config/entity_patterns.yaml at startup and
extended by plugins via the load_entity_patterns hook.

Ported from Reva's src/reva/routing/resolver.py (entity ID layer only).
Indexed/anaphoric reference resolution is Phase 2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


@dataclass
class EntityMatch:
    """A recognized entity ID in the message."""
    id: str
    domain: str
    entity_type: str
    position: int  # character offset in message


@dataclass
class ResolvedMessage:
    """Result of reference resolution."""
    text: str
    original: str
    entity_matches: list[EntityMatch] = field(default_factory=list)
    inferred_domain: str | None = None
    context_hints: list[str] = field(default_factory=list)


# Compiled patterns cache: {domain: [(compiled_regex, entity_type), ...]}
_compiled: dict[str, list[tuple[re.Pattern, str]]] = {}


def load_entity_patterns(path: str | Path | None = None) -> dict:
    """Load entity patterns from a YAML file.

    Returns the raw dict for merging with plugin-provided patterns.
    """
    if path is None:
        path = Path("config/entity_patterns.yaml")
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("domains", {})
    except Exception as e:
        logger.warning(f"Failed to load entity patterns from {path}: {e}")
        return {}


def compile_patterns(domains: dict) -> None:
    """Compile regex patterns from the domain config dict.

    Called once at startup after merging base + plugin patterns.
    """
    _compiled.clear()
    for domain, cfg in domains.items():
        patterns = cfg.get("patterns", []) if isinstance(cfg, dict) else []
        compiled = []
        for p in patterns:
            regex_str = p.get("regex", "")
            entity_type = p.get("entity_type", "unknown")
            if not regex_str:
                continue
            try:
                compiled.append((re.compile(regex_str), entity_type))
            except re.error as e:
                logger.warning(f"Invalid entity pattern for {domain}: {regex_str} — {e}")
        if compiled:
            _compiled[domain] = compiled
    logger.info(f"Entity patterns compiled: {sum(len(v) for v in _compiled.values())} patterns across {len(_compiled)} domains")


def resolve_references(
    message: str,
    entity_patterns: dict | None = None,
) -> ResolvedMessage:
    """Recognize entity IDs in a message using compiled patterns.

    Args:
        message: The user's message text.
        entity_patterns: Not used directly (patterns are pre-compiled).
            Kept for API compatibility.

    Returns:
        ResolvedMessage with entity_matches and inferred_domain.
    """
    result = ResolvedMessage(text=message, original=message)

    if not _compiled or not message:
        return result

    for domain, patterns in _compiled.items():
        for regex, entity_type in patterns:
            for match in regex.finditer(message):
                result.entity_matches.append(EntityMatch(
                    id=match.group(0),
                    domain=domain,
                    entity_type=entity_type,
                    position=match.start(),
                ))

    if result.entity_matches:
        domains_found = {m.domain for m in result.entity_matches}
        if len(domains_found) == 1:
            result.inferred_domain = domains_found.pop()
        else:
            # Multiple domains found — signal cross-domain query
            result.context_hints.append(
                f"Multiple domains detected: {', '.join(sorted(domains_found))}"
            )

    return result
