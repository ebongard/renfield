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
_memory_total = None
_memory_cleanup_total = None
_mcp_tool_duration_seconds = None
_mcp_tool_errors_total = None
_agent_outcome_total = None
_injection_attempts_total = None
_budget_reductions_total = None
_output_guard_violations_total = None
_llm_response_truncated_total = None
_auth_provider_unreachable_total = None
_login_failure_total = None
_authz_denied_total = None
_kg_conflation_candidates = None
_speaker_inprocess_embedding_blocked_total = None
_orchestrator_domains_requested_total = None
_orchestrator_domains_rendered_total = None
_orchestrator_contract_version_mismatch_total = None
_orchestrator_contract_demotions_total = None
_mcp_health_ticks_total = None
_mcp_health_problem_servers = None


def _init_metrics():
    """Initialize Prometheus metric objects (lazy, only when enabled)."""
    global _metrics_initialized
    global _http_requests_total, _http_request_duration_seconds
    global _websocket_connections
    global _llm_call_duration_seconds, _agent_steps_total
    global _circuit_breaker_state, _circuit_breaker_failures_total
    global _memory_total, _memory_cleanup_total
    global _mcp_tool_duration_seconds, _mcp_tool_errors_total
    global _agent_outcome_total, _injection_attempts_total
    global _budget_reductions_total, _output_guard_violations_total
    global _llm_response_truncated_total
    global _auth_provider_unreachable_total
    global _login_failure_total, _authz_denied_total
    global _kg_conflation_candidates
    global _speaker_inprocess_embedding_blocked_total
    global _orchestrator_domains_requested_total, _orchestrator_domains_rendered_total
    global _orchestrator_contract_version_mismatch_total, _orchestrator_contract_demotions_total
    global _mcp_health_ticks_total, _mcp_health_problem_servers

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

        _memory_total = Gauge(
            "renfield_memory_active_total",
            "Total active memories",
        )

        _kg_conflation_candidates = Gauge(
            "renfield_kg_conflation_candidates",
            "Distinct-name same-type KG entity pairs embedding >= the monitor "
            "threshold (a forming generic-centroid magnet / mis-embedding "
            "tripwire; expected 0)",
        )

        _memory_cleanup_total = Counter(
            "renfield_memory_cleanup_total",
            "Total memories cleaned up",
            ["reason"],
        )

        _mcp_tool_duration_seconds = Histogram(
            "renfield_mcp_tool_duration_seconds",
            "MCP tool call duration in seconds",
            ["server", "tool"],
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )

        _mcp_tool_errors_total = Counter(
            "renfield_mcp_tool_errors_total",
            "Total MCP tool call errors",
            ["server", "tool"],
        )

        _agent_outcome_total = Counter(
            "renfield_agent_outcome_total",
            "Agent loop outcomes",
            ["outcome"],
        )

        _injection_attempts_total = Counter(
            "renfield_injection_attempts_total",
            "Prompt injection attempts detected",
            ["category"],
        )

        _budget_reductions_total = Counter(
            "renfield_token_budget_reductions_total",
            "Token budget reduction passes triggered",
            ["pass_name"],
        )

        _output_guard_violations_total = Counter(
            "renfield_output_guard_violations_total",
            "Output guard violations detected",
            ["violation"],
        )

        # MCP self-detection tick heartbeat (#1107): a monitor whose tick wedged
        # used to be indistinguishable from "all healthy" (both are silent).
        # A flatlining counter under a running backend = the monitor is stuck.
        _mcp_health_ticks_total = Counter(
            "renfield_mcp_health_ticks_total",
            "Completed MCP health monitor ticks",
        )
        _mcp_health_problem_servers = Gauge(
            "renfield_mcp_health_problem_servers",
            "Degraded/down MCP servers seen by the last completed monitor tick",
        )

        # Answers cut off at the output-token cap. This used to be entirely
        # invisible: the user simply got a reply that stopped mid-sentence.
        # Any sustained non-zero rate means num_predict is too low for what
        # the model is being asked to produce.
        _llm_response_truncated_total = Counter(
            "renfield_llm_response_truncated_total",
            "LLM completions that hit the output-token cap (finish_reason=length)",
            ["model", "call_type"],
        )

        # Pluggable-auth fail-open observability. Name intentionally matches
        # the cross-repo design contract (ebongard/renfield#591) verbatim —
        # no `renfield_` prefix — so dashboards/alerts written against the
        # approved design resolve without translation.
        _auth_provider_unreachable_total = Counter(
            "auth_provider_unreachable_total",
            "Auth provider skipped during the credential walk because it "
            "errored or timed out (fail-open). A non-zero rate means a "
            "provider is silently down.",
            ["provider_id"],
        )

        # Auth observability (#696). Failed logins and authz denials were
        # previously silent (no log, no metric), leaving credential-stuffing and
        # privilege-probing invisible to monitoring. Labels are low-cardinality
        # reason/permission strings — never the username or token (no PII, no
        # enumeration oracle from the metrics surface).
        _login_failure_total = Counter(
            "renfield_login_failure_total",
            "Failed login attempts by reason (bad_credentials, inactive, "
            "locked_out, provider_declined).",
            ["reason"],
        )

        _authz_denied_total = Counter(
            "renfield_authz_denied_total",
            "Authorization denials (HTTP 403) by the permission that was "
            "required (or 'inactive_account' / 'unauthenticated').",
            ["permission"],
        )

        _speaker_inprocess_embedding_blocked_total = Counter(
            "renfield_speaker_inprocess_embedding_blocked_total",
            "In-process (SpeechBrain) speaker-embedding use refused because "
            "speaker_inprocess_embeddings_enabled is off. SpeechBrain and the "
            "voice-server ONNX model do NOT share a representation space; a "
            "non-zero rate means a caller would have written/compared "
            "cross-space embeddings (P0 of "
            "docs/design/voice-identity-wakeword-verification.md).",
            ["path"],
        )

        # Orchestrator domain-coverage instrumentation (typed-contracts plan
        # Phase 0 / outside-voice finding #1). requested = domains the planner
        # fanned out to; rendered = domains that contributed content to the
        # combined answer. The ratio rendered/requested over time is the
        # measured residual domain-drop rate that gates the per-domain Phase-3
        # rollout — so it's built on evidence, not speculation.
        _orchestrator_domains_requested_total = Counter(
            "renfield_orchestrator_domains_requested_total",
            "Sub-agent domains the orchestrator planner fanned out to",
        )
        _orchestrator_domains_rendered_total = Counter(
            "renfield_orchestrator_domains_rendered_total",
            "Sub-agent domains that contributed content to the combined answer",
        )
        _orchestrator_contract_version_mismatch_total = Counter(
            "renfield_orchestrator_contract_version_mismatch_total",
            "Domain contract registrations refused due to version skew "
            "(fail-closed to Tier 2)",
            ["domain"],
        )
        _orchestrator_contract_demotions_total = Counter(
            "renfield_orchestrator_contract_demotions_total",
            "Tier-1 typed-contract renders that demoted to Tier 2 "
            "(declined / verify-fail / raised)",
            ["domain", "reason"],
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


def record_orchestrator_render(requested: int, rendered: int):
    """Record orchestrator domain coverage for a multi-domain turn.

    requested = domains fanned out to; rendered = domains that contributed
    content. A persistent gap (rendered < requested) is the residual
    domain-drop rate the typed-contracts Phase-3 rollout is gated on.
    """
    if not _metrics_initialized:
        return
    if requested > 0:
        _orchestrator_domains_requested_total.inc(requested)
    if rendered > 0:
        _orchestrator_domains_rendered_total.inc(rendered)


def record_contract_version_mismatch(domain: str):
    """A domain contract was refused due to version skew (fail-closed to Tier 2)."""
    if not _metrics_initialized:
        return
    _orchestrator_contract_version_mismatch_total.labels(domain=domain).inc()


def record_contract_demotion(domain: str, reason: str):
    """A Tier-1 typed render demoted to Tier 2 (reason: declined/verify/error)."""
    if not _metrics_initialized:
        return
    _orchestrator_contract_demotions_total.labels(domain=domain, reason=reason).inc()


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


def record_memory_cleanup(counts: dict):
    """Record memory cleanup results."""
    if not _metrics_initialized:
        return
    for reason, count in counts.items():
        if count > 0:
            _memory_cleanup_total.labels(reason=reason).inc(count)


def set_memory_total(count: int):
    """Set current active memory count."""
    if not _metrics_initialized:
        return
    _memory_total.set(count)


def set_kg_conflation_candidates(count: int):
    """Set the current count of distinct-name same-type near-duplicate KG pairs.

    A tripwire gauge: it should sit at 0. A non-zero value means two entities
    that SHOULD be distinct embed close enough to risk an inline fold (the
    generic-centroid magnet class). See ``services/kg_conflation_monitor.py``.
    """
    if not _metrics_initialized:
        return
    _kg_conflation_candidates.set(count)


def record_mcp_tool_call(server: str, tool: str, duration: float, success: bool):
    """Record an MCP tool call with duration and success/failure."""
    if not _metrics_initialized:
        return
    _mcp_tool_duration_seconds.labels(server=server, tool=tool).observe(duration)
    if not success:
        _mcp_tool_errors_total.labels(server=server, tool=tool).inc()


def record_mcp_health_tick(problem_count: int | None):
    """Record one MCP health monitor tick (heartbeat, #1107). The counter ticks
    for every ATTEMPTED tick (liveness — a flatline means the loop is stuck);
    the gauge is only set when the tick completed with a verdict
    (``problem_count is not None``) so a failing get_status can't fake 0."""
    if not _metrics_initialized:
        return
    _mcp_health_ticks_total.inc()
    if problem_count is not None:
        _mcp_health_problem_servers.set(problem_count)


def record_agent_outcome(outcome: str):
    """Record an agent loop outcome (success/error/max_steps/timeout/loop_detected)."""
    if not _metrics_initialized:
        return
    _agent_outcome_total.labels(outcome=outcome).inc()


def record_injection_attempt(category: str):
    """Record a prompt injection attempt detection."""
    if not _metrics_initialized:
        return
    _injection_attempts_total.labels(category=category).inc()


def record_budget_reduction(pass_name: str):
    """Record a token budget reduction pass being triggered."""
    if not _metrics_initialized:
        return
    _budget_reductions_total.labels(pass_name=pass_name).inc()


def record_auth_provider_unreachable(provider_id: str):
    """Record that a credential provider was skipped (fail-open) because it
    errored or timed out during the registry's priority walk."""
    if not _metrics_initialized:
        return
    _auth_provider_unreachable_total.labels(provider_id=provider_id).inc()


def record_login_failure(reason: str):
    """Record a failed login attempt (#696).

    `reason` is a low-cardinality label: "bad_credentials", "inactive",
    "locked_out", or "provider_declined". Never pass the username.
    """
    if not _metrics_initialized:
        return
    _login_failure_total.labels(reason=reason).inc()


def record_authz_denied(permission: str):
    """Record an authorization denial / HTTP 403 (#696).

    `permission` is the required permission string, or "inactive_account" /
    "unauthenticated" for the non-permission 401/403 paths.
    """
    if not _metrics_initialized:
        return
    _authz_denied_total.labels(permission=permission).inc()


def record_speaker_inprocess_embedding_blocked(path: str):
    """Record a refused in-process (SpeechBrain) speaker-embedding use.

    `path` names the blocked seam ("whisper", "route_enroll",
    "route_identify", "route_verify") so a dashboard shows WHERE the
    cross-space attempt came from.
    """
    if not _metrics_initialized:
        return
    _speaker_inprocess_embedding_blocked_total.labels(path=path).inc()


def record_output_guard_violation(violation: str):
    """Record an output guard violation."""
    if not _metrics_initialized:
        return
    _output_guard_violations_total.labels(violation=violation).inc()


def record_llm_response_truncated(model: str, call_type: str):
    """Record a completion that hit the output-token cap (finish_reason=length)."""
    if not _metrics_initialized:
        return
    _llm_response_truncated_total.labels(model=model, call_type=call_type).inc()


# === Middleware & Endpoint Setup ===


import re as _re

# Fallback heuristic for paths WITHOUT a matched route (404s, scanner probes):
# numeric ids, UUIDs, and long hex tokens collapse to `{id}` so even unmatched
# traffic can't mint unbounded label values.
_ID_SEGMENT_RE = _re.compile(
    r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,})$"
)


def normalize_endpoint(path: str) -> str:
    """Heuristic id-collapse for the metrics `endpoint` label
    (e.g. /api/knowledge/documents/123 → /api/knowledge/documents/{id}).

    Only the FALLBACK for requests without a matched route — matched requests
    use the exact route template (``scope["route"].path_format``), which also
    covers non-hex string path params ({session_id}, {intent_name}, …) this
    regex cannot recognise.
    """
    parts = path.split("/")
    return "/".join("{id}" if _ID_SEGMENT_RE.match(p) else p for p in parts)


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

            # Cardinality-safe endpoint label: the EXACT route template when
            # the request matched a route (covers every path param, including
            # non-hex strings like {session_id}); heuristic id-collapse only
            # for unmatched paths (404s / scanner probes).
            route = request.scope.get("route")
            path_format = getattr(route, "path_format", None)
            endpoint = path_format or normalize_endpoint(request.url.path)
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
