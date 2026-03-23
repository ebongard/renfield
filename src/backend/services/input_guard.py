"""
Input Guard -- Prompt injection detection and input sanitization.

Provides two main functions:
- sanitize_user_input(text): Escapes format strings, strips delimiter tags,
  neutralizes role markers. Replaces the basic _sanitize_user_input() in
  ollama_service.py.
- detect_injection(text) -> InjectionResult: Weighted pattern scoring across
  5 categories. Returns score and matched patterns.
"""

import re
from dataclasses import dataclass, field

from loguru import logger

BLOCK_THRESHOLD = 0.8

# ---------------------------------------------------------------------------
# Injection detection patterns (bilingual DE/EN)
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: dict[str, list[tuple[re.Pattern, float]]] = {
    "instruction_override": [
        (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I), 0.8),
        (re.compile(r"disregard\s+(all\s+)?(your\s+)?instructions", re.I), 0.8),
        (re.compile(r"vergiss\s+(alle\s+)?(deine\s+)?regeln", re.I), 0.8),
        (re.compile(r"ignorier[e]?\s+(alle\s+)?(?:vorherigen?\s+)?(?:anweisungen|regeln|instruktionen)", re.I), 0.8),
        (re.compile(r"new\s+instructions?\s*:", re.I), 0.8),
        (re.compile(r"neue\s+anweisungen?\s*:", re.I), 0.8),
        (re.compile(r"ab\s+jetzt\s+(?:gelt|folg)", re.I), 0.8),
        (re.compile(r"from\s+now\s+on\s+(?:ignore|forget|disregard)", re.I), 0.8),
        (re.compile(r"forget\s+everything\s+you\s+(?:were|have\s+been)\s+told", re.I), 0.8),
        (re.compile(r"override\s+(?:all\s+)?(?:your\s+)?(?:previous\s+)?rules", re.I), 0.8),
    ],
    "system_prompt_extraction": [
        (re.compile(r"repeat\s+your\s+system\s+prompt", re.I), 0.7),
        (re.compile(r"(?:show|print|display|output)\s+(?:me\s+)?(?:your\s+)?(?:system\s+)?(?:instructions|prompt|rules)", re.I), 0.7),
        (re.compile(r"zeig[e]?\s+(?:mir\s+)?deine\s+(?:system\s*)?(?:anweisungen|regeln|prompt)", re.I), 0.7),
        (re.compile(r"what\s+(?:are|is)\s+your\s+(?:system\s+)?(?:instructions|prompt|rules)", re.I), 0.7),
        (re.compile(r"gib\s+(?:mir\s+)?dein(?:en)?\s+system\s*prompt", re.I), 0.7),
    ],
    "role_impersonation": [
        (re.compile(r"you\s+are\s+now\s+(?:a\s+)?(?!going|about)", re.I), 0.6),
        (re.compile(r"du\s+bist\s+jetzt\s+(?:ein\s+)?", re.I), 0.6),
        (re.compile(r"act\s+as\s+(?:a\s+)?(?:different|new|unrestricted)", re.I), 0.6),
        (re.compile(r"(?:pretend|imagine)\s+(?:you\s+are|to\s+be)\s+(?:a\s+)?(?:different|new)", re.I), 0.6),
    ],
    "gdpr_bypass": [
        (re.compile(r"ignore\s+data\s+protection", re.I), 0.9),
        (re.compile(r"dsgvo\s+gilt\s+nicht", re.I), 0.9),
        (re.compile(r"bypass\s+(?:privacy|auth|security)", re.I), 0.9),
        (re.compile(r"datenschutz\s+(?:ignorieren|umgehen|abschalten)", re.I), 0.9),
        (re.compile(r"skip\s+(?:all\s+)?(?:safety|security)\s+checks?", re.I), 0.9),
    ],
    "delimiter_injection": [
        (re.compile(r"</(?:system|memory_context|tool_result|assistant|context|user)>", re.I), 0.5),
        (re.compile(r"<(?:system|assistant|user)>", re.I), 0.5),
    ],
}

# Tags that should be stripped from user input
_DELIMITER_TAGS = re.compile(
    r"</?(system|memory_context|tool_result|assistant|user|context)>",
    re.I,
)

# Role markers at the start of a line
_ROLE_MARKERS = re.compile(
    r"^(System|Assistant|User|Tool)\s*:\s*",
    re.I | re.MULTILINE,
)


@dataclass
class InjectionResult:
    """Result of injection detection analysis."""

    score: float = 0.0
    blocked: bool = False
    matched_patterns: list[str] = field(default_factory=list)
    category_scores: dict[str, float] = field(default_factory=dict)


def sanitize_user_input(text: str, max_length: int = 4000) -> str:
    """Sanitize user input before embedding in LLM prompts.

    Steps:
    1. Truncate to max_length
    2. Escape format strings: { -> {{, } -> }}
    3. Strip delimiter tags (</memory_context>, </system>, etc.)
    4. Neutralize role markers at line start (System:, Assistant:, etc.)
    5. Remove backtick sequences
    """
    if not text:
        return ""

    # 1. Truncate
    if len(text) > max_length:
        text = text[:max_length] + "..."

    # 2. Escape format strings
    text = text.replace("{", "{{").replace("}", "}}")

    # 3. Strip delimiter tags
    text = _DELIMITER_TAGS.sub("", text)

    # 4. Neutralize role markers
    text = _ROLE_MARKERS.sub(r'[User said "\1:"] ', text)

    # 5. Remove backtick sequences
    text = text.replace("```", "")

    return text.strip()


def detect_injection(text: str) -> InjectionResult:
    """Analyze text for prompt injection patterns.

    Returns InjectionResult with score (max of matched pattern weights).
    Blocks at score >= BLOCK_THRESHOLD.
    """
    if not text:
        return InjectionResult()

    category_scores: dict[str, float] = {}
    matched: list[str] = []

    for category, patterns in _INJECTION_PATTERNS.items():
        cat_score = 0.0
        for pattern, weight in patterns:
            if pattern.search(text):
                cat_score = max(cat_score, weight)
                matched.append(f"{category}:{pattern.pattern[:50]}")
        category_scores[category] = cat_score

    score = max(category_scores.values()) if category_scores else 0.0
    blocked = score >= BLOCK_THRESHOLD

    if blocked:
        logger.warning(
            f"Injection detected (score={score:.2f}): {matched}"
        )
        from utils.metrics import record_injection_attempt
        for pattern_id in matched:
            category = pattern_id.split(":")[0]
            record_injection_attempt(category)

    return InjectionResult(
        score=score,
        blocked=blocked,
        matched_patterns=matched,
        category_scores=category_scores,
    )
