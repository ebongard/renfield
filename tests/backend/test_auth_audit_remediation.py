"""Security-audit remediation tests (login & user management).

Covers the fixes from the 2026-07-20 auth audit:
  H1 — grant-only-what-you-hold on role edit/create (roles.py)
  H2 — grant-only + self-role-change block on user role assignment (users.py)
  H3 — token_epoch session revocation on password change
  H4 — logout revokes the refresh token too
  M2 — short-lived, WS-scoped JWT rejected by the REST API
  M3 — login timing equalization (bcrypt runs on the not-found branch)
  M5 — last-admin lockout guards (demote/deactivate/delete)
  M6 — token_blacklist.add reports write failure

The route guards are exercised by calling the handler coroutines directly with a
crafted caller + real DB rows (FastAPI dependency injection is bypassed, which is
exactly what lets us drive a specific low-privilege caller identity).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routes.auth import (
    ChangePasswordRequest,
    LogoutRequest,
    RefreshRequest,
    change_password,
    logout,
    refresh_token,
)
from api.routes.internal_auth import VerifyRequest, verify_token
from api.routes.roles import CreateRoleRequest, UpdateRoleRequest, create_role, update_role
from api.routes.users import (
    CreateUserRequest,
    ResetPasswordRequest,
    UpdateUserRequest,
    create_user,
    delete_user,
    reset_password,
    update_user,
)
from models.database import Role, User
from models.permissions import Permission, missing_grantable_permissions
from services.auth_service import (
    active_admin_ids,
    authenticate_user,
    create_access_token,
    create_refresh_token,
    create_ws_token_jwt,
    decode_token,
    get_current_user,
    get_password_hash,
)


class _FakeBlacklist:
    """In-memory blacklist so tests don't depend on Redis (mirrors test_auth.py)."""

    def __init__(self):
        self._s = set()

    async def add(self, jti, ttl):
        self._s.add(jti)
        return True

    async def is_blacklisted(self, jti):
        return jti in self._s


def _limiter_request(path="/api/auth/refresh"):
    """A real starlette Request so slowapi's @limiter.limit doesn't choke when the
    route handler is called directly."""
    from starlette.requests import Request

    from services.api_rate_limiter import limiter as app_limiter

    state = type("S", (), {})()
    state.limiter = app_limiter
    app = type("A", (), {})()
    app.state = state
    return Request({
        "type": "http", "method": "POST", "path": path,
        "headers": [], "client": ("127.0.0.1", 12345), "app": app,
    })


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _mk_role(db, name, perms, is_system=False) -> Role:
    role = Role(name=name, permissions=perms, is_system=is_system)
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


async def _mk_user(db, username, role, active=True, password=None) -> User:
    user = User(
        username=username,
        password_hash=get_password_hash(password) if password else "x",
        role_id=role.id,
        is_active=active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user, ["role"])
    return user


# ---------------------------------------------------------------------------
# missing_grantable_permissions (H1/H2 core)
# ---------------------------------------------------------------------------

class TestGrantOnlyHelper:
    def test_admin_can_grant_anything(self):
        assert missing_grantable_permissions(["admin"], ["admin", "kb.all", "users.manage"]) == []

    def test_non_admin_cannot_grant_permission_it_lacks(self):
        assert missing_grantable_permissions(["roles.manage"], ["admin"]) == ["admin"]

    def test_hierarchy_allows_granting_implied(self):
        # kb.all implies kb.shared/kb.own → a holder may grant them
        assert missing_grantable_permissions(["kb.all"], ["kb.shared"]) == []

    def test_mcp_wildcard_allows_granting_specific(self):
        assert missing_grantable_permissions(["mcp.*"], ["mcp.weather"]) == []

    def test_mixed_reports_only_the_ungranted(self):
        missing = missing_grantable_permissions(
            ["users.manage", "chat.own"], ["chat.own", "admin", "kb.all"]
        )
        assert set(missing) == {"admin", "kb.all"}


