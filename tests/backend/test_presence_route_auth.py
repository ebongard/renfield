"""Regression guard for the presence-endpoint authorization fix (audit HIGH-2).

Six presence read/analytics endpoints were reachable with NO authentication,
exposing who-is-where + movement patterns for arbitrary user_id. They must now
carry the ROOMS_READ permission dependency (the same class of data the
/debug/sightings + /analytics/timeline siblings already gate).

This introspects the endpoint signatures rather than standing up the full app —
it fails loudly if anyone drops the auth dependency again, without depending on
the (non-functional) CI HTTP-integration harness.
"""
import inspect

import pytest
from fastapi.params import Depends as DependsParam

import ha_glue.api.routes.presence as presence_routes


@pytest.mark.unit
@pytest.mark.parametrize(
    "func_name",
    [
        "get_rooms_presence",
        "get_room_presence",
        "get_user_presence",
        "get_heatmap",
        "get_predictions",
        "get_daily_summary",
    ],
)
def test_presence_read_endpoint_requires_auth_dependency(func_name):
    fn = getattr(presence_routes, func_name)
    sig = inspect.signature(fn)
    assert "current_user" in sig.parameters, (
        f"{func_name} lost its auth dependency — presence data must not be "
        "reachable unauthenticated (audit HIGH-2)"
    )
    default = sig.parameters["current_user"].default
    assert isinstance(default, DependsParam), (
        f"{func_name}.current_user must default to Depends(require_permission(...))"
    )


@pytest.mark.unit
def test_history_target_guard_blocks_cross_user_without_manage():
    """A ROOMS_READ user may not resolve another user's presence target."""
    from fastapi import HTTPException
    from models.permissions import Permission

    from ha_glue.api.routes.presence import _resolve_history_target

    class _U:
        id = 7

        def has_permission(self, p):
            return False  # ROOMS_READ only, no ROOMS_MANAGE

    # Self is fine.
    assert _resolve_history_target(7, _U()) == 7
    # Another user's id → 403.
    with pytest.raises(HTTPException) as exc:
        _resolve_history_target(99, _U())
    assert exc.value.status_code == 403

    # A ROOMS_MANAGE holder may target anyone.
    class _M:
        id = 7

        def has_permission(self, p):
            return p == Permission.ROOMS_MANAGE.value

    assert _resolve_history_target(99, _M()) == 99
