"""
Prometheus Metrics — Optional monitoring endpoint.

Enabled via METRICS_ENABLED=true. Provides HTTP, WebSocket, LLM,
and Circuit Breaker metrics in Prometheus exposition format.

Usage:
    # In main.py:
    from utils.metrics import setup_metrics
    setup_metrics(app)

    # Then: curl http://localhost:8000/metrics
"""

import time
from typing import TYPE_CHECKING

from loguru import logger

from utils.config import settings

if TYPE_CHECKING:
    from fastapi import FastAPI

# Lazy-loaded prometheus_client references
_metrics_initialized = False
_http_requests_total = None
_http_request_duration_seconds = None
_websocket_connections = None
_llm_call_duration_seconds = None
_agent_steps_total = None
_circuit_breaker_state = None
_circuit_breaker_failures_total = None


def _init_metrics():
    """Initialize Prometheus metric objects (lazy, only when enabled)."""
    global _metrics_initialized
    global _http_requests_total, _http_request_duration_seconds
    global _websocket_connections
    global _llm_call_duration_seconds, _agent_steps_total
    global _circuit_breaker_state, _circuit_breaker_failures_total

    if _metrics_initialized:
        return

    try:
        from prometheus_client import Counter, Gauge, Histogram

        _http_requests_total = Counter(
            "renfield_http_requests_total",
            "Total HTTP requests",
            ["method", "endpoint", "status_code"],
        )

        _http_request_duration_seconds = Histogram(
            "renfield_http_request_duration_seconds",
            "HTTP request duration in seconds",
            ["method", "endpoint"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )

        _websocket_connections = Gauge(
            "renfield_websocket_connections",
            "Active WebSocket connections",
            ["type"],
        )

        _llm_call_duration_seconds = Histogram(
            "renfield_llm_call_duration_seconds",
            "LLM call duration in seconds",
            ["model", "call_type"],
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
        )

        _agent_steps_total = Histogram(
            "renfield_agent_steps_total",
            "Number of steps per agent invocation",
            buckets=(1, 2, 3, 5, 8, 12, 20),
        )

        _circuit_breaker_state = Gauge(
            "renfield_circuit_breaker_state",
            "Circuit breaker state (0=closed, 1=open, 2=half_open)",
            ["name"],
        )

        _circuit_breaker_failures_total = Counter(
            "renfield_circuit_breaker_failures_total",
            "Total circuit breaker recorded failures",
            ["name"],
        )

        _metrics_initialized = True
        logger.info("Prometheus metrics initialized")

    except ImportError:
        logger.warning("prometheus-client not installed — metrics disabled")


# === Public API for recording metrics ===


def record_http_request(method: str, endpoint: str, status_code: int, duration: float):
    """Record an HTTP request metric."""
    if not _metrics_initialized:
        return
    _http_requests_total.labels(method=method, endpoint=endpoint, status_code=status_code).inc()
    _http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)


def record_websocket_connect(ws_type: str):
    """Record a WebSocket connection opening."""
    if not _metrics_initialized:
        return
    _websocket_connections.labels(type=ws_type).inc()


def record_websocket_disconnect(ws_type: str):
    """Record a WebSocket connection closing."""
    if not _metrics_initialized:
        return
    _websocket_connections.labels(type=ws_type).dec()


def record_llm_call(model: str, call_type: str, duration: float):
    """Record an LLM call duration."""
    if not _metrics_initialized:
        return
    _llm_call_duration_seconds.labels(model=model, call_type=call_type).observe(duration)


def record_agent_steps(steps: int):
    """Record the number of agent steps in an invocation."""
    if not _metrics_initialized:
        return
    _agent_steps_total.observe(steps)


def record_circuit_breaker_state(name: str, state: str):
    """Record circuit breaker state change."""
    if not _metrics_initialized:
        return
    state_map = {"closed": 0, "open": 1, "half_open": 2}
    _circuit_breaker_state.labels(name=name).set(state_map.get(state, -1))


def record_circuit_breaker_failure(name: str):
    """Record a circuit breaker failure."""
    if not _metrics_initialized:
        return
    _circuit_breaker_failures_total.labels(name=name).inc()


# === Middleware & Endpoint Setup ===


def setup_metrics(app: "FastAPI"):
    """
    Add Prometheus metrics middleware and /metrics endpoint to the app.
    Only active when METRICS_ENABLED=true.
    """
    if not settings.metrics_enabled:
        logger.debug("Prometheus metrics disabled (METRICS_ENABLED=false)")
        return

    _init_metrics()

    if not _metrics_initialized:
        return

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request

    class PrometheusMiddleware(BaseHTTPMiddleware):
        """Collect HTTP request metrics."""

        async def dispatch(self, request: Request, call_next):
            # Skip metrics endpoint itself
            if request.url.path == "/metrics":
                return await call_next(request)

            start = time.monotonic()
            response = await call_next(request)
            duration = time.monotonic() - start

            # Normalize endpoint path (remove IDs to reduce cardinality)
            endpoint = request.url.path
            record_http_request(
                method=request.method,
                endpoint=endpoint,
                status_code=response.status_code,
                duration=duration,
            )
            return response

    app.add_middleware(PrometheusMiddleware)

    # /metrics endpoint
    from fastapi import Response
    from prometheus_client import generate_latest

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint():
        return Response(
            content=generate_latest(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    logger.info("Prometheus /metrics endpoint enabled")