# ---------------------------------------------------------------------------
# H1 — role escalation guard
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestRoleEscalationGuard:
    async def test_manager_cannot_add_admin_to_a_role(self, db_session):
        mgr_role = await _mk_role(db_session, "Manager", ["roles.manage"])
        manager = await _mk_user(db_session, "mgr", mgr_role)
        target = await _mk_role(db_session, "Target", ["chat.own"])

        with pytest.raises(HTTPException) as exc:
            await update_role(
                role_id=target.id,
                request=UpdateRoleRequest(permissions=["admin"]),
                db=db_session,
                user=manager,
            )
        assert exc.value.status_code == 403

    async def test_admin_may_add_admin(self, db_session):
        admin_role = await _mk_role(db_session, "Admin", ["admin"])
        admin = await _mk_user(db_session, "root", admin_role)
        target = await _mk_role(db_session, "Target2", ["chat.own"])

        await update_role(  # should not raise
            role_id=target.id,
            request=UpdateRoleRequest(permissions=["admin"]),
            db=db_session,
            user=admin,
        )
        await db_session.refresh(target)
        assert "admin" in target.permissions

    async def test_non_admin_cannot_edit_system_role_permissions(self, db_session):
        mgr_role = await _mk_role(db_session, "Manager3", ["roles.manage", "chat.own"])
        manager = await _mk_user(db_session, "mgr3", mgr_role)
        sysrole = await _mk_role(db_session, "Familie", ["chat.own"], is_system=True)

        with pytest.raises(HTTPException) as exc:
            await update_role(
                role_id=sysrole.id,
                # only grants chat.own+kb.own (chat.own held) → passes grant-only,
                # but editing a SYSTEM role's perms requires admin
                request=UpdateRoleRequest(permissions=["chat.own", "kb.own"]),
                db=db_session,
                user=manager,
            )
        assert exc.value.status_code == 403

    async def test_create_role_grant_only(self, db_session):
        mgr_role = await _mk_role(db_session, "Manager4", ["roles.manage"])
        manager = await _mk_user(db_session, "mgr4", mgr_role)

        with pytest.raises(HTTPException) as exc:
            await create_role(
                request=CreateRoleRequest(name="Sneaky", description="", permissions=["admin"]),
                db=db_session,
                user=manager,
            )
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# H2 — user role-assignment escalation guard
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestUserRoleEscalationGuard:
    async def test_manager_cannot_create_admin_user(self, db_session):
        mgr_role = await _mk_role(db_session, "UM", ["users.manage"])
        manager = await _mk_user(db_session, "um", mgr_role)
        admin_role = await _mk_role(db_session, "AdminU", ["admin"])

        with pytest.raises(HTTPException) as exc:
            await create_user(
                request=CreateUserRequest(
                    username="newadmin", password="SecurePass123!", role_id=admin_role.id
                ),
                db=db_session,
                current_user=manager,
            )
        assert exc.value.status_code == 403

    async def test_cannot_change_own_role(self, db_session):
        mgr_role = await _mk_role(db_session, "UM2", ["users.manage"])
        manager = await _mk_user(db_session, "um2", mgr_role)
        other = await _mk_role(db_session, "Other2", ["chat.own"])

        with pytest.raises(HTTPException) as exc:
            await update_user(
                user_id=manager.id,
                request=UpdateUserRequest(role_id=other.id),
                db=db_session,
                current_user=manager,
            )
        assert exc.value.status_code == 400

    async def test_manager_cannot_promote_other_to_admin(self, db_session):
        mgr_role = await _mk_role(db_session, "UM3", ["users.manage"])
        manager = await _mk_user(db_session, "um3", mgr_role)
        admin_role = await _mk_role(db_session, "AdminU3", ["admin"])
        guest_role = await _mk_role(db_session, "GuestU3", ["chat.own"])
        victim = await _mk_user(db_session, "victim3", guest_role)

        with pytest.raises(HTTPException) as exc:
            await update_user(
                user_id=victim.id,
                request=UpdateUserRequest(role_id=admin_role.id),
                db=db_session,
                current_user=manager,
            )
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# M5 — last-admin lockout guards
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestLastAdminGuard:
    async def test_active_admin_ids_counts_only_active_admins(self, db_session):
        admin_role = await _mk_role(db_session, "A_LA", ["admin"])
        guest_role = await _mk_role(db_session, "G_LA", ["chat.own"])
        a1 = await _mk_user(db_session, "a1", admin_role)
        await _mk_user(db_session, "a2", admin_role, active=False)  # inactive admin
        await _mk_user(db_session, "g1", guest_role)
        ids = await active_admin_ids(db_session)
        assert ids == {a1.id}

    async def test_cannot_delete_last_admin(self, db_session):
        admin_role = await _mk_role(db_session, "A_DEL", ["admin"])
        mgr_role = await _mk_role(db_session, "M_DEL", ["users.manage"])
        the_admin = await _mk_user(db_session, "solo_admin", admin_role)
        manager = await _mk_user(db_session, "mgr_del", mgr_role)

        with pytest.raises(HTTPException) as exc:
            await delete_user(user_id=the_admin.id, db=db_session, current_user=manager)
        assert exc.value.status_code == 400

    async def test_cannot_deactivate_last_admin(self, db_session):
        admin_role = await _mk_role(db_session, "A_DEA", ["admin"])
        mgr_role = await _mk_role(db_session, "M_DEA", ["users.manage"])
        the_admin = await _mk_user(db_session, "solo_admin2", admin_role)
        manager = await _mk_user(db_session, "mgr_dea", mgr_role)

        with pytest.raises(HTTPException) as exc:
            await update_user(
                user_id=the_admin.id,
                request=UpdateUserRequest(is_active=False),
                db=db_session,
                current_user=manager,
            )
        assert exc.value.status_code == 400

    async def test_deleting_admin_ok_when_another_admin_exists(self, db_session):
        admin_role = await _mk_role(db_session, "A_OK", ["admin"])
        mgr_role = await _mk_role(db_session, "M_OK", ["users.manage"])
        a1 = await _mk_user(db_session, "admin_ok_1", admin_role)
        await _mk_user(db_session, "admin_ok_2", admin_role)  # second admin remains
        manager = await _mk_user(db_session, "mgr_ok", mgr_role)

        # Should not raise — a second admin remains
        await delete_user(user_id=a1.id, db=db_session, current_user=manager)


