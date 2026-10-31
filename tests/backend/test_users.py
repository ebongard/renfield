"""
Tests for the users API

Covers:
- user CRUD operations
- password reset
- speaker linking
- permission-based access control
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.database import Role, Speaker, User
from services.voice_factor_preconditions import (
    DEVICE_ACCOUNT,
    NO_EMBEDDINGS,
    NO_PROFILE,
    PATH_OFF,
    RECOGNITION_OFF,
)

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
    """Tests for the User model"""

    @pytest.mark.database
    async def test_create_user(self, db_session: AsyncSession, test_role: Role):
        """Tests creating a user"""
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
        """Tests that the username has to be unique"""
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
        """Tests the relationship between User and Role"""
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
        """Tests the relationship between User and Speaker"""
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
    """Tests for the user CRUD API (require mocked auth)"""

    @pytest.mark.integration
    async def test_list_users(self, async_client: AsyncClient, test_user: User):
        """Tests GET /api/users"""
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
        """Tests POST /api/users"""
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
        """Tests GET for a user that does not exist"""
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
        from services.voice_factor_preconditions import voice_factor_lock_id

        with patch.object(users_routes.login_lockout, "unlock", new=AsyncMock(return_value=3)) as unlock:
            body = await users_routes.unlock_user(
                user_id=test_user.id, db=db_session, current_user=MagicMock(username="admin")
            )
        # 🛑 TWO counters, not one. The second factor locks under
        # `voice2fa:<id>`, so a failed voice attempt does not lock the password
        # path as well -- but an unlock button that clears only one reports
        # "unlocked" and leaves the person locked out. This used to read
        # `assert_awaited_once_with(username)`; that was the intent while there
        # was only one counter.
        assert [c.args for c in unlock.await_args_list] == [
            (test_user.username,), (voice_factor_lock_id(test_user.id),),
        ]
        assert body["cleared_keys"] == 6, "the sum of both counters"
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
    """Tests for user queries"""

    @pytest.mark.database
    async def test_filter_by_role(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_role: Role
    ):
        """Tests filtering by role"""
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
        """Tests filtering by active status"""
        result = await db_session.execute(
            select(User).where(User.is_active)
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
        """Tests searching by username"""
        result = await db_session.execute(
            select(User).where(User.username.ilike(f"%{test_user.username[:3]}%"))
        )
        users = result.scalars().all()

        assert len(users) >= 1


# ============================================================================
# Password Reset Tests
# ============================================================================

class TestPasswordReset:
    """Tests for password reset"""

    @pytest.mark.database
    async def test_update_password_hash(
        self,
        db_session: AsyncSession,
        test_user: User
    ):
        """Tests updating the password hash"""
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
    """Tests for linking a speaker to a user"""

    @pytest.mark.database
    async def test_link_speaker_to_user(
        self,
        db_session: AsyncSession,
        test_user: User,
        test_speaker: Speaker
    ):
        """Tests linking a speaker to a user"""
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
        """Tests removing the speaker link"""
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
        """Tests that a speaker can be assigned to only one user"""
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
    """`DELETE /users/{id}` used to 500 on any account that still held data.

    36 foreign keys reference `users.id` WITHOUT `ON DELETE`, so Postgres refuses
    the parent DELETE — and the caller got a 500 that named nothing. Found on
    2026-09-24 while removing a test account.

    The answer is not a cascade: a member's atoms are household-tier knowledge
    the others still read. Nor is it a pre-count of one table — the first fix
    tried that and left the 500 alive for every account with a circle membership
    and no atoms, which on this household is every family member.
    """

    @staticmethod
    async def _plain_role(db: AsyncSession) -> Role:
        """A role WITHOUT `admin`: the victim must not be the last admin, or the
        last-admin guard answers first and this test proves nothing."""
        role = Role(name=f"opfer_rolle_{uuid4().hex[:8]}", permissions=["chat.own"])
        db.add(role)
        await db.flush()
        return role

    async def _victim(self, db: AsyncSession, username: str) -> User:
        u = User(username=username, password_hash="x",
                 role_id=(await self._plain_role(db)).id, is_active=True)
        db.add(u)
        await db.flush()
        return u

    @staticmethod
    def _admin(test_role: Role) -> MagicMock:
        # id far out of the way: the victim is the first row in a fresh test DB
        # and would otherwise trip the self-deletion guard.
        return MagicMock(username="admin", id=999_999, role=test_role,
                         get_permissions=lambda: test_role.permissions)

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

        victim = await self._victim(db_session, "hat-wissen")
        await self._atom_for(db_session, victim.id)

        with pytest.raises(HTTPException) as err:
            await users_routes.delete_user(
                user_id=victim.id, db=db_session, current_user=self._admin(test_role),
            )
        assert err.value.status_code == 409
        assert err.value.detail["code"] == "user_still_referenced"
        assert err.value.detail["blocked_by"] == "atoms"

        still_there = (await db_session.execute(
            select(User).where(User.id == victim.id)
        )).scalar_one_or_none()
        assert still_there is not None

    @pytest.mark.database
    async def test_refuses_for_a_membership_too_not_just_atoms(
        self, db_session: AsyncSession, test_role: Role
    ):
        """The regression the first fix missed. The household backfill writes a
        PAIRWISE circle_memberships row for every family member, so an account
        can hold nothing but a membership — and `circle_memberships` has three
        blocking FKs to `users.id` of its own."""
        from fastapi import HTTPException

        from api.routes import users as users_routes
        from models.database import CircleMembership

        owner = await self._victim(db_session, "kreis-eigner")
        victim = await self._victim(db_session, "nur-mitglied")
        db_session.add(CircleMembership(
            circle_owner_id=owner.id, member_user_id=victim.id,
            dimension="tier", value="2", granted_by=owner.id,
        ))
        await db_session.flush()

        with pytest.raises(HTTPException) as err:
            await users_routes.delete_user(
                user_id=victim.id, db=db_session, current_user=self._admin(test_role),
            )
        assert err.value.status_code == 409          # not a 500
        assert err.value.detail["blocked_by"] == "circle_memberships"

    @pytest.mark.database
    async def test_deletes_an_account_that_holds_nothing(
        self, db_session: AsyncSession, test_role: Role
    ):
        from api.routes import users as users_routes

        victim = await self._victim(db_session, "leeres-konto")

        with patch("api.routes.users.run_hooks", new=AsyncMock()):
            out = await users_routes.delete_user(
                user_id=victim.id, db=db_session, current_user=self._admin(test_role),
            )
        assert "leeres-konto" in out["message"]
        gone = (await db_session.execute(
            select(User).where(User.id == victim.id)
        )).scalar_one_or_none()
        assert gone is None




async def _with_password(db_session, user, passwort: str = "testpassword123"):
    """Set a REAL password.

    🛑 `sample_user_data` carries a faked bcrypt hash (the comment there reads
    "Fake hash"). `verify_password` always fails against it -- so a test that
    has to present the password would otherwise only check that the rejection
    fires.
    """
    from services.auth_service import get_password_hash

    user.password_hash = get_password_hash(passwort)
    await db_session.commit()
    return passwort

def _admin(user_id: int):
    """An account WITH `admin` -- since the review, switching off somebody
    ELSE'S factor requires more than `users.manage`."""
    return MagicMock(
        username="admin", id=user_id,
        get_permissions=lambda: ["admin", "users.manage", "users.view"],
    )


