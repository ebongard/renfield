"""Telephony recipients (#1650): active human accounts only — never device accounts or inactive users."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Role, User
from services.telephony_client import recipient_user_ids

pytestmark = [pytest.mark.database]


async def _user(db: AsyncSession, role: Role, name: str, *, active=True, device=False) -> User:
    user = User(
        role_id=role.id,
        username=name,
        email=f"{name}@example.com",
        password_hash="$2b$12$fakefakefakefakefakefake",
        is_active=active,
        is_device_account=device,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_only_active_human_accounts_are_recipients(db_session: AsyncSession):
    role = Role(name="role-tel", description="tel", permissions=["notifications.view"], is_system=False)
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    alice = await _user(db_session, role, "alice")
    bob = await _user(db_session, role, "bob")
    await _user(db_session, role, "gone", active=False)
    await _user(db_session, role, "kitchen-satellite", device=True)

    assert await recipient_user_ids(db_session) == [alice.id, bob.id]
