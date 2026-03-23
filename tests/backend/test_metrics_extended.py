"""Tests for extended metrics -- KPI recording functions."""

import pytest

from utils.metrics import (
    record_agent_outcome,
    record_budget_reduction,
    record_injection_attempt,
    record_mcp_tool_call,
    record_output_guard_violation,
)


class TestMetricsRecordingFunctions:
    """Test that recording functions don't crash when metrics are disabled."""

    @pytest.mark.unit
    def test_record_mcp_tool_call_no_crash(self):
        record_mcp_tool_call("homeassistant", "get_states", 1.5, True)
        record_mcp_tool_call("homeassistant", "get_states", 2.0, False)

    @pytest.mark.unit
    def test_record_agent_outcome_no_crash(self):
        record_agent_outcome("success")
        record_agent_outcome("error")
        record_agent_outcome("max_steps")
        record_agent_outcome("timeout")
        record_agent_outcome("loop_detected")

    @pytest.mark.unit
    def test_record_injection_attempt_no_crash(self):
        record_injection_attempt("instruction_override")
        record_injection_attempt("gdpr_bypass")

    @pytest.mark.unit
    def test_record_budget_reduction_no_crash(self):
        record_budget_reduction("halve_history")
        record_budget_reduction("drop_memory")
        record_budget_reduction("drop_documents")
        record_budget_reduction("truncate_results")

    @pytest.mark.unit
    def test_record_output_guard_violation_no_crash(self):
        record_output_guard_violation("system_prompt_leakage")
        record_output_guard_violation("role_confusion")
