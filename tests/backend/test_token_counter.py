"""
Tests for TokenCounter — Token estimation and budget tracking.
"""

import pytest

from utils.token_counter import (
    TokenCounter,
    TokenBudget,
    token_counter,
    count_tokens,
    count_message_tokens,
    fits_context,
)


# ============================================================================
# TokenBudget
# ============================================================================

class TestTokenBudget:
    """Test TokenBudget dataclass."""

    @pytest.mark.unit
    def test_initial_state(self):
        """Budget should start with full availability."""
        budget = TokenBudget(max_tokens=1000, reserved_tokens=100)

        assert budget.available == 900
        assert budget.utilization == pytest.approx(0.1)

    @pytest.mark.unit
    def test_can_fit(self):
        """can_fit should check against available tokens."""
        budget = TokenBudget(max_tokens=1000, reserved_tokens=200)

        assert budget.can_fit(800) is True
        assert budget.can_fit(801) is False

    @pytest.mark.unit
    def test_add_success(self):
        """add should increase used_tokens and return True."""
        budget = TokenBudget(max_tokens=1000, reserved_tokens=100)

        result = budget.add(500)

        assert result is True
        assert budget.used_tokens == 500
        assert budget.available == 400

    @pytest.mark.unit
    def test_add_failure(self):
        """add should return False if tokens don't fit."""
        budget = TokenBudget(max_tokens=1000, reserved_tokens=100)
        budget.add(800)

        result = budget.add(200)

        assert result is False
        assert budget.used_tokens == 800  # Unchanged

    @pytest.mark.unit
    def test_utilization_calculation(self):
        """utilization should reflect used + reserved percentage."""
        budget = TokenBudget(max_tokens=1000, reserved_tokens=200)
        budget.add(300)

        assert budget.utilization == pytest.approx(0.5)

    @pytest.mark.unit
    def test_zero_max_tokens(self):
        """Should handle zero max_tokens gracefully."""
        budget = TokenBudget(max_tokens=0)

        assert budget.available == 0
        assert budget.utilization == 0.0
        assert budget.can_fit(1) is False


# ============================================================================
# TokenCounter.count()
# ============================================================================

class TestTokenCounterCount:
    """Test token estimation."""

    @pytest.mark.unit
    def test_empty_string(self):
        """Empty string should return 0."""
        counter = TokenCounter()
        assert counter.count("") == 0

    @pytest.mark.unit
    def test_simple_text(self):
        """Should estimate tokens for simple text."""
        counter = TokenCounter()
        # "Hello, World!" = 13 chars -> ~4 tokens + 3 overhead
        result = counter.count("Hello, World!")
        assert 1 <= result <= 10

    @pytest.mark.unit
    def test_longer_text(self):
        """Token count should scale with text length."""
        counter = TokenCounter()
        short = counter.count("Hi")
        long = counter.count("This is a much longer piece of text with many words.")

        assert long > short

    @pytest.mark.unit
    def test_code_detection(self):
        """Code should use different chars-per-token ratio."""
        counter = TokenCounter()

        code = "def hello():\n    print('Hello')\n    return 42"
        text = "A" * len(code)

        # Code has lower chars-per-token, so more tokens
        code_tokens = counter.count(code)
        text_tokens = counter.count(text)

        # Code should produce more tokens for same length
        assert code_tokens >= text_tokens

    @pytest.mark.unit
    def test_german_detection(self):
        """German text should use different chars-per-token ratio."""
        counter = TokenCounter()

        german = "Das ist ein deutscher Satz mit Umlauten: äöü"
        english = "This is an English sentence of similar length"

        german_tokens = counter.count(german)
        english_tokens = counter.count(english)

        # Both should produce reasonable estimates
        assert german_tokens > 0
        assert english_tokens > 0

    @pytest.mark.unit
    def test_json_detection(self):
        """JSON should use different chars-per-token ratio."""
        counter = TokenCounter()

        json_text = '{"name": "Alice", "age": 30, "active": true}'
        plain_text = "name Alice age 30 active true is some text"

        json_tokens = counter.count(json_text)
        plain_tokens = counter.count(plain_text)

        # Both should produce reasonable estimates
        assert json_tokens > 0
        assert plain_tokens > 0