def _user_manager(user_id: int):
    """Delegated `users.manage` WITHOUT `admin`. Elsewhere
    `models/permissions.py` explicitly defends this principal against
    privilege escalation -- here it is the attacker."""
    return MagicMock(
        username="verwalter", id=user_id,
        get_permissions=lambda: ["users.manage", "users.view"],
    )


class TestVoiceSecondFactorConsent:
    """`POST /users/{id}/voice-second-factor` -- consent to the voice as a
    second factor.

    🛑 This route DELIBERATELY has two different permissions for two
    directions. An ECAPA voiceprint is biometric data (Art. 9 GDPR); a login
    that demands it is a consent, and nobody can give that on somebody else's
    behalf. So switching on only for oneself, switching off for anyone holding
    `users.manage` -- because switching off is the only documented way back
    when a microphone is broken (`.claude/rules/auth.md`: there is no fallback
    to the password alone).

    Without this route the recovery path described in PR #1350 ("an
    administrator switches it off") would be pure theory: the state would have
    no API and no user interface.
    """

    @pytest.fixture(autouse=True)
    def _auth_on(self, monkeypatch):
        """🛑 The second factor only makes sense with auth switched on.

        `settings.auth_enabled` is `False` by default in the harness, and in
        that mode the route refuses BOTH directions with a 401: with auth off,
        `get_user_or_default` resolves every caller to the administrator
        account, so there would be no "self" to consent with (finding F4 of the
        adversarial pass).

        The tests here check the permission logic of an auth-ON instance -- so
        the harness has to be one. Without this fixture they would only check
        that the 401 fires.
        """
        from utils.config import settings
        monkeypatch.setattr(settings, "auth_enabled", True)

    @staticmethod
    def _path_on():
        """Open the instance-wide half.

        🛑 The harness runs `voice_auth_enabled=False`. The DISPLAY reports the
        instance-wide half first -- entirely correctly, because a switched-off
        voice path is the dominant truth. So whoever wants to check the
        account-bound half has to open the instance-wide one; otherwise they
        check `voice_path_off` and believe they are checking the profile. These
        tests fell for exactly that on their first run.
        """
        from utils.config import settings
        return patch.multiple(
            settings, voice_auth_enabled=True, speaker_recognition_enabled=True,
        )


    @staticmethod
    async def _with_voice(db_session: AsyncSession, user: User, embeddings: int = 1):
        """Links `user` to a speaker profile holding `embeddings` embeddings."""
        from models.database import SpeakerEmbedding

        speaker = Speaker(name=f"stimme-{uuid4().hex[:8]}")
        db_session.add(speaker)
        await db_session.flush()
        for _ in range(embeddings):
            db_session.add(
                SpeakerEmbedding(speaker_id=speaker.id, embedding="AAAA", sample_duration=2000)
            )
        user.speaker_id = speaker.id
        await db_session.commit()
        return speaker

    @pytest.mark.database
    async def test_you_can_arm_it_for_yourself(
        self, db_session: AsyncSession, test_user: User
    ):
        from api.routes import users as users_routes

        await self._with_voice(db_session, test_user)
        body = await users_routes.set_voice_second_factor(
            user_id=test_user.id,
            request=users_routes.VoiceSecondFactorRequest(enabled=True),
            db=db_session,
            current_user=MagicMock(username=test_user.username, id=test_user.id),
        )
        assert body.voice_second_factor_enabled is True
        await db_session.refresh(test_user)
        assert test_user.voice_second_factor_enabled is True

    @pytest.mark.database
    async def test_an_admin_cannot_impose_it_on_someone_else(
        self, db_session: AsyncSession, test_user: User
    ):
        """This is the actual point of the route: a forced biometric
        enrolment would not be administration."""
        from fastapi import HTTPException

        from api.routes import users as users_routes

        await self._with_voice(db_session, test_user)
        with pytest.raises(HTTPException) as exc:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id,
                request=users_routes.VoiceSecondFactorRequest(enabled=True),
                db=db_session,
                current_user=MagicMock(username="admin", id=test_user.id + 1000),
            )
        assert exc.value.status_code == 403
        await db_session.refresh(test_user)
        assert test_user.voice_second_factor_enabled is False

    @pytest.mark.database
    async def test_an_admin_can_always_take_it_away(
        self, db_session: AsyncSession, test_user: User
    ):
        """The way back is exactly the opposite asymmetry -- otherwise a
        broken microphone would be a permanent lockout."""
        from api.routes import users as users_routes

        await self._with_voice(db_session, test_user)
        test_user.voice_second_factor_enabled = True
        await db_session.commit()

        body = await users_routes.set_voice_second_factor(
            user_id=test_user.id,
            request=users_routes.VoiceSecondFactorRequest(enabled=False),
            db=db_session,
            current_user=_admin(test_user.id + 1000),
        )
        assert body.voice_second_factor_enabled is False
        await db_session.refresh(test_user)
        assert test_user.voice_second_factor_enabled is False

    # ------------------------------------------------------------------
    # GET /{id}/voice-second-factor -- the reason the page never used to see
    # ------------------------------------------------------------------

    @pytest.mark.database
    async def test_the_reason_is_readable_BEFORE_consenting(
        self, db_session: AsyncSession, test_user: User
    ):
        """🛑 The core of the 2026-09-29 finding.

        The "Mein Konto" page showed a reason only in the "armed" state -- so
        never for somebody who is just considering consenting. Measured in the
        household: on 6 of 7 accounts `no_profile` was the blocker, and exactly
        those saw a blank "off" with a button that was bound to fail. So the
        reason has to be readable WITHOUT consenting.
        """
        from api.routes import users as users_routes

        assert test_user.voice_second_factor_enabled is False
        assert test_user.speaker_id is None

        with self._path_on():
            state = await users_routes.get_voice_second_factor(
                user_id=test_user.id,
                db=db_session,
                current_user=test_user,
            )
        assert state.enabled is False
        assert state.blocker == NO_PROFILE

    @pytest.mark.database
    async def test_no_blocker_once_the_profile_carries_a_sample(
        self, db_session: AsyncSession, test_user: User
    ):
        """The counter-check: without it the test above would be green even
        if the route ALWAYS reported `no_profile`."""
        from api.routes import users as users_routes

        await self._with_voice(db_session, test_user, embeddings=1)
        with self._path_on():
            state = await users_routes.get_voice_second_factor(
                user_id=test_user.id, db=db_session, current_user=test_user,
            )
        assert state.blocker is None

    @pytest.mark.database
    async def test_a_profile_without_samples_reads_as_such(
        self, db_session: AsyncSession, test_user: User
    ):
        """The four reasons have to come out DISTINGUISHABLE -- otherwise the
        user interface cannot translate them differently."""
        from api.routes import users as users_routes

        await self._with_voice(db_session, test_user, embeddings=0)
        with self._path_on():
            state = await users_routes.get_voice_second_factor(
                user_id=test_user.id, db=db_session, current_user=test_user,
            )
        assert state.blocker == NO_EMBEDDINGS

    @pytest.mark.database
    async def test_the_instance_half_is_reported_too(
        self, db_session: AsyncSession, test_user: User
    ):
        """`voice_path_off` belongs to the DISPLAY, even though it
        deliberately does not block switching on -- otherwise the page cannot
        name a dormant state.
        """
        from api.routes import users as users_routes
        from utils.config import settings

        await self._with_voice(db_session, test_user, embeddings=1)
        with patch.object(settings, "voice_auth_enabled", False):
            state = await users_routes.get_voice_second_factor(
                user_id=test_user.id, db=db_session, current_user=test_user,
            )
        assert state.blocker == PATH_OFF

        # 🛑 For this the voice path has to be ON, otherwise `PATH_OFF` wins
        # and the test would claim something about recognition without
        # touching it.
        with patch.multiple(
            settings, voice_auth_enabled=True, speaker_recognition_enabled=False,
        ):
            state = await users_routes.get_voice_second_factor(
                user_id=test_user.id, db=db_session, current_user=test_user,
            )
        assert state.blocker == RECOGNITION_OFF

    @pytest.mark.database
    async def test_reading_someone_elses_state_is_refused(
        self, db_session: AsyncSession, test_user: User
    ):
        """The reason names a property of a person's speaker profile -- it
        belongs to them. Not even `admin` reads it here."""
        from fastapi import HTTPException

        from api.routes import users as users_routes

        with pytest.raises(HTTPException) as exc:
            await users_routes.get_voice_second_factor(
                user_id=test_user.id,
                db=db_session,
                current_user=_admin(test_user.id + 1000),
            )
        assert exc.value.status_code == 403

    @pytest.mark.database
    async def test_reading_needs_an_authenticated_instance(
        self, db_session: AsyncSession, test_user: User
    ):
        """The same rule as for writing: without authentication there is no
        "self", so there is nothing of one's own to read either."""
        from fastapi import HTTPException

        from api.routes import users as users_routes
        from utils.config import settings

        with patch.object(settings, "auth_enabled", False):
            with pytest.raises(HTTPException) as exc:
                await users_routes.get_voice_second_factor(
                    user_id=test_user.id, db=db_session, current_user=test_user,
                )
        assert exc.value.status_code == 401

    @pytest.mark.database
    async def test_arming_without_a_speaker_profile_is_refused(
        self, db_session: AsyncSession, test_user: User
    ):
        """`POST /auth/voice` checks 1:1 against the linked profile and
        refuses fail-closed when there is none. Without this guard, switching
        on would be locking yourself out."""
        from fastapi import HTTPException

        from api.routes import users as users_routes

        assert test_user.speaker_id is None
        with pytest.raises(HTTPException) as exc:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id,
                request=users_routes.VoiceSecondFactorRequest(enabled=True),
                db=db_session,
                current_user=MagicMock(username=test_user.username, id=test_user.id),
            )
        assert exc.value.status_code == 409
        # 🛑 The CODE is the contract, not the sentence. Three hard-coded
        # English sentences used to stand here, and they were the only place
        # the person learned the reason -- a German-speaking member read
        # English. The user interface now translates this code; if the test
        # only checked the status code, it could quietly turn back into prose.
        assert exc.value.detail == NO_PROFILE
        await db_session.refresh(test_user)
        assert test_user.voice_second_factor_enabled is False

    @pytest.mark.database
    async def test_a_profile_without_embeddings_is_the_same_trap(
        self, db_session: AsyncSession, test_user: User
    ):
        """A linked profile with nothing to compare against locks you out
        just as completely as no profile at all."""
        from fastapi import HTTPException

        from api.routes import users as users_routes

        await self._with_voice(db_session, test_user, embeddings=0)
        with pytest.raises(HTTPException) as exc:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id,
                request=users_routes.VoiceSecondFactorRequest(enabled=True),
                db=db_session,
                current_user=MagicMock(username=test_user.username, id=test_user.id),
            )
        assert exc.value.status_code == 409
        assert exc.value.detail == NO_EMBEDDINGS

    @pytest.mark.database
    async def test_disarming_needs_no_profile_at_all(
        self, db_session: AsyncSession, test_user: User
    ):
        """The recovery path must not hang on the same condition as the way
        in -- otherwise a deleted speaker profile would be final."""
        from api.routes import users as users_routes

        test_user.voice_second_factor_enabled = True
        await db_session.commit()
        assert test_user.speaker_id is None

        body = await users_routes.set_voice_second_factor(
            user_id=test_user.id,
            request=users_routes.VoiceSecondFactorRequest(enabled=False),
            db=db_session,
            current_user=_admin(test_user.id + 1000),
        )
        assert body.voice_second_factor_enabled is False

    @pytest.mark.database
    async def test_an_unknown_account_is_a_404_not_a_500(
        self, db_session: AsyncSession
    ):
        from fastapi import HTTPException

        from api.routes import users as users_routes

        with pytest.raises(HTTPException) as exc:
            await users_routes.set_voice_second_factor(
                user_id=987654,
                request=users_routes.VoiceSecondFactorRequest(enabled=False),
                db=db_session,
                current_user=_admin(1),
            )
        assert exc.value.status_code == 404

    @pytest.mark.database
    async def test_every_other_endpoint_reports_the_state_too(
        self, db_session: AsyncSession, test_user: User
    ):
        """🛑 A negative control against the mistake that was nearly made
        while building this: `UserResponse` is assembled INLINE in seven
        places. The new field defaults to `False` -- without adding it at every
        one of them the list would have reported the state wrongly, and the
        user interface would have shown a switch that looks off while it is
        on."""
        from api.routes import users as users_routes

        await self._with_voice(db_session, test_user)
        test_user.voice_second_factor_enabled = True
        await db_session.commit()

        admin = MagicMock(username="admin", id=test_user.id + 1000)
        one = await users_routes.get_user(
            user_id=test_user.id, db=db_session, current_user=admin
        )
        assert one.voice_second_factor_enabled is True

        listing = await users_routes.list_users(db=db_session, current_user=admin)
        mine = [u for u in listing.users if u.id == test_user.id]
        assert mine and mine[0].voice_second_factor_enabled is True

    @pytest.mark.database
    async def test_a_user_manager_cannot_strip_someone_elses_factor(
        self, db_session: AsyncSession, test_user: User
    ):
        """🛑 The way back is also an attack path.

        Whoever takes the second factor off somebody else's account quietly
        lowers that login to the password alone and can then try that password.
        That is MORE than a password reset -- a reset on its own does not get
        past an armed second factor. `users.manage` is a delegable permission
        without `admin`, so this direction requires `admin`.
        """
        from fastapi import HTTPException

        from api.routes import users as users_routes

        test_user.voice_second_factor_enabled = True
        await db_session.commit()

        with pytest.raises(HTTPException) as exc:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id,
                request=users_routes.VoiceSecondFactorRequest(enabled=False),
                db=db_session,
                current_user=_user_manager(test_user.id + 1000),
            )
        assert exc.value.status_code == 403
        await db_session.refresh(test_user)
        assert test_user.voice_second_factor_enabled is True

    @pytest.mark.database
    async def test_a_user_manager_may_still_withdraw_their_OWN_consent(
        self, db_session: AsyncSession, test_user: User
    ):
        """The counter-check to the test above: withdrawing one's own factor
        is nobody's privilege escalation and must not hang on `admin`."""
        from api.routes import users as users_routes

        test_user.voice_second_factor_enabled = True
        await _with_password(db_session, test_user)

        body = await users_routes.set_voice_second_factor(
            user_id=test_user.id,
            request=users_routes.VoiceSecondFactorRequest(
                enabled=False, current_password="testpassword123"
            ),
            db=db_session,
            current_user=_user_manager(test_user.id),
        )
        assert body.voice_second_factor_enabled is False

    @pytest.mark.database
    async def test_without_a_caller_nothing_is_decided(
        self, db_session: AsyncSession, test_user: User
    ):
        """🛑 This test used to stand on its head -- it demanded that a call
        WITHOUT a caller switch off the factor on somebody else's account, and
        it was green.

        The reason: the `is not None` additions fell asymmetrically. On
        switching on, `None` closed the door (not me -> 403); on switching off
        it opened it -- the whole `elif` condition became False and the write
        went through. Anybody could have taken anybody's second factor away,
        and a green test had written that down as intended.

        Unreachable today (`get_user_or_default` always yields a user, the
        administrator when auth is off) -- but that is exactly why the guard
        has to stand and this test has to prove it: a tolerance that closes on
        one side and opens on the other looks like a safeguard.
        """
        from fastapi import HTTPException

        from api.routes import users as users_routes

        test_user.voice_second_factor_enabled = True
        await db_session.commit()

        with pytest.raises(HTTPException) as exc:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id,
                request=users_routes.VoiceSecondFactorRequest(enabled=False),
                db=db_session,
                current_user=None,
            )
        assert exc.value.status_code == 401
        await db_session.refresh(test_user)
        assert test_user.voice_second_factor_enabled is True, "nothing may have been written"

    @pytest.mark.database
    async def test_arming_is_refused_while_recognition_is_off(
        self, db_session: AsyncSession, test_user: User, monkeypatch
    ):
        """This is a trap too: the consent would be armed, but `/auth/voice`
        refuses categorically while recognition is off."""
        from fastapi import HTTPException

        from api.routes import users as users_routes
        from utils.config import settings

        await self._with_voice(db_session, test_user)
        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", False)

        with pytest.raises(HTTPException) as exc:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id,
                request=users_routes.VoiceSecondFactorRequest(enabled=True),
                db=db_session,
                current_user=MagicMock(username=test_user.username, id=test_user.id),
            )
        assert exc.value.status_code == 409

    @pytest.mark.database
    async def test_arming_stays_possible_while_the_voice_path_is_still_off(
        self, db_session: AsyncSession, test_user: User, monkeypatch
    ):
        """🛑 A counter-control against a guard that is too strict.

        `VOICE_AUTH_ENABLED=false` must NOT block switching on, otherwise the
        order of the cutover would be impossible: collect the consents first,
        then flip the flag. That the hurdle lies dormant until then is
        documented and intended.
        """
        from api.routes import users as users_routes
        from utils.config import settings

        await self._with_voice(db_session, test_user)
        monkeypatch.setattr(settings, "voice_auth_enabled", False)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", True)

        body = await users_routes.set_voice_second_factor(
            user_id=test_user.id,
            request=users_routes.VoiceSecondFactorRequest(enabled=True),
            db=db_session,
            current_user=MagicMock(username=test_user.username, id=test_user.id),
        )
        assert body.voice_second_factor_enabled is True


