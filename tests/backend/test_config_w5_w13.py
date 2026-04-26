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

import pytest
from loguru import logger


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

    Asserts BOTH conditions: the settings reference must be present AND
    the matching `await asyncio.wait_for(... timeout=<literal>)` form
    must NOT be present, so a partial revert that adds back the literal
    while leaving the settings ref also fails the test.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    cases = [
        ("src/backend/services/agent_service.py", "settings.agent_preselect_timeout", r"\btimeout=10\.0\b"),
        ("src/backend/services/orchestrator.py", "settings.orchestrator_synthesis_timeout", r"\btimeout=30\.0\b"),
        ("src/backend/services/mcp_client.py", "settings.geocode_http_timeout", r"\btimeout=8\.0\b"),
        ("src/backend/services/federation_query_responder.py", "settings.federation_synthesis_timeout", r"\btimeout=30\.0\b"),
        ("src/backend/services/rag_eval_service.py", "settings.rag_eval_answer_timeout", r"\btimeout=60\b"),
        ("src/backend/services/rag_eval_service.py", "settings.rag_eval_score_timeout", r"\btimeout=30\b"),
    ]
    for rel_path, must_contain, banned_literal_re in cases:
        src = (repo_root / rel_path).read_text()
        assert must_contain in src, (
            f"{rel_path}: missing reference `{must_contain}` — "
            "W5 fix expects this call site to use the Settings field"
        )
        # Strip out comments before checking — historical "previously timeout=10.0"
        # comments are accurate and shouldn't trigger a regression failure.
        src_no_comments = "\n".join(
            line.split("#", 1)[0] for line in src.splitlines()
        )
        assert not re.search(banned_literal_re, src_no_comments), (
            f"{rel_path}: literal matching `{banned_literal_re}` reappeared "
            "in non-comment code — W5 expects all call sites to use the "
            "Settings field, not a hardcoded number"
        )


# --- W13 — placeholder defaults trigger a loud warning (in production env) ---

@pytest.mark.unit
def test_w13_warns_in_production_when_postgres_password_is_changeme(monkeypatch):
    """In a production-style RENFIELD_ENV with placeholder defaults still
    in place, the validator must emit a WARN-level message naming the
    offending field.
    """
    monkeypatch.setenv("RENFIELD_ENV", "production")
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
        "warn_on_changeme_defaults must emit a clearly-marked WARN line "
        f"in production. Got: {output!r}"
    )
    assert "postgres_password" in output, (
        "Warning must name the offending field for grep-ability. "
        f"Got: {output!r}"
    )


@pytest.mark.unit
def test_w13_silent_in_development_even_with_placeholders(monkeypatch):
    """In dev (default RENFIELD_ENV), the validator must stay silent even
    when placeholder defaults are in place — the warning is a
    production-deploy guard, not a dev-environment annoyance.
    """
    monkeypatch.delenv("RENFIELD_ENV", raising=False)  # default → "development"
    for var in ("POSTGRES_PASSWORD", "SECRET_KEY", "DEFAULT_ADMIN_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    captured = io.StringIO()
    sink_id = logger.add(captured, level="WARNING", format="{level}|{message}")
    try:
        from utils.config import Settings

        Settings()
    finally:
        logger.remove(sink_id)

    output = captured.getvalue()
    assert "INSECURE DEFAULT" not in output, (
        "Validator must stay silent in development env (this is the "
        "default-case behaviour to avoid spamming dev/test logs). "
        f"Got unexpected warning: {output!r}"
    )


@pytest.mark.unit
def test_w13_no_warning_in_production_when_secrets_are_set(monkeypatch):
    """When all three placeholder fields are overridden via env in a
    production-style RENFIELD_ENV, the validator must stay silent.
    """
    monkeypatch.setenv("RENFIELD_ENV", "production")
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
        f"overridden in production. Got unexpected warning: {output!r}"
    )


@pytest.mark.unit
def test_w13_changeme_fields_match_settings_defaults():
    """Drift guard: every field listed in `_CHANGEME_FIELDS` must exist on
    the Settings class, AND its class-level default must be a string-like
    placeholder (resolved via SecretStr unwrap if applicable). If a
    future commit renames the field or replaces the placeholder default
    without updating `_CHANGEME_FIELDS`, this test catches it.
    """
    from utils.config import _CHANGEME_FIELDS, Settings
    from pydantic import SecretStr

    fields = Settings.model_fields
    for name in _CHANGEME_FIELDS:
        assert name in fields, (
            f"_CHANGEME_FIELDS lists `{name}` but Settings has no such field. "
            "Was it renamed? Update _CHANGEME_FIELDS to match."
        )
        default = fields[name].default
        if isinstance(default, SecretStr):
            default = default.get_secret_value()
        assert isinstance(default, str) and default, (
            f"`{name}` should ship with a string placeholder default (so the "
            f"validator has a known value to compare against), got: {default!r}"
        )
