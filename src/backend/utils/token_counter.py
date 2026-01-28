"""
Token Counter — Estimates token counts for LLM prompts.

Provides token budget tracking to prevent context overflow.

Usage:
    from utils.token_counter import token_counter

    # Estimate tokens
    count = token_counter.count("Hello, world!")

    # Check if within budget
    if token_counter.fits_budget(prompt, max_tokens=4000):
        # Safe to send to LLM

    # Truncate to fit budget
    truncated = token_counter.truncate_to_budget(text, max_tokens=2000)
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from loguru import logger


@dataclass
class TokenBudget:
    """Tracks token usage against a budget."""
    max_tokens: int
    used_tokens: int = 0
    reserved_tokens: int = 0  # Reserved for response

    @property
    def available(self) -> int:
        """Available tokens for content."""
        return max(0, self.max_tokens - self.used_tokens - self.reserved_tokens)

    @property
    def utilization(self) -> float:
        """Percentage of budget used."""
        if self.max_tokens == 0:
            return 0.0
        return (self.used_tokens + self.reserved_tokens) / self.max_tokens

    def can_fit(self, tokens: int) -> bool:
        """Check if additional tokens fit in budget."""
        return tokens <= self.available

    def add(self, tokens: int) -> bool:
        """Add tokens to usage. Returns False if would exceed budget."""
        if not self.can_fit(tokens):
            return False
        self.used_tokens += tokens
        return True


class TokenCounter:
    """
    Estimates token counts for text.

    Uses a simple character/word-based heuristic that works well for
    most LLMs. More accurate than word count, less complex than
    full tokenization.

    Heuristic:
    - Average ~4 characters per token for English text
    - German text tends to have ~5 characters per token
    - Code tends to have ~3 characters per token

    For more accuracy with specific models, this could be extended
    to use tiktoken or model-specific tokenizers.
    """

    # Characters per token estimates for different content types
    CHARS_PER_TOKEN_DEFAULT = 4.0
    CHARS_PER_TOKEN_GERMAN = 4.5
    CHARS_PER_TOKEN_CODE = 3.0
    CHARS_PER_TOKEN_JSON = 3.5

    def __init__(self, chars_per_token: float = CHARS_PER_TOKEN_DEFAULT):
        """
        Initialize the token counter.

        Args:
            chars_per_token: Average characters per token for estimation
        """
        self.chars_per_token = chars_per_token

    def count(self, text: str) -> int:
        """
        Estimate token count for text.

        Args:
            text: The text to count tokens for

        Returns:
            Estimated token count
        """
        if not text:
            return 0

        # Detect content type and adjust ratio
        chars_per_token = self._detect_content_type(text)

        # Count characters (excluding some whitespace)
        char_count = len(text)

        # Estimate tokens
        tokens = int(char_count / chars_per_token)

        # Add overhead for special tokens (BOS, EOS, etc.)
        tokens += 3

        return max(1, tokens)

    def count_messages(self, messages: List[dict]) -> int:
        """
        Estimate token count for a list of chat messages.

        Args:
            messages: List of message dicts with 'role' and 'content'

        Returns:
            Total estimated token count
        """
        total = 0
        for msg in messages:
            # Each message has overhead (~4 tokens for role + formatting)
            total += 4
            total += self.count(msg.get("content", ""))
        # Add overhead for message structure
        total += 3
        return total

    def fits_budget(self, text: str, max_tokens: int, reserved: int = 0) -> bool:
        """
        Check if text fits within token budget.

        Args:
            text: Text to check
            max_tokens: Maximum allowed tokens
            reserved: Tokens reserved for response

        Returns:
            True if text fits in budget
        """
        return self.count(text) <= (max_tokens - reserved)

    def truncate_to_budget(
        self,
        text: str,
        max_tokens: int,
        reserved: int = 0,
        suffix: str = "...[truncated]"
    ) -> Tuple[str, bool]:
        """
        Truncate text to fit within token budget.

        Args:
            text: Text to truncate
            max_tokens: Maximum allowed tokens
            reserved: Tokens reserved for response
            suffix: Suffix to add when truncated

        Returns:
            Tuple of (truncated_text, was_truncated)
        """
        available = max_tokens - reserved - self.count(suffix)

        if self.count(text) <= available:
            return text, False

        # Binary search for optimal truncation point
        target_chars = int(available * self.chars_per_token)

        # Truncate at word boundary
        if target_chars >= len(text):
            return text, False

        truncated = text[:target_chars]
        last_space = truncated.rfind(" ")
        if last_space > target_chars * 0.8:  # Don't cut too much
            truncated = truncated[:last_space]

        return truncated + suffix, True

    def truncate_messages_to_budget(
        self,
        messages: List[dict],
        max_tokens: int,
        reserved: int = 500,
        keep_system: bool = True,
        keep_last_n: int = 2
    ) -> List[dict]:
        """
        Truncate message history to fit within budget.

        Preserves system message and most recent messages.

        Args:
            messages: List of messages
            max_tokens: Maximum token budget
            reserved: Tokens reserved for response
            keep_system: Always keep system message
            keep_last_n: Always keep last N messages

        Returns:
            Truncated message list
        """
        if not messages:
            return []

        available = max_tokens - reserved
        result = []

        # Identify messages to keep
        system_msg = None
        if keep_system and messages and messages[0].get("role") == "system":
            system_msg = messages[0]
            messages = messages[1:]

        # Always keep last N messages
        keep_msgs = messages[-keep_last_n:] if len(messages) >= keep_last_n else messages
        older_msgs = messages[:-keep_last_n] if len(messages) > keep_last_n else []

        # Calculate fixed token usage
        fixed_tokens = 0
        if system_msg:
            fixed_tokens += self.count(system_msg.get("content", "")) + 4
        for msg in keep_msgs:
            fixed_tokens += self.count(msg.get("content", "")) + 4

        # Add older messages that fit
        remaining = available - fixed_tokens
        kept_older = []

        for msg in reversed(older_msgs):
            msg_tokens = self.count(msg.get("content", "")) + 4
            if msg_tokens <= remaining:
                kept_older.insert(0, msg)
                remaining -= msg_tokens
            else:
                break

        # Build final message list
        if system_msg:
            result.append(system_msg)
        result.extend(kept_older)
        result.extend(keep_msgs)

        return result

    def create_budget(
        self,
        max_tokens: int,
        reserved_for_response: int = 500
    ) -> TokenBudget:
        """
        Create a token budget tracker.

        Args:
            max_tokens: Maximum total tokens
            reserved_for_response: Tokens to reserve for LLM response

        Returns:
            TokenBudget instance
        """
        return TokenBudget(
            max_tokens=max_tokens,
            reserved_tokens=reserved_for_response
        )

    def _detect_content_type(self, text: str) -> float:
        """
        Detect content type and return appropriate chars-per-token ratio.
        """
        # Check for JSON
        if text.strip().startswith("{") or text.strip().startswith("["):
            return self.CHARS_PER_TOKEN_JSON

        # Check for code (simple heuristics)
        code_indicators = ["def ", "class ", "function ", "import ", "const ", "let ", "var "]
        if any(indicator in text for indicator in code_indicators):
            return self.CHARS_PER_TOKEN_CODE

        # Check for German text (umlauts and common words)
        german_chars = "äöüÄÖÜß"
        german_words = ["der", "die", "das", "und", "ist", "ein", "eine", "nicht"]
        text_lower = text.lower()

        has_umlauts = any(c in text for c in german_chars)
        has_german_words = sum(1 for w in german_words if f" {w} " in f" {text_lower} ") >= 2

        if has_umlauts or has_german_words:
            return self.CHARS_PER_TOKEN_GERMAN

        return self.CHARS_PER_TOKEN_DEFAULT


# Global instance
token_counter = TokenCounter()


# Convenience functions
def count_tokens(text: str) -> int:
    """Estimate tokens in text."""
    return token_counter.count(text)


def count_message_tokens(messages: List[dict]) -> int:
    """Estimate tokens in message list."""
    return token_counter.count_messages(messages)


def fits_context(text: str, max_tokens: int, reserved: int = 500) -> bool:
    """Check if text fits in context window."""
    return token_counter.fits_budget(text, max_tokens, reserved)
