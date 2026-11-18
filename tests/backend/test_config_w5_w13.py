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
The admin password is read only by the API process, so it is checked at
API startup (`warn_on_api_changeme_defaults`), not in every worker.
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

    # Resolve the backend source root from an importable package, not by
    # walking up from __file__: in the container the tests are mounted at
    # /tests while the backend is at /app (no src/backend prefix).
    import utils.config as _config_module

    backend_root = Path(_config_module.__file__).resolve().parent.parent
    cases = [
        ("services/agent_service.py", "settings.agent_preselect_timeout", r"\btimeout=10\.0\b"),
        ("services/orchestrator.py", "settings.orchestrator_synthesis_timeout", r"\btimeout=30\.0\b"),
        ("services/mcp_client.py", "settings.geocode_http_timeout", r"\btimeout=8\.0\b"),
        ("services/federation_query_responder.py", "settings.federation_synthesis_timeout", r"\btimeout=30\.0\b"),
        ("services/rag_eval_service.py", "settings.rag_eval_answer_timeout", r"\btimeout=60\b"),
        ("services/rag_eval_service.py", "settings.rag_eval_score_timeout", r"\btimeout=30\b"),
    ]
    for rel_path, must_contain, banned_literal_re in cases:
        src = (backend_root / rel_path).read_text()
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

    from utils.config import Settings

    # The build/CI container ships a populated `.env` AND OS env vars with
    # real (non-placeholder) secrets — both layered on by pydantic-settings.
    # To exercise the W13 validator's "placeholder still in use" branch
    # deterministically, construct Settings with the secret fields set
    # explicitly to their class-level placeholder defaults (read live from
    # model_fields so this never drifts from the real default strings).
    placeholders = {}
    for field_name in ("postgres_password", "default_admin_password"):
        default = Settings.model_fields[field_name].default
        placeholders[field_name] = (
            default.get_secret_value() if hasattr(default, "get_secret_value") else default
        )
    # secret_key must be STRONG here: #692's fail_closed_on_insecure_jwt_key
    # HARD-RAISES on a production env + placeholder secret_key, which would
    # propagate out of Settings(...) before this test can assert the W13 warn.
    # The two guards share the production trigger; W13 is about the OTHER
    # placeholder secrets (postgres_password / default_admin_password), so keep
    # those placeholder and clear the JWT-key guard with a strong key.
    placeholders["secret_key"] = "x" * 48

    captured = io.StringIO()
    sink_id = logger.add(captured, level="WARNING", format="{level}|{message}")
    try:
        Settings(_env_file=None, **placeholders)  # triggers the validator
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
    from pydantic import SecretStr

    from utils.config import _CHANGEME_FIELDS, Settings

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


@pytest.mark.unit
def test_w13_api_changeme_fields_match_settings_defaults():
    """Same drift guard for the API-only list, and the two lists stay apart:
    a name in both would be checked twice in the backend and still in every
    worker."""
    from pydantic import SecretStr

    from utils.config import _API_CHANGEME_FIELDS, _CHANGEME_FIELDS, Settings

    assert not set(_API_CHANGEME_FIELDS) & set(_CHANGEME_FIELDS)
    for name in _API_CHANGEME_FIELDS:
        assert name in Settings.model_fields, f"`{name}` is not a Settings field"
        default = Settings.model_fields[name].default
        if isinstance(default, SecretStr):
            default = default.get_secret_value()
        assert isinstance(default, str) and default


def _settings_like_a_worker_pod():
    """Settings the way a worker pod builds them: production, the shared
    secrets set, the API-only admin password NOT given (its placeholder)."""
    from utils.config import Settings

    admin_default = Settings.model_fields["default_admin_password"].default
    return Settings(
        _env_file=None,
        renfield_env="production",
        postgres_password="real-strong-password-from-secret",
        secret_key="x" * 48,
        default_admin_password=(
            admin_default.get_secret_value()
            if hasattr(admin_default, "get_secret_value")
            else admin_default
        ),
    )


def _warnings_during(fn) -> str:
    captured = io.StringIO()
    sink_id = logger.add(captured, level="WARNING", format="{level}|{message}")
    try:
        fn()
    finally:
        logger.remove(sink_id)
    return captured.getvalue()


@pytest.mark.unit
def test_w13_a_worker_without_the_admin_password_starts_without_the_warning():
    """A worker never reads the default admin password and is not given it;
    building its Settings must not warn about it (it did on every worker start
    in both instances, which trains everyone to skip the warning)."""
    output = _warnings_during(_settings_like_a_worker_pod)
    assert "INSECURE DEFAULT" not in output, output


@pytest.mark.unit
def test_w13_the_api_check_still_names_the_admin_password():
    settings = _settings_like_a_worker_pod()
    output = _warnings_during(settings.warn_on_api_changeme_defaults)
    assert "INSECURE DEFAULT" in output, output
    assert "default_admin_password" in output and "DEFAULT_ADMIN_PASSWORD" in output, output
    assert "postgres_password" not in output and "secret_key" not in output, output


@pytest.mark.unit
def test_w13_the_api_check_is_silent_in_development():
    from utils.config import Settings

    settings = Settings(_env_file=None, renfield_env="development")
    assert "INSECURE DEFAULT" not in _warnings_during(settings.warn_on_api_changeme_defaults)


@pytest.mark.unit
async def test_w13_the_api_auth_startup_runs_the_admin_password_check(monkeypatch):
    """The API startup is where the check moved to: `_init_auth` must run it,
    and before its own try, so a failing auth init cannot swallow it."""
    import api.lifecycle as lifecycle
    import services.auth_service as auth_service

    monkeypatch.setattr(lifecycle, "settings", _settings_like_a_worker_pod())

    async def _fail(*_a, **_k):
        raise RuntimeError("no database in this test")

    monkeypatch.setattr(auth_service, "ensure_default_roles", _fail)

    class _NoSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lifecycle, "AsyncSessionLocal", lambda: _NoSession())

    captured = io.StringIO()
    sink_id = logger.add(captured, level="WARNING", format="{level}|{message}")
    try:
        await lifecycle._init_auth()
    finally:
        logger.remove(sink_id)
    output = captured.getvalue()
    assert "INSECURE DEFAULT" in output and "default_admin_password" in output, output
