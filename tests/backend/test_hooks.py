"""Tests for the async hook system (utils/hooks.py)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.hooks import HOOK_EVENTS, clear_hooks, register_hook, run_hooks


@pytest.fixture(autouse=True)
def _isolate_hooks():
    """Ensure hooks are cleaned between tests."""
    clear_hooks()
    yield
    clear_hooks()


# --- register_hook ---


def test_register_hook():
    """register_hook accepts known events."""
    hook = AsyncMock()
    register_hook("startup", hook)
    # No assertion needed — no exception means success


def test_register_unknown_event_raises():
    """register_hook raises ValueError for unknown events."""
    with pytest.raises(ValueError, match="Unknown hook event"):
        register_hook("does_not_exist", AsyncMock())


def test_uplift_events_are_registerable():
    """Router / orchestrator uplift events must be in HOOK_EVENTS.

    These events are declared for plugins (e.g. Reva) that ship forward-
    looking handlers. Renfield does not fire them yet but they MUST be
    registerable or plugin bootstrap aborts mid-chain with ValueError,
    silently skipping every hook after the first unknown event.
    """
    uplift_events = {
        "load_entity_patterns",
        "post_routing",
        "extend_orchestrator_roles",
        "pre_orchestration",
        "post_orchestration",
        "pre_sub_agent",
        "post_sub_agent",
        "check_output",
        "filter_agent_tools",
        "agent_system_prompt",
    }
    missing = uplift_events - HOOK_EVENTS
    assert not missing, (
        f"Uplift events missing from HOOK_EVENTS: {sorted(missing)}. "
        f"Adding them unblocks plugins that register speculative handlers."
    )
    for event in uplift_events:
        register_hook(event, AsyncMock())  # must not raise


@pytest.mark.asyncio
async def test_filter_agent_tools_hook_can_narrow_registry():
    """A plugin handler on `filter_agent_tools` may mutate the registry in place.

    Mirrors the chat_handler call site: fired with `registry`, `role`, `message`
    after AgentToolRegistry.create() and before the agent loop. Return value is
    ignored; the handler restricts the tool set by mutating `registry._tools`.
    """

    class _Registry:
        def __init__(self, names):
            self._tools = {n: object() for n in names}

    registry = _Registry(["a", "b", "c", "d"])
    role = MagicMock()
    role.name = "release"
    role.sub_intent = "deliveries"

    async def _filter(registry=None, role=None, message="", **kwargs):
        # plugin keeps only tools tied to the classified sub-intent
        keep = {"a", "b"}
        for name in list(registry._tools):
            if name not in keep:
                registry._tools.pop(name, None)

    register_hook("filter_agent_tools", _filter)
    results = await run_hooks(
        "filter_agent_tools", registry=registry, role=role, message="Zeige Deliveries"
    )

    assert results == []  # handler returns None → no opinion collected
    assert set(registry._tools) == {"a", "b"}


@pytest.mark.asyncio
async def test_filter_agent_tools_faulty_handler_leaves_registry_unfiltered():
    """A crashing filter degrades to the unfiltered registry (run_hooks fail-open)."""

    class _Registry:
        def __init__(self, names):
            self._tools = {n: object() for n in names}

    registry = _Registry(["a", "b", "c"])

    async def _boom(**kwargs):
        raise RuntimeError("bad filter config")

    register_hook("filter_agent_tools", _boom)
    await run_hooks("filter_agent_tools", registry=registry, role=MagicMock(), message="x")

    assert set(registry._tools) == {"a", "b", "c"}  # untouched, no crash


# --- run_hooks ---


@pytest.mark.asyncio
async def test_run_hooks_calls_registered():
    """Registered hooks are called with kwargs."""
    hook = AsyncMock(return_value=None)
    register_hook("startup", hook)

    await run_hooks("startup", app="fake_app")

    hook.assert_awaited_once_with(app="fake_app")


@pytest.mark.asyncio
async def test_run_hooks_collects_results():
    """Non-None results are collected."""
    hook_a = AsyncMock(return_value="context_a")
    hook_b = AsyncMock(return_value="context_b")
    register_hook("retrieve_context", hook_a)
    register_hook("retrieve_context", hook_b)

    results = await run_hooks("retrieve_context", query="test")

    assert results == ["context_a", "context_b"]


@pytest.mark.asyncio
async def test_run_hooks_ignores_none():
    """None results are filtered out."""
    hook_a = AsyncMock(return_value=None)
    hook_b = AsyncMock(return_value="data")
    register_hook("post_message", hook_a)
    register_hook("post_message", hook_b)

    results = await run_hooks("post_message", user_msg="hi", assistant_msg="hello")

    assert results == ["data"]


@pytest.mark.asyncio
async def test_run_hooks_empty():
    """No hooks registered → empty list, no error."""
    results = await run_hooks("startup", app="x")
    assert results == []


@pytest.mark.asyncio
async def test_hook_error_does_not_propagate():
    """A failing hook is logged but does not crash the caller."""
    bad_hook = AsyncMock(side_effect=RuntimeError("boom"))
    good_hook = AsyncMock(return_value="ok")
    register_hook("shutdown", bad_hook)
    register_hook("shutdown", good_hook)

    results = await run_hooks("shutdown", app="x")

    # bad_hook failed, good_hook still ran
    assert results == ["ok"]
    bad_hook.assert_awaited_once()
    good_hook.assert_awaited_once()


# --- clear_hooks ---


@pytest.mark.asyncio
async def test_clear_hooks():
    """clear_hooks removes all hooks."""
    hook = AsyncMock(return_value="val")
    register_hook("startup", hook)
    clear_hooks()

    results = await run_hooks("startup", app="x")

    assert results == []
    hook.assert_not_awaited()


# --- ordering ---


@pytest.mark.asyncio
async def test_multiple_hooks_same_event():
    """Hooks for the same event run in registration order."""
    call_order = []

    async def first(**kw):
        call_order.append(1)

    async def second(**kw):
        call_order.append(2)

    async def third(**kw):
        call_order.append(3)

    register_hook("startup", first)
    register_hook("startup", second)
    register_hook("startup", third)

    await run_hooks("startup", app="x")

    assert call_order == [1, 2, 3]


# --- HOOK_EVENTS whitelist ---


def test_hook_events_is_frozenset():
    """HOOK_EVENTS is immutable and non-empty."""
    assert isinstance(HOOK_EVENTS, frozenset)
    assert len(HOOK_EVENTS) > 0


# --- plugin loading integration ---


@pytest.mark.asyncio
async def test_plugin_loading():
    """A plugin module with 'module:callable' format calls the callable."""
    mock_register = MagicMock()

    # Replicate _load_plugin_module logic to avoid importing lifecycle (heavy DB deps)
    spec = "fake_plugin.hooks:register"
    module_path, attr_name = spec.rsplit(":", 1)

    with patch("importlib.import_module") as mock_import:
        import importlib

        fake_mod = MagicMock()
        fake_mod.register = mock_register
        mock_import.return_value = fake_mod

        mod = importlib.import_module(module_path)
        fn = getattr(mod, attr_name)
        fn()

        mock_import.assert_called_once_with("fake_plugin.hooks")
        mock_register.assert_called_once()


def test_plugin_spec_parsing():
    """Plugin spec 'module:callable' splits correctly."""
    spec = "renfield_twin.hooks:register"
    module_path, attr_name = spec.rsplit(":", 1)
    assert module_path == "renfield_twin.hooks"
    assert attr_name == "register"


def test_plugin_spec_no_callable():
    """Plugin spec without ':' means module-only import."""
    spec = "renfield_twin.hooks"
    assert ":" not in spec


class TestHookPriority:
    """get_hook_handlers orders by priority (higher runs later); run_hooks
    deliberately ignores priority."""

    @pytest.mark.unit
    def test_priority_orders_handlers_for_chained_consumers(self):
        from utils.hooks import get_hook_handlers, register_hook

        async def early(**_):
            return None

        async def guard(**_):
            return None

        async def late_registered(**_):
            return None

        register_hook("pre_mcp_call", guard, priority=100)
        register_hook("pre_mcp_call", early)
        register_hook("pre_mcp_call", late_registered)

        handlers = get_hook_handlers("pre_mcp_call")
        # Default-priority handlers keep registration order; the high-priority
        # guard runs LAST even though it registered first.
        assert handlers == [early, late_registered, guard]

    @pytest.mark.unit
    def test_default_priority_keeps_registration_order(self):
        from utils.hooks import get_hook_handlers, register_hook

        async def a(**_):
            return None

        async def b(**_):
            return None

        register_hook("pre_mcp_call", a)
        register_hook("pre_mcp_call", b)
        assert get_hook_handlers("pre_mcp_call") == [a, b]
