"""Real Prometheus collectors for tests, in a registry of their own.

`utils.metrics` builds its collectors lazily and registers them in the global
default registry, where a second initialisation in the same process raises on
the duplicate names. `live_metrics()` runs the real `_init_metrics()` against a
private `CollectorRegistry` and restores the module afterwards, so a test can
read real samples (and real label validation) without leaking state.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from unittest.mock import patch

import prometheus_client

import utils.metrics as metrics_module


def _is_collector(value: object) -> bool:
    return hasattr(value, "_labelnames") and hasattr(value, "describe")


def _module_state() -> dict[str, object]:
    """The lazily initialised slots of `utils.metrics`: the flag and every
    collector reference (None while disabled)."""
    return {
        name: value
        for name, value in vars(metrics_module).items()
        if name.startswith("_")
        and not name.startswith("__")
        and (value is None or isinstance(value, bool) or _is_collector(value))
    }


@contextmanager
def live_metrics() -> Iterator[prometheus_client.CollectorRegistry]:
    """Initialise `utils.metrics` for real, isolated; yield its registry."""
    registry = prometheus_client.CollectorRegistry()
    saved = _module_state()
    with (
        patch.object(prometheus_client, "Counter", partial(prometheus_client.Counter, registry=registry)),
        patch.object(prometheus_client, "Gauge", partial(prometheus_client.Gauge, registry=registry)),
        patch.object(prometheus_client, "Histogram", partial(prometheus_client.Histogram, registry=registry)),
    ):
        metrics_module._metrics_initialized = False
        try:
            metrics_module._init_metrics()
            assert metrics_module._metrics_initialized, "prometheus_client did not initialise"
            yield registry
        finally:
            for name, value in saved.items():
                setattr(metrics_module, name, value)


def registered_series() -> dict[str, frozenset[str]]:
    """Every series name the backend can export -> the labels it carries.

    Read from the collectors `_init_metrics()` really builds, so a renamed
    metric or a dropped label changes this map. Series names follow the
    exposition format: a counter exports `<name>_total`, a histogram
    `<name>_bucket` (with `le`), `<name>_sum` and `<name>_count`.
    """
    series: dict[str, frozenset[str]] = {}
    with live_metrics() as registry:
        collectors = [v for v in vars(metrics_module).values() if _is_collector(v)]
        # Nothing may be registered that the module does not hold a reference
        # to, or this map would silently be incomplete.
        assert len(collectors) == len(registry._collector_to_names)
        for collector in collectors:
            (described,) = collector.describe()
            labels = frozenset(collector._labelnames)
            if described.type == "counter":
                series[f"{described.name}_total"] = labels
            elif described.type == "histogram":
                series[f"{described.name}_bucket"] = labels | {"le"}
                series[f"{described.name}_sum"] = labels
                series[f"{described.name}_count"] = labels
            elif described.type == "gauge":
                series[described.name] = labels
            else:  # pragma: no cover - a new collector type must be mapped here
                raise AssertionError(f"unmapped collector type: {described.type}")
    return series


def gauge_series() -> dict[str, frozenset[str]]:
    """The series that are gauges -> their labels (a subset of
    `registered_series`). A gauge has no rate, and one without labels holds a
    single value that exists from the start."""
    with live_metrics():
        return {
            described.name: frozenset(collector._labelnames)
            for collector in vars(metrics_module).values()
            if _is_collector(collector)
            for described in collector.describe()
            if described.type == "gauge"
        }


def sample_count(registry: prometheus_client.CollectorRegistry, name: str, **labels: str) -> float:
    """The value of one sample, 0.0 when the series does not exist."""
    return registry.get_sample_value(name, labels) or 0.0


def label_values(registry: prometheus_client.CollectorRegistry, family: str, label: str) -> set[str]:
    """Every value `label` takes on the samples of the metric family `family`."""
    return {
        sample.labels[label]
        for metric in registry.collect()
        if metric.name == family
        for sample in metric.samples
        if label in sample.labels
    }
