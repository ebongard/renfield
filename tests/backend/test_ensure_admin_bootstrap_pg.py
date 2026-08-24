"""ensure_admin_user bootstrap hardening (login/user-mgmt audit).

The bootstrap admin must ALWAYS start with must_change_password=True — even
when DEFAULT_ADMIN_PASSWORD is operator-set — so a weak/shared env password
cannot become a permanent standing credential. The frontend forced-rotation
gate + change-password reuse-rejection make that flag actionable + real.
"""
import pytest

from services import auth_service


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_configured_password_still_forces_rotation(pg_db_session, monkeypatch):
    """A non-'changeme' DEFAULT_ADMIN_PASSWORD must still set must_change_password."""
    from pydantic import SecretStr

    monkeypatch.setattr(
        auth_service.settings, "default_admin_username", "bootstrapadmin", raising=False
    )
    monkeypatch.setattr(
        auth_service.settings,
        "default_admin_password",
        SecretStr("an-operator-set-password"),
        raising=False,
    )

    admin = await auth_service.ensure_admin_user(pg_db_session)

    assert admin is not None
    assert admin.username == "bootstrapadmin"
    assert admin.must_change_password is True
    assert auth_service.verify_password("an-operator-set-password", admin.password_hash)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_bootstrap_skipped_when_users_exist(pg_db_session, monkeypatch):
    """ensure_admin_user is a no-op once any user exists (no second admin)."""
    from pydantic import SecretStr

    monkeypatch.setattr(
        auth_service.settings, "default_admin_username", "firstadmin", raising=False
    )
    monkeypatch.setattr(
        auth_service.settings, "default_admin_password", SecretStr("pw-one"), raising=False
    )
    first = await auth_service.ensure_admin_user(pg_db_session)
    assert first is not None

    second = await auth_service.ensure_admin_user(pg_db_session)
    assert second is None