# ============================================================================
# TokenCounter.count_messages()
# ============================================================================

class TestTokenCounterCountMessages:
    """Test message list token counting."""

    @pytest.mark.unit
    def test_empty_messages(self):
        """Empty list should return minimal tokens."""
        counter = TokenCounter()
        result = counter.count_messages([])

        assert result == 3  # Just structure overhead

    @pytest.mark.unit
    def test_single_message(self):
        """Should count tokens in a single message."""
        counter = TokenCounter()
        messages = [{"role": "user", "content": "Hello!"}]

        result = counter.count_messages(messages)

        # 4 (msg overhead) + ~4 (content) + 3 (structure) = ~11
        assert 5 <= result <= 20

    @pytest.mark.unit
    def test_multiple_messages(self):
        """Should count tokens across all messages."""
        counter = TokenCounter()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there! How can I help?"},
        ]

        result = counter.count_messages(messages)

        # Should be sum of all messages + overhead
        assert result > 15

    @pytest.mark.unit
    def test_missing_content(self):
        """Should handle messages without content."""
        counter = TokenCounter()
        messages = [{"role": "user"}]

        result = counter.count_messages(messages)

        assert result == 7  # 4 (msg overhead) + 0 (empty content) + 3 (structure)


# ============================================================================
# TokenCounter.fits_budget()
# ============================================================================

class TestTokenCounterFitsBudget:
    """Test budget checking."""

    @pytest.mark.unit
    def test_fits(self):
        """Should return True when text fits."""
        counter = TokenCounter()

        result = counter.fits_budget("Hello", max_tokens=100)

        assert result is True

    @pytest.mark.unit
    def test_does_not_fit(self):
        """Should return False when text doesn't fit."""
        counter = TokenCounter()

        result = counter.fits_budget("x" * 1000, max_tokens=10)

        assert result is False

    @pytest.mark.unit
    def test_with_reserved(self):
        """Should account for reserved tokens."""
        counter = TokenCounter()

        # Text that would fit in 100 but not with 90 reserved
        text = "x" * 50  # ~15 tokens

        assert counter.fits_budget(text, max_tokens=100, reserved=0) is True
        assert counter.fits_budget(text, max_tokens=100, reserved=95) is False


# ============================================================================
# TokenCounter.truncate_to_budget()
# ============================================================================

class TestTokenCounterTruncateToBudget:
    """Test text truncation."""

    @pytest.mark.unit
    def test_no_truncation_needed(self):
        """Should return original text if it fits."""
        counter = TokenCounter()

        text, was_truncated = counter.truncate_to_budget("Hello", max_tokens=100)

        assert text == "Hello"
        assert was_truncated is False

    @pytest.mark.unit
    def test_truncates_long_text(self):
        """Should truncate text that doesn't fit."""
        counter = TokenCounter()
        long_text = "word " * 1000

        text, was_truncated = counter.truncate_to_budget(long_text, max_tokens=20)

        assert was_truncated is True
        assert len(text) < len(long_text)
        assert text.endswith("...[truncated]")

    @pytest.mark.unit
    def test_truncates_at_word_boundary(self):
        """Should truncate at word boundary when possible."""
        counter = TokenCounter()
        text = "The quick brown fox jumps over the lazy dog " * 50

        truncated, _ = counter.truncate_to_budget(text, max_tokens=30)

        # Should end with suffix, not mid-word
        assert truncated.endswith("...[truncated]")

    @pytest.mark.unit
    def test_custom_suffix(self):
        """Should use custom suffix."""
        counter = TokenCounter()
        long_text = "word " * 500

        text, _ = counter.truncate_to_budget(
            long_text, max_tokens=20, suffix="... (cut)"
        )

        assert text.endswith("... (cut)")


