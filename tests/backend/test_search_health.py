"""Tests for the SearXNG functional health probe (#1162).

Covers the pure verdict logic and the httpx-mocked probe entry point:
(a) healthy (>= threshold distinct general engines),
(b) degraded (below threshold),
(c) degraded when unresponsive_engines covers the backbone,
(d) unknown on timeout/error,
(e) flag-off / no searxng_api_url -> skipped (unknown, no HTTP).

httpx is mocked throughout — no real SearXNG is contacted.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from services import search_health
from services.search_health import (
    PROBE_QUERY,
    _classify,
    probe_search_functional,
)
from utils.config import settings

pytestmark = [pytest.mark.backend, pytest.mark.unit]


def _payload(*, results=None, unresponsive=None) -> dict:
    return {
        "results": results if results is not None else [],
        "unresponsive_engines": unresponsive if unresponsive is not None else [],
    }


def _install_fake_httpx(monkeypatch, *, json_payload=None, exc=None, status_code=200):
    """Replace httpx.AsyncClient used by search_health with a fake.

    ``exc`` (if given) is raised from ``get`` to simulate timeout/connect errors.
    """
    resp = MagicMock()
    resp.json.return_value = json_payload if json_payload is not None else {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=MagicMock()
        )

    client = MagicMock()
    if exc is not None:
        client.get = AsyncMock(side_effect=exc)
    else:
        client.get = AsyncMock(return_value=resp)

    @asynccontextmanager
    async def _fake_client(*args, **kwargs):
        yield client

    monkeypatch.setattr(search_health.httpx, "AsyncClient", _fake_client)
    return client


# --- pure verdict logic (_classify) ------------------------------------------


class TestClassify:
    def test_healthy_above_threshold(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        payload = _payload(
            results=[
                {"engine": "bing", "title": "a"},
                {"engines": ["google", "duckduckgo"], "title": "b"},
            ]
        )
        v = _classify(payload)
        assert v["verdict"] == "healthy"
        assert v["contributing_engines"] == 3
        assert v["unresponsive"] == []

    def test_degraded_below_threshold(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        # Only Wikipedia answered — the classic false-green outage.
        payload = _payload(results=[{"engine": "wikipedia", "title": "x"}])
        v = _classify(payload)
        assert v["verdict"] == "degraded"
        assert v["contributing_engines"] == 1

    def test_degraded_zero_engines(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_min_engines", 1)
        v = _classify(_payload(results=[]))
        assert v["verdict"] == "degraded"
        assert v["contributing_engines"] == 0

    def test_degraded_when_backbone_unresponsive(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        monkeypatch.setattr(settings, "search_functional_backbone_engines", "bing,google-cse")
        # Enough distinct engines to clear the count threshold, but the backbone is down.
        payload = _payload(
            results=[
                {"engine": "wikipedia"},
                {"engine": "wikidata"},
                {"engine": "qwant"},
            ],
            unresponsive=[["bing", "timeout"], ["google-cse", "CAPTCHA"]],
        )
        v = _classify(payload)
        assert v["verdict"] == "degraded"
        assert "bing" in v["reason"] or "google-cse" in v["reason"]
        assert set(v["unresponsive"]) == {"bing", "google-cse"}

    def test_scraper_unresponsive_with_backbone_up_is_healthy(self, monkeypatch):
        """#1162 over-sensitivity fix: a CAPTCHA-prone scraper (duckduckgo) being
        unresponsive must NOT flag degraded while the real backbone still answers —
        ddg is not in the default backbone set."""
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        monkeypatch.setattr(settings, "search_functional_backbone_engines", "bing,google-cse")
        payload = _payload(
            results=[{"engine": "bing"}, {"engine": "google-cse"}],
            unresponsive=[["duckduckgo", "CAPTCHA"]],
        )
        v = _classify(payload)
        assert v["verdict"] == "healthy"
        assert v["unresponsive"] == ["duckduckgo"]

    def test_backbone_engines_config_driven(self, monkeypatch):
        """The backbone set is read from config (ConfigMap-tunable, no release) —
        adding an engine makes its outage count; removing one makes it not."""
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        payload = _payload(
            results=[{"engine": "bing"}, {"engine": "google-cse"}],
            unresponsive=[["duckduckgo", "CAPTCHA"]],
        )
        # ddg IN the configured backbone → its outage now flags degraded
        monkeypatch.setattr(settings, "search_functional_backbone_engines", "bing,duckduckgo")
        assert _classify(payload)["verdict"] == "degraded"
        # ddg NOT in the configured backbone → healthy
        monkeypatch.setattr(settings, "search_functional_backbone_engines", "bing,google-cse")
        assert _classify(payload)["verdict"] == "healthy"

    def test_scalar_unresponsive_entries(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_min_engines", 1)
        payload = _payload(
            results=[{"engine": "bing"}, {"engine": "google"}],
            unresponsive=["startpage"],
        )
        v = _classify(payload)
        # backbone bing/google contributed, startpage unresponsive is not backbone
        assert v["verdict"] == "healthy"
        assert v["unresponsive"] == ["startpage"]

    def test_dedup_engines_across_shapes(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        payload = _payload(
            results=[
                {"engine": "Bing"},
                {"engines": ["bing", "GOOGLE"]},
            ]
        )
        v = _classify(payload)
        # case-folded + deduped -> {bing, google}
        assert v["contributing_engines"] == 2
        assert v["verdict"] == "healthy"

    def test_missing_results_key_is_degraded(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        v = _classify({})  # no results, no unresponsive
        assert v["verdict"] == "degraded"
        assert v["contributing_engines"] == 0


# --- probe entry point (httpx-mocked) ----------------------------------------


class TestProbe:
    @pytest.mark.asyncio
    async def test_flag_off_skips_probe_no_http(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_probe_enabled", False)
        monkeypatch.setattr(settings, "searxng_api_url", "http://searxng.local")

        # Any HTTP attempt would blow up — prove none is made.
        def _boom(*a, **k):
            raise AssertionError("httpx must not be called when flag is off")

        monkeypatch.setattr(search_health.httpx, "AsyncClient", _boom)

        v = await probe_search_functional()
        assert v["verdict"] == "unknown"
        assert "deaktiviert" in v["reason"]

    @pytest.mark.asyncio
    async def test_no_url_skips_probe(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_probe_enabled", True)
        monkeypatch.setattr(settings, "searxng_api_url", None)

        def _boom(*a, **k):
            raise AssertionError("httpx must not be called without a URL")

        monkeypatch.setattr(search_health.httpx, "AsyncClient", _boom)

        v = await probe_search_functional()
        assert v["verdict"] == "unknown"
        assert "URL" in v["reason"]

    @pytest.mark.asyncio
    async def test_healthy(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_probe_enabled", True)
        monkeypatch.setattr(settings, "searxng_api_url", "http://searxng.local")
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        _install_fake_httpx(
            monkeypatch,
            json_payload=_payload(
                results=[{"engine": "bing"}, {"engine": "google"}]
            ),
        )
        v = await probe_search_functional()
        assert v["verdict"] == "healthy"
        assert v["contributing_engines"] == 2

    @pytest.mark.asyncio
    async def test_degraded(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_probe_enabled", True)
        monkeypatch.setattr(settings, "searxng_api_url", "http://searxng.local")
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        _install_fake_httpx(
            monkeypatch,
            json_payload=_payload(results=[{"engine": "wikipedia"}]),
        )
        v = await probe_search_functional()
        assert v["verdict"] == "degraded"

    @pytest.mark.asyncio
    async def test_degraded_backbone_unresponsive(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_probe_enabled", True)
        monkeypatch.setattr(settings, "searxng_api_url", "http://searxng.local")
        monkeypatch.setattr(settings, "search_functional_min_engines", 2)
        _install_fake_httpx(
            monkeypatch,
            json_payload=_payload(
                results=[{"engine": "wikipedia"}, {"engine": "wikidata"}],
                unresponsive=[["bing", "CAPTCHA"], ["google", "CAPTCHA"]],
            ),
        )
        v = await probe_search_functional()
        assert v["verdict"] == "degraded"
        assert set(v["unresponsive"]) == {"bing", "google"}

    @pytest.mark.asyncio
    async def test_unknown_on_timeout(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_probe_enabled", True)
        monkeypatch.setattr(settings, "searxng_api_url", "http://searxng.local")
        _install_fake_httpx(
            monkeypatch, exc=httpx.TimeoutException("timed out")
        )
        v = await probe_search_functional()
        assert v["verdict"] == "unknown"
        assert "fehlgeschlagen" in v["reason"]

    @pytest.mark.asyncio
    async def test_unknown_on_http_error(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_probe_enabled", True)
        monkeypatch.setattr(settings, "searxng_api_url", "http://searxng.local")
        _install_fake_httpx(monkeypatch, json_payload={}, status_code=502)
        v = await probe_search_functional()
        assert v["verdict"] == "unknown"

    @pytest.mark.asyncio
    async def test_unknown_on_non_dict_json(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_probe_enabled", True)
        monkeypatch.setattr(settings, "searxng_api_url", "http://searxng.local")
        _install_fake_httpx(monkeypatch, json_payload=["not", "a", "dict"])
        v = await probe_search_functional()
        assert v["verdict"] == "unknown"

    @pytest.mark.asyncio
    async def test_probe_uses_neutral_query_and_json(self, monkeypatch):
        monkeypatch.setattr(settings, "search_functional_probe_enabled", True)
        monkeypatch.setattr(settings, "searxng_api_url", "http://searxng.local/")
        client = _install_fake_httpx(
            monkeypatch,
            json_payload=_payload(results=[{"engine": "bing"}, {"engine": "google"}]),
        )
        await probe_search_functional()
        # URL has no double slash, params request the JSON API + general category.
        args, kwargs = client.get.call_args
        assert args[0] == "http://searxng.local/search"
        assert kwargs["params"]["q"] == PROBE_QUERY
        assert kwargs["params"]["format"] == "json"
        assert kwargs["params"]["categories"] == "general"
