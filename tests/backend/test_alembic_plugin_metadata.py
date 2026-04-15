"""
Tests for `alembic/env.py` plugin metadata discovery (X-idra/reva#151).

The env.py reads `PLUGIN_METADATA_MODULES` at import time and appends
each plugin's `Base.metadata` to Alembic's `target_metadata` list so
autogenerate doesn't emit spurious `drop_table` statements for plugin-
owned tables that live on a separate SQLAlchemy Base.

These tests avoid actually importing `alembic/env.py` (which has
side effects on Alembic context). Instead they exercise the same
discovery logic via a small helper that mirrors the env.py block.
"""

from __future__ import annotations

import importlib
import os
import sys
import types

import pytest
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base


def _discover_plugin_metadatas(env_value: str) -> list:
    """Mirror the env.py discovery logic for unit testing.

    Keep this in lockstep with the corresponding block in `alembic/env.py`.
    If that block changes shape, update this helper and the tests will
    still pin the contract.
    """
    result: list = []
    for spec in env_value.split(","):
        spec = spec.strip()
        if not spec:
            continue
        try:
            mod = importlib.import_module(spec)
        except ImportError:
            continue
        plugin_base = getattr(mod, "Base", None)
        if plugin_base is None:
            continue
        result.append(plugin_base.metadata)
    return result


@pytest.fixture
def _fake_plugin_module(request):
    """Install a fake plugin module in sys.modules and yield its dotted name."""
    mod_name = f"test_fake_plugin_{os.getpid()}_{id(request)}"
    mod = types.ModuleType(mod_name)

    FakeBase = declarative_base()

    class FakeTable(FakeBase):
        __tablename__ = "reva_fake_plugin_test_table"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    mod.Base = FakeBase
    sys.modules[mod_name] = mod

    yield mod_name

    sys.modules.pop(mod_name, None)


def test_empty_env_var_returns_no_plugins():
    """No PLUGIN_METADATA_MODULES → empty list."""
    assert _discover_plugin_metadatas("") == []


def test_whitespace_only_entries_are_ignored():
    """`,,, ,,` expands to nothing."""
    assert _discover_plugin_metadatas(",, ,, ") == []


def test_nonexistent_module_is_ignored_silently():
    """ImportError → skipped, not raised. Platform-only deploy compat."""
    metadatas = _discover_plugin_metadatas("definitely.not.a.real.module")
    assert metadatas == []


def test_module_without_base_attribute_is_skipped(monkeypatch):
    """A module that imports successfully but has no `Base` is not added."""
    mod_name = f"test_no_base_plugin_{os.getpid()}"
    mod = types.ModuleType(mod_name)
    sys.modules[mod_name] = mod
    try:
        assert _discover_plugin_metadatas(mod_name) == []
    finally:
        sys.modules.pop(mod_name, None)


def test_single_plugin_module_is_registered(_fake_plugin_module):
    """A well-formed plugin module gets its MetaData added."""
    metadatas = _discover_plugin_metadatas(_fake_plugin_module)
    assert len(metadatas) == 1
    tables = set(metadatas[0].tables.keys())
    assert "reva_fake_plugin_test_table" in tables


def test_multiple_plugin_modules_are_all_registered(_fake_plugin_module):
    """Comma-separated list, first entry good, second entry broken."""
    env = f"{_fake_plugin_module}, definitely.not.real"
    metadatas = _discover_plugin_metadatas(env)
    # Broken one is silently skipped, good one is kept.
    assert len(metadatas) == 1
    tables = set(metadatas[0].tables.keys())
    assert "reva_fake_plugin_test_table" in tables


def test_target_metadata_shape_matches_env_py_contract(_fake_plugin_module):
    """When plugin metadatas are present, env.py builds a list.

    When none are present, env.py passes a bare MetaData. Alembic
    accepts both shapes; we just pin the contract so the env.py block
    doesn't regress to always-list or always-scalar.
    """
    # Bare platform
    target_empty = _compute_target_metadata([], _platform_metadata())
    assert not isinstance(target_empty, list)

    # With one plugin
    plugin_mds = _discover_plugin_metadatas(_fake_plugin_module)
    target_with_plugin = _compute_target_metadata(plugin_mds, _platform_metadata())
    assert isinstance(target_with_plugin, list)
    assert len(target_with_plugin) == 2


# ----- shape helpers (mirror env.py) ---------------------------------------


def _platform_metadata():
    FakeBase = declarative_base()

    class PlatformTable(FakeBase):
        __tablename__ = "platform_test_table"
        id = Column(Integer, primary_key=True)

    return FakeBase.metadata


def _compute_target_metadata(plugin_metadatas: list, platform_metadata):
    """Mirror the `target_metadata = ...` line at the bottom of env.py."""
    return [platform_metadata, *plugin_metadatas] if plugin_metadatas else platform_metadata
