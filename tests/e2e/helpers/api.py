"""Backend API client for downstream-state assertions.

Pure UI-render checks miss bugs like the 2026-04-24 Paperless extractor
regression (UI says "uploaded", Paperless has no correspondent /
document_type / tags). Tests that call the backend REST API to assert
the post-state are the remedy — the UI test drives the action, this
module checks the real result landed.

Base URL is https://renfield.local; HTTPS is self-signed so we allow
insecure verification. Auth is optional today (AUTH_ENABLED=false in
most deploys); when auth lands, attach a bearer via RENFIELD_TEST_TOKEN.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

BASE_URL = "https://renfield.local"
_TOKEN_ENV = "RENFIELD_TEST_TOKEN"

_HEADERS = {}
if os.environ.get(_TOKEN_ENV):
    _HEADERS["Authorization"] = f"Bearer {os.environ[_TOKEN_ENV]}"


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        verify=False,     # noqa: S501 — self-signed cert, intentional
        headers=_HEADERS,
        timeout=30.0,
    )


# --- Conversations -----------------------------------------------------

def list_conversations(*, limit: int = 10) -> list[dict[str, Any]]:
    """Return recent conversations (newest first)."""
    with _client() as c:
        r = c.get("/api/conversations", params={"limit": limit})
        r.raise_for_status()
        return r.json()


def get_conversation(session_id: str) -> dict[str, Any]:
    with _client() as c:
        r = c.get(f"/api/conversations/{session_id}")
        r.raise_for_status()
        return r.json()


def delete_conversation(session_id: str) -> None:
    with _client() as c:
        r = c.delete(f"/api/conversations/{session_id}")
        r.raise_for_status()


# --- Knowledge base ----------------------------------------------------

def list_knowledge_bases() -> list[dict[str, Any]]:
    with _client() as c:
        r = c.get("/api/knowledge/bases")
        r.raise_for_status()
        return r.json()


def list_documents(*, knowledge_base_id: int | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    if knowledge_base_id is not None:
        params["knowledge_base_id"] = knowledge_base_id
    with _client() as c:
        r = c.get("/api/knowledge/documents", params=params)
        r.raise_for_status()
        return r.json()


def delete_document(doc_id: int) -> None:
    with _client() as c:
        r = c.delete(f"/api/knowledge/documents/{doc_id}")
        r.raise_for_status()


# --- Health ------------------------------------------------------------

def health_ready() -> dict[str, Any]:
    with _client() as c:
        r = c.get("/health/ready")
        r.raise_for_status()
        return r.json()
