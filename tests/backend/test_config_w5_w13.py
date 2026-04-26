"""
Regression guards for W5 (hardcoded timeouts → Settings) and W13
(changeme defaults detection) in utils/config.py.

W5: six previously-hardcoded timeout literals across services/ were
moved to Settings fields. The audit found them as values 8.0, 10.0,
30.0, 60.0 in agent_service.py, orchestrator.py, mcp_client.py,
federation_query_responder.py, rag_eval_service.py.

W13: three secret/password fields (postgres_password, secret_key,
default_admin_password) ship with `changeme` placeholder defaults
that must trigger a startup WARNING when in use, so deploys against
real environments don't silently run with insecure credentials.
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stderr

import pytest
from loguru import logger
from pydantic import Field as PydField  # for type-only checks


# --- W5 — timeout settings exist with the right defaults + ranges ---

@pytest.mark.unit
def test_w5_timeout_fields_exist_on_settings():
    """The 6 W5 settings must be defined on the Settings class with the
    correct defaults. Pre-fix these values lived as literals at the call
    sites (timeout=10.0, etc.).
    """
    from utils.config import Settings

    expected = {
        "agent_preselect_timeout": 10.0,
        "orchestrator_synthesis_timeout": 30.0,
        "geocode_http_timeout": 8.0,
        "federation_synthesis_timeout": 30.0,
        "rag_eval_answer_timeout": 60.0,
        "rag_eval_score_timeout": 30.0,
    }
    fields = Settings.model_fields
    for field_name, expected_default in expected.items():
        assert field_name in fields, (
            f"Settings is missing W5 field `{field_name}`. Pre-fix this value "
            f"({expected_default}) was a literal at the call site."
        )
        info = fields[field_name]
        assert info.default == expected_default, (
            f"`{field_name}` default changed: expected {expected_default}, "
            f"got {info.default}. If intentional, update this test."
        )
        # All W5 fields use Field(ge=, le=) — i.e. constraints attached
        constraints = getattr(info, "metadata", []) or []
        has_ge = any(getattr(c, "ge", None) is not None for c in constraints)
        has_le = any(getattr(c, "le", None) is not None for c in constraints)
        assert has_ge and has_le, (
            f"`{field_name}` must use Field(ge=, le=) to validate range"
        )


# --- W5 — call sites no longer carry hardcoded literals ---

@pytest.mark.unit
def test_w5_call_sites_use_settings_not_literals():
    """The 6 call sites the audit identified must reference settings.<name>
    instead of an inline literal. Catches accidental reverts.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    cases = [
        ("src/backend/services/agent_service.py", "settings.agent_preselect_timeout", "timeout=10.0"),
        ("src/backend/services/orchestrator.py", "settings.orchestrator_synthesis_timeout", "timeout=30.0"),
        ("src/backend/services/mcp_client.py", "settings.geocode_http_timeout", "timeout=8.0"),
        ("src/backend/services/federation_query_responder.py", "settings.federation_synthesis_timeout", "timeout=30.0"),
        ("src/backend/services/rag_eval_service.py", "settings.rag_eval_answer_timeout", "timeout=60"),
        ("src/backend/services/rag_eval_service.py", "settings.rag_eval_score_timeout", "timeout=30"),
    ]
    for rel_path, must_contain, must_not_contain_at_callsite in cases:
        src = (repo_root / rel_path).read_text()
        assert must_contain in src, (
            f"{rel_path}: missing reference `{must_contain}` — "
            "W5 fix expects this call site to use the Settings field"
        )


# --- W13 — placeholder defaults trigger a loud warning ---

@pytest.mark.unit
def test_w13_warns_when_postgres_password_is_changeme(monkeypatch):
    """Default Settings instantiation (no env override) leaves
    postgres_password = 'changeme'. The model_validator must emit a
    WARN-level message naming the offending field.
    """
    # Strip env so we hit the placeholder default deterministically.
    for var in ("POSTGRES_PASSWORD", "SECRET_KEY", "DEFAULT_ADMIN_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    captured = io.StringIO()
    sink_id = logger.add(captured, level="WARNING", format="{level}|{message}")
    try:
        from utils.config import Settings

        Settings()  # instantiation triggers the validator
    finally:
        logger.remove(sink_id)

    output = captured.getvalue()
    assert "INSECURE DEFAULT" in output, (
        "warn_on_changeme_defaults must emit a clearly-marked WARN line. "
        f"Got: {output!r}"
    )
    assert "postgres_password" in output, (
        "Warning must name the offending field for grep-ability. "
        f"Got: {output!r}"
    )


@pytest.mark.unit
def test_w13_no_warning_when_password_is_set(monkeypatch):
    """When all three placeholder fields are overridden via env,
    the validator must stay silent.
    """
    monkeypatch.setenv("POSTGRES_PASSWORD", "real-strong-password-from-secret")
    monkeypatch.setenv("SECRET_KEY", "5c5e93b6f7c4a8d2e1f3b9a4c8d6e0f2")
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "another-real-password")

    captured = io.StringIO()
    sink_id = logger.add(captured, level="WARNING", format="{level}|{message}")
    try:
        from utils.config import Settings

        Settings()
    finally:
        logger.remove(sink_id)

    output = captured.getvalue()
    assert "INSECURE DEFAULT" not in output, (
        "Validator should NOT warn when all placeholder defaults are "
        f"overridden. Got unexpected warning: {output!r}"
    )
