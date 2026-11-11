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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

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
_rag_search_duration_seconds = None
_agent_outcome_total = None
_agent_schema_repairs_total = None
_voice_empty_turn_total = None
_injection_attempts_total = None
_budget_reductions_total = None
_output_guard_violations_total = None
_llm_response_truncated_total = None
_router_fallback_total = None
_embedding_errors_total = None
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
    global _mcp_tool_duration_seconds, _rag_search_duration_seconds
    global _agent_outcome_total, _agent_schema_repairs_total, _injection_attempts_total
    global _voice_empty_turn_total
    global _budget_reductions_total, _output_guard_violations_total
    global _llm_response_truncated_total
    global _router_fallback_total, _embedding_errors_total
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
            "LLM call duration in seconds (agent loop calls only, see LLM_CALL_TYPES)",
            ["model", "call_type", "outcome"],
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
        )

        _agent_steps_total = Histogram(
            "renfield_agent_steps_total",
            # Unit spelled out where dashboards show it: one observation per
            # AgentService.run(), so an orchestrated turn adds one PER SUB-AGENT.
            "Steps per agent run (orchestrated turns: one observation per sub-agent)",
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

        # One observation per call that was actually DISPATCHED to a server
        # (#82). Refusals before dispatch (permission, validation, rate limit,
        # not connected) say nothing about the server and are not observed.
        # No `tool` label: tool names come from the remote server's tools/list,
        # so their number is not ours to bound. `outcome` follows the health
        # verdict's vocabulary (MCP_TOOL_OUTCOMES): only `timeout` is what
        # `calls_failing` counts; `app_error` is an answer, not an outage.
        _mcp_tool_duration_seconds = Histogram(
            "renfield_mcp_tool_duration_seconds",
            "Duration of MCP tool calls dispatched to a server, by outcome",
            ["server", "outcome"],
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )

        # One observation per RAGRetrieval.search() (#82): embedding, both
        # retrievers, rerank and context expansion together.
        _rag_search_duration_seconds = Histogram(
            "renfield_rag_search_duration_seconds",
            "Duration of a RAG search (embedding + retrieval + rerank), by outcome",
            ["outcome"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )

        _agent_outcome_total = Counter(
            "renfield_agent_outcome_total",
            "Agent run outcomes, one per AgentService.run() — an orchestrated "
            "turn counts once per sub-agent, so this is NOT a request count",
            ["outcome"],
        )

        # Agent responses accepted only after repairing their shape (#1381),
        # e.g. a final answer without its "action" key. The run itself counts
        # as an ordinary final_answer; this counter keeps visible how often
        # the model breaks the schema. Primed at startup.
        _agent_schema_repairs_total = Counter(
            "renfield_agent_schema_repairs_total",
            "Agent responses accepted after repairing their JSON shape",
            ["kind"],
        )

        # Voice turns that produced no text, by WHY (#1386; kinds in
        # ha_glue/services/voice_empty_turn.py). They used to end silently,
        # and a false wake was indistinguishable from unintelligible speech.
        # Primed at startup.
        _voice_empty_turn_total = Counter(
            "renfield_voice_empty_turn_total",
            "Voice turns that ended without text, by source and kind",
            ["source", "kind"],
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

        # Routing silently collapsed to the generic role. The LLM router
        # (AGENT_ROUTER_URL) failing does not error the turn: classify()
        # returns 'general' and the user gets a generic answer with no
        # role prompt, no sub-intent and no deterministic tool filter.
        # Two days of that in 2026-09 left no trace beyond a WARNING per
        # turn, while /api/health stayed green (it only lists models).
        _router_fallback_total = Counter(
            "renfield_router_fallback_total",
            "Router LLM classification fell back to the general role",
            ["reason"],
        )

        # Embedding calls that raised. Every consumer (semantic router,
        # memory retrieval, KG, episodic memory) catches the exception,
        # logs a WARNING and degrades to 'no context' — so a dead
        # embedding endpoint is invisible everywhere except the log.
        _embedding_errors_total = Counter(
            "renfield_embedding_errors_total",
            "Embedding requests that raised (endpoint down, model failed to load)",
            ["model"],
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


#: WebSocket route path -> value of the `type` label on
#: renfield_websocket_connections. A path that is not listed (a plugin route)
#: counts as WEBSOCKET_TYPE_OTHER, so the label can never carry a raw path.
WEBSOCKET_TYPES = {
    "/ws": "chat",
    "/ws/satellite": "satellite",
    "/ws/device": "device",
    "/ws/kiosk": "kiosk",
    "/ws/user": "user",
    "/ws/knowledge-graph": "knowledge_graph",
    "/ws/wakeword": "wakeword",
}
WEBSOCKET_TYPE_OTHER = "other"
_WEBSOCKET_TYPE_VALUES = frozenset({*WEBSOCKET_TYPES.values(), WEBSOCKET_TYPE_OTHER})


def _bounded(value: str, allowed: "frozenset[str] | tuple[str, ...]", fallback: str) -> str:
    """Keep a label value inside its fixed vocabulary."""
    return value if value in allowed else fallback


def record_websocket_connect(ws_type: str):
    """Record a WebSocket connection opening (`ws_type`: a WEBSOCKET_TYPES value)."""
    if not _metrics_initialized:
        return
    ws_type = _bounded(ws_type, _WEBSOCKET_TYPE_VALUES, WEBSOCKET_TYPE_OTHER)
    _websocket_connections.labels(type=ws_type).inc()


def record_websocket_disconnect(ws_type: str):
    """Record a WebSocket connection closing (`ws_type`: a WEBSOCKET_TYPES value)."""
    if not _metrics_initialized:
        return
    ws_type = _bounded(ws_type, _WEBSOCKET_TYPE_VALUES, WEBSOCKET_TYPE_OTHER)
    _websocket_connections.labels(type=ws_type).dec()


def _prime_websocket_connections() -> None:
    """Export a 0 sample per known type, so an idle type reads 0, not 'no data'."""
    for ws_type in sorted(_WEBSOCKET_TYPE_VALUES):
        _websocket_connections.labels(type=ws_type)


#: How a timed call ended, as `observe_duration` classifies it.
CALL_OUTCOMES = ("ok", "timeout", "error")

#: The call types renfield_llm_call_duration_seconds is recorded for: the
#: agent loop's LLM calls in services/agent_service.py. Other LLM consumers
#: (router, intent, memory extraction, RAG answers) are not timed here.
LLM_CALL_TYPES = ("agent_preselect", "agent_step", "agent_retry", "agent_summary")


def record_llm_call(model: str, call_type: str, duration: float, outcome: str = "ok"):
    """Record an LLM call duration.

    `model` is the configured model name of the role (settings or
    agent_roles.yaml), never something a request supplies.
    """
    if not _metrics_initialized:
        return
    _llm_call_duration_seconds.labels(
        model=model,
        call_type=_bounded(call_type, LLM_CALL_TYPES, "other"),
        outcome=_bounded(outcome, CALL_OUTCOMES, "error"),
    ).observe(duration)


def record_rag_search(duration: float, outcome: str = "ok"):
    """Record the duration of one RAG search."""
    if not _metrics_initialized:
        return
    _rag_search_duration_seconds.labels(
        outcome=_bounded(outcome, CALL_OUTCOMES, "error"),
    ).observe(duration)


@contextmanager
def observe_duration(record: Callable[..., Any], *labels: str) -> Iterator[None]:
    """Time the enclosed block and report it as ``record(*labels, seconds, outcome)``.

    The outcome is one of CALL_OUTCOMES, read off how the block ended; the
    block's own exception always propagates unchanged. A cancellation is not
    recorded: the caller gave up, the callee did nothing wrong. A failure
    inside `record` is logged and swallowed, so timing can never fail the
    call it measures. With metrics disabled this only runs the block.
    """
    if not _metrics_initialized:
        yield
        return
    started = time.monotonic()
    outcome: str | None = "ok"
    try:
        yield
    except TimeoutError:
        outcome = "timeout"
        raise
    except Exception:
        outcome = "error"
        raise
    except BaseException:
        outcome = None
        raise
    finally:
        if outcome is not None:
            try:
                record(*labels, time.monotonic() - started, outcome)
            except Exception as e:
                logger.warning(f"Could not record a duration metric: {e!r}")


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


#: Outcome of an MCP tool call that reached a server. `timeout` is the only
#: one the health verdict counts (`calls_failing`); `app_error` is an error
#: RESULT (isError / inner envelope), i.e. the server answered; `error` is an
#: exception from the call, a died session included.
MCP_TOOL_OUTCOMES = ("ok", "app_error", "timeout", "error")


def record_mcp_tool_call(server: str, duration: float, outcome: str):
    """Record one dispatched MCP tool call.

    `server` is the configured server name (config/mcp_servers.yaml), never a
    name taken from a tool call the model wrote.
    """
    if not _metrics_initialized:
        return
    _mcp_tool_duration_seconds.labels(
        server=server,
        outcome=_bounded(outcome, MCP_TOOL_OUTCOMES, "error"),
    ).observe(duration)


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


#: Every label value of renfield_agent_outcome_total (#1381). Set in
#: `services/agent_service.py`: `final_answer` / `aborted` by
#: `_record_turn_metrics`, `cancelled` / `error` by run(), the rest by the exit
#: paths, and `summary_<trigger>` by `_build_summary_answer`. Primed at startup.
AGENT_SUMMARY_TRIGGERS = (
    "timeout_total",
    "circuit_open",
    "timeout_step",
    "parse_failed",
    "invalid_tools",
    "repeated_empty",
    "loop_detected",
    "same_tool",
    "max_steps",
)
AGENT_OUTCOMES = (
    "final_answer",
    "aborted",
    "cancelled",
    "error",
    "llm_error",
    "empty_response",
    "parse_failed_no_results",
    *(f"summary_{trigger}" for trigger in AGENT_SUMMARY_TRIGGERS),
)


def record_agent_outcome(outcome: str):
    """Record how an agent run ended (one of `AGENT_OUTCOMES`)."""
    if not _metrics_initialized:
        return
    _agent_outcome_total.labels(outcome=outcome).inc()


def _prime_agent_outcome() -> None:
    """Export a 0 sample per outcome (see `_prime_llm_response_truncated`)."""
    for outcome in AGENT_OUTCOMES:
        _agent_outcome_total.labels(outcome=outcome)


#: Every kind `record_agent_schema_repair` is called with. Primed at startup
#: so the first repair of a process is an increase from 0, not a new series.
AGENT_SCHEMA_REPAIR_KINDS = ("answer_without_action",)


def record_agent_schema_repair(kind: str):
    """Record an agent response accepted after repairing its JSON shape (#1381)."""
    if not _metrics_initialized:
        return
    _agent_schema_repairs_total.labels(kind=kind).inc()


def _prime_agent_schema_repairs() -> None:
    """Export a 0 sample per repair kind (see `_prime_llm_response_truncated`)."""
    for kind in AGENT_SCHEMA_REPAIR_KINDS:
        _agent_schema_repairs_total.labels(kind=kind)


#: Label values of renfield_voice_empty_turn_total. Their meaning is
#: documented where they are decided, `ha_glue/services/voice_empty_turn.py`;
#: the vocabulary lives here because platform code must not import ha_glue.
VOICE_EMPTY_TURN_KINDS = (
    "no_audio",
    "stt_error",
    "vad_floor",
    "vad_heard_speech",
    "cap_reached",
    "button",
    "unknown_end",
)
VOICE_EMPTY_TURN_SOURCES = ("satellite", "device")


def record_voice_empty_turn(source: str, kind: str):
    """Record a voice turn that ended without text (#1386)."""
    if not _metrics_initialized:
        return
    _voice_empty_turn_total.labels(source=source, kind=kind).inc()


def _prime_voice_empty_turn() -> None:
    """Export a 0 sample per source and kind (see `_prime_llm_response_truncated`)."""
    for source in VOICE_EMPTY_TURN_SOURCES:
        for kind in VOICE_EMPTY_TURN_KINDS:
            _voice_empty_turn_total.labels(source=source, kind=kind)


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


# The call types the agent loop records truncation for (see
# agent_service._warn_if_truncated call sites). Primed at startup below.
LLM_TRUNCATION_CALL_TYPES = ("agent_step", "agent_retry", "agent_summary")


def _prime_llm_response_truncated() -> None:
    """Export a 0 sample for the agent model's truncation series at startup.

    A labelled Counter emits no series until a label set is first used
    ("Metrics with labels are not initialized when declared … It is
    recommended to initialize the label values by calling the .labels()
    method alone" — prometheus client_python, Labels). So the first cut of a
    process created its series AT 1, and `increase()` over a window cannot
    see a 0→1 step it never sampled: RevaAnswerTruncated
    (`increase(...[30m]) > 0`) was blind to the first truncation of every
    pod. Measured in Reva prod on 2026-09-28 — a logged agent_summary cut
    showed up as an increase of 0.

    Only the DEFAULT agent model is primed; a role with its own model still
    creates its series on first use. That leaves the gap for those roles, but
    priming guesses would export series for models that never run.
    """
    from utils.llm_client import default_agent_model

    model = default_agent_model()
    if not model:
        return
    for call_type in LLM_TRUNCATION_CALL_TYPES:
        _llm_response_truncated_total.labels(model=model, call_type=call_type)


def record_llm_response_truncated(model: str, call_type: str):
    """Record a completion that hit the output-token cap (finish_reason=length)."""
    if not _metrics_initialized:
        return
    _llm_response_truncated_total.labels(model=model, call_type=call_type).inc()


def record_router_fallback(reason: str):
    """Record an LLM router classification that fell back to 'general'.

    ``reason`` is one of ``timeout``, ``error`` or ``invalid_role``.
    """
    if not _metrics_initialized:
        return
    _router_fallback_total.labels(reason=reason).inc()


def record_embedding_error(model: str):
    """Record an embedding request that raised."""
    if not _metrics_initialized:
        return
    _embedding_errors_total.labels(model=model or "").inc()


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


class WebSocketGaugeMiddleware:
    """Keep renfield_websocket_connections in step with the open sockets.

    Pure ASGI and the ONE place the gauge moves: it counts a connection when
    the application accepts it and releases it when the endpoint returns, by
    whatever path (clean close, client drop, exception, cancellation). A
    handshake that is refused (auth, origin) is never counted. Installed by
    `setup_metrics` only, so with metrics disabled it is not in the stack.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "websocket":
            await self.app(scope, receive, send)
            return

        ws_type = WEBSOCKET_TYPES.get(scope.get("path", ""), WEBSOCKET_TYPE_OTHER)
        accepted = False

        async def counting_send(message: dict) -> None:
            nonlocal accepted
            if message.get("type") == "websocket.accept" and not accepted:
                accepted = True
                self._record(record_websocket_connect, ws_type)
            await send(message)

        try:
            await self.app(scope, receive, counting_send)
        finally:
            if accepted:
                self._record(record_websocket_disconnect, ws_type)

    @staticmethod
    def _record(record: Callable[[str], None], ws_type: str) -> None:
        try:
            record(ws_type)
        except Exception as e:
            logger.warning(f"Could not record a WebSocket connection metric: {e!r}")


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

    try:
        _prime_llm_response_truncated()
    except Exception as e:
        # Priming must never block startup. WARNING, not debug: a silently
        # unprimed counter is exactly the blind spot the priming exists to close.
        logger.warning(f"Could not prime renfield_llm_response_truncated_total: {e!r}")

    _prime_agent_outcome()
    _prime_agent_schema_repairs()
    _prime_voice_empty_turn()
    _prime_websocket_connections()

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
    app.add_middleware(WebSocketGaugeMiddleware)

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
