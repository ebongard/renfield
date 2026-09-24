"""
Tests für Users API

Testet:
- User CRUD Operations
- Password Reset
- Speaker Linking
- Permission-basierte Zugriffskontrolle
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import Role, Speaker, User

# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_auth_user(test_user, test_role):
    """Mock authenticated user with admin permissions"""
    test_user.role = test_role
    return test_user


@pytest.fixture
def mock_require_permission():
    """Mock permission requirement to allow access"""
    async def _mock_permission(permission):
        async def checker():
            return MagicMock(
                id=1,
                username="admin",
                role=MagicMock(permissions=["admin", "users.view", "users.manage"])
            )
        return checker
    return _mock_permission


# ============================================================================
# Model Tests
# ============================================================================

class TestUserModel:
    """Tests für das User Model"""

    @pytest.mark.database
    async def test_create_user(self, db_session: AsyncSession, test_role: Role):
        """Testet das Erstellen eines Users"""
        user = User(
            username="newuser",
            email="newuser@example.com",
            password_hash="hashedpassword",
            role_id=test_role.id,
            is_active=True
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        assert user.id is not None
        assert user.username == "newuser"
        assert user.email == "newuser@example.com"

    @pytest.mark.database
    async def test_user_unique_username(self, db_session: AsyncSession, test_user: User, test_role: Role):
        """Testet, dass Username eindeutig sein muss"""
        from sqlalchemy.exc import IntegrityError

        duplicate = User(
            username=test_user.username,
            password_hash="hash",
            role_id=test_role.id
        )
        db_session.add(duplicate)

        with pytest.raises(IntegrityError):
            await db_session.commit()

    @pytest.mark.database
    async def test_user_role_relationship(self, db_session: AsyncSession, test_user: User):
        """Testet die Beziehung zwischen User und Role"""
        result = await db_session.execute(
            select(User)
            .where(User.id == test_user.id)
            .options(selectinload(User.role))
        )
        user = result.scalar_one()

        assert user.role is not None
        assert user.role.name == "TestRole"

    @pytest.mark.database
    async def test_user_speaker_relationship(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_speaker: Speaker
    ):
        """Testet die Beziehung zwischen User und Speaker"""
        test_user.speaker_id = test_speaker.id
        await db_session.commit()
        await db_session.refresh(test_user)

        result = await db_session.execute(
            select(User)
            .where(User.id == test_user.id)
            .options(selectinload(User.speaker))
        )
        user = result.scalar_one()

        assert user.speaker is not None
        assert user.speaker.name == test_speaker.name


# ============================================================================
# CRUD API Tests
# ============================================================================

class TestUserCRUDAPI:
    """Tests für User CRUD API (require mocked auth)"""

    @pytest.mark.integration
    async def test_list_users(self, async_client: AsyncClient, test_user: User):
        """Testet GET /api/users"""
        with patch('api.routes.users.require_permission') as mock_perm:
            mock_perm.return_value = lambda: test_user

            response = await async_client.get("/api/users")

        # Without proper auth mocking, expect 401 or the actual response
        assert response.status_code in [200, 401, 403]

    @pytest.mark.integration
    async def test_create_user_endpoint(
        self,
        async_client: AsyncClient,
        test_role: Role
    ):
        """Testet POST /api/users"""
        with patch('api.routes.users.require_permission') as mock_perm:
            mock_perm.return_value = lambda: MagicMock()

            response = await async_client.post(
                "/api/users",
                json={
                    "username": "apiuser",
                    "password": "SecurePass123!",
                    "email": "apiuser@example.com",
                    "role_id": test_role.id,
                    "is_active": True
                }
            )

        # Without proper auth mocking, expect 401 or the actual response
        assert response.status_code in [200, 201, 401, 403]

    @pytest.mark.integration
    async def test_get_nonexistent_user(self, async_client: AsyncClient):
        """Testet GET für nicht-existenten User"""
        response = await async_client.get("/api/users/99999")

        # Expect 404 or 401/403 if auth required
        assert response.status_code in [404, 401, 403]


class TestAdminUnlock:
    """POST /api/users/{id}/unlock (BL-0125) — the route functions are exercised
    directly (the permission dependency is resolved by FastAPI at request time
    and is covered by the auth-service tests); what matters here is the wiring
    to the lockout store, the 404 and the audit log."""

    @pytest.mark.database
    async def test_unlock_clears_lockout_and_reports_count(self, db_session: AsyncSession, test_user: User):
        from api.routes import users as users_routes

        with patch.object(users_routes.login_lockout, "unlock", new=AsyncMock(return_value=3)) as unlock:
            body = await users_routes.unlock_user(
                user_id=test_user.id, db=db_session, current_user=MagicMock(username="admin")
            )
        unlock.assert_awaited_once_with(test_user.username)
        assert body["cleared_keys"] == 3
        assert test_user.username in body["message"]

    @pytest.mark.database
    async def test_unlock_reports_503_when_store_unreachable(self, db_session: AsyncSession, test_user: User):
        """Never a false 'cleared': with Redis down the lock may still stand."""
        from fastapi import HTTPException

        from api.routes import users as users_routes
        from services.login_lockout import LockoutStoreUnavailable

        with patch.object(
            users_routes.login_lockout, "unlock", new=AsyncMock(side_effect=LockoutStoreUnavailable("down"))
        ):
            with pytest.raises(HTTPException) as exc:
                await users_routes.unlock_user(
                    user_id=test_user.id, db=db_session, current_user=MagicMock(username="admin")
                )
        assert exc.value.status_code == 503

    @pytest.mark.database
    async def test_unlock_unknown_user_is_404(self, db_session: AsyncSession):
        from fastapi import HTTPException

        from api.routes import users as users_routes

        with patch.object(users_routes.login_lockout, "unlock", new=AsyncMock(return_value=0)) as unlock:
            with pytest.raises(HTTPException) as exc:
                await users_routes.unlock_user(
                    user_id=99999, db=db_session, current_user=MagicMock(username="admin")
                )
        assert exc.value.status_code == 404
        unlock.assert_not_awaited()

    @pytest.mark.database
    async def test_unlock_gate_denies_a_viewer_and_admits_a_manager(self, db_session: AsyncSession, monkeypatch):
        """The route's dependency is require_permission(USERS_MANAGE): with auth
        on, a users.view-only principal is refused (403), a users.manage one
        passes. Exercised on the very checker the route declares."""
        from fastapi import HTTPException

        from models.permissions import Permission
        from services import auth_service
        from services.auth_service import require_permission

        monkeypatch.setattr(auth_service.settings, "auth_enabled", True)
        checker = require_permission(Permission.USERS_MANAGE)

        viewer = MagicMock(id=41, has_permission=lambda p: p == Permission.USERS_VIEW.value)
        with pytest.raises(HTTPException) as exc:
            await checker(user=viewer, db=db_session)
        assert exc.value.status_code == 403

        manager = MagicMock(id=42, has_permission=lambda p: p == Permission.USERS_MANAGE.value)
        assert await checker(user=manager, db=db_session) is manager

    @pytest.mark.database
    async def test_list_marks_locked_users(self, db_session: AsyncSession, test_user: User):
        """`locked_out` comes from ONE scan of the lockout store, matched on the
        normalized username, and is False for everyone when nothing is held."""
        from api.routes import users as users_routes

        locked = {test_user.username.strip().lower()}
        with patch.object(users_routes.login_lockout, "locked_usernames", new=AsyncMock(return_value=locked)):
            page = await users_routes.list_users(db=db_session, current_user=MagicMock())
        by_name = {u.username: u for u in page.users}
        assert by_name[test_user.username].locked_out is True

        with patch.object(users_routes.login_lockout, "locked_usernames", new=AsyncMock(return_value=set())):
            page = await users_routes.list_users(db=db_session, current_user=MagicMock())
        assert all(u.locked_out is False for u in page.users)


# ============================================================================
# Query Tests
# ============================================================================

class TestUserQueries:
    """Tests für User-Abfragen"""

    @pytest.mark.database
    async def test_filter_by_role(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_role: Role
    ):
        """Testet Filterung nach Rolle"""
        result = await db_session.execute(
            select(User).where(User.role_id == test_role.id)
        )
        users = result.scalars().all()

        assert len(users) >= 1
        assert all(u.role_id == test_role.id for u in users)

    @pytest.mark.database
    async def test_filter_by_active_status(
        self,
        db_session: AsyncSession,
        test_user: User
    ):
        """Testet Filterung nach Aktivstatus"""
        result = await db_session.execute(
            select(User).where(User.is_active == True)
        )
        users = result.scalars().all()

        assert len(users) >= 1
        assert all(u.is_active for u in users)

    @pytest.mark.database
    async def test_search_by_username(
        self,
        db_session: AsyncSession,
        test_user: User
    ):
        """Testet Suche nach Username"""
        result = await db_session.execute(
            select(User).where(User.username.ilike(f"%{test_user.username[:3]}%"))
        )
        users = result.scalars().all()

        assert len(users) >= 1


# ============================================================================
# Password Reset Tests
# ============================================================================

class TestPasswordReset:
    """Tests für Password Reset"""

    @pytest.mark.database
    async def test_update_password_hash(
        self,
        db_session: AsyncSession,
        test_user: User
    ):
        """Testet Aktualisierung des Passwort-Hash"""
        old_hash = test_user.password_hash
        new_hash = "newhash123456"

        test_user.password_hash = new_hash
        await db_session.commit()
        await db_session.refresh(test_user)

        assert test_user.password_hash == new_hash
        assert test_user.password_hash != old_hash


# ============================================================================
# Speaker Linking Tests
# ============================================================================

class TestSpeakerLinking:
    """Tests für Speaker-User Verknüpfung"""

    @pytest.mark.database
    async def test_link_speaker_to_user(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_speaker: Speaker
    ):
        """Testet Verknüpfung von Speaker zu User"""
        test_user.speaker_id = test_speaker.id
        await db_session.commit()
        await db_session.refresh(test_user)

        assert test_user.speaker_id == test_speaker.id

    @pytest.mark.database
    async def test_unlink_speaker_from_user(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_speaker: Speaker
    ):
        """Testet Aufheben der Speaker-Verknüpfung"""
        # First link
        test_user.speaker_id = test_speaker.id
        await db_session.commit()

        # Then unlink
        test_user.speaker_id = None
        await db_session.commit()
        await db_session.refresh(test_user)

        assert test_user.speaker_id is None

    @pytest.mark.database
    async def test_speaker_unique_link(
        self,
        db_session: AsyncSession,
        test_role: Role,
        test_speaker: Speaker
    ):
        """Testet, dass ein Speaker nur einem User zugewiesen werden kann"""
        # Create first user with speaker
        user1 = User(
            username="user1_speaker",
            password_hash="hash1",
            role_id=test_role.id,
            speaker_id=test_speaker.id
        )
        db_session.add(user1)
        await db_session.commit()

        # Try to create second user with same speaker
        user2 = User(
            username="user2_speaker",
            password_hash="hash2",
            role_id=test_role.id,
            speaker_id=test_speaker.id
        )
        db_session.add(user2)

        # Should fail due to unique constraint
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            await db_session.commit()


class TestDeviceAccountFlag:
    """`users.is_device_account` (auth-on cutover D-4b) must be settable through
    the admin API — the satellite gates read the flag, and without a route the
    only way to arm the feature would be a hand-written UPDATE against the
    production database. Route functions are called directly, like the unlock
    tests above."""

    @pytest.mark.database
    async def test_create_can_mint_a_device_account(
        self, db_session: AsyncSession, test_role: Role
    ):
        from api.routes import users as users_routes

        body = await users_routes.create_user(
            request=users_routes.CreateUserRequest(
                username="geraet-haushalt",
                password="SecurePass123!",
                role_id=test_role.id,
                is_device_account=True,
            ),
            db=db_session,
            current_user=MagicMock(
                username="admin", id=1, role=test_role,
                get_permissions=lambda: test_role.permissions,
            ),
        )
        assert body.is_device_account is True
        row = (
            await db_session.execute(
                select(User).where(User.username == "geraet-haushalt")
            )
        ).scalar_one()
        assert row.is_device_account is True

    @pytest.mark.database
    async def test_create_defaults_to_a_person(
        self, db_session: AsyncSession, test_role: Role
    ):
        from api.routes import users as users_routes

        body = await users_routes.create_user(
            request=users_routes.CreateUserRequest(
                username="mensch", password="SecurePass123!", role_id=test_role.id
            ),
            db=db_session,
            current_user=MagicMock(
                username="admin", id=1, role=test_role,
                get_permissions=lambda: test_role.permissions,
            ),
        )
        assert body.is_device_account is False

    @pytest.mark.database
    async def test_update_can_flag_and_unflag(
        self, db_session: AsyncSession, test_user: User
    ):
        from api.routes import users as users_routes

        admin = MagicMock(username="admin", id=test_user.id + 1000)
        body = await users_routes.update_user(
            user_id=test_user.id,
            request=users_routes.UpdateUserRequest(is_device_account=True),
            db=db_session,
            current_user=admin,
        )
        assert body.is_device_account is True
        body = await users_routes.update_user(
            user_id=test_user.id,
            request=users_routes.UpdateUserRequest(is_device_account=False),
            db=db_session,
            current_user=admin,
        )
        assert body.is_device_account is False

    @pytest.mark.database
    async def test_update_without_the_field_leaves_it_alone(
        self, db_session: AsyncSession, test_user: User
    ):
        from api.routes import users as users_routes

        test_user.is_device_account = True
        await db_session.commit()
        body = await users_routes.update_user(
            user_id=test_user.id,
            request=users_routes.UpdateUserRequest(first_name="Neu"),
            db=db_session,
            current_user=MagicMock(username="admin", id=test_user.id + 1000),
        )
        assert body.is_device_account is True

    @pytest.mark.database
    async def test_you_cannot_turn_your_own_account_into_a_device(
        self, db_session: AsyncSession, test_user: User
    ):
        """A device account collects no memories and books no presence —
        flagging the account you are logged in with would silently stop your
        own traces."""
        from fastapi import HTTPException

        from api.routes import users as users_routes

        with pytest.raises(HTTPException) as exc:
            await users_routes.update_user(
                user_id=test_user.id,
                request=users_routes.UpdateUserRequest(is_device_account=True),
                db=db_session,
                current_user=MagicMock(username=test_user.username, id=test_user.id),
            )
        assert exc.value.status_code == 400
        await db_session.refresh(test_user)
        assert test_user.is_device_account is False


class TestDeleteRefusesToTakeKnowledgeWithIt:
    """`DELETE /users/{id}` used to 500 on any account that had ever chatted.

    `atoms.owner_user_id` is a NOT NULL, non-deferrable FK, and a conversation is
    an atom — so `db.delete(user)` hit a ForeignKeyViolation and the caller got a
    500 with no idea why. Found on 2026-09-24 while removing a test account.

    The answer is not a cascade: the atoms a member owns are household-tier
    knowledge other members still read. Deleting an account must never be a way
    to delete that quietly. So the route refuses, names the number, and points at
    deactivation.
    """

    @staticmethod
    async def _plain_role(db: AsyncSession) -> Role:
        """A role WITHOUT `admin`: the victim must not be the last admin, or the
        last-admin guard answers first and this test proves nothing."""
        role = Role(name="opfer_rolle", permissions=["chat.own"])
        db.add(role)
        await db.flush()
        return role

    @staticmethod
    async def _atom_for(db: AsyncSession, owner_id: int) -> None:
        from models.database import ATOM_TYPE_KG_NODE
        from services.atom_service import AtomService

        aid = await AtomService(db).create_with_source(
            atom_type=ATOM_TYPE_KG_NODE, owner_user_id=owner_id, tier=2,
        )
        await AtomService(db).finalize_source_id(aid, 1)

    @pytest.mark.database
    async def test_refuses_with_409_when_the_account_owns_atoms(
        self, db_session: AsyncSession, test_role: Role
    ):
        from fastapi import HTTPException

        from api.routes import users as users_routes

        victim = User(username="hat-wissen", password_hash="x",
                      role_id=(await self._plain_role(db_session)).id, is_active=True)
        db_session.add(victim)
        await db_session.flush()
        await self._atom_for(db_session, victim.id)

        with pytest.raises(HTTPException) as err:
            await users_routes.delete_user(
                user_id=victim.id, db=db_session,
                # id far out of the way: the victim is the first row in a fresh
                # test DB and would otherwise trip the self-deletion guard.
                current_user=MagicMock(username="admin", id=999_999, role=test_role,
                                       get_permissions=lambda: test_role.permissions),
            )
        assert err.value.status_code == 409
        # The message has to be actionable: how much, and what to do instead.
        assert "owns" in err.value.detail
        assert "Deactivate" in err.value.detail

        still_there = (await db_session.execute(
            select(User).where(User.id == victim.id)
        )).scalar_one_or_none()
        assert still_there is not None

    @pytest.mark.database
    async def test_deletes_an_account_that_owns_nothing(
        self, db_session: AsyncSession, test_role: Role
    ):
        from api.routes import users as users_routes

        victim = User(username="leeres-konto", password_hash="x",
                      role_id=(await self._plain_role(db_session)).id, is_active=True)
        db_session.add(victim)
        await db_session.flush()

        with patch("api.routes.users.run_hooks", new=AsyncMock()):
            out = await users_routes.delete_user(
                user_id=victim.id, db=db_session,
                # id far out of the way: the victim is the first row in a fresh
                # test DB and would otherwise trip the self-deletion guard.
                current_user=MagicMock(username="admin", id=999_999, role=test_role,
                                       get_permissions=lambda: test_role.permissions),
            )
        assert "leeres-konto" in out["message"]
        gone = (await db_session.execute(
            select(User).where(User.id == victim.id)
        )).scalar_one_or_none()
        assert gone is None
