"""
Complexity Detector — Regex-based detection of multi-step queries.

Determines whether a user message requires the Agent Loop (complex, multi-step)
or the standard single-intent path (simple, one action).

Zero-cost: No LLM call, pure regex matching on German and English patterns.
Semantic feedback override: Checks past corrections before regex fallback.
"""
import re
from typing import List, Optional, Tuple

from loguru import logger


class ComplexityDetector:
    """Detects whether a user query requires the multi-step Agent Loop."""

    # Conditional patterns: "wenn ... dann", "falls ... dann", etc.
    CONDITIONAL_PATTERNS: List[re.Pattern] = [
        re.compile(r'\bwenn\b.*\bdann\b', re.IGNORECASE),
        re.compile(r'\bfalls\b.*\bdann\b', re.IGNORECASE),
        re.compile(r'\bsofern\b.*\bdann\b', re.IGNORECASE),
        re.compile(r'\bif\b.*\bthen\b', re.IGNORECASE),
    ]

    # Sequential patterns: "und dann", "danach", "anschließend"
    SEQUENCE_PATTERNS: List[re.Pattern] = [
        re.compile(r'\bund dann\b', re.IGNORECASE),
        re.compile(r'\bdanach\b', re.IGNORECASE),
        re.compile(r'\banschließend\b', re.IGNORECASE),
        re.compile(r'\bals nächstes\b', re.IGNORECASE),
        re.compile(r'\band then\b', re.IGNORECASE),
        re.compile(r'\bafterwards?\b', re.IGNORECASE),
    ]

    # Comparison patterns: "wärmer als", "höher als", "mehr als"
    COMPARISON_PATTERNS: List[re.Pattern] = [
        re.compile(r'\b(wärmer|kälter|höher|niedriger|mehr|weniger|größer|kleiner|teurer|billiger)\s+als\b', re.IGNORECASE),
        re.compile(r'\b(warmer|colder|higher|lower|more|less|greater|cheaper)\s+than\b', re.IGNORECASE),
        re.compile(r'\büber\s+\d+', re.IGNORECASE),
        re.compile(r'\bunter\s+\d+', re.IGNORECASE),
        re.compile(r'\b(above|below)\s+\d+', re.IGNORECASE),
    ]

    # Multi-action patterns: "schalte X ein und mach Y"
    MULTI_ACTION_PATTERNS: List[re.Pattern] = [
        # German: action verb + "und" + action verb
        re.compile(
            r'\b(schalte|mach|stelle|öffne|schließe|starte|stoppe|suche|finde|hole|zeige|sende|schicke)\b'
            r'.*\bund\b.*'
            r'\b(schalte|mach|stelle|öffne|schließe|starte|stoppe|suche|finde|hole|zeige|sende|schicke)\b',
            re.IGNORECASE
        ),
        # English: action verb + "and" + action verb
        re.compile(
            r'\b(turn|switch|set|open|close|start|stop|search|find|get|show|send)\b'
            r'.*\band\b.*'
            r'\b(turn|switch|set|open|close|start|stop|search|find|get|show|send)\b',
            re.IGNORECASE
        ),
    ]

    # Combined question patterns: "wie ist X und Y"
    COMBINED_QUESTION_PATTERNS: List[re.Pattern] = [
        re.compile(
            r'\b(wie|was|wer|wo|wann)\b.*\bund\b.*\b(wie|was|wer|wo|wann)\b',
            re.IGNORECASE
        ),
        re.compile(
            r'\b(how|what|who|where|when)\b.*\band\b.*\b(how|what|who|where|when)\b',
            re.IGNORECASE
        ),
    ]

    # All pattern groups with labels for debugging
    ALL_PATTERN_GROUPS: List[Tuple[str, List[re.Pattern]]] = [
        ("conditional", CONDITIONAL_PATTERNS),
        ("sequence", SEQUENCE_PATTERNS),
        ("comparison", COMPARISON_PATTERNS),
        ("multi_action", MULTI_ACTION_PATTERNS),
        ("combined_question", COMBINED_QUESTION_PATTERNS),
    ]

    @classmethod
    def needs_agent(cls, message: str) -> bool:
        """
        Determine if a message requires the Agent Loop.

        Args:
            message: The user's message text

        Returns:
            True if the message is complex and needs multi-step processing
        """
        if not message or len(message) < 10:
            return False

        for _group_name, patterns in cls.ALL_PATTERN_GROUPS:
            for pattern in patterns:
                if pattern.search(message):
                    return True

        return False

    @classmethod
    async def needs_agent_with_feedback(cls, message: str) -> bool:
        """
        Check corrections first, then fall back to regex.

        If a semantically similar message was previously corrected
        (simple→complex or complex→simple), use the correction.
        Otherwise, fall back to the standard regex-based detection.
        """
        try:
            from services.database import AsyncSessionLocal
            from services.intent_feedback_service import IntentFeedbackService
            async with AsyncSessionLocal() as db:
                service = IntentFeedbackService(db)
                override = await service.check_complexity_override(message)
                if override is not None:
                    logger.info(
                        f"📝 Complexity override from feedback: "
                        f"{'complex' if override else 'simple'} for '{message[:60]}...'"
                    )
                    return override
        except Exception as e:
            logger.warning(f"⚠️ Complexity feedback check failed: {e}")

        return cls.needs_agent(message)

    @classmethod
    def detect_patterns(cls, message: str) -> List[str]:
        """
        Detect which complexity patterns match (for debugging/logging).

        Args:
            message: The user's message text

        Returns:
            List of matched pattern group names
        """
        if not message:
            return []

        matched = []
        for group_name, patterns in cls.ALL_PATTERN_GROUPS:
            for pattern in patterns:
                if pattern.search(message):
                    matched.append(group_name)
                    break  # One match per group is enough

        return matched