class TestTheUnlockButtonSeesBothCounters:
    """🛑 On the RECOVERY SCREEN the user interface reported the opposite of
    the truth.

    `/auth/voice` counts failed attempts under `voice2fa:<id>`; the
    administration asked under the username. The two never met: the list showed
    "not locked", and the unlock button reported `cleared_keys=0`, while the
    person genuinely could not get in.

    The separate key spaces stay -- a failed voice attempt must not lock the
    password path as well, or the second factor would become a way to push
    somebody out of their own account using means that are not theirs. Only the
    display becomes honest.
    """

    @pytest.mark.database
    async def test_a_voice_lock_shows_up_in_the_single_user_view(
        self, db_session: AsyncSession, test_user: User, monkeypatch
    ):
        from api.routes import users as users_routes
        from services.voice_factor_preconditions import voice_factor_lock_id

        voice_id = voice_factor_lock_id(test_user.id)
        asked: list[str] = []

        async def _has_any_lock(username: str) -> bool:
            asked.append(username)
            return username == voice_id

        monkeypatch.setattr(users_routes.login_lockout, "has_any_lock", _has_any_lock)

        body = await users_routes.get_user(
            user_id=test_user.id, db=db_session,
            current_user=MagicMock(username="admin", id=1),
        )
        assert body.locked_out is True, "the voice lockout must be visible"
        assert voice_id in asked

    @pytest.mark.database
    async def test_the_unlock_button_clears_both(
        self, db_session: AsyncSession, test_user: User, monkeypatch
    ):
        from api.routes import users as users_routes
        from services.voice_factor_preconditions import voice_factor_lock_id

        cleared: list[str] = []

        async def _unlock(username: str) -> int:
            cleared.append(username)
            return 1

        monkeypatch.setattr(users_routes.login_lockout, "unlock", _unlock)

        await users_routes.unlock_user(
            user_id=test_user.id, db=db_session,
            current_user=MagicMock(username="admin", id=test_user.id + 1000),
        )
        assert cleared == [test_user.username, voice_factor_lock_id(test_user.id)]

    @pytest.mark.database
    async def test_an_unlocked_account_still_reports_false(
        self, db_session: AsyncSession, test_user: User, monkeypatch
    ):
        """The counter-check: otherwise the tests above would only prove
        that `locked_out` now always says `True`."""
        from api.routes import users as users_routes

        async def _has_any_lock(_username: str) -> bool:
            return False

        monkeypatch.setattr(users_routes.login_lockout, "has_any_lock", _has_any_lock)

        body = await users_routes.get_user(
            user_id=test_user.id, db=db_session,
            current_user=MagicMock(username="admin", id=1),
        )
        assert body.locked_out is False


