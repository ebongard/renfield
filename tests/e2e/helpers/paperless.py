"""Paperless-NGX API client for post-upload state assertions.

Exists so tests can answer the question that a pure-UI smoke test
can't: "the UI said the document was uploaded — is it ACTUALLY in
Paperless, and did it land with the correspondent / document_type /
tags the agent was supposed to extract?"

The 2026-04-24 extractor regression (PR #467) passed every smoke test
we had — the UI dutifully showed "erfolgreich hochgeladen" while the
document hit Paperless stripped of all metadata. This module makes
that class of bug fail a test instead of a user.

Configuration: reads `PAPERLESS_API_URL` + `PAPERLESS_API_TOKEN` from
the env (same names the backend uses). Tests skip cleanly when the
token isn't available, so CI without a Paperless instance does not
false-fail.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

URL = os.environ.get("PAPERLESS_API_URL") or os.environ.get("PAPERLESS_URL", "")
TOKEN = os.environ.get("PAPERLESS_API_TOKEN", "")


def require_paperless() -> None:
    """Skip the current test if Paperless isn't reachable from the
    runner. Keeps the suite runnable in environments without Paperless
    (e.g. a PR reviewer's laptop) without silently passing."""
    if not URL or not TOKEN:
        pytest.skip("PAPERLESS_API_URL / PAPERLESS_API_TOKEN not set")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=URL,
        headers={"Authorization": f"Token {TOKEN}"},
        timeout=30.0,
        verify=False,     # noqa: S501 — may be http:// or self-signed
    )


def list_documents(*, query: str | None = None,
                   ordering: str = "-created",
                   page_size: int = 25) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"ordering": ordering, "page_size": page_size}
    if query:
        params["query"] = query
    with _client() as c:
        r = c.get("/api/documents/", params=params)
        r.raise_for_status()
        return r.json().get("results", [])


def get_document(doc_id: int) -> dict[str, Any]:
    with _client() as c:
        r = c.get(f"/api/documents/{doc_id}/")
        r.raise_for_status()
        return r.json()


def find_document_by_title(title: str, *, timeout_s: float = 20.0,
                            poll_interval_s: float = 1.0) -> dict[str, Any] | None:
    """Poll Paperless's search endpoint until a document with `title`
    appears, or timeout. The consumer may take a second or three to
    ingest the upload and produce the document row."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for doc in list_documents(query=title, page_size=10):
            if doc.get("title", "").strip() == title.strip():
                return doc
        time.sleep(poll_interval_s)
    return None


def delete_document(doc_id: int) -> None:
    with _client() as c:
        r = c.delete(f"/api/documents/{doc_id}/")
        r.raise_for_status()