# ---------------------------------------------------------------------------
# H3/H4/M2 — token epoch, scope, revocation
# ---------------------------------------------------------------------------

class TestTokenClaims:
    def test_access_token_embeds_epoch(self):
        tok = create_access_token({"sub": "1"}, token_epoch=7)
        assert decode_token(tok)["epoch"] == 7

    def test_ws_token_is_scoped_and_short_lived(self):
        import time

        tok = create_ws_token_jwt(1, token_epoch=0)
        payload = decode_token(tok)
        assert payload["scope"] == "ws"
        assert payload["type"] == "access"
        # exp is minutes away at most, not the 24h of a normal access token
        assert 0 < payload["exp"] - time.time() < 600


@pytest.mark.database
class TestGetCurrentUserGuards:
    @pytest.fixture(autouse=True)
    def _auth_on(self, monkeypatch):
        # Flip ONLY auth_enabled on the real settings object (don't replace it —
        # create_access_token/create_ws_token_jwt read the real numeric fields
        # like access_token_expire_minutes / ws_jwt_expire_seconds), and stub the
        # blacklist check so these tests don't require a live Redis.
        from services import auth_service

        monkeypatch.setattr(auth_service.settings, "auth_enabled", True, raising=False)
        with patch(
            "services.token_blacklist.token_blacklist.is_blacklisted",
            new=AsyncMock(return_value=False),
        ):
            yield

    async def test_ws_scoped_token_rejected_on_rest(self, db_session):
        role = await _mk_role(db_session, "WSR", ["chat.own"])
        user = await _mk_user(db_session, "wsuser", role)
        tok = create_ws_token_jwt(user.id, token_epoch=0)
        with pytest.raises(HTTPException) as exc:
            await get_current_user(token=tok, db=db_session, request=None)
        assert exc.value.status_code == 401
        assert "scope" in exc.value.detail.lower()

    async def test_stale_epoch_token_rejected(self, db_session):
        role = await _mk_role(db_session, "EPR", ["chat.own"])
        user = await _mk_user(db_session, "epuser", role)
        user.token_epoch = 3
        await db_session.commit()
        # token minted at epoch 2 < current 3 → revoked
        tok = create_access_token({"sub": str(user.id)}, token_epoch=2)
        with pytest.raises(HTTPException) as exc:
            await get_current_user(token=tok, db=db_session, request=None)
        assert exc.value.status_code == 401

    async def test_current_epoch_token_accepted(self, db_session):
        role = await _mk_role(db_session, "EPR2", ["chat.own"])
        user = await _mk_user(db_session, "epuser2", role)
        user.token_epoch = 3
        await db_session.commit()
        tok = create_access_token({"sub": str(user.id)}, token_epoch=3)
        got = await get_current_user(token=tok, db=db_session, request=None)
        assert got.id == user.id


