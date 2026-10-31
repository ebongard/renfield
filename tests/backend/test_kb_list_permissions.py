"""
GET /api/knowledge/bases and the KB search gates, under the REAL
`has_permission` and the real role permission sets.

Regression: `list_knowledge_bases` denied with
`has_permission(perms, Permission.KB_NONE)`. Every higher KB rung implies
kb.none (`PERMISSION_HIERARCHY`), so that matched every authenticated
non-admin — the Familie role (kb.shared) got an empty list. The earlier unit
test masked it by monkeypatching `has_permission` to reject KB_NONE.

Also holds the structural guard: no bottom rung (`Permission.*_NONE`) may be
passed to `has_permission` & co. anywhere in the backend — such a check is
true for every user above the bottom rung and therefore never a deny gate.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from models.permissions import (
    DEFAULT_ROLES,
    PERMISSION_HIERARCHY,
    Permission,
    holds_no_rung_above,
)

ASKER_ID = 42
OTHER_ID = 99

OWN_KB = 1
PUBLIC_KB = 2
SHARED_KB = 3
PRIVATE_KB = 4
OWNERLESS_KB = 5


def _role_permissions(name: str) -> list[str]:
    for role in DEFAULT_ROLES:
        if role["name"] == name:
            return list(role["permissions"])
    raise AssertionError(f"no default role {name!r}")


# A kb.own user: no default role carries kb.own, so it is a custom role.
KB_OWN_PERMISSIONS = [Permission.KB_OWN.value, Permission.CHAT_OWN.value]


def _kb(kb_id: int, owner_id: int | None, is_public: bool) -> SimpleNamespace:
    # SimpleNamespace (not MagicMock): Pydantic validates the response fields.
    return SimpleNamespace(
        id=kb_id, name=f"kb{kb_id}", description="", is_active=True,
        is_public=is_public, owner_id=owner_id,
        created_at=None, updated_at=None,
    )


@pytest.fixture
def kb_route(monkeypatch):
    """The route module with auth on and the explicit-share lookups faked:
    the asker holds a document-level grant on SHARED_KB only."""
    from api.routes import knowledge as route_mod
    from services import kb_shares_service

    async def fake_shared_ids(db, user_id):
        return {SHARED_KB} if user_id == ASKER_ID else set()

    async def fake_levels(db, user_id, kb_ids=None):
        if user_id != ASKER_ID:
            return {}
        return {SHARED_KB: "read"} if kb_ids is None or SHARED_KB in kb_ids else {}

    monkeypatch.setattr(kb_shares_service, "list_user_shared_kb_ids", fake_shared_ids)
    monkeypatch.setattr(kb_shares_service, "get_user_kb_permission_levels", fake_levels)
    monkeypatch.setattr(route_mod.settings, "auth_enabled", True)
    return route_mod


async def _list_ids(route_mod, permissions: list[str]) -> set[int]:
    fake_rag = MagicMock()
    fake_rag.list_knowledge_bases = AsyncMock(return_value=[
        _kb(OWN_KB, ASKER_ID, is_public=False),
        _kb(PUBLIC_KB, OTHER_ID, is_public=True),
        _kb(SHARED_KB, OTHER_ID, is_public=False),
        _kb(PRIVATE_KB, OTHER_ID, is_public=False),
        _kb(OWNERLESS_KB, None, is_public=False),
    ])
    owners = MagicMock()
    owners.all = lambda: []
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=owners)
    user = SimpleNamespace(id=ASKER_ID, get_permissions=lambda: permissions)

    result = await route_mod.list_knowledge_bases(rag=fake_rag, user=user, db=fake_db)
    return {kb.id for kb in result}


@pytest.mark.asyncio
@pytest.mark.unit
class TestListKnowledgeBasesByRole:
    async def test_familie_kb_shared_sees_own_public_and_shared(self, kb_route):
        ids = await _list_ids(kb_route, _role_permissions("Familie"))
        assert ids == {OWN_KB, PUBLIC_KB, SHARED_KB}

    async def test_kb_own_sees_own_and_shared_but_not_public(self, kb_route):
        ids = await _list_ids(kb_route, KB_OWN_PERMISSIONS)
        assert ids == {OWN_KB, SHARED_KB}

    async def test_gast_kb_none_sees_nothing(self, kb_route):
        assert await _list_ids(kb_route, _role_permissions("Gast")) == set()

    async def test_role_without_any_kb_rung_sees_nothing(self, kb_route):
        # Kiosk carries no KB rung at all: fail-closed, same as kb.none.
        assert await _list_ids(kb_route, _role_permissions("Kiosk")) == set()

    async def test_admin_kb_all_sees_everything(self, kb_route):
        ids = await _list_ids(kb_route, _role_permissions("Admin"))
        assert ids == {OWN_KB, PUBLIC_KB, SHARED_KB, PRIVATE_KB, OWNERLESS_KB}


def _search_calls(route_mod, permissions: list[str], fake_rag):
    """One coroutine factory per search endpoint (POST and GET)."""
    from api.routes.knowledge_schemas import SearchRequest

    user = SimpleNamespace(id=ASKER_ID, get_permissions=lambda: permissions)
    return [
        lambda: route_mod.search_knowledge(
            request=SearchRequest(query="x"), rag=fake_rag, user=user, db=MagicMock(),
        ),
        lambda: route_mod.search_knowledge_get(
            q="x", top_k=5, knowledge_base_id=None, threshold=None,
            rag=fake_rag, user=user, db=MagicMock(),
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.unit
class TestSearchGateByRole:
    """Both search endpoints deny before touching RAG when the asker holds
    no rung above kb.none."""

    @pytest.mark.parametrize("role", ["Gast", "Kiosk"])
    async def test_no_kb_rung_is_denied(self, kb_route, role):
        fake_rag = MagicMock()
        fake_rag.search = AsyncMock(return_value=[])
        for call in _search_calls(kb_route, _role_permissions(role), fake_rag):
            with pytest.raises(HTTPException) as exc:
                await call()
            assert exc.value.status_code == 403
        fake_rag.search.assert_not_called()

    @pytest.mark.parametrize("permissions", [
        _role_permissions("Familie"), KB_OWN_PERMISSIONS, _role_permissions("Admin"),
    ], ids=["familie", "kb_own", "admin"])
    async def test_kb_rung_passes_the_gate(self, kb_route, permissions):
        fake_rag = MagicMock()
        fake_rag.search = AsyncMock(return_value=[])
        for call in _search_calls(kb_route, permissions, fake_rag):
            await call()
        assert fake_rag.search.await_count == 2
        assert all(c.kwargs["user_id"] == ASKER_ID for c in fake_rag.search.await_args_list)


@pytest.mark.unit
class TestHoldsNoRungAbove:
    @pytest.mark.parametrize("bottom,above", [
        (Permission.KB_NONE, [Permission.KB_OWN, Permission.KB_SHARED, Permission.KB_ALL]),
        (Permission.HA_NONE, [Permission.HA_READ, Permission.HA_CONTROL, Permission.HA_FULL]),
        (Permission.CAM_NONE, [Permission.CAM_VIEW, Permission.CAM_FULL]),
    ])
    def test_every_ladder(self, bottom, above):
        assert holds_no_rung_above([bottom.value], bottom) is True
        assert holds_no_rung_above([], bottom) is True
        for perm in above:
            assert holds_no_rung_above([perm.value], bottom) is False, perm


# --- Structural guard -------------------------------------------------------

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
PERMISSION_CHECKS = {"has_permission", "has_any_permission", "has_all_permissions"}
BOTTOM_RUNGS = {
    perm.name
    for perm in PERMISSION_HIERARCHY
    if not PERMISSION_HIERARCHY[perm]
    and any(perm in implied for implied in PERMISSION_HIERARCHY.values())
    and perm.name.endswith("_NONE")
}


def _bottom_rung_checks(tree: ast.AST) -> list[tuple[int, str]]:
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in PERMISSION_CHECKS:
            continue
        for arg in ast.walk(ast.Module(body=[ast.Expr(a) for a in node.args[1:]], type_ignores=[])):
            if (
                isinstance(arg, ast.Attribute)
                and arg.attr in BOTTOM_RUNGS
                and isinstance(arg.value, ast.Name)
                and arg.value.id == "Permission"
            ):
                hits.append((node.lineno, arg.attr))
    return hits


@pytest.mark.unit
def test_bottom_rungs_are_known():
    assert {"KB_NONE", "HA_NONE", "CAM_NONE"} <= BOTTOM_RUNGS


@pytest.mark.unit
def test_detector_catches_a_bottom_rung_check():
    # Negative control: the scanner must flag the exact shape of the bug.
    tree = ast.parse("if has_permission(perms, Permission.KB_NONE):\n    pass\n")
    assert _bottom_rung_checks(tree) == [(1, "KB_NONE")]
    assert _bottom_rung_checks(ast.parse('"""has_permission(p, Permission.KB_NONE)"""')) == []


@pytest.mark.unit
def test_no_permission_check_against_a_bottom_rung():
    """`has_permission(perms, Permission.X_NONE)` is true for every user above
    the bottom rung — use `holds_no_rung_above` as the deny gate instead."""
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        for lineno, rung in _bottom_rung_checks(ast.parse(path.read_text(), filename=str(path))):
            offenders.append(f"{path.relative_to(BACKEND)}:{lineno} ({rung})")
    assert offenders == []