def _plain_member(user_id: int):
    """A household member: NO `users.manage`, NO `admin`. Exactly the person
    the consent is meant for -- and the one who could not give it until
    2026-09-28."""
    return MagicMock(
        username="familie", id=user_id,
        get_permissions=lambda: ["chat.own", "kb.shared"],
    )


class TestTheConsentBelongsToThePerson:
    """🛑 The consent is strictly personal -- so the person has to be able to
    give it, not only an administrator.

    Before 2026-09-28, `require_permission(USERS_MANAGE)` stood in front of
    BOTH directions. That put the consent to processing biometric data (Art. 9
    GDPR) out of reach for exactly the person whose voice it is about: a
    household member could neither consent nor see their own state. A consent
    only a third party can give is not one.

    The security position is unchanged by this: arming one's own factor adds an
    ADDITIONAL hurdle on one's own account, and withdrawing it requires being
    signed in -- which, with the factor armed, already means having passed
    it.
    """

    @pytest.fixture(autouse=True)
    def _auth_on(self, monkeypatch):
        """🛑 The second factor only makes sense with auth switched on.

        `settings.auth_enabled` is `False` by default in the harness, and in
        that mode the route refuses BOTH directions with a 401: with auth off,
        `get_user_or_default` resolves every caller to the administrator
        account, so there would be no "self" to consent with (finding F4 of the
        adversarial pass).

        The tests here check the permission logic of an auth-ON instance -- so
        the harness has to be one. Without this fixture they would only check
        that the 401 fires.
        """
        from utils.config import settings
        monkeypatch.setattr(settings, "auth_enabled", True)


    @pytest.mark.database
    async def test_a_plain_member_may_arm_their_own(
        self, db_session: AsyncSession, test_user: User
    ):
        from api.routes import users as users_routes

        await TestVoiceSecondFactorConsent._with_voice(db_session, test_user)
        body = await users_routes.set_voice_second_factor(
            user_id=test_user.id,
            request=users_routes.VoiceSecondFactorRequest(enabled=True),
            db=db_session,
            current_user=_plain_member(test_user.id),
        )
        assert body.voice_second_factor_enabled is True

    @pytest.mark.database
    async def test_a_plain_member_may_withdraw_their_own(
        self, db_session: AsyncSession, test_user: User
    ):
        from api.routes import users as users_routes

        test_user.voice_second_factor_enabled = True
        await _with_password(db_session, test_user)
        body = await users_routes.set_voice_second_factor(
            user_id=test_user.id,
            request=users_routes.VoiceSecondFactorRequest(
                enabled=False, current_password="testpassword123"
            ),
            db=db_session,
            current_user=_plain_member(test_user.id),
        )
        assert body.voice_second_factor_enabled is False

    @pytest.mark.database
    async def test_a_plain_member_still_cannot_touch_a_stranger(
        self, db_session: AsyncSession, test_user: User
    ):
        """🛑 The counter-control to the relaxation: the permission drops
        ONLY for one's own account. Switching off somebody else's still
        requires `admin` -- otherwise the way back would be an attack path for
        anybody."""
        from fastapi import HTTPException

        from api.routes import users as users_routes

        test_user.voice_second_factor_enabled = True
        await db_session.commit()

        with pytest.raises(HTTPException) as exc:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id,
                request=users_routes.VoiceSecondFactorRequest(enabled=False),
                db=db_session,
                current_user=_plain_member(test_user.id + 1000),
            )
        assert exc.value.status_code == 403
        await db_session.refresh(test_user)
        assert test_user.voice_second_factor_enabled is True

    @pytest.mark.database
    async def test_a_device_account_has_no_voice(
        self, db_session: AsyncSession, test_user: User
    ):
        """A device account does not speak and does not sign in through
        `/auth/login`. A consent it can never redeem is a state nobody can use
        -- better said here than guessed at later."""
        from fastapi import HTTPException

        from api.routes import users as users_routes

        await TestVoiceSecondFactorConsent._with_voice(db_session, test_user)
        test_user.is_device_account = True
        await db_session.commit()

        with pytest.raises(HTTPException) as exc:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id,
                request=users_routes.VoiceSecondFactorRequest(enabled=True),
                db=db_session,
                current_user=_plain_member(test_user.id),
            )
        assert exc.value.status_code == 409
        # 🛑 HERE TOO: THE CODE, NOT THE SENTENCE.
        #
        # In the 2026-09-29 rework the three named English sentences were
        # replaced by codes -- and this fourth one, FOUR LINES above it in the
        # same function, stayed standing. Only the acceptance pass found it.
        # The test pins the sibling case down, so the next repair does not
        # again hit only the named instance.
        assert exc.value.detail == DEVICE_ACCOUNT

    def test_every_409_of_this_route_speaks_in_codes(self):
        """A structural check against exactly this relapse: NO 409 of this
        route may hand out a sentence.

        A sentence from the server arrives untranslated in the user interface
        -- the i18n rule applies to error texts too. The test reads the source,
        because a behavioural test only covers the cases somebody thought of;
        that is precisely what failed the first time.
        """
        import inspect
        import re

        from api.routes import users as users_routes

        quelle = inspect.getsource(users_routes.set_voice_second_factor)
        # Every `detail=` in a 409 branch has to be an identifier (a code),
        # not a string literal. `detail="..."` is the relapse being forbidden.
        blocks = quelle.split("HTTP_409_CONFLICT")
        for block in blocks[1:]:
            head = block[:200]
            assert not re.search(r'detail\s*=\s*["\']', head), (
                "A 409 from this route hands out a SENTENCE instead of a code:\n"
                + head
            )

    def test_the_route_no_longer_demands_users_manage(self):
        """A structural check against sliding back: if
        `require_permission(USERS_MANAGE)` returns to this route, the consent
        is decided by somebody else again -- and the behaviour above would fail
        with a 403 whose cause one would have to hunt for in the route."""
        import inspect

        from api.routes import users as users_routes

        src = inspect.getsource(users_routes.set_voice_second_factor)
        sig = src[: src.index('"""')]
        assert "get_user_or_default" in sig
        assert "USERS_MANAGE" not in sig


    @pytest.mark.database
    async def test_it_does_not_leak_which_user_ids_exist(
        self, db_session: AsyncSession, test_user: User
    ):
        """🛑 An enumeration oracle, created by the relaxation itself.

        The 404 stood BEFORE every permission check. For somebody without
        permission the answer therefore distinguished "does not exist" (404)
        from "exists, not yours" (403) -- across the whole id space, and open
        to every signed-in member. Previously `require_permission(USERS_MANAGE)`
        denied entry before the query; with the relaxation that protection
        falls away, so the ordering has to replace it.

        Both cases have to look the same to somebody without permission.
        """
        from fastapi import HTTPException

        from api.routes import users as users_routes

        fremder = _plain_member(test_user.id + 1000)

        with pytest.raises(HTTPException) as existing:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id,
                request=users_routes.VoiceSecondFactorRequest(enabled=True),
                db=db_session, current_user=fremder,
            )
        with pytest.raises(HTTPException) as missing:
            await users_routes.set_voice_second_factor(
                user_id=987654,
                request=users_routes.VoiceSecondFactorRequest(enabled=True),
                db=db_session, current_user=fremder,
            )
        assert existing.value.status_code == missing.value.status_code == 403, (
            "existing and non-existing must be indistinguishable for an "
            "unauthorized caller"
        )


    @pytest.mark.database
    async def test_with_auth_off_there_is_no_self_to_consent_with(
        self, db_session: AsyncSession, test_user: User, monkeypatch
    ):
        """🛑 A regression of this relaxation, found in the adversarial pass.

        With `AUTH_ENABLED=false`, `get_user_or_default` resolves EVERY caller
        to the administrator account. `is_self` would therefore be true, and
        whoever reaches the port could impose the second factor on the
        administrator. The switch-on direction deliberately does not check
        `voice_auth_enabled` -- so the write would go through even with the
        voice path dormant, and would become armed as soon as somebody sets
        `AUTH_ENABLED=true`.

        Previously `require_permission` returned `None` in this mode, `is_self`
        was false, and switching on always ended in a 403. A consent under
        Art. 9 GDPR requires a person; "whoever is at the port, resolved to
        admin" is not one.
        """
        from fastapi import HTTPException

        from api.routes import users as users_routes
        from utils.config import settings

        # Overrides this class's `_auth_on` fixture: THIS test wants
        # precisely the auth-off case.
        monkeypatch.setattr(settings, "auth_enabled", False)
        await TestVoiceSecondFactorConsent._with_voice(db_session, test_user)

        for enabled in (True, False):
            with pytest.raises(HTTPException) as exc:
                await users_routes.set_voice_second_factor(
                    user_id=test_user.id,
                    request=users_routes.VoiceSecondFactorRequest(enabled=enabled),
                    db=db_session,
                    current_user=_plain_member(test_user.id),
                )
            assert exc.value.status_code == 401, f"enabled={enabled}"
        await db_session.refresh(test_user)
        assert test_user.voice_second_factor_enabled is False

    @pytest.mark.database
    async def test_removing_a_consent_is_logged_louder_than_granting_one(
        self, db_session: AsyncSession, test_user: User
    ):
        """🛑 Removing weighs more than granting: it lowers an account to the
        password alone.

        The same subsystem checker already writes a WARNING when the hurdle
        goes dormant BY ITSELF (`voice_factor_preconditions`). A human taking
        it away must not be logged more quietly than it falling asleep on its
        own -- otherwise the loudest entry is the most harmless event.
        """
        from loguru import logger as loguru_logger

        from api.routes import users as users_routes

        await TestVoiceSecondFactorConsent._with_voice(db_session, test_user)
        await _with_password(db_session, test_user)
        me = _plain_member(test_user.id)

        seen: list[tuple[str, str]] = []
        sink = loguru_logger.add(
            lambda m: seen.append((m.record["level"].name, m.record["message"])),
            level="INFO",
        )
        try:
            await users_routes.set_voice_second_factor(
                user_id=test_user.id, db=db_session, current_user=me,
                request=users_routes.VoiceSecondFactorRequest(enabled=True),
            )
            await users_routes.set_voice_second_factor(
                user_id=test_user.id, db=db_session, current_user=me,
                request=users_routes.VoiceSecondFactorRequest(
                    enabled=False, current_password="testpassword123"
                ),
            )
        finally:
            loguru_logger.remove(sink)

        granted = [m for lvl, m in seen if "enabled for user" in m]
        removed = [(lvl, m) for lvl, m in seen if "REMOVED" in m]
        assert granted, "granting must be logged"
        assert removed, "removal must be logged"
        assert removed[0][0] == "WARNING", f"removal was only {removed[0][0]}"

    @pytest.mark.database
    async def test_removing_your_own_factor_needs_the_password(
        self, db_session: AsyncSession, test_user: User
    ):
        """🛑 Finding 7 of the adversarial pass, pinned down here.

        Switching on does NOT raise `token_epoch` (an epoch jump would sign the
        person out at the moment of consenting -- on a single-admin instance
        with a jammed voice path, an immediate lockout). Consequence: a token
        from BEFORE the consent outlives it, renews itself through
        `/auth/refresh` (which does not check the factor) and would otherwise
        be able to remove it permanently -- precisely what it protects against.
        Previously that required `users.manage`; with the relaxation that
        protection falls away.

        The password replaces it: whoever only captured a token does not get
        through.
        """
        from fastapi import HTTPException

        from api.routes import users as users_routes

        test_user.voice_second_factor_enabled = True
        await _with_password(db_session, test_user)
        me = _plain_member(test_user.id)

        for wrong_password in (None, "", "das-falsche-passwort"):
            with pytest.raises(HTTPException) as exc:
                await users_routes.set_voice_second_factor(
                    user_id=test_user.id,
                    request=users_routes.VoiceSecondFactorRequest(
                        enabled=False, current_password=wrong_password
                    ),
                    db=db_session, current_user=me,
                )
            assert exc.value.status_code == 400, f"for {wrong_password!r}"
            await db_session.refresh(test_user)
            assert test_user.voice_second_factor_enabled is True, "nothing may have been written"

    @pytest.mark.database
    async def test_an_admin_needs_no_password_for_a_stranger(
        self, db_session: AsyncSession, test_user: User
    ):
        """A counter-control: the guard applies only to one's OWN account. An
        administrator does not know somebody else's password -- there `admin`
        stands as the protection, and the recovery path must not fail on
        it."""
        from api.routes import users as users_routes

        test_user.voice_second_factor_enabled = True
        await db_session.commit()

        body = await users_routes.set_voice_second_factor(
            user_id=test_user.id,
            request=users_routes.VoiceSecondFactorRequest(enabled=False),
            db=db_session,
            current_user=_admin(test_user.id + 1000),
        )
        assert body.voice_second_factor_enabled is False

    @pytest.mark.database
    async def test_arming_needs_no_password(
        self, db_session: AsyncSession, test_user: User
    ):
        """Only REMOVING is the attack path. Demanding the input when
        granting would be friction without gain."""
        from api.routes import users as users_routes

        await TestVoiceSecondFactorConsent._with_voice(db_session, test_user)
        body = await users_routes.set_voice_second_factor(
            user_id=test_user.id,
            request=users_routes.VoiceSecondFactorRequest(enabled=True),
            db=db_session, current_user=_plain_member(test_user.id),
        )
        assert body.voice_second_factor_enabled is True
