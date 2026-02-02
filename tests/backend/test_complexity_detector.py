"""
Tests for ComplexityDetector — Regex-based detection of multi-step queries.
"""

import pytest
from services.complexity_detector import ComplexityDetector


class TestComplexityDetectorNeedsAgent:
    """Test needs_agent() — determines if message requires Agent Loop."""

    # === Simple queries (should NOT trigger agent) ===

    @pytest.mark.unit
    def test_simple_light_command(self):
        assert ComplexityDetector.needs_agent("Schalte das Licht ein") is False

    @pytest.mark.unit
    def test_simple_question(self):
        assert ComplexityDetector.needs_agent("Wie spät ist es?") is False

    @pytest.mark.unit
    def test_simple_greeting(self):
        assert ComplexityDetector.needs_agent("Hallo, wie geht es dir?") is False

    @pytest.mark.unit
    def test_simple_weather(self):
        assert ComplexityDetector.needs_agent("Wie ist das Wetter?") is False

    @pytest.mark.unit
    def test_empty_message(self):
        assert ComplexityDetector.needs_agent("") is False

    @pytest.mark.unit
    def test_none_message(self):
        assert ComplexityDetector.needs_agent(None) is False

    @pytest.mark.unit
    def test_short_message(self):
        """Messages shorter than 10 chars should never trigger agent."""
        assert ComplexityDetector.needs_agent("Hallo") is False

    @pytest.mark.unit
    def test_simple_english(self):
        assert ComplexityDetector.needs_agent("Turn on the lights") is False

    # === Conditional patterns ===

    @pytest.mark.unit
    def test_conditional_wenn_dann(self):
        msg = "Wenn es draußen regnet, dann schließe die Fenster"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_conditional_falls_dann(self):
        msg = "Falls die Temperatur über 25 Grad ist, dann schalte die Klimaanlage ein"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_conditional_sofern_dann(self):
        msg = "Sofern das Licht an ist, dann dimme es auf 50 Prozent"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_conditional_if_then(self):
        msg = "If the temperature is above 20 degrees then search for a hotel"
        assert ComplexityDetector.needs_agent(msg) is True

    # === Sequence patterns ===

    @pytest.mark.unit
    def test_sequence_und_dann(self):
        msg = "Hole das Wetter und dann suche ein Restaurant"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_sequence_danach(self):
        msg = "Schalte das Licht ein, danach stelle die Heizung auf 22 Grad"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_sequence_anschliessend(self):
        msg = "Suche das Wetter in Berlin, anschließend buche ein Hotel"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_sequence_als_naechstes(self):
        msg = "Zeige mir die Nachrichten, als nächstes suche nach Sportergebnissen"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_sequence_and_then(self):
        msg = "Get the weather and then find a restaurant"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_sequence_afterwards(self):
        msg = "Turn on the lights, afterwards set the thermostat"
        assert ComplexityDetector.needs_agent(msg) is True

    # === Comparison patterns ===

    @pytest.mark.unit
    def test_comparison_waermer_als(self):
        msg = "Wenn es wärmer als 20 Grad ist, suche ein Hotel"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_comparison_hoeher_als(self):
        msg = "Zeige mir Aktien die höher als 100 Euro sind"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_comparison_ueber_zahl(self):
        msg = "Finde Hotels über 4 Sterne in Berlin"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_comparison_unter_zahl(self):
        msg = "Suche Restaurants unter 50 Euro pro Person"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_comparison_above(self):
        msg = "Find hotels with rating above 4 stars"
        assert ComplexityDetector.needs_agent(msg) is True

    # === Multi-action patterns ===

    @pytest.mark.unit
    def test_multi_action_german(self):
        msg = "Schalte das Licht ein und stelle die Heizung auf 22 Grad"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_multi_action_english(self):
        msg = "Turn on the lights and set the thermostat to 72"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_multi_action_suche_und_zeige(self):
        msg = "Suche nach dem Wetter und zeige mir die Nachrichten"
        assert ComplexityDetector.needs_agent(msg) is True

    # === Combined question patterns ===

    @pytest.mark.unit
    def test_combined_question_german(self):
        msg = "Wie ist das Wetter in Berlin und was gibt es Neues in den Nachrichten?"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_combined_question_english(self):
        msg = "What is the weather like and how are the stock prices today?"
        assert ComplexityDetector.needs_agent(msg) is True

    # === Playback + multi-action patterns ===

    @pytest.mark.unit
    def test_multi_action_suche_und_spiele(self):
        msg = "Suche Musik von ZZ Top und spiele das erste Album im Arbeitszimmer"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_multi_action_finde_und_hoere(self):
        msg = "Finde Alben von Queen und höre das beste im Wohnzimmer"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_multi_action_search_and_play(self):
        msg = "Search for Pink Floyd albums and play the first one in the office"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_multi_action_find_and_listen(self):
        msg = "Find jazz playlists and listen to one in the bedroom"
        assert ComplexityDetector.needs_agent(msg) is True

    @pytest.mark.unit
    def test_simple_play_command(self):
        """Single play command should NOT trigger agent."""
        assert ComplexityDetector.needs_agent("Spiele Musik von Queen") is False

    # === Full example from Issue #49 ===

    @pytest.mark.unit
    def test_full_agent_example(self):
        msg = "Wie ist das Wetter in Berlin und wenn die Temperatur höher ist als 10 Grad, dann suche mir ein 4 Sterne Hotel für das nächste Wochenende"
        assert ComplexityDetector.needs_agent(msg) is True