# ---------------------------------------------------------------------------
# H3 — password change revokes all other sessions (epoch bump) + re-issues
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestChangePasswordRevocation:
    async def test_bumps_epoch_and_returns_fresh_tokens(self, db_session):
        role = await _mk_role(db_session, "CP", ["chat.own"])
        user = await _mk_user(db_session, "cpuser", role, password="OldPass123!")
        assert (user.token_epoch or 0) == 0

        resp = await change_password(
            request=ChangePasswordRequest(
                current_password="OldPass123!", new_password="BrandNew456!"
            ),
            user=user,
            db=db_session,
        )
        await db_session.refresh(user)
        assert user.token_epoch == 1  # every prior token now invalid
        # a fresh pair carrying the new epoch is returned so THIS device stays in
        assert resp["access_token"] and resp["refresh_token"]
        assert decode_token(resp["access_token"])["epoch"] == 1

    async def test_wrong_current_password_rejected(self, db_session):
        role = await _mk_role(db_session, "CP2", ["chat.own"])
        user = await _mk_user(db_session, "cpuser2", role, password="RightPass123!")
        with pytest.raises(HTTPException) as exc:
            await change_password(
                request=ChangePasswordRequest(
                    current_password="WrongPass!", new_password="BrandNew456!"
                ),
                user=user,
                db=db_session,
            )
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# H4 — logout revokes the refresh token too
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestLogoutRevokesRefresh:
    async def test_refresh_token_is_blacklisted(self, db_session):
        from services import auth_service

        role = await _mk_role(db_session, "LO", ["chat.own"])
        user = await _mk_user(db_session, "louser", role)
        access = create_access_token({"sub": str(user.id)}, token_epoch=0)
        refresh = auth_service.create_refresh_token(user.id, token_epoch=0)
        access_jti = decode_token(access)["jti"]
        refresh_jti = decode_token(refresh)["jti"]

        added = {}

        async def _fake_add(jti, ttl):
            added[jti] = ttl
            return True

        with patch(
            "services.token_blacklist.token_blacklist.add",
            new=AsyncMock(side_effect=_fake_add),
        ):
            await logout(
                user=user,
                token=access,
                logout_request=LogoutRequest(refresh_token=refresh),
            )
        # BOTH the access and the refresh token were revoked
        assert access_jti in added
        assert refresh_jti in added

    async def test_logout_503_when_blacklist_write_fails(self, db_session):
        role = await _mk_role(db_session, "LO2", ["chat.own"])
        user = await _mk_user(db_session, "louser2", role)
        access = create_access_token({"sub": str(user.id)}, token_epoch=0)

        with patch(
            "services.token_blacklist.token_blacklist.add",
            new=AsyncMock(return_value=False),  # M6: write failed
        ):
            with pytest.raises(HTTPException) as exc:
                await logout(user=user, token=access, logout_request=None)
        assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# M3 — login timing equalization
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestLoginTimingEqualization:
    async def test_verify_runs_on_unknown_user(self, db_session):
        with patch("services.auth_service.verify_password", return_value=False) as vp:
            result = await authenticate_user(db_session, "does-not-exist", "whatever")
        assert result is None
        # bcrypt round MUST run even for a non-existent user (no timing oracle)
        vp.assert_called_once()


