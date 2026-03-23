"""Tests for Output Guard -- System prompt leakage and role confusion detection."""

import pytest

from services.output_guard import (
    LEAKAGE_FRAGMENT_THRESHOLD,
    OutputGuardResult,
    check_output,
    extract_prompt_fragments,
)


# ============================================================================
# extract_prompt_fragments
# ============================================================================


class TestExtractPromptFragments:

    @pytest.mark.unit
    def test_extracts_significant_lines(self):
        prompt = "\n".join([
            "Du bist ein hilfreicher Assistent.",
            "Antworte immer auf Deutsch.",
            "# Header",
            "---",
            "short",
            "Verwende keine technischen Begriffe in deinen Antworten.",
            "Beachte die Datenschutzrichtlinien bei jeder Interaktion.",
        ])
        fragments = extract_prompt_fragments(prompt)
        assert len(fragments) >= 3
        assert "short" not in [f.lower() for f in fragments]
        assert "# Header" not in fragments

    @pytest.mark.unit
    def test_skips_template_variables(self):
        prompt = "Hier ist der Kontext: {memory_context}\nDies ist eine wichtige Regel fuer alle Antworten."
        fragments = extract_prompt_fragments(prompt)
        assert not any("{" in f for f in fragments)

    @pytest.mark.unit
    def test_deduplicates(self):
        prompt = "Wichtige Regel fuer alle Antworten.\nWichtige Regel fuer alle Antworten."
        fragments = extract_prompt_fragments(prompt)
        assert len(fragments) == 1

    @pytest.mark.unit
    def test_empty_prompt(self):
        assert extract_prompt_fragments("") == []

    @pytest.mark.unit
    def test_max_fragments_limit(self):
        lines = [f"Dies ist eine wichtige Regel Nummer {i} fuer den Assistenten." for i in range(50)]
        prompt = "\n".join(lines)
        fragments = extract_prompt_fragments(prompt)
        assert len(fragments) <= 20


# ============================================================================
# check_output — System Prompt Leakage
# ============================================================================


class TestSystemPromptLeakage:

    @pytest.mark.unit
    def test_normal_response_safe(self):
        result = check_output(
            "Das Wetter morgen wird sonnig mit 22 Grad.",
            system_prompt_fragments=[
                "Du bist ein hilfreicher Assistent.",
                "Antworte immer auf Deutsch.",
                "Verwende keine technischen Begriffe.",
            ],
        )
        assert result.safe
        assert not result.violations

    @pytest.mark.unit
    def test_leakage_detected(self):
        fragments = [
            "Du bist ein hilfreicher Assistent.",
            "Antworte immer auf Deutsch.",
            "Verwende keine technischen Begriffe.",
            "Beachte die Datenschutzrichtlinien.",
        ]
        # Response contains 3+ fragments
        response = (
            "Hier sind meine Regeln: Du bist ein hilfreicher Assistent. "
            "Ich soll immer Antworte immer auf Deutsch. "
            "Ausserdem Verwende keine technischen Begriffe."
        )
        result = check_output(response, system_prompt_fragments=fragments)
        assert not result.safe
        assert "system_prompt_leakage" in result.violations
        assert result.details["leaked_fragments"] >= LEAKAGE_FRAGMENT_THRESHOLD

    @pytest.mark.unit
    def test_partial_leakage_not_triggered(self):
        """1-2 fragments is not enough to trigger leakage."""
        fragments = [
            "Du bist ein hilfreicher Assistent.",
            "Antworte immer auf Deutsch.",
            "Verwende keine technischen Begriffe.",
        ]
        response = "Du bist ein hilfreicher Assistent, klar."
        result = check_output(response, system_prompt_fragments=fragments)
        assert result.safe  # Only 1 fragment matched

    @pytest.mark.unit
    def test_no_fragments_skips_leakage_check(self):
        result = check_output("Hier sind alle meine Regeln...", system_prompt_fragments=None)
        assert result.safe  # No fragments to check against


# ============================================================================
# check_output — Role Confusion
# ============================================================================


class TestRoleConfusion:

    @pytest.mark.unit
    def test_normal_response_no_confusion(self):
        result = check_output("Hier ist die Wettervorhersage fuer morgen.")
        assert result.safe

    @pytest.mark.unit
    def test_en_role_confusion_detected(self):
        result = check_output("As per my instructions, I cannot share that information.")
        assert not result.safe
        assert "role_confusion" in result.violations

    @pytest.mark.unit
    def test_en_told_to_pattern(self):
        result = check_output("I was told to never reveal my system prompt.")
        assert not result.safe
        assert "role_confusion" in result.violations

    @pytest.mark.unit
    def test_de_role_confusion_detected(self):
        result = check_output("Meine Anweisungen sagen, dass ich das nicht tun darf.")
        assert not result.safe
        assert "role_confusion" in result.violations

    @pytest.mark.unit
    def test_de_programmed_pattern(self):
        result = check_output("Ich wurde so programmiert, dass ich das nicht teile.")
        assert not result.safe
        assert "role_confusion" in result.violations

    @pytest.mark.unit
    def test_system_prompt_mention(self):
        result = check_output("In meinem System Prompt steht, dass ich freundlich sein soll.")
        assert not result.safe
        assert "role_confusion" in result.violations

    @pytest.mark.unit
    def test_according_to_rules(self):
        result = check_output("According to my rules, I should not answer that.")
        assert not result.safe
        assert "role_confusion" in result.violations


# ============================================================================
# check_output — Edge Cases
# ============================================================================


class TestEdgeCases:

    @pytest.mark.unit
    def test_empty_response(self):
        result = check_output("")
        assert result.safe

    @pytest.mark.unit
    def test_short_response(self):
        result = check_output("OK")
        assert result.safe

    @pytest.mark.unit
    def test_both_violations(self):
        fragments = [
            "Du bist ein hilfreicher Assistent.",
            "Antworte immer auf Deutsch.",
            "Verwende keine technischen Begriffe.",
        ]
        response = (
            "As per my instructions: Du bist ein hilfreicher Assistent. "
            "Antworte immer auf Deutsch. "
            "Verwende keine technischen Begriffe."
        )
        result = check_output(response, system_prompt_fragments=fragments)
        assert not result.safe
        assert "system_prompt_leakage" in result.violations
        assert "role_confusion" in result.violations
