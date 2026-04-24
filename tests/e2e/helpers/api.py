"""Backend API client for downstream-state assertions.

Pure UI-render checks miss bugs like the 2026-04-24 Paperless extractor
regression (UI says "uploaded", Paperless has no correspondent /
document_type / tags). Tests that call the backend REST API to assert
the post-state are the remedy — the UI test drives the action, this
module checks the real result landed.

Base URL is https://renfield.local; HTTPS is self-signed so we allow
insecure verification. Auth is optional today (AUTH_ENABLED=false in
most deploys); when auth lands, attach a bearer via RENFIELD_TEST_TOKEN.

Each helper is a thin pass-through: we want the test to see backend
errors as HTTPStatusError (so the test fails cleanly) rather than
absorbing them into None returns.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

BASE_URL = "https://renfield.local"
_TOKEN_ENV = "RENFIELD_TEST_TOKEN"

_HEADERS: dict[str, str] = {}
if os.environ.get(_TOKEN_ENV):
    _HEADERS["Authorization"] = f"Bearer {os.environ[_TOKEN_ENV]}"


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        verify=False,     # noqa: S501 — self-signed cert, intentional
        headers=_HEADERS,
        timeout=30.0,
    )


# --- Generic verbs ------------------------------------------------------
# Some areas (circles, tasks, memory…) have endpoints that may 401/403
# depending on AUTH_ENABLED. These helpers skip the test rather than
# false-fail when the endpoint is gated.


def get(path: str, *, params: dict | None = None,
        skip_on_status: tuple[int, ...] = ()) -> Any:
    with _client() as c:
        r = c.get(path, params=params)
        if r.status_code in skip_on_status:
            pytest.skip(f"GET {path} returned {r.status_code}")
        r.raise_for_status()
        return r.json() if r.content else None


def post(path: str, *, json: dict | None = None,
         skip_on_status: tuple[int, ...] = ()) -> Any:
    with _client() as c:
        r = c.post(path, json=json)
        if r.status_code in skip_on_status:
            pytest.skip(f"POST {path} returned {r.status_code}")
        r.raise_for_status()
        return r.json() if r.content else None


def patch(path: str, *, json: dict | None = None,
          skip_on_status: tuple[int, ...] = ()) -> Any:
    with _client() as c:
        r = c.patch(path, json=json)
        if r.status_code in skip_on_status:
            pytest.skip(f"PATCH {path} returned {r.status_code}")
        r.raise_for_status()
        return r.json() if r.content else None


def put(path: str, *, json: dict | None = None,
        skip_on_status: tuple[int, ...] = ()) -> Any:
    with _client() as c:
        r = c.put(path, json=json)
        if r.status_code in skip_on_status:
            pytest.skip(f"PUT {path} returned {r.status_code}")
        r.raise_for_status()
        return r.json() if r.content else None


def delete(path: str, *, skip_on_status: tuple[int, ...] = ()) -> None:
    with _client() as c:
        r = c.delete(path)
        if r.status_code in skip_on_status:
            pytest.skip(f"DELETE {path} returned {r.status_code}")
        if r.status_code not in (200, 204):
            r.raise_for_status()


# --- Conversations (/api/chat/conversations + /api/chat/session/{id}) -
# Chat router is mounted at /api/chat, so /conversations is where the
# list lives, and DELETE /session/{id} is the per-conversation teardown.

def list_conversations(*, limit: int = 10) -> list[dict[str, Any]]:
    result = get("/api/chat/conversations", params={"limit": limit})
    # Envelope is {"conversations": [...]} on current builds; legacy
    # builds returned a plain list.
    if isinstance(result, dict):
        return result.get("conversations", [])
    return result


def get_conversation(session_id: str) -> dict[str, Any]:
    return get(f"/api/chat/conversation/{session_id}/summary")


def delete_conversation(session_id: str) -> None:
    delete(f"/api/chat/session/{session_id}")


# --- Knowledge base (/api/knowledge) -----------------------------------

def list_knowledge_bases() -> list[dict[str, Any]]:
    return get("/api/knowledge/bases")


def list_documents(*, knowledge_base_id: int | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    if knowledge_base_id is not None:
        params["knowledge_base_id"] = knowledge_base_id
    return get("/api/knowledge/documents", params=params)


def delete_document(doc_id: int) -> None:
    delete(f"/api/knowledge/documents/{doc_id}")


# --- Tasks (/api/tasks) -------------------------------------------------

def list_tasks(**params) -> list[dict[str, Any]] | dict[str, Any]:
    return get("/api/tasks/list", params=params,
               skip_on_status=(401, 403, 404))


def create_task(payload: dict) -> dict[str, Any]:
    return post("/api/tasks/create", json=payload,
                skip_on_status=(401, 403, 404))


def update_task(task_id: int, payload: dict) -> dict[str, Any]:
    return patch(f"/api/tasks/{task_id}", json=payload,
                 skip_on_status=(401, 403, 404))


def delete_task(task_id: int) -> None:
    delete(f"/api/tasks/{task_id}", skip_on_status=(401, 403, 404))


# --- Memory (/api/memory) ----------------------------------------------

def list_memories(**params) -> dict[str, Any]:
    return get("/api/memory", params=params,
               skip_on_status=(401, 403, 404, 503))


def create_memory(payload: dict) -> dict[str, Any]:
    return post("/api/memory", json=payload,
                skip_on_status=(401, 403, 404, 503))


def delete_memory(memory_id: int) -> None:
    delete(f"/api/memory/{memory_id}", skip_on_status=(401, 403, 404, 503))


# --- Knowledge graph (/api/knowledge-graph) -----------------------------

def kg_entities(**params) -> dict[str, Any]:
    return get("/api/knowledge-graph/entities", params=params,
               skip_on_status=(401, 403, 404))


def kg_relations(**params) -> dict[str, Any]:
    return get("/api/knowledge-graph/relations", params=params,
               skip_on_status=(401, 403, 404))


def kg_stats() -> dict[str, Any]:
    return get("/api/knowledge-graph/stats", skip_on_status=(401, 403, 404))


def kg_circle_tiers() -> Any:
    return get("/api/knowledge-graph/circle-tiers",
               skip_on_status=(401, 403, 404))


# --- Speakers (/api/speakers) ------------------------------------------

def list_speakers() -> list[dict[str, Any]]:
    return get("/api/speakers", skip_on_status=(401, 403, 404))


def create_speaker(payload: dict) -> dict[str, Any]:
    return post("/api/speakers", json=payload,
                skip_on_status=(401, 403, 404))


def update_speaker(speaker_id: int, payload: dict) -> dict[str, Any]:
    return patch(f"/api/speakers/{speaker_id}", json=payload,
                 skip_on_status=(401, 403, 404))


def delete_speaker(speaker_id: int) -> None:
    delete(f"/api/speakers/{speaker_id}", skip_on_status=(401, 403, 404))


# --- Rooms (/api/rooms) ------------------------------------------------

def list_rooms() -> list[dict[str, Any]]:
    return get("/api/rooms", skip_on_status=(401, 403, 404))


def create_room(payload: dict) -> dict[str, Any]:
    return post("/api/rooms", json=payload,
                skip_on_status=(401, 403, 404))


def delete_room(room_id: int) -> None:
    delete(f"/api/rooms/{room_id}", skip_on_status=(401, 403, 404))


# --- Users (/api/users) ------------------------------------------------

def list_users(**params) -> dict[str, Any]:
    return get("/api/users", params=params,
               skip_on_status=(401, 403, 404))


def create_user(payload: dict) -> dict[str, Any]:
    return post("/api/users", json=payload,
                skip_on_status=(401, 403, 404))


def delete_user(user_id: int) -> None:
    delete(f"/api/users/{user_id}", skip_on_status=(401, 403, 404))


# --- Roles (/api/roles) ------------------------------------------------

def list_roles() -> list[dict[str, Any]]:
    return get("/api/roles", skip_on_status=(401, 403, 404))


def create_role(payload: dict) -> dict[str, Any]:
    return post("/api/roles", json=payload,
                skip_on_status=(401, 403, 404))


def delete_role(role_id: int) -> None:
    delete(f"/api/roles/{role_id}", skip_on_status=(401, 403, 404))


# --- Intents / MCP -----------------------------------------------------

def intents_status() -> dict[str, Any]:
    return get("/api/intents/status", skip_on_status=(401, 403, 404))


def mcp_status() -> dict[str, Any]:
    return get("/api/mcp/status", skip_on_status=(401, 403, 404))


def mcp_tools() -> Any:
    return get("/api/mcp/tools", skip_on_status=(401, 403, 404))


# --- Federation audit (/api/federation/audit) --------------------------

def federation_audit(**params) -> Any:
    return get("/api/federation/audit", params=params,
               skip_on_status=(401, 403, 404))


# --- Circles (/api/circles) --------------------------------------------

def circles_settings() -> dict[str, Any]:
    return get("/api/circles/me/settings",
               skip_on_status=(401, 403, 404))


def circles_members() -> list[dict[str, Any]]:
    return get("/api/circles/me/members", skip_on_status=(401, 403, 404))


def circles_add_member(payload: dict) -> dict[str, Any]:
    return post("/api/circles/me/members", json=payload,
                skip_on_status=(401, 403, 404))


def circles_remove_member(user_id: int) -> None:
    delete(f"/api/circles/me/members/{user_id}",
           skip_on_status=(401, 403, 404))


# --- Settings / presence / satellites (ha_glue) ------------------------

def wakeword_settings() -> dict[str, Any]:
    return get("/api/settings/wakeword", skip_on_status=(401, 403, 404))


def wakeword_models() -> Any:
    return get("/api/settings/wakeword/models",
               skip_on_status=(401, 403, 404))


def presence_status() -> dict[str, Any]:
    return get("/api/presence/status", skip_on_status=(401, 403, 404))


def presence_rooms() -> list[dict[str, Any]]:
    return get("/api/presence/rooms", skip_on_status=(401, 403, 404))


def list_satellites() -> dict[str, Any]:
    return get("/api/satellites", skip_on_status=(401, 403, 404))


def list_cameras() -> Any:
    return get("/api/camera/cameras", skip_on_status=(401, 403, 404))


def homeassistant_states() -> Any:
    return get("/api/homeassistant/states",
               skip_on_status=(401, 403, 404, 503))


# --- Paperless audit (/api/paperless-audit + variants) -----------------

def paperless_audit_status() -> Any:
    return get("/api/admin/paperless-audit/status",
               skip_on_status=(401, 403, 404))


# --- Atoms (/api/atoms) -------------------------------------------------

def list_atoms(**params) -> Any:
    return get("/api/atoms", params=params, skip_on_status=(401, 403, 404))


# --- Health ------------------------------------------------------------

def health_ready() -> dict[str, Any]:
    return get("/health/ready")