# ---------------------------------------------------------------------------
# M6 — blacklist write failure is reported
# ---------------------------------------------------------------------------

class TestBlacklistWriteFailure:
    async def test_add_returns_false_on_redis_error(self):
        from services.token_blacklist import TokenBlacklist

        bl = TokenBlacklist()
        redis = MagicMock()
        redis.setex = AsyncMock(side_effect=RuntimeError("redis down"))
        bl._redis = redis
        assert await bl.add("some-jti", 60) is False

    async def test_add_returns_true_on_success(self):
        from services.token_blacklist import TokenBlacklist

        bl = TokenBlacklist()
        redis = MagicMock()
        redis.setex = AsyncMock(return_value=True)
        bl._redis = redis
        assert await bl.add("some-jti", 60) is True

    async def test_add_noop_for_expired_ttl_is_true(self):
        from services.token_blacklist import TokenBlacklist

        assert await TokenBlacklist().add("j", 0) is True


# ---------------------------------------------------------------------------
# H3/H4 — /refresh enforces the epoch (gap test) + deploy backward-compat
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestRefreshEpoch:
    @pytest.fixture(autouse=True)
    def _no_limiter(self, monkeypatch):
        from services.api_rate_limiter import limiter as app_limiter
        from services import token_blacklist as tb_mod

        monkeypatch.setattr(app_limiter, "enabled", False)
        monkeypatch.setattr(tb_mod, "token_blacklist", _FakeBlacklist())
        yield

    async def test_refresh_rejects_stale_epoch(self, db_session):
        role = await _mk_role(db_session, "RFE", ["chat.own"])
        user = await _mk_user(db_session, "rfe", role)
        user.token_epoch = 5
        await db_session.commit()
        stale = create_refresh_token(user.id, token_epoch=4)  # < 5 → revoked
        with pytest.raises(HTTPException) as exc:
            await refresh_token(
                request=_limiter_request(),
                refresh_request=RefreshRequest(refresh_token=stale),
                db=db_session,
            )
        assert exc.value.status_code == 401

    async def test_refresh_accepts_no_epoch_claim_on_epoch0_user(self, db_session):
        # Deploy-day backward compat: a pre-feature refresh token (no epoch claim)
        # against a token_epoch=0 user is still honored (0 >= 0), no mass logout.
        role = await _mk_role(db_session, "RFE0", ["chat.own"])
        user = await _mk_user(db_session, "rfe0", role)
        legacy = create_refresh_token(user.id)  # no token_epoch → no epoch claim
        resp = await refresh_token(
            request=_limiter_request(),
            refresh_request=RefreshRequest(refresh_token=legacy),
            db=db_session,
        )
        assert resp.access_token and resp.refresh_token


@pytest.mark.database
class TestDeployBackwardCompat:
    @pytest.fixture(autouse=True)
    def _auth_on(self, monkeypatch):
        from services import auth_service

        monkeypatch.setattr(auth_service.settings, "auth_enabled", True, raising=False)
        with patch(
            "services.token_blacklist.token_blacklist.is_blacklisted",
            new=AsyncMock(return_value=False),
        ):
            yield

    async def test_no_epoch_claim_accepted_on_epoch0_user(self, db_session):
        role = await _mk_role(db_session, "BWC", ["chat.own"])
        user = await _mk_user(db_session, "bwc", role)  # token_epoch defaults 0
        legacy = create_access_token({"sub": str(user.id)})  # no epoch claim
        got = await get_current_user(token=legacy, db=db_session, request=None)
        assert got.id == user.id


