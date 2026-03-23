"""
Output Guard -- Detects system prompt leakage and role confusion in assistant responses.

Provides:
- check_output(response, fragments) -> OutputGuardResult: Analyzes assistant output
- extract_prompt_fragments(system_prompt) -> list[str]: Extracts significant fragments
  from the system prompt for leakage detection.
"""

import re
from dataclasses import dataclass, field

from loguru import logger

# Minimum number of leaked fragments to trigger leakage detection
LEAKAGE_FRAGMENT_THRESHOLD = 3

# Minimum fragment length to be considered significant
MIN_FRAGMENT_LENGTH = 20

# Maximum number of fragments to extract from system prompt
MAX_FRAGMENTS = 20

# ---------------------------------------------------------------------------
# Role confusion patterns (bilingual DE/EN)
# ---------------------------------------------------------------------------

_ROLE_CONFUSION_PATTERNS: list[re.Pattern] = [
    # EN patterns
    re.compile(r"as\s+(?:you\s+)?instructed\s+me\s+to\s+(?:ignore|follow|do)", re.I),
    re.compile(r"as\s+per\s+my\s+(?:instructions|rules|guidelines|programming)", re.I),
    re.compile(r"(?:i\s+was|i\'m|i\s+am)\s+(?:told|instructed|programmed)\s+to", re.I),
    re.compile(r"my\s+(?:system\s+)?(?:prompt|instructions)\s+(?:say|tell|state)", re.I),
    re.compile(r"according\s+to\s+my\s+(?:rules|instructions|guidelines)", re.I),
    re.compile(r"i\s+(?:cannot|can\'t)\s+(?:do\s+that|comply)\s+because\s+my\s+(?:rules|instructions)", re.I),
    # DE patterns
    re.compile(r"(?:meine|laut\s+meinen?)\s+(?:anweisungen|regeln|instruktionen)\s+(?:sagen|besagen|verbieten)", re.I),
    re.compile(r"mir\s+wurde\s+(?:gesagt|beigebracht|angewiesen)", re.I),
    re.compile(r"ich\s+(?:wurde|bin)\s+(?:so\s+)?(?:programmiert|angewiesen|instruiert)", re.I),
    re.compile(r"(?:mein(?:em|en|er|e)?|im)\s+system\s*prompt", re.I),
]


@dataclass
class OutputGuardResult:
    """Result of output guard analysis."""

    safe: bool = True
    violations: list[str] = field(default_factory=list)
    details: dict[str, int] = field(default_factory=dict)


def extract_prompt_fragments(
    system_prompt: str, min_length: int = MIN_FRAGMENT_LENGTH
) -> list[str]:
    """Extract significant fragments from a system prompt for leakage detection.

    Returns up to MAX_FRAGMENTS unique, non-template lines that are long enough
    to be meaningful indicators of leakage.
    """
    if not system_prompt:
        return []

    fragments: list[str] = []
    seen: set[str] = set()

    for line in system_prompt.splitlines():
        line = line.strip()

        # Skip short lines, empty lines, template variables, headers
        if len(line) < min_length:
            continue
        if "{" in line and "}" in line:
            continue
        if line.startswith("#") or line.startswith("---"):
            continue

        normalized = line.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        fragments.append(line)

        if len(fragments) >= MAX_FRAGMENTS:
            break

    return fragments


def check_output(
    response: str,
    system_prompt_fragments: list[str] | None = None,
) -> OutputGuardResult:
    """Analyze an assistant response for security violations.

    Checks for:
    1. System prompt leakage (>= LEAKAGE_FRAGMENT_THRESHOLD fragments found)
    2. Role confusion patterns (assistant reveals its instructions)

    Args:
        response: The assistant's response text.
        system_prompt_fragments: Pre-extracted fragments from extract_prompt_fragments().
            If None, only role confusion detection runs.

    Returns:
        OutputGuardResult with safe=True if no violations found.
    """
    if not response or len(response) < 10:
        return OutputGuardResult()

    violations: list[str] = []
    details: dict[str, int] = {}
    response_lower = response.lower()

    # 1. System prompt leakage detection
    if system_prompt_fragments:
        leaked_count = 0
        for fragment in system_prompt_fragments:
            if fragment.lower() in response_lower:
                leaked_count += 1

        details["leaked_fragments"] = leaked_count
        if leaked_count >= LEAKAGE_FRAGMENT_THRESHOLD:
            violations.append("system_prompt_leakage")
            logger.warning(
                f"System prompt leakage detected: {leaked_count} fragments "
                f"found in response ({len(response)} chars)"
            )

    # 2. Role confusion detection
    confusion_count = 0
    for pattern in _ROLE_CONFUSION_PATTERNS:
        if pattern.search(response):
            confusion_count += 1

    details["role_confusion_matches"] = confusion_count
    if confusion_count > 0:
        violations.append("role_confusion")
        logger.warning(
            f"Role confusion detected: {confusion_count} pattern(s) "
            f"matched in response ({len(response)} chars)"
        )

    return OutputGuardResult(
        safe=len(violations) == 0,
        violations=violations,
        details=details,
    )
