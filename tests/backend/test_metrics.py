"""Tests for Prometheus metrics module."""

from unittest.mock import MagicMock, patch


class TestMetricsDisabled:
    """Tests when metrics are disabled (default)."""

    def test_record_functions_are_noop_when_not_initialized(self):
        """Metric recording functions should silently do nothing when not initialized."""
        from utils.metrics import (
            record_agent_steps,
            record_circuit_breaker_failure,
            record_circuit_breaker_state,
            record_http_request,
            record_llm_call,
            record_websocket_connect,
            record_websocket_disconnect,
        )

        # None of these should raise
        record_http_request("GET", "/api/chat", 200, 0.5)
        record_websocket_connect("chat")
        record_websocket_disconnect("chat")
        record_llm_call("llama3.2", "chat", 1.5)
        record_agent_steps(3)
        record_circuit_breaker_state("ollama_llm", "closed")
        record_circuit_breaker_failure("ollama_llm")

    def test_setup_metrics_skips_when_disabled(self):
        """setup_metrics should do nothing when METRICS_ENABLED=false."""
        from utils.metrics import setup_metrics

        mock_app = MagicMock()

        with patch("utils.metrics.settings") as mock_settings:
            mock_settings.metrics_enabled = False
            setup_metrics(mock_app)

        # No middleware should be added
        mock_app.add_middleware.assert_not_called()


class TestMetricsEnabled:
    """Tests when metrics are enabled."""

    def test_init_metrics_creates_collectors(self):
        """_init_metrics should create Prometheus collectors."""
        import utils.metrics as metrics_module

        # Reset state
        metrics_module._metrics_initialized = False

        with patch.dict("sys.modules", {"prometheus_client": MagicMock()}):
            metrics_module._init_metrics()

        assert metrics_module._metrics_initialized is True
        assert metrics_module._http_requests_total is not None
        assert metrics_module._http_request_duration_seconds is not None
        assert metrics_module._websocket_connections is not None
        assert metrics_module._llm_call_duration_seconds is not None
        assert metrics_module._agent_steps_total is not None
        assert metrics_module._circuit_breaker_state is not None
        assert metrics_module._circuit_breaker_failures_total is not None

        # Cleanup
        metrics_module._metrics_initialized = False
        metrics_module._http_requests_total = None
        metrics_module._http_request_duration_seconds = None
        metrics_module._websocket_connections = None
        metrics_module._llm_call_duration_seconds = None
        metrics_module._agent_steps_total = None
        metrics_module._circuit_breaker_state = None
        metrics_module._circuit_breaker_failures_total = None

    def test_init_metrics_handles_missing_prometheus_client(self):
        """_init_metrics should handle missing prometheus_client gracefully."""
        import utils.metrics as metrics_module

        metrics_module._metrics_initialized = False

        with patch.dict("sys.modules", {"prometheus_client": None}):
            # Force ImportError by removing the module
            import sys
            saved = sys.modules.get("prometheus_client")
            sys.modules["prometheus_client"] = None
            try:
                metrics_module._init_metrics()
            finally:
                if saved is not None:
                    sys.modules["prometheus_client"] = saved
                else:
                    sys.modules.pop("prometheus_client", None)

        # Should remain uninitialized
        assert metrics_module._metrics_initialized is False

    def test_record_http_request_when_initialized(self):
        """record_http_request should call Prometheus counter/histogram."""
        import utils.metrics as metrics_module

        mock_counter = MagicMock()
        mock_histogram = MagicMock()

        metrics_module._metrics_initialized = True
        metrics_module._http_requests_total = mock_counter
        metrics_module._http_request_duration_seconds = mock_histogram

        try:
            metrics_module.record_http_request("GET", "/api/chat", 200, 0.123)

            mock_counter.labels.assert_called_once_with(
                method="GET", endpoint="/api/chat", status_code=200
            )
            mock_counter.labels().inc.assert_called_once()

            mock_histogram.labels.assert_called_once_with(
                method="GET", endpoint="/api/chat"
            )
            mock_histogram.labels().observe.assert_called_once_with(0.123)
        finally:
            metrics_module._metrics_initialized = False
            metrics_module._http_requests_total = None
            metrics_module._http_request_duration_seconds = None

    def test_record_websocket_connect_disconnect(self):
        """WebSocket gauge should inc on connect and dec on disconnect."""
        import utils.metrics as metrics_module

        mock_gauge = MagicMock()

        metrics_module._metrics_initialized = True
        metrics_module._websocket_connections = mock_gauge

        try:
            metrics_module.record_websocket_connect("chat")
            mock_gauge.labels.assert_called_with(type="chat")
            mock_gauge.labels().inc.assert_called_once()

            metrics_module.record_websocket_disconnect("satellite")
            mock_gauge.labels.assert_called_with(type="satellite")
            mock_gauge.labels().dec.assert_called_once()
        finally:
            metrics_module._metrics_initialized = False
            metrics_module._websocket_connections = None

    def test_record_circuit_breaker_state_mapping(self):
        """Circuit breaker state should map to numeric values."""
        import utils.metrics as metrics_module

        mock_gauge = MagicMock()

        metrics_module._metrics_initialized = True
        metrics_module._circuit_breaker_state = mock_gauge

        try:
            metrics_module.record_circuit_breaker_state("ollama_llm", "closed")
            mock_gauge.labels().set.assert_called_with(0)

            metrics_module.record_circuit_breaker_state("ollama_llm", "open")
            mock_gauge.labels().set.assert_called_with(1)

            metrics_module.record_circuit_breaker_state("ollama_llm", "half_open")
            mock_gauge.labels().set.assert_called_with(2)
        finally:
            metrics_module._metrics_initialized = False
            metrics_module._circuit_breaker_state = None


class TestCircuitBreakerMetricsIntegration:
    """Test that circuit breaker records metrics on state transitions."""

    async def test_circuit_breaker_records_failure_metric(self):
        """CircuitBreaker.record_failure should call metrics."""
        from utils.circuit_breaker import CircuitBreaker

        breaker = CircuitBreaker(name="test", failure_threshold=3)

        with patch("utils.metrics.record_circuit_breaker_failure") as mock_record:
            await breaker.record_failure()
            mock_record.assert_called_once_with("test")

    async def test_circuit_breaker_records_state_on_open(self):
        """CircuitBreaker should record state metric when transitioning to OPEN."""
        from utils.circuit_breaker import CircuitBreaker

        breaker = CircuitBreaker(name="test", failure_threshold=2)

        with patch("utils.metrics.record_circuit_breaker_state") as mock_state:
            await breaker.record_failure()
            await breaker.record_failure()  # Should trigger OPEN
            mock_state.assert_called_with("test", "open")

    async def test_circuit_breaker_records_state_on_closed(self):
        """CircuitBreaker should record state metric when recovering to CLOSED."""
        from utils.circuit_breaker import CircuitBreaker

        breaker = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.0)

        with patch("utils.metrics.record_circuit_breaker_state") as mock_state:
            await breaker.record_failure()  # -> OPEN
            await breaker.allow_request()   # -> HALF_OPEN (recovery_timeout=0)
            await breaker.record_success()  # -> CLOSED
            mock_state.assert_called_with("test", "closed")