# ---------------------------------------------------------------------------
# M5 — secondary last-admin branches (gap tests)
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestLastAdminSecondaryBranches:
    async def test_update_role_cannot_strip_last_admin(self, db_session):
        # Sole admin editing their own admin-granting role to drop `admin`.
        admin_role = await _mk_role(db_session, "URLA", ["admin"])
        admin = await _mk_user(db_session, "urla_admin", admin_role)
        with pytest.raises(HTTPException) as exc:
            await update_role(
                role_id=admin_role.id,
                request=UpdateRoleRequest(permissions=["chat.own"]),
                db=db_session,
                user=admin,
            )
        assert exc.value.status_code == 400

    async def test_demote_last_admin_via_role_change(self, db_session):
        # A users.manage manager demotes the sole admin to a role whose perms the
        # manager holds (so grant-only + self-role-change don't fire first).
        admin_role = await _mk_role(db_session, "DLA_A", ["admin"])
        mgr_role = await _mk_role(db_session, "DLA_M", ["users.manage", "chat.own"])
        guest_role = await _mk_role(db_session, "DLA_G", ["chat.own"])
        the_admin = await _mk_user(db_session, "dla_admin", admin_role)
        manager = await _mk_user(db_session, "dla_mgr", mgr_role)
        with pytest.raises(HTTPException) as exc:
            await update_user(
                user_id=the_admin.id,
                request=UpdateUserRequest(role_id=guest_role.id),
                db=db_session,
                current_user=manager,
            )
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# H3 — admin reset-password revokes sessions (gap test)
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestResetPasswordRevocation:
    async def test_reset_bumps_epoch_and_forces_rotation(self, db_session):
        mgr_role = await _mk_role(db_session, "RP_M", ["users.manage"])
        manager = await _mk_user(db_session, "rp_mgr", mgr_role)
        victim_role = await _mk_role(db_session, "RP_V", ["chat.own"])
        victim = await _mk_user(db_session, "rp_victim", victim_role)
        assert (victim.token_epoch or 0) == 0

        await reset_password(
            user_id=victim.id,
            request=ResetPasswordRequest(new_password="FreshPass123!"),
            db=db_session,
            current_user=manager,
        )
        await db_session.refresh(victim)
        assert victim.token_epoch == 1  # attacker's live tokens revoked
        assert victim.must_change_password is True


# ---------------------------------------------------------------------------
# H1 — roles routes allow auth-off (user=None) — no fail-closed over-block
# ---------------------------------------------------------------------------

@pytest.mark.database
class TestRolesAuthOff:
    async def test_create_role_allowed_when_no_caller(self, db_session):
        role = await create_role(
            request=CreateRoleRequest(name="AuthOffRole", description="", permissions=["admin"]),
            db=db_session,
            user=None,  # auth-off / single-user mode
        )
        assert "admin" in role.permissions

    async def test_update_role_allowed_when_no_caller(self, db_session):
        role = await _mk_role(db_session, "AO_U", ["chat.own"])
        await update_role(
            role_id=role.id,
            request=UpdateRoleRequest(permissions=["admin", "kb.all"]),
            db=db_session,
            user=None,
        )
        await db_session.refresh(role)
        assert set(role.permissions) == {"admin", "kb.all"}


# ---------------------------------------------------------------------------
# M2 — the WS-scoped token is rejected by the internal verify path too
# ---------------------------------------------------------------------------

class TestInternalVerifyGuards:
    async def test_ws_scoped_token_rejected(self):
        # A ws-scoped token must not validate as a voice session (scope check runs
        # before any DB lookup, so no fixtures needed).
        ws = create_ws_token_jwt(1, token_epoch=0)
        with pytest.raises(HTTPException) as exc:
            await verify_token(VerifyRequest(token=ws))
        assert exc.value.status_code == 401
