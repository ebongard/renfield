"""
Tests for Circuit Breaker utility.
"""

import pytest
import time
from unittest.mock import patch

from utils.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    CircuitOpenError,
    llm_circuit_breaker,
    agent_circuit_breaker,
)


class TestCircuitBreakerStates:
    """Test circuit breaker state transitions."""

    @pytest.mark.unit
    def test_initial_state_is_closed(self):
        breaker = CircuitBreaker(name="test")
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    @pytest.mark.unit
    def test_closed_allows_requests(self):
        breaker = CircuitBreaker(name="test")
        assert breaker.allow_request() is True

    @pytest.mark.unit
    def test_failures_accumulate_in_closed(self):
        breaker = CircuitBreaker(name="test", failure_threshold=3)
        breaker.record_failure()
        assert breaker.failure_count == 1
        assert breaker.state == CircuitState.CLOSED

        breaker.record_failure()
        assert breaker.failure_count == 2
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.unit
    def test_opens_after_threshold_failures(self):
        breaker = CircuitBreaker(name="test", failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()

        assert breaker.state == CircuitState.OPEN
        assert breaker.failure_count == 3

    @pytest.mark.unit
    def test_open_blocks_requests(self):
        breaker = CircuitBreaker(name="test", failure_threshold=2)
        breaker.record_failure()
        breaker.record_failure()

        assert breaker.state == CircuitState.OPEN
        assert breaker.allow_request() is False

    @pytest.mark.unit
    def test_success_resets_failure_count_in_closed(self):
        breaker = CircuitBreaker(name="test", failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.failure_count == 2

        breaker.record_success()
        assert breaker.failure_count == 0


class TestCircuitBreakerRecovery:
    """Test circuit breaker recovery mechanism."""

    @pytest.mark.unit
    def test_transitions_to_half_open_after_timeout(self):
        breaker = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout=0.1)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # Wait for recovery timeout
        time.sleep(0.15)

        # Request should transition to half-open and be allowed
        assert breaker.allow_request() is True
        assert breaker.state == CircuitState.HALF_OPEN

    @pytest.mark.unit
    def test_half_open_limits_requests(self):
        breaker = CircuitBreaker(
            name="test",
            failure_threshold=2,
            recovery_timeout=0.05,
            half_open_max_calls=1
        )
        breaker.record_failure()
        breaker.record_failure()

        time.sleep(0.1)

        # First request transitions to half-open
        assert breaker.allow_request() is True
        assert breaker.state == CircuitState.HALF_OPEN

        # Second request should be blocked
        assert breaker.allow_request() is False

    @pytest.mark.unit
    def test_success_in_half_open_closes_circuit(self):
        breaker = CircuitBreaker(
            name="test",
            failure_threshold=2,
            recovery_timeout=0.05,
            half_open_max_calls=1
        )
        breaker.record_failure()
        breaker.record_failure()

        time.sleep(0.1)
        breaker.allow_request()  # Transition to half-open
        breaker.record_success()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    @pytest.mark.unit
    def test_failure_in_half_open_opens_circuit(self):
        breaker = CircuitBreaker(
            name="test",
            failure_threshold=2,
            recovery_timeout=0.05,
            half_open_max_calls=1
        )
        breaker.record_failure()
        breaker.record_failure()

        time.sleep(0.1)
        breaker.allow_request()  # Transition to half-open
        breaker.record_failure()  # Fail during test

        assert breaker.state == CircuitState.OPEN


class TestCircuitBreakerReset:
    """Test manual reset functionality."""

    @pytest.mark.unit
    def test_manual_reset(self):
        breaker = CircuitBreaker(name="test", failure_threshold=2)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        breaker.reset()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
        assert breaker.allow_request() is True


class TestCircuitBreakerStatus:
    """Test status reporting."""

    @pytest.mark.unit
    def test_get_status(self):
        breaker = CircuitBreaker(
            name="test_breaker",
            failure_threshold=5,
            recovery_timeout=60.0
        )
        breaker.record_failure()

        status = breaker.get_status()

        assert status["name"] == "test_breaker"
        assert status["state"] == "closed"
        assert status["failure_count"] == 1
        assert status["failure_threshold"] == 5
        assert status["recovery_timeout"] == 60.0


class TestGlobalCircuitBreakers:
    """Test global circuit breaker instances."""

    @pytest.mark.unit
    def test_llm_circuit_breaker_exists(self):
        assert llm_circuit_breaker is not None
        assert llm_circuit_breaker.name == "ollama_llm"
        # Reset after test
        llm_circuit_breaker.reset()

    @pytest.mark.unit
    def test_agent_circuit_breaker_exists(self):
        assert agent_circuit_breaker is not None
        assert agent_circuit_breaker.name == "agent_llm"
        # Reset after test
        agent_circuit_breaker.reset()

    @pytest.mark.unit
    def test_global_breakers_are_independent(self):
        llm_circuit_breaker.reset()
        agent_circuit_breaker.reset()

        llm_circuit_breaker.record_failure()

        assert llm_circuit_breaker.failure_count == 1
        assert agent_circuit_breaker.failure_count == 0

        llm_circuit_breaker.reset()
