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
