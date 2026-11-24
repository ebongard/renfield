"""Prometheus metrics for the voice-server (#1642).

Dark by default. With ``METRICS_ENABLED=false`` nothing is created, no port is
bound, and every ``record_*``/``observe`` call returns at once, so the flag-off
path does exactly what it did before.

With the flag on, ``start_exporter()`` serves the metrics on their OWN port
(``METRICS_PORT``, default 9100), not on the API port 8080. The external
Ingress routes the WHOLE path ``/`` of port 8080 (see the private_k8s
``voice-server/40-ingress.yaml``), so a ``/metrics`` route there would be
reachable from outside the cluster. The metrics port is in no Service an
Ingress points at, and a NetworkPolicy admits only the Prometheus agent.

CONTENT-FREE: labels carry only a pass, an outcome, a configured voice name or
a component name. Never a transcript, a user, a device, a client or a speaker.

The voice-server is shared by several products (household, xidra, Reva), so
these series describe the server as a whole. The prefix is ``voice_server_``,
not ``renfield_``.

A failure while recording is logged and swallowed. A metric must never break
a voice turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from voice_server.config import settings

logger = logging.getLogger(__name__)

#: Outcomes of a timed call. ``cancelled`` is a barge-in or a closed session;
#: it is not an error and must not show up as one.
OUTCOMES = ("ok", "error", "cancelled")

#: STT passes: ``partial`` is the in-flight pass while the user is still
#: speaking, ``final`` the strict pass whose text is used.
STT_PASSES = ("partial", "final")

#: Components whose warm/ready state can be exported. ``meeting`` only when
#: MEETING_ENABLED is on: off, it never warms up, and a permanent 0 would read
#: as "broken" to any alert on this gauge.
COMPONENTS = ("stt", "speaker", "meeting")


def exported_components() -> tuple[str, ...]:
    """The components this configuration exports a ready value for."""
    if settings.meeting_enabled:
        return COMPONENTS
    return tuple(c for c in COMPONENTS if c != "meeting")

_registry: Any = None
_stt_duration: Any = None
_tts_sentence_duration: Any = None
_speaker_embed_duration: Any = None
_sessions_active: Any = None
_sessions_rejected: Any = None
_component_ready: Any = None


def enabled() -> bool:
    """True once ``init_metrics()`` created the metrics."""
    return _registry is not None


def init_metrics() -> bool:
    """Create the metrics in a private registry. Idempotent.

    Returns False when ``METRICS_ENABLED`` is off, without importing
    prometheus_client.
    """
    global _registry, _stt_duration, _tts_sentence_duration
    global _speaker_embed_duration, _sessions_active, _sessions_rejected
    global _component_ready

    if not settings.metrics_enabled:
        return False
    if _registry is not None:
        return True

    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

    registry = CollectorRegistry()
    _stt_duration = Histogram(
        "voice_server_stt_duration_seconds",
        "Duration of one faster-whisper pass over the buffered audio",
        ["pass", "outcome"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60),
        registry=registry,
    )
    _tts_sentence_duration = Histogram(
        "voice_server_tts_sentence_duration_seconds",
        "Duration of the Piper synthesis of one sentence",
        ["voice", "outcome"],
        buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8),
        registry=registry,
    )
    _speaker_embed_duration = Histogram(
        "voice_server_speaker_embed_duration_seconds",
        "Duration of one ECAPA speaker embedding",
        ["outcome"],
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
        registry=registry,
    )
    _sessions_active = Gauge(
        "voice_server_sessions_active",
        "Open /ws/voice sessions",
        registry=registry,
    )
    _sessions_rejected = Counter(
        "voice_server_sessions_rejected_total",
        "/ws/voice sessions refused because the server was at capacity",
        ["reason"],
        registry=registry,
    )
    _component_ready = Gauge(
        "voice_server_component_ready",
        "1 when the component finished its warmup, 0 when it did not",
        ["component"],
        registry=registry,
    )

    # Prime every bounded label combination so a panel shows 0 instead of
    # "No data" before the first event. A labelled series that has never been
    # observed is not exported at all, which looks exactly like a broken scrape.
    for p in STT_PASSES:
        for o in OUTCOMES:
            _stt_duration.labels(**{"pass": p, "outcome": o})
    for voice in sorted({settings.piper_default_voice_de, settings.piper_default_voice_en}):
        for o in OUTCOMES:
            _tts_sentence_duration.labels(voice=voice, outcome=o)
    for o in OUTCOMES:
        _speaker_embed_duration.labels(outcome=o)
    _sessions_rejected.labels(reason="capacity")
    for c in exported_components():
        _component_ready.labels(component=c).set(0)

    _registry = registry
    return True


def start_exporter() -> Any:
    """Create the metrics and serve them on ``METRICS_PORT``.

    Returns the HTTP server, which the caller shuts down, or None when the flag
    is off. A bind failure raises: a pod that was told to export metrics and
    cannot is a misconfiguration and should fail its startup loudly.
    """
    if not init_metrics():
        return None
    from prometheus_client import start_http_server

    server, _thread = start_http_server(
        settings.metrics_port, addr=settings.host, registry=_registry
    )
    logger.info("metrics on :%d", settings.metrics_port)
    return server


def _outcome_of(exc: BaseException | None) -> str:
    if exc is None:
        return "ok"
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    return "error"


@contextmanager
def _timed(histogram: Any, labels: dict[str, str]) -> Iterator[None]:
    if histogram is None:
        yield
        return
    start = time.monotonic()
    exc: BaseException | None = None
    try:
        yield
    except BaseException as e:  # re-raised; only classified here
        exc = e
        raise
    finally:
        try:
            histogram.labels(outcome=_outcome_of(exc), **labels).observe(
                time.monotonic() - start
            )
        except Exception:  # a metric must never break a voice turn
            logger.warning("could not record a voice-server metric", exc_info=True)


def observe_stt(*, partial: bool):
    """Time one STT pass: ``with observe_stt(partial=...): ...``."""
    return _timed(_stt_duration, {"pass": "partial" if partial else "final"})


def observe_tts_sentence(voice: str):
    """Time the synthesis of one sentence with the given configured voice."""
    return _timed(_tts_sentence_duration, {"voice": voice})


def observe_speaker_embed():
    """Time one speaker embedding."""
    return _timed(_speaker_embed_duration, {})


def set_sessions_active(n: int) -> None:
    if _sessions_active is None:
        return
    try:
        _sessions_active.set(n)
    except Exception:
        logger.warning("could not record a voice-server metric", exc_info=True)


def record_session_rejected(reason: str) -> None:
    if _sessions_rejected is None:
        return
    try:
        _sessions_rejected.labels(reason=reason).inc()
    except Exception:
        logger.warning("could not record a voice-server metric", exc_info=True)


def set_component_ready(component: str, ready: bool) -> None:
    if _component_ready is None or component not in exported_components():
        return
    try:
        _component_ready.labels(component=component).set(1 if ready else 0)
    except Exception:
        logger.warning("could not record a voice-server metric", exc_info=True)


def _reset_for_tests() -> None:
    """Drop the metrics so a test can init them again with other settings."""
    global _registry, _stt_duration, _tts_sentence_duration
    global _speaker_embed_duration, _sessions_active, _sessions_rejected
    global _component_ready
    _registry = _stt_duration = _tts_sentence_duration = None
    _speaker_embed_duration = _sessions_active = _sessions_rejected = None
    _component_ready = None
