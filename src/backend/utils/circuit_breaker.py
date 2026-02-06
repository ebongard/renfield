"""
Circuit Breaker — Protects against cascading failures from external services.

Implements the Circuit Breaker pattern with three states:
- CLOSED: Normal operation, requests pass through
- OPEN: Service is failing, requests are rejected immediately
- HALF_OPEN: Testing if service recovered, allows limited requests

Usage:
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)

    async def call_llm():
        if not await breaker.allow_request():
            raise CircuitOpenError("LLM service temporarily unavailable")
        try:
            result = await ollama.chat(...)
            await breaker.record_success()
            return result
        except Exception as e:
            await breaker.record_failure()
            raise
"""

import asyncio
import time
from enum import Enum

from loguru import logger


class CircuitState(str, Enum):
    """Circuit breaker states."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, reject requests
    HALF_OPEN = "half_open" # Testing recovery


class CircuitOpenError(Exception):
    """Raised when circuit is open and requests are blocked."""
    pass


class CircuitBreaker:
    """
    Thread-safe Circuit Breaker implementation.

    Args:
        name: Identifier for logging
        failure_threshold: Number of consecutive failures to open circuit
        recovery_timeout: Seconds to wait before testing recovery
        half_open_max_calls: Max calls allowed in half-open state
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float | None = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit state."""
        return self._state

    @property
    def failure_count(self) -> int:
        """Number of consecutive failures."""
        return self._failure_count

    async def allow_request(self) -> bool:
        """
        Check if a request should be allowed.

        Returns:
            True if request is allowed, False if circuit is open
        """
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                if self._last_failure_time is not None:
                    elapsed = time.monotonic() - self._last_failure_time
                    if elapsed >= self.recovery_timeout:
                        self._transition_to_half_open()
                        # Count this transition request against half_open limit
                        self._half_open_calls = 1
                        return True
                return False

            if self._state == CircuitState.HALF_OPEN:
                # Allow limited requests in half-open state
                if self._half_open_calls < self.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False

            return False

    async def record_success(self) -> None:
        """Record a successful request."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_max_calls:
                    self._transition_to_closed()
            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed request."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            # Prometheus instrumentation (optional import)
            try:
                from utils.metrics import record_circuit_breaker_failure
                record_circuit_breaker_failure(self.name)
            except Exception:
                pass

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open goes back to open
                self._transition_to_open()
            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.failure_threshold:
                    self._transition_to_open()

    def reset(self) -> None:
        """Manually reset the circuit to closed state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._last_failure_time = None
        logger.info(f"🔌 Circuit breaker '{self.name}' manually reset to CLOSED")

    def _transition_to_open(self) -> None:
        """Transition to OPEN state."""
        self._state = CircuitState.OPEN
        self._half_open_calls = 0
        self._success_count = 0
        self._record_state_metric("open")
        logger.warning(
            f"🔴 Circuit breaker '{self.name}' OPENED after {self._failure_count} failures"
        )

    def _transition_to_half_open(self) -> None:
        """Transition to HALF_OPEN state."""
        self._state = CircuitState.HALF_OPEN
        self._half_open_calls = 0
        self._success_count = 0
        self._record_state_metric("half_open")
        logger.info(
            f"🟡 Circuit breaker '{self.name}' HALF_OPEN — testing recovery"
        )

    def _transition_to_closed(self) -> None:
        """Transition to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._record_state_metric("closed")
        logger.info(
            f"🟢 Circuit breaker '{self.name}' CLOSED — service recovered"
        )

    def _record_state_metric(self, state: str) -> None:
        """Record state change to Prometheus (if available)."""
        try:
            from utils.metrics import record_circuit_breaker_state
            record_circuit_breaker_state(self.name, state)
        except Exception:
            pass

    def get_status(self) -> dict:
        """Get current circuit breaker status."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "last_failure_time": self._last_failure_time,
        }


# === Global Circuit Breaker Instances ===
# These can be imported and used across the application

from utils.config import settings

# Circuit breaker for Ollama LLM calls
llm_circuit_breaker = CircuitBreaker(
    name="ollama_llm",
    failure_threshold=settings.cb_failure_threshold,
    recovery_timeout=settings.cb_llm_recovery_timeout,
    half_open_max_calls=1,
)

# Circuit breaker for Agent Loop LLM calls (separate to avoid affecting chat)
agent_circuit_breaker = CircuitBreaker(
    name="agent_llm",
    failure_threshold=settings.cb_failure_threshold,
    recovery_timeout=settings.cb_agent_recovery_timeout,
    half_open_max_calls=1,
)