class TestComplexityDetectorPatterns:
    """Test detect_patterns() — returns matched pattern group names."""

    @pytest.mark.unit
    def test_empty_returns_empty(self):
        assert ComplexityDetector.detect_patterns("") == []

    @pytest.mark.unit
    def test_none_returns_empty(self):
        assert ComplexityDetector.detect_patterns(None) == []

    @pytest.mark.unit
    def test_simple_returns_empty(self):
        assert ComplexityDetector.detect_patterns("Schalte das Licht ein") == []

    @pytest.mark.unit
    def test_conditional_detected(self):
        patterns = ComplexityDetector.detect_patterns("Wenn es regnet, dann schließe die Fenster")
        assert "conditional" in patterns

    @pytest.mark.unit
    def test_sequence_detected(self):
        patterns = ComplexityDetector.detect_patterns("Hole das Wetter und dann suche ein Restaurant")
        assert "sequence" in patterns

    @pytest.mark.unit
    def test_comparison_detected(self):
        patterns = ComplexityDetector.detect_patterns("Zeige Aktien höher als 100 Euro")
        assert "comparison" in patterns

    @pytest.mark.unit
    def test_multi_action_detected(self):
        patterns = ComplexityDetector.detect_patterns("Schalte das Licht ein und stelle die Heizung an")
        assert "multi_action" in patterns

    @pytest.mark.unit
    def test_combined_question_detected(self):
        patterns = ComplexityDetector.detect_patterns("Wie ist das Wetter und was gibt es in den Nachrichten?")
        assert "combined_question" in patterns

    @pytest.mark.unit
    def test_multiple_patterns_detected(self):
        """Full example should match multiple pattern groups."""
        msg = "Wenn es wärmer als 20 Grad ist, dann suche ein Hotel und dann buche es"
        patterns = ComplexityDetector.detect_patterns(msg)
        assert len(patterns) >= 2
        assert "conditional" in patterns
        assert "comparison" in patterns

    @pytest.mark.unit
    def test_each_group_detected_once(self):
        """Each pattern group should appear at most once."""
        msg = "Wenn es wärmer als 10 ist und kälter als 30, dann suche ein Hotel und dann buche es"
        patterns = ComplexityDetector.detect_patterns(msg)
        # No duplicates
        assert len(patterns) == len(set(patterns))