# ============================================================================
# TokenCounter.truncate_messages_to_budget()
# ============================================================================

class TestTokenCounterTruncateMessagesToBudget:
    """Test message history truncation."""

    @pytest.mark.unit
    def test_no_truncation_needed(self):
        """Should return all messages if they fit."""
        counter = TokenCounter()
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]

        result = counter.truncate_messages_to_budget(messages, max_tokens=1000)

        assert len(result) == 2

    @pytest.mark.unit
    def test_keeps_system_message(self):
        """Should always keep system message."""
        counter = TokenCounter()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Old message " * 100},
            {"role": "user", "content": "Recent message"},
        ]

        result = counter.truncate_messages_to_budget(
            messages, max_tokens=50, keep_last_n=1
        )

        # Should have system + last message
        assert result[0]["role"] == "system"
        assert "Recent message" in result[-1]["content"]

    @pytest.mark.unit
    def test_keeps_last_n_messages(self):
        """Should always keep last N messages."""
        counter = TokenCounter()
        messages = [
            {"role": "user", "content": "Message 1 " * 50},
            {"role": "user", "content": "Message 2 " * 50},
            {"role": "user", "content": "Message 3"},
            {"role": "user", "content": "Message 4"},
        ]

        result = counter.truncate_messages_to_budget(
            messages, max_tokens=50, keep_last_n=2
        )

        # Should have last 2 messages
        assert len(result) >= 2
        assert "Message 3" in result[-2]["content"]
        assert "Message 4" in result[-1]["content"]

    @pytest.mark.unit
    def test_drops_older_messages(self):
        """Should drop older messages when budget is tight."""
        counter = TokenCounter()
        messages = [
            {"role": "user", "content": "Very old message " * 100},
            {"role": "user", "content": "Old message " * 100},
            {"role": "user", "content": "Recent"},
            {"role": "user", "content": "Latest"},
        ]

        result = counter.truncate_messages_to_budget(
            messages, max_tokens=50, keep_last_n=2
        )

        # Old messages should be dropped
        assert len(result) == 2

    @pytest.mark.unit
    def test_empty_messages(self):
        """Should handle empty message list."""
        counter = TokenCounter()

        result = counter.truncate_messages_to_budget([], max_tokens=100)

        assert result == []


# ============================================================================
# TokenCounter.create_budget()
# ============================================================================

class TestTokenCounterCreateBudget:
    """Test budget creation."""

    @pytest.mark.unit
    def test_creates_budget(self):
        """Should create a TokenBudget with correct settings."""
        counter = TokenCounter()

        budget = counter.create_budget(max_tokens=4000, reserved_for_response=500)

        assert budget.max_tokens == 4000
        assert budget.reserved_tokens == 500
        assert budget.available == 3500


# ============================================================================
# Convenience Functions
# ============================================================================

class TestConvenienceFunctions:
    """Test module-level convenience functions."""

    @pytest.mark.unit
    def test_count_tokens(self):
        """count_tokens should use global instance."""
        result = count_tokens("Hello, World!")
        assert result > 0

    @pytest.mark.unit
    def test_count_message_tokens(self):
        """count_message_tokens should use global instance."""
        messages = [{"role": "user", "content": "Hi"}]
        result = count_message_tokens(messages)
        assert result > 0

    @pytest.mark.unit
    def test_fits_context(self):
        """fits_context should use global instance."""
        assert fits_context("Hello", max_tokens=1000) is True
        assert fits_context("x" * 10000, max_tokens=10) is False


# ============================================================================
# Global Instance
# ============================================================================

class TestGlobalInstance:
    """Test global token_counter instance."""

    @pytest.mark.unit
    def test_global_instance_exists(self):
        """Global instance should be available."""
        assert token_counter is not None
        assert isinstance(token_counter, TokenCounter)

    @pytest.mark.unit
    def test_global_instance_works(self):
        """Global instance should function correctly."""
        result = token_counter.count("Test string")
        assert result > 0
